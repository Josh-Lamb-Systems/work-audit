"""Pure helpers for turning 5-second frames into time windows the AI describes."""
from __future__ import annotations

import math
from datetime import datetime
from typing import Any, Dict, List, Optional, Sequence

from .db import ACTIVE_STATES
from .util import hms


def window_start(ts: float, window_seconds: int) -> float:
    return math.floor(ts / window_seconds) * window_seconds


def _get(f: Any, k: str) -> Any:
    try:
        return f[k]
    except (KeyError, IndexError):
        return None


def runs(frames: Sequence[Any], interval: float) -> List[Dict[str, Any]]:
    """Group consecutive frames that share state/app/title into runs."""
    out: List[Dict[str, Any]] = []
    for f in frames:
        state = _get(f, "state")
        group = "active" if state in ACTIVE_STATES else state
        key = (group, _get(f, "app"), _get(f, "title") if group != "private" else None,
               _get(f, "domain") if group != "private" else None, _get(f, "reason") if group != "active" else None)
        ts = float(_get(f, "ts"))
        if out and out[-1]["key"] == key and ts - out[-1]["last_ts"] <= interval * 2.5:
            r = out[-1]
            r["frames"].append(f)
            r["last_ts"] = ts
            r["end"] = ts + interval
        else:
            out.append({"key": key, "group": group, "app": _get(f, "app"), "title": _get(f, "title"),
                        "url": _get(f, "url"), "domain": _get(f, "domain"), "reason": _get(f, "reason"),
                        "start": ts, "last_ts": ts, "end": ts + interval, "frames": [f]})
    return out


def select_frames(frames: Sequence[Any], interval: float, max_images: int) -> List[Any]:
    """Pick up to max_images frames with images: the last frame of each app/window run
    (the most complete view of that piece of work), longest runs first, then fill
    remaining slots with the most-changed frames. Returned in time order."""
    with_img = [f for f in frames if _get(f, "image")]
    if not with_img:
        return []
    rs = runs([f for f in frames if _get(f, "state") in ACTIVE_STATES], interval)
    reps = []
    for r in rs:
        imgs = [f for f in r["frames"] if _get(f, "image")]
        if imgs:
            reps.append((r["end"] - r["start"], imgs[-1]))
    reps.sort(key=lambda x: -x[0])
    chosen = [f for _, f in reps[:max_images]]
    chosen_ids = {_get(f, "id") for f in chosen}
    if len(chosen) < max_images:
        rest = [f for f in with_img if _get(f, "id") not in chosen_ids]
        rest.sort(key=lambda f: -(_get(f, "diff") if _get(f, "diff") is not None else 255.0))
        for f in rest:
            if len(chosen) >= max_images:
                break
            chosen.append(f)
            chosen_ids.add(_get(f, "id"))
    chosen.sort(key=lambda f: float(_get(f, "ts")))
    return chosen


def parse_clock_on(day_ref_ts: float, s: Any) -> Optional[float]:
    """'10:42' (local time on the same date as day_ref_ts) -> epoch seconds."""
    if not isinstance(s, str):
        return None
    s = s.strip()
    try:
        parts = s.split(":")
        h, m = int(parts[0]), int(parts[1][:2])
        sec = int(parts[2][:2]) if len(parts) > 2 else 0
        if not (0 <= h < 24 and 0 <= m < 60 and 0 <= sec < 60):
            return None
    except (ValueError, IndexError):
        return None
    base = datetime.fromtimestamp(day_ref_ts)
    return base.replace(hour=h, minute=m, second=sec, microsecond=0).timestamp()


def place_segments(segs: List[Dict[str, Any]], win_start: float, win_end: float) -> List[Dict[str, Any]]:
    """Give model segments non-overlapping boundaries that exactly cover the window.
    With valid, distinct start times they are ordered by time; otherwise the model's own
    order is kept and the window is split evenly between them."""
    if not segs:
        return []
    times = [parse_clock_on(win_start, s.get("start")) for s in segs]
    ok = all(t is not None and win_start - 120 <= t < win_end for t in times)
    order: List[int] = list(range(len(segs)))
    starts: List[float] = []
    if ok:
        order = sorted(order, key=lambda i: (times[i], i))
        starts = [max(win_start, times[i]) for i in order]
        starts[0] = win_start
        ok = all(b > a for a, b in zip(starts, starts[1:]))
    if not ok:
        order = list(range(len(segs)))
        step = (win_end - win_start) / float(len(segs))
        starts = [win_start + k * step for k in range(len(segs))]
    out: List[Dict[str, Any]] = []
    for k, i in enumerate(order):
        s = segs[i]
        s["start_ts"] = starts[k]
        s["end_ts"] = starts[k + 1] if k + 1 < len(order) else win_end
        out.append(s)
    return out


def assign_seconds(segs: List[Dict[str, Any]], frames: Sequence[Any], interval: float) -> None:
    """Attribute each active frame to the segment whose time range contains it (or the nearest one),
    then tighten each segment's start/end to the frames it actually got."""
    for s in segs:
        s["seconds"] = 0.0
        s["_first"] = s["_last"] = None
    if not segs:
        return
    for f in frames:
        if _get(f, "state") not in ACTIVE_STATES:
            continue
        ts = float(_get(f, "ts"))
        best = None
        for s in segs:
            if s["start_ts"] <= ts < s["end_ts"]:
                best = s
                break
        if best is None:
            best = min(segs, key=lambda s: min(abs(ts - s["start_ts"]), abs(ts - s["end_ts"])))
        best["seconds"] += interval
        best["_first"] = ts if best["_first"] is None else min(best["_first"], ts)
        best["_last"] = ts if best["_last"] is None else max(best["_last"], ts)
    for s in segs:
        if s["_first"] is not None:
            s["start_ts"] = s["_first"]
            s["end_ts"] = s["_last"] + interval
        s.pop("_first", None)
        s.pop("_last", None)


def describe_run(r: Dict[str, Any]) -> str:
    title = (r.get("title") or "").strip()
    bits = [r.get("app") or "?"]
    if title:
        bits.append('"%s"' % title[:120])
    if r.get("domain"):
        bits.append(r["domain"])
    return " — ".join(bits)


def run_lines(rs: List[Dict[str, Any]], limit: int = 30) -> List[str]:
    lines = []
    for r in rs[:limit]:
        lines.append("- %s–%s  %s" % (hms(r["start"]), hms(r["end"]), describe_run(r)))
    if len(rs) > limit:
        lines.append("- … %d more short switches" % (len(rs) - limit))
    return lines
