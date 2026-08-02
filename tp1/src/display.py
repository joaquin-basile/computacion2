"""TUI con curses para el monitor de procesos (TP1, bloque 3, tarea E).

7 vistas alternables (1-7 / r m f t s p g), lista de procesos arriba,
panel de detalle abajo, keybindings segun consigna lineas 185-194.

Decisiones clave:
* curses (stdlib), nodelay + getch polled a 10Hz, repaint 5-10Hz.
* Snapshot se lee UNA vez por repaint para no castigar el Manager.dict.
* Funciones puras (_sort_processes, _fmt_age, _stale, _handle_key)
  separadas del dibujo para que sean unit-testeables sin curses.
 * Si no hay TTY (curses.initscr() falla), cae a plain-print.
* Tolerante a claves faltantes/vacias en el snapshot: D1/D2/D3
  pueden no haber terminado al momento de verificar.
"""
import curses
import sys
import time


# ---------------------------------------------------------------------------
# Constantes de layout
# ---------------------------------------------------------------------------

VIEWS = (
    (1, "resumen",    "Resumen",         "r"),
    (2, "memoria",    "Memoria",         "m"),
    (3, "fds",        "File descriptors","f"),
    (4, "threads",    "Threads",         "t"),
    (5, "senales",    "Senales",         "s"),
    (6, "scheduling", "Scheduling",      "p"),
    (7, "sistema",    "Sistema global",  "g"),
)
VIEW_KEY = {
    "resumen": 1, "memoria": 2, "fds": 3, "threads": 4,
    "senales": 5, "scheduling": 6, "sistema": 7,
}
# Tecla alterna (r/m/f/t/s/p/g) por nombre de vista.
VIEW_ALT = {alt: n for (n, _key, _name, alt) in VIEWS}
# Nombre de vista a partir de su numero 1-7.
VIEW_OF = {n: key for (n, key, _name, _alt) in VIEWS}
# Etiqueta visible por numero de vista.
VIEW_LABEL = {n: name for (n, _key, name, _alt) in VIEWS}

INTERVAL_STEPS = {
    "resumen": 0.5, "memoria": 1.0, "fds": 1.0, "threads": 0.5,
    "senales": 1.0, "scheduling": 1.0, "sistema": 0.5,
}
INTERVAL_MAX = 60.0
SORT_CYCLE = ("cpu", "rss", "pid")
# Edad maxima (segundos) para considerar un dato "fresco". Pasado eso
# se renderiza "stale" pero NO se oculta -- la consigna pide ver
# incluso datos viejos para debug.
STALE_AGE = 10.0


# ---------------------------------------------------------------------------
# Helpers puros (testeables sin curses)
# ---------------------------------------------------------------------------

def _now():
    return time.time()


def _age(snapshot, key):
    """Edad en segundos de la clave. 0.0 si nunca se escribio."""
    ts = snapshot.get("_ts") or {}
    t = ts.get(key, 0.0) or 0.0
    if t <= 0:
        return float("inf")
    return _now() - t


def _stale(snapshot, key, max_age=STALE_AGE):
    age = _age(snapshot, key)
    if age == float("inf"):
        return True
    return age > max_age


def _fmt_age(snapshot, key):
    """Render amigable de la edad: '0.4s', '12.3s' o '— sin datos —'."""
    age = _age(snapshot, key)
    if age == float("inf"):
        return "— sin datos —"
    if age >= 999:
        return "— sin datos —"
    return f"{age:.1f}s"


def _is_empty(d):
    """True si el sub-snapshot no es dict o esta vacio (D1/D2/D3 no hechos)."""
    return not isinstance(d, dict) or len(d) == 0


def _sort_processes(resumen, sort_key):
    """Devuelve [(pid, data)] ordenadas. resumen: snapshot['resumen'] o {}."""
    items = [(p, d) for p, d in (resumen or {}).items()
             if isinstance(d, dict)]
    if sort_key == "cpu":
        items.sort(key=lambda kv: kv[1].get("cpu_percent", 0.0), reverse=True)
    elif sort_key == "rss":
        items.sort(key=lambda kv: kv[1].get("rss_kb", 0), reverse=True)
    elif sort_key == "pid":
        items.sort(key=lambda kv: kv[0])
    return items


def _matches_filters(pid, data, filter_text, filter_user):
    """Aplica / y u. filter_text: substring ci en comm+cmdline."""
    if filter_text:
        ft = filter_text.lower()
        comm = (data.get("comm") or "").lower()
        cmd = (data.get("cmdline") or "").lower()
        if ft not in comm and ft not in cmd:
            return False
    if filter_user:
        fu = filter_user.lower()
        u = (data.get("username") or "").lower()
        if u != fu and not (fu.isdigit() and str(data.get("uid", "")) == fu):
            return False
    return True


def _visible_pids(resumen, sort_key, filter_text, filter_user, limit=None):
    """Lista final que va al top: ordenada, filtrada, con cap opcional."""
    items = _sort_processes(resumen, sort_key)
    out = []
    for pid, d in items:
        if not _matches_filters(pid, d, filter_text, filter_user):
            continue
        out.append((pid, d))
        if limit is not None and len(out) >= limit:
            break
    return out


