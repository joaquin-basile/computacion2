"""procfs.py — pure parser functions for Linux /proc filesystem files.

All functions in this module are PURE: they take content (str or bytes) or
a list of names as input and return Python dicts / lists / scalars. They
perform NO I/O (no ``open()``, no ``os.listdir()`` inside).

The I/O layer lives in ``src/collector.py`` (task C): that module
walks the filesystem, reads the raw bytes, and hands them to these
parsers. This split is what makes the parsers unit-testable on macOS
(no ``/proc`` on macOS) using the fixtures under ``tests/fixtures/``.

Conventions
-----------
* Numeric fields are returned as ``int`` (kB for memory sizes, jiffies
  for CPU times, signal mask as a 64-bit ``int``).
* String fields stay as ``str`` (``comm``, ``Cpus_allowed_list``, etc.).
* Memory sizes from ``/proc/<pid>/status`` are returned as int kB
  (the trailing `` kB`` is stripped).
* Multi-value fields (``Uid``, ``Gid``) are returned as the real
  (first) value under their normal key and as a full list under
  ``"Uid_list"`` / ``"Gid_list"``.
* Signal masks are returned as int; the caller passes them to
  :func:`decode_signal_mask` to get the human-readable names.
* Signal masks follow the real Linux kernel layout: ``bit (N-1) ==
  signal N`` (see :func:`decode_signal_mask` docstring for details).
"""

from __future__ import annotations

import os
import signal as _signal

__all__ = [
    "parse_stat",
    "parse_status",
    "parse_cmdline",
    "parse_fd_listing",
    "parse_fd_link_target",
    "parse_task_listing",
    "parse_task_stat",
    "parse_task_comm",
    "parse_task_status",
    "parse_maps",
    "parse_proc_stat",
    "parse_loadavg",
    "parse_meminfo",
    "parse_uptime",
    "decode_signal_mask",
    "list_pids",
    "cpu_percent",
    "SIGNAL_BY_VALUE",
]


# ---------------------------------------------------------------------------
# Signal name table (signals 1..64)
# ---------------------------------------------------------------------------
# The table is keyed by signal number (1..64): signal 1 == SIGHUP,
# signal 2 == SIGINT, ..., signal 64 == SIGRTMAX. ``decode_signal_mask``
# maps each set bit to a name using the real Linux kernel layout
# ``bit (N-1) == signal N`` (e.g. real ``SigIgn: ...0004`` is bit 2 =
# signal 3 = SIGQUIT). The dict keys are signal numbers, not bit
# indices; the (N-1) offset lives only in the bit-test of
# :func:`decode_signal_mask`.
#
# The table is hardcoded for portability: on macOS the stdlib
# ``signal.Signals`` enum only contains the 31 standard POSIX
# signals, not the 32 Linux real-time ones. We want ``decode_signal_mask``
# to produce a length-64 list for ``(1 << 64) - 1`` on any host, so
# the table is fixed.

STANDARD_SIGNAL_NAMES: dict[int, str] = {
    1: "SIGHUP", 2: "SIGINT", 3: "SIGQUIT", 4: "SIGILL", 5: "SIGTRAP",
    6: "SIGABRT", 7: "SIGBUS", 8: "SIGFPE", 9: "SIGKILL", 10: "SIGUSR1",
    11: "SIGSEGV", 12: "SIGUSR2", 13: "SIGPIPE", 14: "SIGALRM", 15: "SIGTERM",
    16: "SIGSTKFLT", 17: "SIGCHLD", 18: "SIGCONT", 19: "SIGSTOP",
    20: "SIGTSTP",
    21: "SIGTTIN", 22: "SIGTTOU", 23: "SIGURG", 24: "SIGXCPU", 25: "SIGXFSZ",
    26: "SIGVTALRM", 27: "SIGPROF", 28: "SIGWINCH", 29: "SIGIO", 30: "SIGPWR",
    31: "SIGSYS",
}
# Cross-check against the platform's signal.Signals enum when available.
# This catches the case where the host is not Linux (e.g. macOS uses
# SIGEMT for signal 7 instead of SIGBUS) and keeps the table consistent
# with the stdlib. We ONLY override signals that are in the enum;
# anything else keeps the hardcoded Linux name (real-time signals 32-64
# are not in macOS's enum, so we fall back to the hardcoded table).
for _sig in _signal.Signals:
    if 1 <= _sig.value <= 64:
        STANDARD_SIGNAL_NAMES[_sig.value] = _sig.name

