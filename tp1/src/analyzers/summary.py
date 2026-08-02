"""Analizador: resumen de procesos (fase 3, implementacion real)."""
import os
import pwd
import sys
import time

import procfs
from .base import drain_queue, loop_con_evento, read_bytes, read_text


def _username(uid):
    try:
        return pwd.getpwuid(uid).pw_name
    except (KeyError, OverflowError):
        return None


def collect_one(pid, prev_jiffies, now, clock_tick):
    """Lee /proc/<pid>/{stat,status,cmdline,comm} y devuelve el dict resumen.

    prev_jiffies: dict mutable pid -> {"utime": int, "stime": int, "t": float}.
    Se actualiza in-place con los valores actuales para el siguiente tick.
    Devuelve None si el PID murio o no es accesible.
    """
    base = f"/proc/{pid}"
    try:
        stat = read_text(f"{base}/stat")
        status = read_text(f"{base}/status")
        cmdline_bytes = read_bytes(f"{base}/cmdline")
        comm = read_text(f"{base}/comm").strip()
    except (FileNotFoundError, ProcessLookupError, PermissionError):
        return None

    parsed_stat = procfs.parse_stat(stat)
    parsed_status = procfs.parse_status(status)

    utime = parsed_stat.get("utime", 0)
    stime = parsed_stat.get("stime", 0)

    prev = prev_jiffies.get(pid)
    if prev is None:
        cpu_percent = 0.0
        elapsed = 0.0
    else:
        elapsed = max(now - prev["t"], 0.0)
        cpu_percent = procfs.cpu_percent(
            {"utime": prev["utime"], "stime": prev["stime"]},
            {"utime": utime, "stime": stime},
            elapsed,
            clock_tick=clock_tick,
        )
    prev_jiffies[pid] = {"utime": utime, "stime": stime, "t": now}

    return {
        "pid": pid,
        "ppid": parsed_status.get("PPid", 0),
        "uid": parsed_status.get("Uid", 0),
        "username": _username(parsed_status.get("Uid", 0)),
        "gid": parsed_status.get("Gid", 0),
        "state": parsed_stat.get("state", "?"),
        "comm": comm or parsed_stat.get("comm", "?"),
        "cmdline": procfs.parse_cmdline(cmdline_bytes),
        "cpu_percent": round(cpu_percent, 2),
        "threads": parsed_status.get("Threads", 0),
        "rss_kb": parsed_status.get("VmRSS", 0),
    }


def main(event, intervalo, snapshot, q):
    """Firma: (event, intervalo, snapshot, q). Escribe snapshot['resumen']."""
    print("[resumen] iniciado", flush=True)
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
        seen_pids = set()
        for pid in last_pids:
            try:
                data = collect_one(pid, prev_jiffies, now, clock_tick)
            except (FileNotFoundError, ProcessLookupError, PermissionError):
                prev_jiffies.pop(pid, None)
                continue
            except Exception as e:
                print(
                    f"[resumen] error en pid {pid}: {e}",
                    file=sys.stderr,
                    flush=True,
                )
                continue
            if data is not None:
                result[pid] = data
                seen_pids.add(pid)
        stale = [k for k in prev_jiffies if k not in seen_pids]
        for k in stale:
            del prev_jiffies[k]
        snapshot["resumen"] = result
        snapshot["_ts"]["resumen"] = now

    loop_con_evento(event, intervalo, tick)