def _format_cmd(d, max_len=40):
    cmd = d.get("cmdline", "") or ""
    if not cmd:
        cmd = f"[{d.get('comm', '?')}]"
    if max_len and len(cmd) > max_len:
        cmd = cmd[: max_len - 3] + "..."
    return cmd


# ---------------------------------------------------------------------------
# Top-list columns. (label, width, getter). Width -1 = variable.
# ---------------------------------------------------------------------------

def _top_columns():
    return (
        ("PID",    6, lambda pid, d: f"{pid}"),
        ("PPID",   5, lambda pid, d: f"{d.get('ppid', 0)}"),
        ("USER",   7,
         lambda pid, d: (d.get("username") or str(d.get("uid", "?")))[:7]),
        ("STATE",  6, lambda pid, d: str(d.get("state", "?"))[:6]),
        ("CPU%",   5, lambda pid, d: f"{d.get('cpu_percent', 0.0):.1f}"),
        ("RSS(MB)",7, lambda pid, d: f"{d.get('rss_kb', 0) / 1024:.1f}"),
        ("THR",    4, lambda pid, d: f"{d.get('threads', 0)}"),
        ("CMD",   -1, lambda pid, d: _format_cmd(d, max_len=80)),
    )


def _build_top_header_line(width):
    cols = _top_columns()
    parts = []
    for label, w, _ in cols:
        if w < 0:
            parts.append(label)
        else:
            parts.append(f"{label:<{w}}")
    line = " ".join(parts)
    if len(line) > width:
        line = line[: width - 1]
    return line


def _build_top_row(pid, d, width):
    cols = _top_columns()
    parts = []
    for _label, w, getter in cols:
        val = getter(pid, d)
        if w < 0:
            parts.append(val)
        else:
            parts.append(f"{val:<{w}}")
    line = " ".join(parts)
    if len(line) > width:
        line = line[: width - 1]
    return line


# ---------------------------------------------------------------------------
# Detail-panel content per view
# ---------------------------------------------------------------------------

def _decode_signals(mask):
    """Decodifica una mascara de senales usando procfs.decode_signal_mask."""
    try:
        from procfs import decode_signal_mask
    except Exception:
        return [f"  mascara: 0x{int(mask):x}"]
    if not mask:
        return ["  (vacio)"]
    names = decode_signal_mask(int(mask))
    if not names:
        return [f"  (mascara 0x{int(mask):x} sin bits conocidos)"]
    # Wrap a ~80 chars por linea.
    lines = []
    cur = "  "
    for n in names:
        cand = (cur + " " + n) if cur.strip() else "  " + n
        if len(cand) > 78:
            lines.append(cur.rstrip())
            cur = "  " + n
        else:
            cur = cand
    if cur.strip():
        lines.append(cur.rstrip())
    return lines