# Linux real-time signals 32..64 (SIGRTMIN+0 .. SIGRTMAX).
# Filled in only if not already populated by the stdlib override above.
for _n in range(32, 65):
    STANDARD_SIGNAL_NAMES.setdefault(_n, f"SIGRTMIN+{_n - 32}")

SIGNAL_BY_VALUE: dict[int, str] = dict(STANDARD_SIGNAL_NAMES)


# ---------------------------------------------------------------------------
# /proc/<pid>/stat
# ---------------------------------------------------------------------------
# Kernel field numbering is 1-indexed. The full list is documented in
# ``man 5 proc``. We only extract what the consigna requires.
#
# IMPORTANT: the spec says ``pgrp`` is field 7, but the real kernel
# has ``pgrp`` at field 5 (and field 7 is ``tty_nr``). This is a known
# error in the consigna copy we were given; we follow the kernel truth
# and return the real field 5 under the key ``"pgrp"``. The dict also
# exposes ``"session_id"`` (kernel field 6) and ``"tty_nr"``
# (kernel field 7) for completeness, so callers can pick.
_STAT_FIELDS: dict[str, int] = {
    "pid": 1, "comm": 2, "state": 3,
    "ppid": 4, "pgrp": 5, "session_id": 6,
    "tty_nr": 7, "tpgid": 8, "flags": 9,
    "minflt": 10, "cminflt": 11, "majflt": 12, "cmajflt": 13,
    "utime": 14, "stime": 15, "cutime": 16, "cstime": 17,
    "priority": 18, "nice": 19, "num_threads": 20,
    "rt_priority": 40, "policy": 41,
}


def parse_stat(content: str) -> dict:
    """Parse ``/proc/<pid>/stat`` (or ``/proc/<pid>/task/<tid>/stat``).

    Returns a dict with the consigna fields plus a few extras for
    completeness. Field numbering follows the kernel 1-indexed
    convention; see the module docstring for the ``pgrp`` /
    ``session_id`` field-numbering note.

    The ``comm`` field (kernel field 2) is wrapped in parentheses and
    can contain spaces and other parentheses, e.g. ``(my weird (proc))``.
    We split using the *last* ``)`` so the rest of the line is plain
    space-separated fields.
    """
    rparen = content.rfind(")")
    if rparen == -1:
        raise ValueError("malformed /proc/<pid>/stat: no closing ')'")
    # The opening '(' for the comm field is the FIRST '(' in the line.
    # The closing ')' is the LAST ')' (because comm can contain '(' and
    # ')', so the LAST ')' is the one that closes the comm).
    lparen = content.find("(")
    if lparen == -1 or lparen > rparen:
        raise ValueError("malformed /proc/<pid>/stat: no opening '('")

    before = content[:lparen].split()           # [pid]
    comm = content[lparen + 1:rparen]          # "my weird (proc)"
    after = content[rparen + 1:].split()        # state and beyond
    fields = before + [comm] + after
    # 0-indexed index for kernel field N is N - 1.
    result: dict = {"comm": fields[1]}
    for key, kernel_field in _STAT_FIELDS.items():
        idx = kernel_field - 1
        if idx >= len(fields):
            continue
        raw = fields[idx]
        if key == "comm":
            continue
        try:
            result[key] = int(raw)
        except ValueError:
            result[key] = raw
    return result


# ---------------------------------------------------------------------------
# /proc/<pid>/status
# ---------------------------------------------------------------------------

