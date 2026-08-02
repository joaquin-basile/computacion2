"""Handlers de senales del monitor.

SIGINT/SIGTERM (shutdown via Event) y SIGHUP/SIGUSR1/SIGUSR2 (reload,
dump, verbose) usan el MISMO mecanismo: wakeup pipe async-signal-safe.

Patron: signal.set_wakeup_fd escribe 1 byte por senial al pipe; un
reader thread daemonico drena el pipe y dispara las acciones reales
(shutdown, reload config, dump JSON, toggle verbose). Dentro del
handler SOLO se hace `os.write` (async-signal-safe). Toda la logica
vive en el thread.

SIGWINCH no se registra aca: curses lo maneja solo cuando este en uso
(tarea E). Cuando display es el placeholder sin curses, el repaint
del proximo tick alcanza.
"""
import json
import os
import signal
import sys
import threading
import time
from pathlib import Path


_log_stream = sys.stdout


def set_log_stream(stream):
    """Redirige los prints del reader thread al log compartido."""
    global _log_stream
    _log_stream = stream


def instalar_handlers(event, snapshot, interval_values, config_path,
                      shared_config, verbose_flag):
    """SIGINT/SIGTERM/SIGHUP/SIGUSR1/SIGUSR2 via wakeup pipe + reader thread.

    Consolidacion de los instaladores previos: todas las seniales
    todas las seniales comparten un unico pipe. event.set() para shutdown
    NO se ejecuta en el handler (el proxy de Manager usa socket+pickle y
    no es async-safe); lo hace el reader thread al leer el byte.

    Args:
        event: Manager.Event compartido; SIGINT/SIGTERM lo setean.
        snapshot: Manager.dict con el snapshot del monitor.
        interval_values: dict nombre_vista -> Value('d') (mutado en SIGHUP).
        config_path: ruta al config.json (str).
        shared_config: Manager.dict; SIGHUP recarga intervalo_minimo/filtros.
        verbose_flag: Value('b', bool) que SIGUSR2 togglea.

    Returns:
        (r_fd, w_fd, thread) por si main quiere cleanup; normalmente
        se ignora (el thread es daemon y muere con el proceso).

    Notas:
        - set_wakeup_fd hace que el signal handler a nivel C de Python
          escriba AUTOMATICAMENTE 1 byte con el numero de senial al pipe
          cada vez que llega una senial. Por eso el handler Python aca
          es un no-op: si escribiera tambien, duplicariamos bytes.
        - Aun necesitamos un handler Python (sino la senial mata el
          proceso con su accion por defecto). El no-op cumple: previene
          la accion por defecto y delega el trabajo al reader thread.
    """
    r_fd, w_fd = os.pipe()
    import fcntl
    flags = fcntl.fcntl(w_fd, fcntl.F_GETFL)
    fcntl.fcntl(w_fd, fcntl.F_SETFL, flags | os.O_NONBLOCK)
    signal.set_wakeup_fd(w_fd)

    def _handler(sig, frame):
        # No-op: el C-level signal handler de Python ya escribio el
        # byte al wakeup_fd. Esto solo evita la accion por defecto
        # de la senial (ej. terminate para SIGTERM).
        pass

    for sig in (signal.SIGINT, signal.SIGTERM,
                signal.SIGHUP, signal.SIGUSR1, signal.SIGUSR2):
        signal.signal(sig, _handler)

    def _reader():
        """Daemon: drena el wakeup pipe y dispara acciones (NO async-safe)."""
        while True:
            try:
                data = os.read(r_fd, 64)
            except OSError:
                break  # pipe cerrado -> salir (cleanup o fin de proceso)
            for byte in data:
                sig = byte
                if sig in (signal.SIGINT, signal.SIGTERM):
                    event.set()
                elif sig == signal.SIGHUP:
                    _reload_config(interval_values, config_path, shared_config)
                elif sig == signal.SIGUSR1:
                    _dump_snapshot(snapshot, config_path)
                elif sig == signal.SIGUSR2:
                    _toggle_verbose(verbose_flag)

    t = threading.Thread(target=_reader, daemon=True, name="signal-reader")
    t.start()
    return r_fd, w_fd, t