def _detail_for_view(state, snapshot, view_key, pid, data):
    """Devuelve lista de lineas (str) para el panel de detalle.

    Si no hay PID seleccionado o no hay datos, devuelve placeholder.
    Si el sub-snapshot de la vista esta vacio (analizador no listo),
    lo dice explicitamente.
    """
    if pid is None or data is None:
        return ["(ningun proceso seleccionado — use ↑/↓ para elegir)"]

    lines = []
    lines.append(f"PID: {pid}  PPID: {data.get('ppid', '?')}  "
                 f"USER: {data.get('username') or data.get('uid', '?')}  "
                 f"STATE: {data.get('state', '?')}")
    lines.append(f"comm:    {data.get('comm', '?')}")
    lines.append(f"cmdline: {_format_cmd(data, max_len=200)}")
    lines.append("")

    if view_key == "resumen":
        lines.append(f"CPU% (1 core):   {data.get('cpu_percent', 0.0):.2f}")
        lines.append(f"Threads:         {data.get('threads', 0)}")
        lines.append(f"RSS (kB):        {data.get('rss_kb', 0)}")
        if data.get("uid") is not None:
            lines.append(
                f"UID/GID:         {data.get('uid', '?')}/"
                f"{data.get('gid', '?')}"
            )

    elif view_key == "memoria":
        mem = (snapshot.get("memoria") or {}).get(pid)
        if _is_empty(mem):
            lines.append("— sin datos del analizador de memoria "
                         "(aun no corrio) —")
        else:
            lines.append(f"VmSize:  {mem.get('vm_size_kb', 0):>8} kB")
            lines.append(f"VmRSS:   {mem.get('vm_rss_kb', 0):>8} kB")
            lines.append(f"VmData:  {mem.get('vm_data_kb', 0):>8} kB")
            lines.append(f"VmStk:   {mem.get('vm_stk_kb', 0):>8} kB")
            lines.append(f"VmExe:   {mem.get('vm_exe_kb', 0):>8} kB")
            lines.append(f"VmLib:   {mem.get('vm_lib_kb', 0):>8} kB")
            lines.append(f"VmHWM:   {mem.get('vm_hwm_kb', 0):>8} kB")
            lines.append(f"VmSwap:  {mem.get('vm_swap_kb', 0):>8} kB")
            lines.append(f"minflt:  {mem.get('minflt', 0)}")
            lines.append(f"majflt:  {mem.get('majflt', 0)}")
            lines.append(f"cminflt: {mem.get('cminflt', 0)}")
            lines.append(f"cmajflt: {mem.get('cmajflt', 0)}")
            segs = mem.get("segments") or {}
            lines.append(
                f"segments: text={segs.get('text', 0)} "
                f"data={segs.get('data', 0)} heap={segs.get('heap', 0)} "
                f"stack={segs.get('stack', 0)} "
                f"shared={segs.get('shared', 0)} other={segs.get('other', 0)} "
                f"total={segs.get('total', 0)}"
            )

    elif view_key == "fds":
        fds = (snapshot.get("fds") or {}).get(pid)
        if not fds:
            lines.append("— sin datos del analizador de FDs "
                         "(aun no corrio) —")
        else:
            verbose = state.get("verbose", False)
            cap = len(fds) if verbose else min(10, len(fds))
            lines.append(
                f"total FDs: {len(fds)}"
                f"{' (verbose)' if verbose else ''}"
            )
            for fd in fds[:cap]:
                target = fd.get("target", "?")
                if len(target) > 60:
                    target = target[:57] + "..."
                lines.append(f"  fd {fd.get('fd', '?'):>4}  "
                             f"{str(fd.get('kind', '?')):<11}  {target}")
            if len(fds) > cap:
                lines.append(
                    f"  ... ({len(fds) - cap} mas — "
                    f"modo verbose para ver todos)"
                )

    elif view_key == "threads":
        ths = (snapshot.get("threads") or {}).get(pid)
        if not ths:
            lines.append("— sin datos del analizador de threads "
                         "(aun no corrio) —")
        else:
            lines.append(f"total threads: {len(ths)}")
            for t in ths:
                lines.append(
                    f"  tid {t.get('tid', '?'):>6}  "
                    f"{str(t.get('name', '?'))[:16]:<16}  "
                    f"{str(t.get('state', '?')):<3}  "
                    f"CPU {t.get('cpu_percent', 0.0):>5.1f}  "
                    f"volCS {t.get('voluntary_cs', 0):<5}  "
                    f"invCS {t.get('involuntary_cs', 0)}"
                )

    elif view_key == "senales":
        sen = (snapshot.get("senales") or {}).get(pid)
        if _is_empty(sen):
            lines.append("— sin datos del analizador de senales "
                         "(aun no corrio) —")
        else:
            for label, key in (
                ("SigBlk", "sigblk_raw"), ("SigIgn", "sigign_raw"),
                ("SigCgt", "sigcgt_raw"), ("SigPnd", "sigpnd_raw"),
                ("ShdPnd", "shdpnd_raw"),
            ):
                mask = sen.get(key, 0)
                lines.append(f"{label}  (0x{int(mask):016x}):")
                lines.extend(_decode_signals(mask))

    elif view_key == "scheduling":
        sch = (snapshot.get("scheduling") or {}).get(pid)
        if _is_empty(sch):
            lines.append("— sin datos del analizador de scheduling "
                         "(aun no corrio) —")
        else:
            lines.append(f"nice:            {sch.get('nice', '?')}")
            lines.append(f"priority:        {sch.get('priority', '?')}")
            lines.append(
                f"policy:          "
                f"{sch.get('policy_name', sch.get('policy', '?'))}"
            )
            lines.append(f"rt_priority:     {sch.get('rt_priority', '?')}")
            lines.append(f"cpu_affinity:    {sch.get('cpu_affinity', '?')}")
            lines.append(f"utime:           {sch.get('utime', '?')}")
            lines.append(f"stime:           {sch.get('stime', '?')}")
            lines.append(f"session_id:      {sch.get('session_id', '?')}")
            lines.append(f"pgrp:            {sch.get('pgrp', '?')}")
            lines.append(f"voluntary_cs:    {sch.get('voluntary_cs', '?')}")
            lines.append(f"nonvoluntary_cs: {sch.get('involuntary_cs', '?')}")

    elif view_key == "sistema":
        # El sistema es global, no per-PID. Si el usuario selecciono un
        # proceso seguimos mostrando stats globales abajo igual.
        sis = snapshot.get("sistema") or {}
        lines.extend(_global_stats_lines(sis, snapshot))

    return lines


def _top3_lines(snapshot):
    """Top 3 procesos por CPU y por RSS para la vista sistema."""
    resumen = snapshot.get("resumen") or {}
    if not resumen:
        return []
    lines = []
    top_cpu = _sort_processes(resumen, "cpu")[:3]
    for pid, d in top_cpu:
        lines.append(
            f"top CPU: {pid:<6} {_format_cmd(d, max_len=40)} "
            f"{d.get('cpu_percent', 0.0):>5.1f}%"
        )
    top_rss = _sort_processes(resumen, "rss")[:3]
    for pid, d in top_rss:
        lines.append(
            f"top RSS: {pid:<6} {_format_cmd(d, max_len=40)} "
            f"{d.get('rss_kb', 0):>6}kB"
        )
    return lines