def parse_status(content: str) -> dict:
    """Parse ``/proc/<pid>/status`` (or the per-thread variant).

    Returns a dict with all ``Key: Value`` lines. The consigna-required
    keys are returned with the following types:

    * ``Name`` (str)
    * ``PPid`` (int)
    * ``Uid`` (int, real UID), ``Uid_list`` (list[int])
    * ``Gid`` (int, real GID), ``Gid_list`` (list[int])
    * ``Threads`` (int)
    * ``VmSize``, ``VmRSS``, ``VmData``, ``VmStk``, ``VmExe``,
      ``VmLib``, ``VmHWM``, ``VmSwap`` (int kB, suffix stripped)
    * ``SigBlk``, ``SigIgn``, ``SigCgt``, ``SigPnd``, ``ShdPnd`` (int)
    * ``voluntary_ctxt_switches``, ``nonvoluntary_ctxt_switches`` (int)
    * ``Cpus_allowed_list`` (str, e.g. ``"0-3"``)
    * ``Nice`` (int, can be negative)

    Other keys (e.g. ``State``, ``CapBnd``, ``Seccomp``) are kept as
    either int (if numeric) or str, so callers can pick what they need.
    """
    result: dict = {}
    for line in content.splitlines():
        if not line.strip() or ":" not in line:
            continue
        key, _, value = line.partition(":")
        key = key.strip()
        value = value.strip()
        # Memory size: "1234 kB" -> 1234
        if value.endswith(" kB"):
            num = value[:-3].strip()
            if _is_int(num):
                result[key] = int(num)
                continue
        # Uid / Gid: multi-value (real, eff, saved, fs)
        if key in ("Uid", "Gid"):
            parts = value.split()
            ints = [int(p) for p in parts if _is_int(p)]
            if ints:
                result[key] = ints[0]
                result[f"{key}_list"] = ints
            else:
                result[key] = value
                result[f"{key}_list"] = value
            continue
        # Signal masks (SigBlk, SigIgn, SigCgt, SigPnd, ShdPnd) and
        # capability masks (CapInh, CapPrm, CapEff, CapBnd, CapAmb) are
        # written as 64-bit / 32-bit hex (e.g. "0000000000001000").
        if key in ("SigBlk", "SigIgn", "SigCgt", "SigPnd", "ShdPnd",
                   "CapInh", "CapPrm", "CapEff", "CapBnd", "CapAmb"):
            if value and all(c in "0123456789abcdefABCDEF" for c in value):
                result[key] = int(value, 16)
                continue
        # Default: try decimal int, else keep as string
        if _is_int(value):
            result[key] = int(value)
        else:
            result[key] = value
    return result


def _is_int(s: str) -> bool:
    """True if ``s`` is a plain integer literal (optional leading minus)."""
    if not s:
        return False
    return s.lstrip("-").isdigit()


# ---------------------------------------------------------------------------
# /proc/<pid>/cmdline
# ---------------------------------------------------------------------------

def parse_cmdline(content: str | bytes) -> str:
    """Join the null-separated args of ``/proc/<pid>/cmdline`` with spaces.

    The kernel writes raw bytes (potentially non-UTF8) separated by NULs.
    We replace NULs with spaces, decode as UTF-8 with replacement, and
    strip leading/trailing whitespace. Returns ``""`` for empty input.
    """
    if isinstance(content, bytes):
        text = content.replace(b"\x00", b" ").decode("utf-8", errors="replace")
    else:
        text = content.replace("\x00", " ")
    return text.strip()


# ---------------------------------------------------------------------------
# Directory listings (numeric -> int)
# ---------------------------------------------------------------------------

def _digits_to_sorted_ints(names: list[str]) -> list[int]:
    return sorted(int(n) for n in names if n.isdigit())


def parse_fd_listing(names: list[str]) -> list[int]:
    """Filter an ``os.listdir('/proc/<pid>/fd')`` result to PIDs (int).

    Non-numeric entries (which shouldn't exist in normal ``fd`` dirs
    but we don't trust that) are dropped. Returned sorted ascending.
    """
    return _digits_to_sorted_ints(names)


def parse_task_listing(names: list[str]) -> list[int]:
    """Same shape as :func:`parse_fd_listing` but for ``/proc/<pid>/task``."""
    return _digits_to_sorted_ints(names)


