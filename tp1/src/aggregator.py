"""Agregador: mantiene vivo el snapshot global y reporta staleness.

Los analizadores son los unicos que escriben a snapshot[key]; el
agregador no escribe datos de procesos. Su rol es:

  1. Asegurar que el snapshot esta inicializado con todas las claves.
  2. Monitorear la edad de cada clave y loguear si alguna se atrasa.

Inicialmente main.py inicializa snapshot; este proceso es defensivo
por si se reinicia aisladamente (tests, scripts).
"""
import time

from analyzers.base import CLAVES, loop_con_evento


def main(event, intervalo, snapshot):
    """Firma: (event, intervalo, snapshot). Solo inicializa y vigila.

    Si por algun motivo las claves no estan (proceso arrancado
    fuera de main.py, por ejemplo), las crea vacias.
    """
    print("[agregador] iniciado", flush=True)
    for k in CLAVES:
        if k not in snapshot:
            snapshot[k] = {}
    if "_ts" not in snapshot:
        snapshot["_ts"] = {k: 0.0 for k in CLAVES}
    else:
        for k in CLAVES:
            snapshot["_ts"].setdefault(k, 0.0)

    def tick():
        ts = snapshot.get("_ts", {})
        now = time.time()
        for k in CLAVES:
            ts_k = ts.get(k, 0.0)
            age = now - ts_k
            if ts_k > 0 and age > 30.0:
                print(
                    f"[agregador] WARN: snapshot[{k!r}] stale {age:.1f}s",
                    flush=True,
                )

    loop_con_evento(event, intervalo, tick)