def _global_stats_lines(sis, snapshot):
    if not sis:
        return ["— sin datos del analizador de sistema (aun no corrio) —"]
    lines = []
    load1 = sis.get("load1")
    if load1 is not None:
        lines.append(
            f"loadavg: 1m={load1} 5m={sis.get('load5', '?')} "
            f"15m={sis.get('load15', '?')}  "
            f"running={sis.get('running_count', '?')}/"
            f"{sis.get('total_count', '?')}"
        )
    breakdown = sis.get("cpu_breakdown") or {}
    if breakdown or sis.get("cpu_percent") is not None:
        lines.append(
            f"CPU: busy={sis.get('cpu_percent', '?')}%  "
            f"user={breakdown.get('user', '?')} "
            f"system={breakdown.get('system', '?')} "
            f"idle={breakdown.get('idle', '?')} "
            f"iowait={breakdown.get('iowait', '?')}"
        )
    mem = sis.get("mem") or {}
    if mem:
        lines.append(
            f"mem: total={mem.get('total_kb', '?')}kB "
            f"free={mem.get('free_kb', '?')}kB "
            f"buf={mem.get('buffers_kb', '?')}kB "
            f"cached={mem.get('cached_kb', '?')}kB"
        )
        lines.append(
            f"swap: total={mem.get('swap_total_kb', '?')}kB "
            f"free={mem.get('swap_free_kb', '?')}kB"
        )
    procs = sis.get("processes") or {}
    by_state = procs.get("by_state") or {}
    threads_total = None
    resumen = snapshot.get("resumen")
    if resumen:
        threads_total = sum(
            d.get("threads", 0)
            for d in resumen.values()
            if isinstance(d, dict)
        )
    if procs or threads_total is not None:
        total = procs.get("total")
        lines.append(
            f"procs: total={total if total is not None else '?'} "
            f"running={by_state.get('R', '?')} "
            f"sleeping={by_state.get('S', '?')} "
            f"zombie={by_state.get('Z', '?')} "
            f"threads={threads_total if threads_total is not None else '?'} "
            f"d={by_state.get('D', '?')} "
            f"t={by_state.get('T', '?')} "
            f"i={by_state.get('I', '?')} "
            f"other={by_state.get('other', '?')}"
        )
    top3 = _top3_lines(snapshot)
    if top3:
        lines.append("")
        lines.extend(top3)
    if sis.get("uptime") is not None:
        lines.append(f"uptime: {sis.get('uptime')}s  "
                     f"btime: {sis.get('btime', '?')}")
    if not lines:
        lines.append(
            f"(snapshot 'sistema' presente pero sin campos reconocidos: "
            f"{list(sis.keys())[:6]}...)"
        )
    return lines


# ---------------------------------------------------------------------------
# Key handling (puro)
# ---------------------------------------------------------------------------

def _next_sort(current):
    """Cycle al siguiente sort key. cpu -> rss -> pid -> cpu."""
    try:
        i = SORT_CYCLE.index(current)
    except ValueError:
        return "cpu"
    return SORT_CYCLE[(i + 1) % len(SORT_CYCLE)]


def _bump_interval(value_obj, view_key, delta, config):
    """Suma delta al Value, clamp por intervalo_minimo y INTERVAL_MAX.

    Devuelve (nuevo_valor, clamped: bool). clamped=True significa que
    se intento ir mas alla del limite (util para que el caller muestre
    un mensaje "min"/"max" si quiere).
    """
    if value_obj is None:
        return 0.0, True
    minimo = float((config.get("intervalo_minimo") or {}).get(view_key, 0.0))
    cur = float(value_obj.value)
    new = cur + delta
    if new < minimo:
        new = minimo
        value_obj.value = minimo
        return new, True
    if new > INTERVAL_MAX:
        new = INTERVAL_MAX
        value_obj.value = INTERVAL_MAX
        return new, True
    value_obj.value = new
    return new, False