def list_pids(entries: list[str]) -> list[int]:
    """Filter an ``os.listdir('/proc')`` result to PIDs (int).

    Drops non-numeric entries such as ``"self"``, ``"cpuinfo"``,
    ``"loadavg"``, etc. Returned sorted ascending.
    """
    return _digits_to_sorted_ints(entries)


# ---------------------------------------------------------------------------
# FD link target
# ---------------------------------------------------------------------------

def parse_fd_link_target(target: str) -> dict:
    """Classify a ``readlink('/proc/<pid>/fd/<n>')`` result.

    Returns ``{"kind": str, "detail": str}``. Heuristic:

    * ``socket:[N]``        -> kind ``"socket"``, detail ``"N"``
    * ``pipe:[N]``          -> kind ``"pipe"``,   detail ``"N"``
    * ``/dev/pts/N``        -> kind ``"tty"``,    detail ``"/dev/pts/N"``
    * ``/dev/tty``          -> kind ``"tty"``,    detail ``"/dev/tty"``
    * ``anon_inode:[name]`` -> kind ``"anon_inode"``, detail ``"name"``
    * ``/some/path``        -> kind ``"file"``,   detail the path
    * anything else         -> kind ``"other"``,  detail the original string
    """
    if target.startswith("socket:"):
        return {"kind": "socket", "detail": _between_brackets(target)}
    if target.startswith("pipe:"):
        return {"kind": "pipe", "detail": _between_brackets(target)}
    if target.startswith("/dev/pts/") or target == "/dev/tty":
        return {"kind": "tty", "detail": target}
    if target.startswith("anon_inode:"):
        rest = target[len("anon_inode:"):]
        if rest.startswith("[") and rest.endswith("]"):
            rest = rest[1:-1]
        return {"kind": "anon_inode", "detail": rest}
    if target.startswith("/"):
        return {"kind": "file", "detail": target}
    return {"kind": "other", "detail": target}


def _between_brackets(s: str) -> str:
    """Extract the inside of ``[...]`` if present, else strip the prefix."""
    l = s.find("[")
    r = s.rfind("]")
    if l != -1 and r != -1 and r > l:
        return s[l + 1:r]
    if ":" in s:
        return s.split(":", 1)[1]
    return s


# ---------------------------------------------------------------------------
# /proc/<pid>/task/<tid>/...
# ---------------------------------------------------------------------------

def parse_task_stat(content: str) -> dict:
    """Parse ``/proc/<pid>/task/<tid>/stat``.

    Format is identical to :func:`parse_stat`; the only difference is
    that kernel field 1 is the TID, not the PID. We return the same
    dict shape as :func:`parse_stat`; callers that need to distinguish
    can rely on the surrounding context (or add a ``"tid"`` key from
    the directory name).
    """
    return parse_stat(content)


def parse_task_comm(content: str) -> str:
    """Parse ``/proc/<pid>/task/<tid>/comm`` (one name, trailing newline)."""
    return content.strip()


def parse_task_status(content: str) -> dict:
    """Parse ``/proc/<pid>/task/<tid>/status`` — same format as the
    process status.

    The consigna-relevant fields are ``Name``, ``voluntary_ctxt_switches``,
    ``nonvoluntary_ctxt_switches``. The parser keeps all other keys too,
    so :func:`parse_status` is reused verbatim.
    """
    return parse_status(content)


# ---------------------------------------------------------------------------
# /proc/<pid>/maps
# ---------------------------------------------------------------------------

# A "shared library" pathname is one that points to a .so or lives in
# a known lib directory. Real /proc maps include paths like:
#   /lib/x86_64-linux-gnu/libc.so.6
#   /usr/lib/libfoo.so.1.2.3
#   /lib64/ld-linux-x86-64.so.2
_SHARED_LIB_PREFIXES = ("/lib", "/usr/lib", "/lib64", "/usr/lib64")
_SHARED_LIB_SUFFIX = ".so"


