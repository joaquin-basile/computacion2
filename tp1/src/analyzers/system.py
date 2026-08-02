"""Analizador: sistema global (block 3, task D3).

Combina cuatro fuentes globales con un conteo por estado de procesos:

* /proc/stat       -> CPU aggregate (delta) + btime
* /proc/loadavg    -> load1/5/15 + running/total counts
* /proc/meminfo    -> MemTotal/Free/Buffers/Cached + SwapTotal/Free
* /proc/uptime     -> uptime en segundos
* /proc/<pid>/stat -> conteo de procesos por estado (R/S/D/T/Z/I + other)

El CPU% global se calcula como delta entre el tick anterior y el actual
sobre el agregado ``cpu`` de /proc/stat. En el primer tick (sin prev) o
si ``total_delta <= 0`` se emite 0.0 para evitar division por cero.

Decisiones de diseno
--------------------
* ``threads_total`` NO se incluye aca: derivarlo requiere leer
  /proc/<pid>/status por PID (otro N+1 syscalls). El display lo
  calcula sumando ``snapshot['resumen'][pid]['threads']`` cuando lo
  necesite, o como un derivado trivial desde la vista Resumen.
* El conteo por estado SI se hace aca (la consigna lo pide
  explicitamente), aunque hay redundancia con ``resumen``: usamos un
  mini-parser inline que solo lee el campo 3 de /proc/<pid>/stat
  (~250 bytes por PID) en lugar de invocar a ``procfs.parse_stat``
  (que parsea todos los campos).
* El parser de estado busca el ULTIMO ``)`` de la linea: el campo
  ``comm`` (campo 2) puede contener espacios y parentesis, asi que
  el ULTIMO ``)`` es el delimitador confiable.
* Buckets exactos: ``R``, ``S``, ``D``, ``T``, ``Z``, ``I`` +
  ``other`` (atrapa ``X``, ``t``, ``P``, ``W``, ``?`` y cualquier
  estado raro). ``t`` (tracing stop) se considera variante de
  ``T`` (stopped) y se cuenta en ``T`` — ambas son "proceso
  detenido" desde el punto de vista del usuario.

Snapshot shape
--------------
::

    {
        "ts": float,
        "btime": int,
        "uptime": float,
        "load1": float, "load5": float, "load15": float,
        "running_count": int,    # /proc/loadavg 4to campo (running)
        "total_count": int,      # /proc/loadavg 4to campo (total)
        "mem": {
            "total_kb": int, "free_kb": int, "buffers_kb": int,
            "cached_kb": int, "swap_total_kb": int, "swap_free_kb": int,
        },
        "cpu_percent": float,         # busy% (0-100), 100 = 1 core saturado
        "cpu_breakdown": {
            "user": float, "system": float, "idle": float, "iowait": float,
            # cada uno es % del total delta (0-100)
        },
        "processes": {
            "total": int,
            "by_state": {"R": int, "S": int, "D": int, "T": int,
                         "Z": int, "I": int, "other": int},
        },
    }
"""
import sys
import time

import procfs
from .base import drain_queue, loop_con_evento, read_text


_BUCKETS = ("R", "S", "D", "T", "Z", "I", "other")
_BREAKDOWN_KEYS = ("user", "system", "idle", "iowait")


def _pid_state(stat_content):
    """Devuelve el char de estado (R/S/D/T/Z/I/...) parseando solo el campo 3.

    El campo 2 (``comm``) puede contener espacios y parentesis, asi que
    el delimitador confiable es el ULTIMO ``)`` de la linea. El estado
    (campo 3) es el primer token que viene despues de ese ``)``.
    Devuelve ``None`` si la linea esta malformada.
    """
    rparen = stat_content.rfind(")")
    if rparen == -1 or rparen + 1 >= len(stat_content):
        return None
    after = stat_content[rparen + 1:].lstrip()
    if not after:
        return None
    return after[0]


def _read_global_files():
    """Lee los 4 archivos globales. Devuelve ``None`` si alguno falla.

    Asi no publicamos un snapshot parcial: si /proc desaparecio (ej. el
    contenedor se esta cerrando), dejamos el ultimo snapshot valido en
    su lugar.
    """
    try:
        proc_stat_text = read_text("/proc/stat")
        loadavg_text = read_text("/proc/loadavg")
        meminfo_text = read_text("/proc/meminfo")
        uptime_text = read_text("/proc/uptime")
    except (FileNotFoundError, PermissionError, OSError) as e:
        print(f"[sistema] no se pudo leer /proc global: {e}",
              file=sys.stderr, flush=True)
        return None
    return {
        "proc_stat": procfs.parse_proc_stat(proc_stat_text),
        "loadavg": procfs.parse_loadavg(loadavg_text),
        "meminfo": procfs.parse_meminfo(meminfo_text),
        "uptime": procfs.parse_uptime(uptime_text),
    }