def _handle_key(ch, state, interval_values, config):
    """Procesa una tecla. Muta state y devuelve un "command" string.

    Commands:
      "quit"        -> termina el main loop (event.set lo hace el caller)
      "redraw"      -> algo visible cambio, repaint ASAP
      "noop"        -> no hace falta repaint
    """
    # Salir
    if ch in (ord("q"), ord("Q")):
        return "quit"
    # Help overlay
    if ch in (ord("h"), ord("H"), ord("?")):
        state["show_help"] = not state.get("show_help", False)
        return "redraw"
    # Cambio de vista
    view_changed = None
    if ord("1") <= ch <= ord("7"):
        view_changed = ch - ord("1") + 1
    elif ch in (ord("r"), ord("R")):
        view_changed = 1
    elif ch in (ord("m"), ord("M")):
        view_changed = 2
    elif ch in (ord("f"), ord("F")):
        view_changed = 3
    elif ch in (ord("t"), ord("T")):
        view_changed = 4
    elif ch in (ord("s"), ord("S")):
        view_changed = 5
    elif ch in (ord("p"), ord("P")):
        view_changed = 6
    elif ch in (ord("g"), ord("G")):
        view_changed = 7
    if view_changed is not None:
        if state["view"] != view_changed:
            state["view"] = view_changed
            # Al cambiar de vista perdemos el pin (la consigna dice que el
            # pin se mantiene aunque se reordene; cambiar de vista es un
            # reset explicito).
            state["pinned_pid"] = None
        return "redraw"

    # Navegacion
    if ch == curses.KEY_UP:
        state["cursor"] = max(0, state.get("cursor", 0) - 1)
        return "redraw"
    if ch == curses.KEY_DOWN:
        # Clamp contra el ultimo item conocido para que el cursor no
        # "derive" mas alla del final (subir vuelve a ser 1:1).
        n = len(state.get("_visible_items") or ())
        state["cursor"] = min(state.get("cursor", 0) + 1, max(0, n - 1))
        return "redraw"

    # Pin (toggle: Enter pinea, Enter de nuevo despinea)
    if ch in (10, 13, curses.KEY_ENTER):
        if state.get("pinned_pid") is not None:
            state["pinned_pid"] = None
        else:
            pid = state.get("selected_pid")
            if pid is not None:
                state["pinned_pid"] = pid
        return "redraw"

    # Sort cycle
    if ch in (ord("c"), ord("C")):
        state["sort_key"] = _next_sort(state.get("sort_key", "cpu"))
        state["cursor"] = 0
        return "redraw"

    # Filtros: se manejan en un sub-loop de input (no aca) — el main
    # loop ve un command aparte para "/", "u", "esc".
    if ch == ord("/"):
        return "prompt_name"
    if ch in (ord("u"), ord("U")):
        return "prompt_user"
    if ch == 27:  # Esc
        if state.get("filter_text") or state.get("filter_user"):
            state["filter_text"] = None
            state["filter_user"] = None
            state["cursor"] = 0
            return "redraw"
        return "noop"

    # Intervalo
    if ch in (ord("+"), ord("="), curses.KEY_RIGHT):  # '=' = '+' en US layout
        view_key = VIEW_OF[state["view"]]
        step = INTERVAL_STEPS.get(view_key, 0.5)
        new, _ = _bump_interval(
            interval_values.get(view_key), view_key, +step, config
        )
        state["_last_interval_change"] = ("+", new, step)
        return "redraw"
    if ch in (ord("-"), ord("_"), curses.KEY_LEFT):
        view_key = VIEW_OF[state["view"]]
        step = INTERVAL_STEPS.get(view_key, 0.5)
        new, _ = _bump_interval(
            interval_values.get(view_key), view_key, -step, config
        )
        state["_last_interval_change"] = ("-", new, step)
        return "redraw"

    return "noop"


# ---------------------------------------------------------------------------
# Dibujo (curses)
# ---------------------------------------------------------------------------

def _safe_addstr(win, y, x, text, attr=0):
    """addstr que recorta silenciosamente. Sin raise en posicion invalida."""
    if y < 0 or x < 0:
        return
    try:
        h, w = win.getmaxyx()
    except curses.error:
        return
    if y >= h or x >= w:
        return
    if not text:
        return
    text = str(text)
    if len(text) > w - x:
        text = text[: max(0, w - x - 1)]
    if not text:
        return
    try:
        win.addstr(y, x, text, attr)
    except curses.error:
        pass


def _hrule(win, y, char="─"):
    try:
        _, w = win.getmaxyx()
    except curses.error:
        return
    if y < 0:
        return
    try:
        win.move(y, 0)
        win.clrtoeol()
    except curses.error:
        return
    line = (char * max(0, w - 1))
    _safe_addstr(win, y, 0, line)


def _draw_header(win, state, snapshot, interval_values, verbose):
    h, w = win.getmaxyx()
    if h < 1:
        return
    uptime = state.get("_uptime_str", "")
    title = (
        f"─ monitor ─  uptime {uptime}  "
        f"verbose={'on' if verbose else 'off'}"
    )
    _safe_addstr(win, 0, 0, title.ljust(w - 1), curses.A_BOLD)
    if h < 2:
        return
    # Linea 2: edad de cada analizador.
    parts = []
    for n, key, name, _alt in VIEWS:
        marker = ">" if n == state["view"] else " "
        parts.append(f"{marker}[{name}]{_fmt_age(snapshot, key)}")
    line = " ".join(parts)
    if len(line) > w - 1:
        line = line[: w - 2]
    _safe_addstr(win, 1, 0, line)
    _hrule(win, 2)


