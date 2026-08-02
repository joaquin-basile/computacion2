"""Analizador: memoria de procesos.

Lee por cada PID:
* /proc/<pid>/status     -> VmSize, VmRSS, VmData, VmStk, VmExe, VmLib,
                            VmHWM, VmSwap
* /proc/<pid>/stat       -> minflt (campo 10), cminflt (campo 11),
                            majflt (campo 12), cmajflt (campo 13)
* /proc/<pid>/maps       -> cantidad de segmentos por kind (text/data/heap/
                            stack/shared/other)

El shape del snapshot es ``dict[pid, {...}]`` y se publica bajo
``snapshot['memoria']`` + ``snapshot['_ts']['memoria']``.
"""
import sys
import time

from .base import drain_queue, loop_con_evento, read_text


def _segment_count(maps):
    """Cuenta segmentos de ``parse_maps`` agrupados por kind.

    Devuelve un dict con todos los kinds posibles (los ausentes valen 0)
    mas un ``total`` que coincide con la cantidad de lineas parseadas.
    Asi el display no tiene que chequear ``in`` para cada kind.
    """
    counts = {"text": 0, "data": 0, "heap": 0, "stack": 0,
              "shared": 0, "other": 0}
    for seg in maps:
        kind = seg.get("kind")
        if kind in counts:
            counts[kind] += 1
    counts["total"] = len(maps)
    return counts


def collect_one(pid):
    """Lee los 3 archivos del PID y devuelve el dict de memoria.

    Devuelve ``None`` si el PID murio o no es accesible. Cualquier otra
    excepcion se propaga para que ``main()`` la loguee y continue.
    """
    base = f"/proc/{pid}"
    status = read_text(f"{base}/status")
    stat = read_text(f"{base}/stat")
    maps_text = read_text(f"{base}/maps")

    import procfs

    parsed_status = procfs.parse_status(status)
    parsed_stat = procfs.parse_stat(stat)
    parsed_maps = procfs.parse_maps(maps_text)

    return {
        "pid": pid,
        "vm_size_kb": parsed_status.get("VmSize", 0),
        "vm_rss_kb": parsed_status.get("VmRSS", 0),
        "vm_data_kb": parsed_status.get("VmData", 0),
        "vm_stk_kb": parsed_status.get("VmStk", 0),
        "vm_exe_kb": parsed_status.get("VmExe", 0),
        "vm_lib_kb": parsed_status.get("VmLib", 0),
        "vm_hwm_kb": parsed_status.get("VmHWM", 0),
        "vm_swap_kb": parsed_status.get("VmSwap", 0),
        "minflt": parsed_stat.get("minflt", 0),
        "majflt": parsed_stat.get("majflt", 0),
        "cminflt": parsed_stat.get("cminflt", 0),
        "cmajflt": parsed_stat.get("cmajflt", 0),
        "segments": _segment_count(parsed_maps),
    }


def main(event, intervalo, snapshot, q):
    """Firma: (event, intervalo, snapshot, q). Escribe snapshot['memoria']."""
    print("[memoria] iniciado", flush=True)
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
                    f"[memoria] error en pid {pid}: {e}",
                    file=sys.stderr,
                    flush=True,
                )
                continue
            if data is not None:
                result[pid] = data
        snapshot["memoria"] = result
        snapshot["_ts"]["memoria"] = time.time()

    loop_con_evento(event, intervalo, tick)