def _count_by_state(pids):
    """Itera pids, lee solo /proc/<pid>/stat, devuelve buckets y total.

    Es la operacion mas cara del analizador (~1ms por PID en un
    sistema normal); con ~200 PIDs y tick de 2s tarda ~50ms. PIDs
    desaparecidos, sin permisos, o cualquier error de E/O se
    ignoran silenciosamente.
    """
    buckets = {b: 0 for b in _BUCKETS}
    total = 0
    for pid in pids:
        try:
            content = read_text(f"/proc/{pid}/stat")
        except (
            FileNotFoundError,
            ProcessLookupError,
            PermissionError,
            OSError,
        ):
            continue
        state = _pid_state(content)
        if state is None:
            continue
        total += 1
        if state == "t":
            buckets["T"] += 1
        elif state in buckets:
            buckets[state] += 1
        else:
            buckets["other"] += 1
    return total, buckets


def _cpu_delta(prev, curr):
    """Calcula busy% y breakdown user/system/idle/iowait.

    Formula::
        total_delta = sum(curr.cpu - prev.cpu)
        idle_delta  = curr.cpu.idle - prev.cpu.idle
        busy        = 100 - (idle_delta / total_delta) * 100
        breakdown[k] = (curr.cpu[k] - prev.cpu[k]) / total_delta * 100

    Si ``prev`` es ``None`` (primer tick) o ``total_delta <= 0``
    (cero variacion, o el reloj del kernel dio un paso hacia atras),
    devuelve 0.0 para todo.
    """
    if prev is None:
        return 0.0, {k: 0.0 for k in _BREAKDOWN_KEYS}
    p_cpu = prev.get("cpu") or {}
    c_cpu = curr.get("cpu") or {}
    all_keys = procfs._CPU_VALUE_KEYS
    total_p = sum(p_cpu.get(k, 0) for k in all_keys)
    total_c = sum(c_cpu.get(k, 0) for k in all_keys)
    total_delta = total_c - total_p
    if total_delta <= 0:
        return 0.0, {k: 0.0 for k in _BREAKDOWN_KEYS}
    breakdown = {}
    for k in _BREAKDOWN_KEYS:
        d = c_cpu.get(k, 0) - p_cpu.get(k, 0)
        breakdown[k] = (d / total_delta) * 100.0
    busy = 100.0 - breakdown["idle"]
    return busy, breakdown


def main(event, intervalo, snapshot, q):
    """Firma: (event, intervalo, snapshot, q). Escribe ``snapshot['sistema']``.

    Mantiene estado local entre ticks: ``prev_cpu`` (para el delta de
    CPU%) y ``last_pids`` (ultima lista de PIDs que paso el recolector,
    usada para el conteo por estado). Si el recolector todavia no
    mando nada, el primer tick no cuenta procesos pero igual publica
    CPU/mem/load.
    """
    print("[sistema] iniciado", flush=True)
    prev_cpu = None
    last_pids: list[int] = []

    def tick():
        nonlocal prev_cpu, last_pids
        latest = drain_queue(q)
        if latest is not None:
            last_pids = latest

        data = _read_global_files()
        if data is None:
            return

        busy, breakdown = _cpu_delta(prev_cpu, data["proc_stat"])
        prev_cpu = data["proc_stat"]

        total_pids, by_state = _count_by_state(last_pids)

        loadavg = data["loadavg"]
        meminfo = data["meminfo"]
        uptime_s, _ = data["uptime"]

        result = {
            "ts": time.time(),
            "btime": data["proc_stat"].get("btime", 0),
            "uptime": uptime_s,
            "load1": loadavg.get("load1", 0.0),
            "load5": loadavg.get("load5", 0.0),
            "load15": loadavg.get("load15", 0.0),
            "running_count": loadavg.get("running", 0),
            "total_count": loadavg.get("total", 0),
            "mem": {
                "total_kb": meminfo.get("MemTotal", 0),
                "free_kb": meminfo.get("MemFree", 0),
                "buffers_kb": meminfo.get("Buffers", 0),
                "cached_kb": meminfo.get("Cached", 0),
                "swap_total_kb": meminfo.get("SwapTotal", 0),
                "swap_free_kb": meminfo.get("SwapFree", 0),
            },
            "cpu_percent": round(busy, 2),
            "cpu_breakdown": {
                k: round(breakdown.get(k, 0.0), 2) for k in _BREAKDOWN_KEYS
            },
            "processes": {
                "total": total_pids,
                "by_state": by_state,
            },
        }
        snapshot["sistema"] = result
        snapshot["_ts"]["sistema"] = time.time()

    loop_con_evento(event, intervalo, tick)