def _draw_top_list(win, state, snapshot, top_start_y, top_height):
    h, w = win.getmaxyx()
    if top_height < 2:
        return None
    view_key = VIEW_OF[state["view"]]
    # Para vista 7 (sistema) el top sigue siendo procesos top-N por CPU.
    resumen = snapshot.get("resumen") or {}
    items = _visible_pids(
        resumen,
        state.get("sort_key", "cpu"),
        state.get("filter_text"),
        state.get("filter_user"),
    )

    # Si el pin apunta a un proceso que ya no esta en la lista (murio)
    # y hay otros procesos, soltamos el pin.
    pinned = state.get("pinned_pid")
    if (pinned is not None and items and
            not any(pid == pinned for pid, _ in items)):
        state["pinned_pid"] = None
        pinned = None

    # Header
    header = _build_top_header_line(w - 1)
    _safe_addstr(win, top_start_y, 0, header, curses.A_REVERSE)
    list_h = top_height - 1
    if list_h < 1:
        return items

    # Viewport: la lista puede ser mas larga que la pantalla; el cursor
    # no puede bajar mas alla del ultimo item.
    cursor = state.get("cursor", 0)
    if items:
        cursor = min(cursor, len(items) - 1)
    else:
        cursor = 0
    # Write-back: si la lista se achico (murio un proceso) el estado queda
    # sincronizado con el clamp y no se acumula deriva.
    if cursor != state.get("cursor", 0):
        state["cursor"] = cursor
    offset = max(0, cursor - list_h + 1)
    visible = items[offset:offset + list_h]
    for i, (pid, d) in enumerate(visible):
        row = _build_top_row(pid, d, w - 1)
        attr = 0
        if pid == pinned:
            attr = curses.A_BOLD
        if offset + i == cursor:
            attr |= curses.A_REVERSE
        _safe_addstr(win, top_start_y + 1 + i, 0, row, attr)

    # Selected pid: pin tiene prioridad, sino cursor -> items[cursor].
    selected_pid = None
    selected_data = None
    if pinned is not None:
        for pid, d in items:
            if pid == pinned:
                selected_pid = pid
                selected_data = d
                break
        if selected_pid is None and _is_empty(resumen):
            selected_pid = None
    if selected_pid is None and items:
        idx = min(cursor, max(0, len(items) - 1))
        selected_pid, selected_data = items[idx]

    # Si el selected no esta en visible (p.ej. filtro lo oculto) y hay items,
    # el panel de detalle mostrara "seleccion invalida" — pero solo si el
    # usuario esta navegando. Para evitar "seleccion rota" cuando el pin se
    # fue por el filtro, limpiamos selected_pid.
    if state.get("filter_text") or state.get("filter_user"):
        if selected_pid == state.get("pinned_pid") and pinned is not None:
            in_visible = any(p == pinned for p, _ in visible)
            if not in_visible:
                selected_pid = None
                selected_data = None

    state["selected_pid"] = selected_pid
    state["selected_data"] = selected_data
    state["_visible_items"] = items
    return items


def _draw_detail(win, state, snapshot, detail_start_y, detail_height,
                  interval_values, config):
    h, w = win.getmaxyx()
    if detail_height < 2:
        return
    view_num = state["view"]
    view_key = VIEW_OF[view_num]
    label = VIEW_LABEL[view_num]
    cur_interval = (
        float(interval_values[view_key].value)
        if interval_values.get(view_key) else 0.0
    )
    minimo = float((config.get("intervalo_minimo") or {}).get(view_key, 0.0))
    sort_label = {"cpu": "CPU%", "rss": "RSS", "pid": "PID"}.get(
        state.get("sort_key", "cpu"), "CPU%"
    )
    filters = []
    if state.get("filter_text"):
        filters.append(f"name={state['filter_text']!r}")
    if state.get("filter_user"):
        filters.append(f"user={state['filter_user']!r}")
    pin_str = f" PIN={state['pinned_pid']}" if state.get("pinned_pid") else ""
    header = (
        f"[Vista {view_num} {label}]  intervalo {cur_interval:.1f}s  "
        f"(min {minimo:.1f}s)  [-] [+]  sort={sort_label}{pin_str}"
    )
    if filters:
        header += "  filter=" + ",".join(filters)
    _safe_addstr(win, detail_start_y, 0, header, curses.A_BOLD)

    body_y = detail_start_y + 1
    body_h = detail_height - 1
    if body_h < 1:
        return

    pid = state.get("selected_pid")
    data = state.get("selected_data")
    if view_key == "sistema" and pid is None:
        sis = snapshot.get("sistema") or {}
        lines = _global_stats_lines(sis, snapshot)
    else:
        lines = _detail_for_view(state, snapshot, view_key, pid, data)

    # Renderizar lines, truncando.
    for i in range(body_h):
        if i >= len(lines):
            break
        line = lines[i]
        if len(line) > w - 1:
            line = line[: w - 2]
        _safe_addstr(win, body_y + i, 0, line)


def _draw_footer(win, state, y, height, interval_values):
    h, w = win.getmaxyx()
    if height < 1 or y >= h:
        return
    # Barra de status: ayuda rapida + ultimo cambio de intervalo.
    msg = (
        "[1-7] vista  [/]name  [u]user  [c]sort  [+/-]int  "
        "[Enter]pin/despin  [↑↓]nav  [h]help  [q]quit"
    )
    if state.get("_last_interval_change"):
        sign, val, step = state["_last_interval_change"]
        msg += f"   |   intervalo {sign}{step:.1f} -> {val:.1f}s"
    _safe_addstr(win, y, 0, msg, curses.A_REVERSE)


