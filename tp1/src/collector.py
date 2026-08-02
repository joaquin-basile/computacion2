"""Recolector central: lista PIDs cada 2s y los reparte a las 7 colas."""
import os
import queue
import time

import procfs


def _list_pids():
    try:
        entries = os.listdir("/proc")
    except (FileNotFoundError, PermissionError):
        return []
    return procfs.list_pids(entries)


def main(event, queues):
    """Firma: (event, queues_dict). Reparte la lista de PIDs vivos.

    queues_dict tiene las claves: resumen, memoria, fds, threads,
    senales, scheduling, sistema. Cada valor es una Queue() vacia
    sin maxsize. Ponemos la MISMA lista en todas las colas para que
    cada analizador pueda iterar sin race contra el filesystem.

    Si una cola esta llena (improbable: las Queue son unbounded), el
    bloque except Queue.Full evita que el recolector se cuelgue.
    """
    print("[recolector] iniciado", flush=True)
    while not event.is_set():
        pids = _list_pids()
        for q in queues.values():
            try:
                q.put_nowait(pids)
            except queue.Full:
                pass
        restante = 2.0
        while restante > 0 and not event.is_set():
            time.sleep(min(restante, 0.2))
            restante -= 0.2