def parse_maps(content: str) -> list[dict]:
    """Parse ``/proc/<pid>/maps``.

    Each line is ``address perms offset dev inode pathname``. Returns a
    list of dicts with the ``kind`` field classifying the mapping:

    * ``"text"``   — ``r-xp`` (executable code, takes priority)
    * ``"data"``   — ``rw-p`` with a regular file pathname
    * ``"shared"`` — ``r--p`` or ``rw-p`` whose path is a shared lib
    * ``"heap"``   — pathname == ``[heap]``
    * ``"stack"``  — pathname == ``[stack]``
    * ``"other"``  — anonymous mappings (no pathname), ``---p``,
      or anything that doesn't fit the above

    Decision rule: we apply the rules in a fixed priority order
    (heap > stack > r-xp > r--p shared > rw-p shared > rw-p data > other)
    so the classification is deterministic.
    """
    result: list[dict] = []
    for line in content.splitlines():
        if not line.strip():
            continue
        parts = line.split(None, 5)
        if len(parts) < 5:
            continue
        address = parts[0]
        perms = parts[1]
        try:
            offset = int(parts[2], 0)
        except ValueError:
            offset = 0
        dev = parts[3]
        try:
            inode = int(parts[4])
        except ValueError:
            inode = 0
        pathname = (parts[5] if len(parts) > 5 else "").strip()
        result.append({
            "address": address,
            "perms": perms,
            "offset": offset,
            "dev": dev,
            "inode": inode,
            "pathname": pathname,
            "kind": _classify_map(perms, pathname),
        })
    return result


def _classify_map(perms: str, pathname: str) -> str:
    """See :func:`parse_maps` for the rule list. Priority order matters."""
    if pathname == "[heap]":
        return "heap"
    if pathname == "[stack]":
        return "stack"
    is_shared = _is_shared_lib_path(pathname)
    if perms == "r-xp":
        return "text"
    if perms.startswith("r--") and is_shared:
        return "shared"
    if perms.startswith("rw-") and is_shared:
        return "shared"
    if perms.startswith("rw-") and pathname:
        return "data"
    if not pathname:
        return "other"
    return "other"


def _is_shared_lib_path(pathname: str) -> bool:
    if not pathname:
        return False
    if any(pathname.startswith(p) for p in _SHARED_LIB_PREFIXES):
        return True
    if _SHARED_LIB_SUFFIX in pathname:
        return True
    return False


# ---------------------------------------------------------------------------
# /proc/stat
# ---------------------------------------------------------------------------

_CPU_VALUE_KEYS = (
    "user", "nice", "system", "idle", "iowait",
    "irq", "softirq", "steal", "guest", "guest_nice",
)


def parse_proc_stat(content: str) -> dict:
    """Parse ``/proc/stat``.

    Returns::

        {
            "cpu":   {"user": int, "nice": int, "system": int, "idle": int,
                      "iowait": int, "irq": int, "softirq": int,
                      "steal": int, "guest": int, "guest_nice": int},
            "btime": int,    # boot time, unix epoch seconds
            "cpus":  [{"cpu": 0, ...same fields...},
                      {"cpu": 1, ...}, ...]   # one per cpuN line, optional
        }

    Only the aggregate ``cpu`` line and ``btime`` are required by the
    consigna; per-CPU entries are kept under ``cpus`` because they are
    essentially free to parse and useful for the global view.
    """
    result: dict = {"cpu": {}, "btime": 0, "cpus": []}
    for line in content.splitlines():
        if not line.strip():
            continue
        name, _, rest = line.partition(" ")
        values = rest.split()
        if name == "cpu":
            result["cpu"] = _cpu_values_to_dict(values)
        elif name.startswith("cpu") and name[3:].isdigit():
            entry = {"cpu": int(name[3:])}
            entry.update(_cpu_values_to_dict(values))
            result["cpus"].append(entry)
        elif name == "btime":
            try:
                result["btime"] = int(values[0])
            except (ValueError, IndexError):
                pass
    return result


def _cpu_values_to_dict(values: list[str]) -> dict:
    out: dict = {}
    for i, k in enumerate(_CPU_VALUE_KEYS):
        if i < len(values):
            try:
                out[k] = int(values[i])
            except ValueError:
                out[k] = 0
        else:
            out[k] = 0
    return out


# ---------------------------------------------------------------------------
# /proc/loadavg
# ---------------------------------------------------------------------------