def _draw_help_overlay(win):
    h, w = win.getmaxyx()
    box_w = min(60, w - 2)
    box_h = min(20, h - 2)
    if box_h < 6:
        return
    y0 = (h - box_h) // 2
    x0 = (w - box_w) // 2
    lines = [
        "AYUDA — monitor (presione h o ? para cerrar)",
        "",
        "1..7 / r m f t s p g   Cambiar de vista",
        "↑ ↓                     Navegar lista",
        "Enter                   Pin/despin proceso (sobrevive reorden)",
        "/                       Filtrar por nombre (substring)",
        "u                       Filtrar por usuario",
        "Esc                     Limpiar filtros",
        "c                       Sort: CPU% -> RSS -> PID",
        "+ / -                   Ajustar intervalo de la vista activa",
        "h / ?                   Esta ayuda",
        "q                       Salir limpiamente",
    ]
    for i, line in enumerate(lines):
        attr = curses.A_REVERSE if i == 0 else 0
        _safe_addstr(win, y0 + i, x0, line.center(box_w - 1), attr)
    # Marco simple.
    for y in range(y0, y0 + box_h):
        _safe_addstr(win, y, x0, "│")
        _safe_addstr(win, y, x0 + box_w - 1, "│")
    _safe_addstr(win, y0, x0, "┌" + "─" * (box_w - 2) + "┐")
    _safe_addstr(win, y0 + box_h - 1, x0, "└" + "─" * (box_w - 2) + "┘")


def _draw(win, state, snapshot, interval_values, config, verbose):
    win.erase()
    h, w = win.getmaxyx()
    if h < 5 or w < 20:
        _safe_addstr(
            win, 0, 0,
            f"pantalla muy pequena ({w}x{h}), resize a >= 80x24",
        )
        win.refresh()
        return

    HEADER_H = 3  # 2 lineas de titulo + 1 hrule
    FOOTER_H = 1
    usable = h - HEADER_H - FOOTER_H
    if usable < 4:
        _safe_addstr(win, 0, 0, f"pantalla muy pequena ({w}x{h})")
        win.refresh()
        return
    # 45% top list, resto detail.
    top_h = max(3, int(usable * 0.45))
    detail_h = usable - top_h
    top_y = HEADER_H
    detail_y = HEADER_H + top_h

    _draw_header(win, state, snapshot, interval_values, verbose)
    _draw_top_list(win, state, snapshot, top_y, top_h)
    _hrule(win, detail_y - 1)
    _draw_detail(win, state, snapshot, detail_y, detail_h,
                 interval_values, config)
    _draw_footer(win, state, h - FOOTER_H, FOOTER_H, interval_values)

    if state.get("show_help"):
        _draw_help_overlay(win)

    win.refresh()


# ---------------------------------------------------------------------------
# Prompt for filter text
# ---------------------------------------------------------------------------

def _prompt_text(win, prompt, initial="", event=None):
    """Lee una linea de texto. Devuelve (texto, ok). ok=False = Esc.

    Dibuja el prompt en la ultima linea y reusa curses echo.
    """
    h, w = win.getmaxyx()
    y = h - 1
    try:
        curses.curs_set(1)
    except curses.error:
        pass
    win.move(y, 0)
    win.clrtoeol()
    _safe_addstr(win, y, 0, prompt)
    _safe_addstr(win, y, len(prompt), initial)
    win.refresh()
    curses.echo()
    buf = list(initial)
    try:
        while True:
            if event is not None and event.is_set():
                return "".join(buf), False
            ch = win.getch()
            if ch == 27:  # Esc
                return "", False
            if ch in (10, 13, curses.KEY_ENTER):
                return "".join(buf), True
            if ch in (curses.KEY_BACKSPACE, 127, 8):
                if buf:
                    buf.pop()
                win.move(y, 0)
                win.clrtoeol()
                _safe_addstr(win, y, 0, prompt + "".join(buf))
            elif 0 <= ch < 256:
                buf.append(chr(ch))
                # echo se encarga de pintar; nos limitamos a no romper.
            win.refresh()
            time.sleep(0.02)
    finally:
        curses.noecho()
        try:
            curses.curs_set(0)
        except curses.error:
            pass


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------

def _initial_state():
    return {
        "view": 1,
        "cursor": 0,
        "selected_pid": None,
        "selected_data": None,
        "pinned_pid": None,
        "filter_text": None,
        "filter_user": None,
        "sort_key": "cpu",
        "show_help": False,
        "verbose": False,
        "_visible_items": [],
        "_uptime_str": "0:00:00",
        "_last_interval_change": None,
        "_last_seen_filters": None,
    }


