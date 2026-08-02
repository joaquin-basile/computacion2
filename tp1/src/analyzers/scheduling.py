"""Analizador: scheduling de procesos (bloque 3, tarea D2).

Por cada PID lee:
* /proc/<pid>/stat  -> nice (19), priority (18), policy (41),
                        rt_priority (40), utime (14), stime (15),
                        session_id (6), pgrp (5)
* /proc/<pid>/status -> Cpus_allowed_list, voluntary_ctxt_switches,
                        nonvoluntary_ctxt_switches

Nota sobre pgrp: la consigna dice "campo 7" pero el campo 7 del
kernel es tty_nr; el verdadero pgrp es el campo 5. procfs.parse_stat
ya devuelve pgrp en la key "pgrp" (campo 5 real). Lo seguimos.

Shape publicado en snapshot['scheduling']:

    {
      pid: {
        "nice": int,
        "priority": int,
        "policy": int,            # raw
        "policy_name": str,       # decoded
        "rt_priority": int,
        "cpu_affinity": str,      # raw "0-3" string
        "voluntary_cs": int,
        "involuntary_cs": int,
        "utime": int,
        "stime": int,
        "session_id": int,
        "pgrp": int,
      },
      ...
    }
"""
import sys
import time

import procfs
from .base import drain_queue, loop_con_evento, read_text


# Tabla de decodificacion del campo policy de /proc/<pid>/stat
# (ver man 5 proc, sched(7)). Linux 6.6+ anadio SCHED_EXT_BATCH (6).
POLICY_NAMES = {
    0: "OTHER",      # SCHED_NORMAL / SCHED_OTHER
    1: "FIFO",       # SCHED_FIFO
    2: "RR",         # SCHED_RR
    3: "BATCH",      # SCHED_BATCH
    4: "IDLE",       # SCHED_IDLE
    5: "DEADLINE",   # SCHED_DEADLINE
    6: "EXT_BATCH",  # SCHED_EXT_BATCH (Linux >= 6.6)
}


def _policy_name(policy_int):
    return POLICY_NAMES.get(policy_int, f"UNKNOWN({policy_int})")


def collect_one(pid):
    """Lee stat y status del PID y devuelve el dict de scheduling.

    Devuelve None si el PID murio o no es accesible.
    """
    base = f"/proc/{pid}"
    try:
        stat = read_text(f"{base}/stat")
        status = read_text(f"{base}/status")
    except (FileNotFoundError, ProcessLookupError, PermissionError):
        return None

    parsed_stat = procfs.parse_stat(stat)
    parsed_status = procfs.parse_status(status)

    policy = parsed_stat.get("policy", 0)

    return {
        "pid": pid,
        "nice": parsed_stat.get("nice", 0),
        "priority": parsed_stat.get("priority", 0),
        "policy": policy,
        "policy_name": _policy_name(policy),
        "rt_priority": parsed_stat.get("rt_priority", 0),
        "cpu_affinity": parsed_status.get("Cpus_allowed_list", ""),
        "voluntary_cs": parsed_status.get("voluntary_ctxt_switches", 0),
        "involuntary_cs": parsed_status.get("nonvoluntary_ctxt_switches", 0),
        "utime": parsed_stat.get("utime", 0),
        "stime": parsed_stat.get("stime", 0),
        "session_id": parsed_stat.get("session_id", 0),
        "pgrp": parsed_stat.get("pgrp", 0),
    }


def main(event, intervalo, snapshot, q):
    """Firma: (event, intervalo, snapshot, q). Escribe
    snapshot['scheduling']."""
    print("[scheduling] iniciado", flush=True)
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
                    f"[scheduling] error en pid {pid}: {e}",
                    file=sys.stderr,
                    flush=True,
                )
                continue
            if data is not None:
                result[pid] = data
        snapshot["scheduling"] = result
        snapshot["_ts"]["scheduling"] = now

    loop_con_evento(event, intervalo, tick)
