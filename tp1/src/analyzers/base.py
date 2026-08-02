"""Helpers compartidos por los 7 analizadores."""
import queue
import time


CLAVES = ("resumen", "memoria", "fds", "threads", "senales",
          "scheduling", "sistema")


def read_text(path):
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        return f.read()


def read_bytes(path):
    with open(path, "rb") as f:
        return f.read()


def drain_queue(q):
    """Devuelve el ultimo elemento puesto en q, o None si esta vacia.

    Patron usado por los 7 analizadores para evitar acumular snapshots
    viejos en su cola cuando un tick se atrasa (backpressure friendly).
    """
    latest = None
    while not q.empty():
        try:
            latest = q.get_nowait()
        except queue.Empty:
            break
    return latest


def loop_con_evento(event, intervalo, cuerpo):
    """Ejecuta cuerpo() cada intervalo.value segundos hasta que event
    se active.

    El sleep se hace en chunks de 0.2s para que el shutdown sea responsivo
    aun con intervalos grandes (ej. senales cada 10s).
    """
    while not event.is_set():
        cuerpo()
        restante = intervalo.value
        while restante > 0 and not event.is_set():
            time.sleep(min(restante, 0.2))
            restante -= 0.2
