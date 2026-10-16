"""ACCF clock: minimal, stdlib-only, Windows-first real-time clock utility.

Design constraints (why it looks like this):
  - Align to the OS local clock (Windows-first) via `datetime.now().astimezone()`.
    NO tz-database dependency: `zoneinfo` raises ZoneInfoNotFoundError on this host
    (no tzdata), so we read the offset the OS already resolved.
  - Two-track time. Wall clock = absolute labels (can jump on NTP/DST). Monotonic =
    durations. NEVER compute a duration from wall time.
  - Continuous drift monitoring WITHOUT a daemon: every call compares against the
    previous sample kept in ACCF_HOME/.clock_sample.json. If |wall_delta - mono_delta|
    exceeds DRIFT_EPS_SEC, the system clock was adjusted between reads -> clock_adjusted.
  - Cost: pure stdlib, O(1). A steady-state call is a clock read plus dict build
    (~3us). The drift sample lives in-process and is flushed to disk at most once
    per PERSIST_INTERVAL_SEC, so no per-call filesystem I/O on the hot path.
  - Two ways to use time, kept deliberately separate:
      glance -> anchor()       one line, no annotation, no disk. "What time is it?"
      record -> now()/drift_check()  full state + drift, writes the sample.
                                 "Has the clock been stable?"
    Looking at a watch must not leave a trace; only calibration does.

CLI (also usable as an accf_call script target):
  python accf_clock.py                       -> one-line glance at the clock (default)
  python accf_clock.py full                  -> full JSON record + since-last drift
  python accf_clock.py drift                 -> explicit clock calibration
  python accf_clock.py age <iso|epoch>       -> age of a timestamp
  python accf_clock.py delta <isoA> <isoB>   -> signed delta between two timestamps
"""
from __future__ import annotations

import json
import os
import sys
import time
from datetime import datetime, timezone

DRIFT_EPS_SEC = 0.5  # wall-vs-monotonic divergence above this == clock was reset
PERSIST_INTERVAL_SEC = 30.0  # disk sample is ONLY for cross-process continuity

# In-process reference sample (wall, mono_ns). This is the authoritative "last read";
# the file on disk is a throttled *record* so a fresh process can still detect a jump.
_MEM_SAMPLE: tuple[float, int] | None = None
_LAST_PERSIST = 0.0
_WEEKDAYS = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")


def _sample_path() -> str:
    home = os.environ.get("ACCF_HOME", os.path.expanduser("~/.accf-mcp"))
    return os.path.join(home, ".clock_sample.json")


def _local_now() -> datetime:
    return datetime.now().astimezone()


def _offset_str(off) -> str:
    secs = int(off.total_seconds())
    sign = "+" if secs >= 0 else "-"
    secs = abs(secs)
    return "%s%02d:%02d" % (sign, secs // 3600, (secs % 3600) // 60)


def _fmt(d: datetime) -> str:
    return "%04d-%02d-%02d %02d:%02d:%02d %s (%s)" % (
        d.year, d.month, d.day, d.hour, d.minute, d.second,
        _offset_str(d.utcoffset()), _WEEKDAYS[d.weekday()])


def _read_sample():
    try:
        with open(_sample_path(), "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def _remember(wall: float, mono_ns: int, persist: bool | None = None) -> None:
    """Advance the reference sample. Disk write is throttled: it exists only so a
    fresh process can detect an NTP step that happened while we were not running."""
    global _MEM_SAMPLE, _LAST_PERSIST
    _MEM_SAMPLE = (wall, mono_ns)
    if persist is False:
        return
    if persist is True or (wall - _LAST_PERSIST) >= PERSIST_INTERVAL_SEC:
        _write_sample(wall, mono_ns)
        _LAST_PERSIST = wall


def _prev_sample():
    """Prefer the in-process reference (zero I/O); fall back to the on-disk record.
    Returns (wall, mono_ns) or None."""
    if _MEM_SAMPLE is not None:
        return _MEM_SAMPLE
    r = _read_sample()
    if not r:
        return None
    try:
        return (float(r.get("epoch")), int(r.get("mono_ns")))
    except Exception:
        return None


def _write_sample(wall: float, mono_ns: int) -> None:
    try:
        p = _sample_path()
        os.makedirs(os.path.dirname(p), exist_ok=True)
        tmp = p + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"epoch": wall, "mono_ns": mono_ns}, f)
        os.replace(tmp, p)
    except Exception:
        pass