def parse_loadavg(content: str) -> dict:
    """Parse ``/proc/loadavg``.

    Format: ``"load1 load5 load15 running/total last_pid"``.

    Returns ``{"load1", "load5", "load15", "running", "total", "last_pid"}``.
    """
    parts = content.split()
    if len(parts) < 5:
        raise ValueError(f"malformed /proc/loadavg: {content!r}")
    running, _, total = parts[3].partition("/")
    return {
        "load1": float(parts[0]),
        "load5": float(parts[1]),
        "load15": float(parts[2]),
        "running": int(running),
        "total": int(total),
        "last_pid": int(parts[4]),
    }


# ---------------------------------------------------------------------------
# /proc/meminfo
# ---------------------------------------------------------------------------

def parse_meminfo(content: str) -> dict:
    """Parse ``/proc/meminfo``.

    Returns a dict of ``{key: int_kB}``. The ``"kB"`` suffix is stripped.
    Non-numeric values are kept as strings.
    """
    result: dict = {}
    for line in content.splitlines():
        if ":" not in line:
            continue
        key, _, value = line.partition(":")
        key = key.strip()
        value = value.strip()
        if value.endswith("kB"):
            value = value[:-2].strip()
        if _is_int(value):
            result[key] = int(value)
        else:
            result[key] = value
    return result


# ---------------------------------------------------------------------------
# /proc/uptime
# ---------------------------------------------------------------------------

def parse_uptime(content: str) -> tuple[float, float]:
    """Parse ``/proc/uptime``: returns ``(uptime_seconds, idle_seconds)``."""
    parts = content.split()
    if len(parts) < 2:
        raise ValueError(f"malformed /proc/uptime: {content!r}")
    return (float(parts[0]), float(parts[1]))


# ---------------------------------------------------------------------------
# Signal mask decoding
# ---------------------------------------------------------------------------

def decode_signal_mask(mask: int) -> list[str]:
    """Decode a 64-bit signal mask to a sorted list of signal names.

    This module follows the real Linux kernel layout:

        bit (N-1) == signal N

    So::

        decode_signal_mask(0)              == []
        decode_signal_mask(1 << 2)         == ["SIGQUIT"]  # bit 2 = signal 3
        decode_signal_mask((1 << 2) | (1 << 14))
                                            == ["SIGQUIT", "SIGTERM"]

    Names are returned in numerical order (SIGHUP first, then SIGINT,
    ..., then ``SIGRTMIN+0``, ``SIGRTMIN+1``, ..., ``SIGRTMIN+31``).
    """
    if mask == 0:
        return []
    names: list[str] = []
    for n in range(1, 65):
        if mask & (1 << (n - 1)):
            name = SIGNAL_BY_VALUE.get(n)
            if name is not None:
                names.append(name)
    return names


# ---------------------------------------------------------------------------
# CPU% from two stat snapshots
# ---------------------------------------------------------------------------

def cpu_percent(
    prev_jiffies: dict,
    curr_jiffies: dict,
    elapsed_seconds: float,
    clock_tick: int = os.sysconf("SC_CLK_TCK"),
) -> float:
    """Return CPU% as a fraction of one core.

    ``prev_jiffies`` and ``curr_jiffies`` are dicts with at least
    ``"utime"`` and ``"stime"`` (the result of :func:`parse_stat`).
    ``elapsed_seconds`` is the wall-clock delta between the two
    snapshots; ``clock_tick`` is the kernel's USER_HZ (typically 100).

    The result is "% of one core": ``100.0`` means one full core was
    saturated, ``200.0`` means two cores, ``12.5`` means an eighth of
    one core. Returns ``0.0`` if ``elapsed_seconds <= 0``.
    """
    if elapsed_seconds <= 0:
        return 0.0
    prev_total = prev_jiffies.get("utime", 0) + prev_jiffies.get("stime", 0)
    curr_total = curr_jiffies.get("utime", 0) + curr_jiffies.get("stime", 0)
    delta = curr_total - prev_total
    if delta < 0:
        return 0.0
    return (delta / (elapsed_seconds * clock_tick)) * 100.0