def _main_loop(stdscr, event, snapshot, interval_values, config, verbose_flag):
    state = _initial_state()
    t0 = _now()
    try:
        curses.curs_set(0)
    except curses.error:
        pass
    stdscr.nodelay(True)
    stdscr.keypad(True)
    last_paint = 0.0
    PAINT_INTERVAL = 0.1  # 10 Hz

    while not event.is_set():
        # Poll verbose flag (Value('b')) — el handler de task F lo togglea.
        try:
            state["verbose"] = bool(verbose_flag.value)
        except Exception:
            pass

        # Update uptime.
        s = int(_now() - t0)
        hh, rem = divmod(s, 3600)
        mm, ss = divmod(rem, 60)
        state["_uptime_str"] = f"{hh}:{mm:02d}:{ss:02d}"

        # SIGHUP puede reescribir 'filtros' en el config compartido; si
        # el valor cambio, lo aplicamos (respeta filtros interactivos).
        try:
            filtros = config.get("filtros") or {}
        except Exception:
            filtros = {}
        if filtros != state.get("_last_seen_filters"):
            state["_last_seen_filters"] = filtros
            new_name = filtros.get("name") or None
            new_user = filtros.get("user") or None
            if (state.get("filter_text"), state.get("filter_user")) != (
                    new_name, new_user):
                state["filter_text"] = new_name
                state["filter_user"] = new_user
                state["cursor"] = 0
                last_paint = 0

        ch = stdscr.getch()
        if ch == curses.KEY_RESIZE:
            # La terminal cambio de tamano: actualizar LINES/COLS y
            # forzar un repaint completo con las nuevas dimensiones.
            if curses.is_term_resized(*stdscr.getmaxyx()):
                curses.resizeterm(*stdscr.getmaxyx())
            stdscr.clear()
            last_paint = 0
        elif ch != -1:
            action = _handle_key(ch, state, interval_values, config)
            if action == "quit":
                event.set()
                break
            if action == "prompt_name":
                cur = state.get("filter_text") or ""
                txt, ok = _prompt_text(stdscr, "filtro nombre: ", cur, event)
                if ok:
                    state["filter_text"] = txt if txt else None
                    state["cursor"] = 0
                last_paint = 0  # force redraw
            elif action == "prompt_user":
                cur = state.get("filter_user") or ""
                txt, ok = _prompt_text(stdscr, "filtro usuario: ", cur, event)
                if ok:
                    state["filter_user"] = txt if txt else None
                    state["cursor"] = 0
                last_paint = 0
            else:
                last_paint = 0  # cualquier cambio visible: repaint ASAP

        if _now() - last_paint > PAINT_INTERVAL:
            # Snapshot proxy read is slow; local copy for this tick.
            local_snap = {
                "resumen":    snapshot.get("resumen") or {},
                "memoria":    snapshot.get("memoria") or {},
                "fds":        snapshot.get("fds") or {},
                "threads":    snapshot.get("threads") or {},
                "senales":    snapshot.get("senales") or {},
                "scheduling": snapshot.get("scheduling") or {},
                "sistema":    snapshot.get("sistema") or {},
                "_ts":        snapshot.get("_ts") or {},
            }
            _draw(stdscr, state, local_snap, interval_values, config,
                  state.get("verbose", False))
            last_paint = _now()

        time.sleep(0.05)


# ---------------------------------------------------------------------------
# Plain-print fallback (no TTY)
# ---------------------------------------------------------------------------

def _top_por_cpu(resumen, n=10):
    return _sort_processes(resumen, "cpu")[:n]


def _plain_print_loop(event, snapshot, interval_values, config, verbose_flag):
    print(
        "[display] sin TTY — fallback plain print",
        file=sys.stderr, flush=True,
    )
    while not event.is_set():
        now = _now()
        ts = snapshot.get("_ts") or {}
        resumen = snapshot.get("resumen") or {}
        sistema = snapshot.get("sistema") or {}
        lineas_ts = " ".join(
            f"[{k}]{now - (ts.get(k, 0.0) or 0.0):.1f}s"
            for _, k, _, _ in VIEWS
        )
        print("=" * 78, flush=True)
        print(f"=== monitor — {len(resumen)} procesos ===", flush=True)
        print(lineas_ts, flush=True)
        if sistema:
            load1 = sistema.get("load1")
            if load1 is not None:
                print(f"[sistema] load1={load1} "
                      f"load5={sistema.get('load5', '?')}", flush=True)
        if resumen:
            print(f"{'PID':<6} {'PPID':<5} {'USER':<7} {'STATE':<6} "
                  f"{'CPU%':>5}  {'RSS(MB)':>6}  {'THR':<4}  CMD",
                  flush=True)
            for pid, d in _top_por_cpu(resumen, n=10):
                print(_build_top_row(pid, d, 78), flush=True)
        else:
            print("(esperando primer tick de resumen...)", flush=True)
        time.sleep(1.0)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def run(event, snapshot, interval_values, config, verbose_flag):
    """Entry point del display.

    Args:
        event:           mp.Event compartido. El loop termina cuando se setea.
        snapshot:        Manager.dict con claves 'resumen'/'memoria'/...
                         y sub-dict '_ts' con los timestamps.
        interval_values: dict {view_name: mp.Value('d', seconds)}.
                         El display lo modifica con +/-.
        config:          dict leido de config.json. Necesitamos
                         'intervalo_minimo' para el clamp.
        verbose_flag:    mp.Value('b') que la tarea F togglea con SIGUSR2.
    """
    try:
        curses.wrapper(lambda stdscr: _main_loop(
            stdscr, event, snapshot, interval_values, config, verbose_flag))
    except Exception as e:
        # curses puede fallar por: no TTY, terminal desconocido, terminal
        # demasiado chico, o error de init. En cualquier caso caemos al
        # fallback para no romper el smoke test sin TTY.
        print(f"[display] curses no disponible ({e!r}), fallback plain-print",
              file=sys.stderr, flush=True)
        try:
            _plain_print_loop(event, snapshot, interval_values, config,
                              verbose_flag)
        except Exception as e2:
            print(f"[display] fallback tambien fallo: {e2!r}",
                  file=sys.stderr, flush=True)