def now(with_since_last: bool = True, persist: bool | None = None) -> dict:
    """Absolute local time + monotonic counter + tz, plus drift since last read.

    Cost profile: the clock read is ~2us; the disk sample is throttled to
    PERSIST_INTERVAL_SEC so a steady-state call stays at single-digit microseconds.
    Pass persist=False for a guaranteed zero-I/O read."""
    d = _local_now()
    wall = time.time()
    mono = time.monotonic_ns()
    out = {
        "iso": d.isoformat(timespec="milliseconds"),
        "date": "%04d-%02d-%02d" % (d.year, d.month, d.day),
        "time": "%02d:%02d:%02d" % (d.hour, d.minute, d.second),
        "weekday": _WEEKDAYS[d.weekday()],
        "epoch": round(wall, 3),
        "mono_ns": mono,
        "tz": d.tzname(),
        "utc_offset_min": int(d.utcoffset().total_seconds() // 60),
    }
    if with_since_last:
        prev = _prev_sample()
        if prev:
            wall_delta = wall - prev[0]
            mono_delta = (mono - prev[1]) / 1e9
            drift = wall_delta - mono_delta
            out["since_last"] = {
                "wall_delta_sec": round(wall_delta, 3),
                "mono_delta_sec": round(mono_delta, 3),
                "drift_sec": round(drift, 3),
                "clock_adjusted": abs(drift) > DRIFT_EPS_SEC,
            }
    _remember(wall, mono, persist)
    return out


def anchor(track: bool = True) -> str:
    """One line of current local time — 'a glance at the watch'.

    This is deliberately NOT a record: no drift block, no annotation, no disk
    write, nothing for the reader to analyse. `track` only refreshes an
    in-process counter that is invisible to the caller and lets drift_check()
    work later; it touches no file and costs ~0.1us.
    """
    global _MEM_SAMPLE
    d = _local_now()
    if track:
        _MEM_SAMPLE = (d.timestamp(), time.monotonic_ns())
    return _fmt(d)


def drift_check(persist: bool | None = None) -> dict:
    """Explicit calibration — the opposite of a glance. Compares the wall clock
    against the monotonic counter since the previous reference sample, so a clock
    step (NTP or manual) becomes visible as |drift| > DRIFT_EPS_SEC."""
    d = _local_now()
    wall = time.time()
    mono = time.monotonic_ns()
    out = {"anchor": _fmt(d), "epoch": round(wall, 3), "mono_ns": mono}
    prev = _prev_sample()
    if prev:
        wall_delta = wall - prev[0]
        mono_delta = (mono - prev[1]) / 1e9
        drift = wall_delta - mono_delta
        out["since_last"] = {
            "wall_delta_sec": round(wall_delta, 3),
            "mono_delta_sec": round(mono_delta, 3),
            "drift_sec": round(drift, 3),
            "clock_adjusted": abs(drift) > DRIFT_EPS_SEC,
        }
    _remember(wall, mono, persist)
    return out


def _parse(ts):
    if isinstance(ts, (int, float)):
        return datetime.fromtimestamp(float(ts), tz=timezone.utc).astimezone()
    s = str(ts).strip()
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=_local_now().tzinfo)
    return dt


def humanize(sec: float) -> str:
    s = abs(sec)
    for unit, div in (("d", 86400), ("h", 3600), ("m", 60)):
        if s >= div:
            return "%s%s" % (("%.1f" % (sec / div)).rstrip("0").rstrip("."), unit)
    return "%ds" % round(sec)


def age(ts) -> dict:
    """Age of a timestamp vs now (wall clock). Prefer delta() for in-session durations."""
    t = _parse(ts)
    n = _local_now()
    sec = (n - t).total_seconds()
    return {"ts": t.isoformat(timespec="seconds"), "now": n.isoformat(timespec="seconds"),
            "age_sec": round(sec, 1), "age_human": humanize(sec), "future": sec < 0}


def delta(a, b) -> dict:
    da, db = _parse(a), _parse(b)
    sec = (db - da).total_seconds()
    return {"from": da.isoformat(timespec="seconds"), "to": db.isoformat(timespec="seconds"),
            "delta_sec": round(sec, 1), "delta_human": humanize(sec)}


def main(argv) -> None:
    cmd = argv[0] if argv else ""
    if cmd == "age" and len(argv) >= 2:
        print(json.dumps(age(argv[1]), ensure_ascii=False, indent=1))
    elif cmd == "delta" and len(argv) >= 3:
        print(json.dumps(delta(argv[1], argv[2]), ensure_ascii=False, indent=1))
    elif cmd == "drift":
        print(json.dumps(drift_check(), ensure_ascii=False, indent=1))
    elif cmd == "full":
        print(json.dumps(now(), ensure_ascii=False, indent=1))
    else:
        print(anchor())


if __name__ == "__main__":
    main(sys.argv[1:])
