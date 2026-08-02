"""Analizador: senales de procesos (bloque 3, tarea D2).

Por cada PID lee /proc/<pid>/status y decodifica las 5 mascaras de
senales (SigBlk, SigIgn, SigCgt, SigPnd, ShdPnd).

NO registra signal handlers — eso vive en src/signals.py. Este modulo
solo DECODIFICA mascaras de procesos. La coincidencia de nombre es
accidental y se desambigua con el import alias en main.py:
    from analyzers.signals import main as analyze_signals

Convencion de bits (la misma que procfs.decode_signal_mask):
    bit (N-1) == senal N
asi decode_signal_mask(1 << 2) == ["SIGQUIT"]. Seguimos la
convencion del kernel real de Linux.

Shape publicado en snapshot['senales']:

    {
      pid: {
        "sigblk":   ["SIGINT", ...],    # nombres decodificados
        "sigblk_raw": int,              # mascara cruda (hex equivalent)
        "sigign":   [...],
        "sigign_raw": int,
        "sigcgt":   [...],
        "sigcgt_raw": int,
        "sigpnd":   [...],
        "sigpnd_raw": int,
        "shdpnd":   [...],
        "shdpnd_raw": int,
      },
      ...
    }
"""
import sys
import time

import procfs
from .base import drain_queue, loop_con_evento, read_text


def collect_one(pid):
    """Lee /proc/<pid>/status y devuelve el dict de senales.

    Devuelve None si el PID murio o no es accesible. La mascara
    cruda (int) puede ser 0 (sin bits seteados) — en ese caso
    decode_signal_mask devuelve [].
    """
    base = f"/proc/{pid}"
    try:
        status = read_text(f"{base}/status")
    except (FileNotFoundError, ProcessLookupError, PermissionError):
        return None

    parsed = procfs.parse_status(status)

    masks = {}
    for key in ("SigBlk", "SigIgn", "SigCgt", "SigPnd", "ShdPnd"):
        raw = parsed.get(key, 0) or 0
        masks[key] = {
            "names": procfs.decode_signal_mask(raw),
            "raw": raw,
        }

    return {
        "pid": pid,
        "sigblk": masks["SigBlk"]["names"],
        "sigblk_raw": masks["SigBlk"]["raw"],
        "sigign": masks["SigIgn"]["names"],
        "sigign_raw": masks["SigIgn"]["raw"],
        "sigcgt": masks["SigCgt"]["names"],
        "sigcgt_raw": masks["SigCgt"]["raw"],
        "sigpnd": masks["SigPnd"]["names"],
        "sigpnd_raw": masks["SigPnd"]["raw"],
        "shdpnd": masks["ShdPnd"]["names"],
        "shdpnd_raw": masks["ShdPnd"]["raw"],
    }


def main(event, intervalo, snapshot, q):
    """Firma: (event, intervalo, snapshot, q). Escribe snapshot['senales']."""
    print("[senales] iniciado", flush=True)
    last_pids = []

    def tick():
        nonlocal last_pids
        latest = drain_queue(q)
        if latest is not None:
            last_pids = latest
        if not last_pids:
            return
        now = time.time()
        result = {}
        for pid in last_pids:
            try:
                data = collect_one(pid)
            except (FileNotFoundError, ProcessLookupError, PermissionError):
                continue
            except Exception as e:
                print(
                    f"[senales] error en pid {pid}: {e}",
                    file=sys.stderr,
                    flush=True,
                )
                continue
            if data is not None:
                result[pid] = data
        snapshot["senales"] = result
        snapshot["_ts"]["senales"] = now

    loop_con_evento(event, intervalo, tick)