def _to_plain(obj):
    """Convierte recursivamente Manager.dict / Manager.list a dict/list plain.

    json.dump no serializa los proxies de multiprocessing.Manager; ademas
    los snapshots pueden tener bytes anidados (comm sin decodificar) que
    se manejan via default=str en json.dump.
    """
    if isinstance(obj, dict):
        return {k: _to_plain(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_to_plain(v) for v in obj]
    return obj


def _reload_config(interval_values, config_path, shared_config):
    """SIGHUP: releer config.json; mutar interval_values y shared_config."""
    try:
        with open(config_path) as f:
            new_config = json.load(f)
        cambios = []
        for view, secs in new_config.get("intervalos", {}).items():
            if view in interval_values:
                nuevo = float(secs)
                if interval_values[view].value != nuevo:
                    interval_values[view].value = nuevo
                    cambios.append(f"{view}={nuevo}s")
        min_actual = shared_config.get("intervalo_minimo")
        min_nuevo = new_config.get("intervalo_minimo", {})
        if min_actual != min_nuevo:
            shared_config["intervalo_minimo"] = min_nuevo
            cambios.append("intervalo_minimo")
        filtros_actual = shared_config.get("filtros")
        filtros_nuevo = new_config.get("filtros", {})
        if filtros_actual != filtros_nuevo:
            shared_config["filtros"] = filtros_nuevo
            cambios.append("filtros")
        if cambios:
            print(
                f"[senales] SIGHUP: config recargada. Cambios: "
                f"{', '.join(cambios)}",
                file=_log_stream,
                flush=True,
            )
        else:
            print(
                "[senales] SIGHUP: config recargada. Sin cambios.",
                file=_log_stream,
                flush=True,
            )
    except FileNotFoundError:
        print(
            f"[senales] SIGHUP: no se encontro {config_path}",
            file=_log_stream,
            flush=True,
        )
    except (json.JSONDecodeError, ValueError, TypeError) as e:
        print(
            f"[senales] SIGHUP: config invalida ({e}); "
            f"se mantiene la anterior",
            file=_log_stream,
            flush=True,
        )
    except OSError as e:
        print(
            f"[senales] SIGHUP: error de I/O ({e})",
            file=_log_stream,
            flush=True,
        )


def _dump_snapshot(snapshot, config_path):
    """SIGUSR1: escribir snapshot completo a dump_<unix_ts>.json.

    Race condition conocido: dos SIGUSR1 muy rapidos pueden pisarse
    el archivo. Para un TP academico es aceptable; se documenta.
    """
    try:
        ts = int(time.time())
        out_dir = Path(config_path).resolve().parent
        path = out_dir / f"dump_{ts}.json"
        payload = _to_plain(dict(snapshot))
        with open(path, "w") as f:
            json.dump(payload, f, indent=2, default=str, sort_keys=True)
        size = path.stat().st_size
        print(
            f"[senales] SIGUSR1: dump escrito a {path} ({size} bytes)",
            file=_log_stream,
            flush=True,
        )
    except OSError as e:
        print(
            f"[senales] SIGUSR1: error de I/O ({e})",
            file=_log_stream,
            flush=True,
        )
    except (TypeError, ValueError) as e:
        print(
            f"[senales] SIGUSR1: error serializando ({e})",
            file=_log_stream,
            flush=True,
        )


def _toggle_verbose(verbose_flag):
    """SIGUSR2: toggle del flag compartido; el display lo polea."""
    verbose_flag.value = not verbose_flag.value
    state = "ON" if verbose_flag.value else "OFF"
    print(
        f"[senales] SIGUSR2: verbose = {state}",
        file=_log_stream,
        flush=True,
    )
