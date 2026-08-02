"""Analizador: file descriptors de procesos.

Para cada PID:
* ``os.listdir('/proc/<pid>/fd')`` -> enteros (``parse_fd_listing``)
* ``os.readlink('/proc/<pid>/fd/<n>')`` por cada FD -> ``parse_fd_link_target``

El shape del snapshot es ``dict[pid, [list of {fd, kind, target}]]`` y se
publica bajo ``snapshot['fds']`` + ``snapshot['_ts']['fds']``.

La lista, no un dict, refleja la consigna: "Lista de FDs abiertos" +
"Destino de cada FD" — el orden es numerico ascendente (parse_fd_listing
los ordena).
"""
import os
import sys
import time

from .base import drain_queue, loop_con_evento


def collect_one(pid):
    """Lista los FDs del PID y devuelve la lista de ``{fd, kind, target}``.

    Devuelve ``None`` si el PID murio o no es accesible. Cualquier otra
    excepcion se propaga para que ``main()`` la loguee y continue.

    ``readlink`` puede tirar ``FileNotFoundError`` / ``PermissionError``
    para un FD especifico si ese FD se cerro entre el listdir y el
    readlink, o si el kernel no nos deja resolverlo (p.ej. FDs creados
    por otros procesos en su mismo namespace). Esos FDs puntuales se
    saltan silenciosamente.
    """
    base = f"/proc/{pid}/fd"
    names = os.listdir(base)

    import procfs

    fds_int = procfs.parse_fd_listing(names)
    out = []
    for fd in fds_int:
        try:
            target = os.readlink(f"{base}/{fd}")
        except (
            FileNotFoundError,
            ProcessLookupError,
            PermissionError,
            OSError,
        ):
            continue
        kind_info = procfs.parse_fd_link_target(target)
        out.append({
            "fd": fd,
            "kind": kind_info.get("kind", "other"),
            "target": target,
        })
    return out


def main(event, intervalo, snapshot, q):
    """Firma: (event, intervalo, snapshot, q). Escribe snapshot['fds']."""
    print("[fds] iniciado", flush=True)
    last_pids = []

    def tick():
        nonlocal last_pids
        latest = drain_queue(q)
        if latest is not None:
            last_pids = latest
        if not last_pids:
            return
        result = {}
        for pid in last_pids:
            try:
                data = collect_one(pid)
            except (FileNotFoundError, ProcessLookupError, PermissionError):
                continue
            except Exception as e:
                print(
                    f"[fds] error en pid {pid}: {e}",
                    file=sys.stderr,
                    flush=True,
                )
                continue
            if data is not None:
                result[pid] = data
        snapshot["fds"] = result
        snapshot["_ts"]["fds"] = time.time()

    loop_con_evento(event, intervalo, tick)
