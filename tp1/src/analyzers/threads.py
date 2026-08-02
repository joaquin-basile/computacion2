"""Analizador: threads / LWPs de procesos (bloque 3, tarea D2).

Por cada PID:
* Lista de threads via os.listdir('/proc/<pid>/task') (parse_task_listing).
* Por cada thread lee:
    - /proc/<pid>/task/<tid>/stat    (state, utime, stime)
    - /proc/<pid>/task/<tid>/comm    (name)
    - /proc/<pid>/task/<tid>/status  (voluntary_cs, involuntary_cs)
* CPU% por thread: delta de jiffies entre ticks, con prev_jiffies
  keyed por (pid, tid) — la tupla, no el pid solo, porque un mismo
  tid puede reciclarse entre pids a lo largo del tiempo.

El shape publicado en snapshot['threads'] es:

    {
      pid: [
        {"tid": int, "name": str, "state": str, "cpu_percent": float,
         "voluntary_cs": int, "involuntary_cs": int},
        ...
      ],
      ...
    }
"""
import os
import sys
import time

import procfs
from .base import drain_queue, loop_con_evento, read_text


def collect_one(pid, tid, prev_jiffies, now, clock_tick):
    """Lee los 3 archivos de un thread y devuelve su dict.

    prev_jiffies: dict mutable con key (pid, tid) -> {"utime", "stime", "t"}.
    Se actualiza in-place. Devuelve None si el thread murio o no es accesible.
    """
    base = f"/proc/{pid}/task/{tid}"
    try:
        stat = read_text(f"{base}/stat")
        comm = read_text(f"{base}/comm")
        status = read_text(f"{base}/status")
    except (FileNotFoundError, ProcessLookupError, PermissionError):
        return None

    parsed_stat = procfs.parse_task_stat(stat)
    parsed_status = procfs.parse_task_status(status)
    name = procfs.parse_task_comm(comm)

    utime = parsed_stat.get("utime", 0)
    stime = parsed_stat.get("stime", 0)
    key = (pid, tid)
    prev = prev_jiffies.get(key)
    if prev is None:
        cpu_percent = 0.0
    else:
        elapsed = max(now - prev["t"], 0.0)
        cpu_percent = procfs.cpu_percent(
            {"utime": prev["utime"], "stime": prev["stime"]},
            {"utime": utime, "stime": stime},
            elapsed,
            clock_tick=clock_tick,
        )
    prev_jiffies[key] = {"utime": utime, "stime": stime, "t": now}

    return {
        "tid": tid,
        "name": name,
        "state": parsed_stat.get("state", "?"),
        "cpu_percent": round(cpu_percent, 2),
        "voluntary_cs": parsed_status.get("voluntary_ctxt_switches", 0),
        "involuntary_cs": parsed_status.get("nonvoluntary_ctxt_switches", 0),
    }


def main(event, intervalo, snapshot, q):
    """Firma: (event, intervalo, snapshot, q). Escribe snapshot['threads']."""
    print("[threads] iniciado", flush=True)
    prev_jiffies = {}
    last_pids = []
    clock_tick = os.sysconf("SC_CLK_TCK")

    def tick():
        nonlocal last_pids
        latest = drain_queue(q)
        if latest is not None:
            last_pids = latest
        if not last_pids:
            return
        now = time.time()
        result = {}
        seen_keys = set()
        for pid in last_pids:
            try:
                tids = procfs.parse_task_listing(
                    os.listdir(f"/proc/{pid}/task")
                )
            except (FileNotFoundError, ProcessLookupError, PermissionError):
                continue
            except Exception as e:
                print(
                    f"[threads] error listando task de pid {pid}: {e}",
                    file=sys.stderr,
                    flush=True,
                )
                continue
            threads_list = []
            for tid in tids:
                try:
                    data = collect_one(pid, tid, prev_jiffies, now, clock_tick)
                except (
                    FileNotFoundError,
                    ProcessLookupError,
                    PermissionError,
                ):
                    continue
                except Exception as e:
                    print(
                        f"[threads] error en pid {pid} tid {tid}: {e}",
                        file=sys.stderr,
                        flush=True,
                    )
                    continue
                if data is not None:
                    threads_list.append(data)
                    seen_keys.add((pid, tid))
            if threads_list:
                result[pid] = threads_list
        # Limpiar entradas de threads que ya no existen para que el dict
        # no crezca sin limite si hay churn alto.
        stale = [k for k in prev_jiffies if k not in seen_keys]
        for k in stale:
            del prev_jiffies[k]
        snapshot["threads"] = result
        snapshot["_ts"]["threads"] = now

    loop_con_evento(event, intervalo, tick)
