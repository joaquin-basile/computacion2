"""Entry point del monitor."""
import json
import multiprocessing as mp
import os
import queue
import signal
import sys
from multiprocessing import Manager, Process, Queue, Value
from pathlib import Path

import aggregator
import collector
from analyzers.base import CLAVES
from signals import instalar_handlers, set_log_stream

# Senales que solo main debe recibir. Hijos las ponen en SIG_IGN.
# Las cinco pasan por el wakeup pipe y disparan acciones en el
# reader thread (shutdown, reload, dump, toggle verbose).
SIGNALS_MAIN_ONLY = (
    signal.SIGINT, signal.SIGTERM,
    signal.SIGHUP, signal.SIGUSR1, signal.SIGUSR2,
)

# Descriptor del log compartido (logs/monitor.log). Los hijos lo duplican
# sobre stdout/stderr para no ensuciar el TTY mientras curses esta activo.
_LOG_FD = None


def spawn(target, name, *args):
    """Arranca un Process que ignora las senales de control en el hijo.

    El wrapper corre en el proceso hijo (post-fork) y pone SIG_IGN antes
    de invocar al target. Asi la senial que llega al process group solo
    es manejada por el main. Tambien redirige stdout/stderr del hijo al
    log compartido para que ningun print llegue al terminal de curses.
    """
    def wrapper():
        if _LOG_FD is not None:
            os.dup2(_LOG_FD, 1)
            os.dup2(_LOG_FD, 2)
        for sig in SIGNALS_MAIN_ONLY:
            signal.signal(sig, signal.SIG_IGN)
        target(*args)

    p = Process(target=wrapper, name=name)
    p.start()
    return p


def main():
    global _LOG_FD
    base = Path(__file__).resolve().parent.parent
    try:
        with open(base / "config.json") as f:
            config = json.load(f)
    except (FileNotFoundError, OSError, json.JSONDecodeError) as e:
        print(
            f"[main] no se pudo leer config.json: {e}",
            file=sys.stderr,
            flush=True,
        )
        sys.exit(1)

    log_dir = base / "logs"
    log_dir.mkdir(exist_ok=True)
    log_file = open(log_dir / "monitor.log", "a")
    _LOG_FD = log_file.fileno()
    set_log_stream(log_file)

    mgr = Manager()
    try:
        shared_config = mgr.dict(config)
        shared_config["intervalo_minimo"] = config.get("intervalo_minimo", {})
        shared_config["filtros"] = config.get("filtros", {})

        shared_event = mgr.Event()

        nombres_vistas = list(config["intervalos"].keys())
        interval_values = {
            nombre: Value("d", float(config["intervalos"][nombre]))
            for nombre in nombres_vistas
        }

        queues = {nombre: Queue() for nombre in nombres_vistas}

        snapshot = mgr.dict()
        for k in CLAVES:
            snapshot[k] = {}
        snapshot["_ts"] = mgr.dict()
        for k in CLAVES:
            snapshot["_ts"][k] = 0.0

        verbose_flag = Value("b", False)
        instalar_handlers(
            shared_event,
            snapshot,
            interval_values,
            str(base / "config.json"),
            shared_config,
            verbose_flag,
        )

        from analyzers import (
            summary, memory, fds, threads, scheduling, system,
        )
        from analyzers.signals import main as analyze_signals

        processes = []
        processes.append(
            spawn(collector.main, "collector", shared_event, queues)
        )
        processes.append(
            spawn(
                aggregator.main,
                "aggregator",
                shared_event,
                interval_values["sistema"],
                snapshot,
            )
        )
        processes.append(
            spawn(
                summary.main,
                "analyzer_summary",
                shared_event,
                interval_values["resumen"],
                snapshot,
                queues["resumen"],
            )
        )
        processes.append(
            spawn(
                memory.main,
                "analyzer_memory",
                shared_event,
                interval_values["memoria"],
                snapshot,
                queues["memoria"],
            )
        )
        processes.append(
            spawn(
                fds.main,
                "analyzer_fds",
                shared_event,
                interval_values["fds"],
                snapshot,
                queues["fds"],
            )
        )
        processes.append(
            spawn(
                threads.main,
                "analyzer_threads",
                shared_event,
                interval_values["threads"],
                snapshot,
                queues["threads"],
            )
        )
        processes.append(
            spawn(
                analyze_signals,
                "analyzer_signals",
                shared_event,
                interval_values["senales"],
                snapshot,
                queues["senales"],
            )
        )
        processes.append(
            spawn(
                scheduling.main,
                "analyzer_scheduling",
                shared_event,
                interval_values["scheduling"],
                snapshot,
                queues["scheduling"],
            )
        )
        processes.append(
            spawn(
                system.main,
                "analyzer_system",
                shared_event,
                interval_values["sistema"],
                snapshot,
                queues["sistema"],
            )
        )

        from display import run
        run(shared_event, snapshot, interval_values, shared_config,
            verbose_flag)

        shared_event.set()
        limpios = []
        terminados = []
        for p in processes:
            p.join(timeout=3)
            if p.is_alive():
                p.terminate()
                p.join(timeout=1)
                terminados.append(p.name)
            else:
                limpios.append(p.name)

        for q in queues.values():
            while not q.empty():
                try:
                    q.get_nowait()
                except queue.Empty:
                    break
        log_file.flush()

        print(
            f"[main] limpios ({len(limpios)}): {limpios}",
            file=sys.stderr,
            flush=True,
        )
        print(
            f"[main] terminados ({len(terminados)}): {terminados}",
            file=sys.stderr,
            flush=True,
        )
    finally:
        mgr.shutdown()


if __name__ == "__main__":
    try:
        mp.set_start_method("fork")
    except (RuntimeError, ValueError):
        pass
    main()
