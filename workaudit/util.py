from __future__ import annotations

import json
import logging
import os
import time
from datetime import datetime, timedelta
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any, Iterable, List, Optional

ROOT = Path(os.environ.get("WORKAUDIT_HOME") or Path(__file__).resolve().parent.parent)


def data_dir() -> Path:
    d = Path(os.environ.get("WORKAUDIT_DATA") or (ROOT / "data"))
    d.mkdir(parents=True, exist_ok=True)
    return d


log = logging.getLogger("workaudit")


def setup_logging(to_stderr: bool = False, level: int = logging.INFO) -> None:
    logs = data_dir() / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    if log.handlers:
        return
    log.setLevel(level)
    fmt = logging.Formatter("%(asctime)s %(levelname)s [%(threadName)s] %(message)s")
    fh = RotatingFileHandler(str(logs / "workaudit.log"), maxBytes=5_000_000, backupCount=3, encoding="utf-8")
    fh.setFormatter(fmt)
    log.addHandler(fh)
    if to_stderr:
        sh = logging.StreamHandler()
        sh.setFormatter(fmt)
        log.addHandler(sh)


# ---------------------------------------------------------------- time helpers

def day_of(ts: float) -> str:
    return time.strftime("%Y-%m-%d", time.localtime(ts))


def hm(ts: float) -> str:
    return time.strftime("%H:%M", time.localtime(ts))


def hms(ts: float) -> str:
    return time.strftime("%H:%M:%S", time.localtime(ts))


def day_bounds(day: str):
    """Local-midnight start and end (epoch seconds) of a YYYY-MM-DD day, DST-safe."""
    d = datetime.strptime(day, "%Y-%m-%d")
    return d.timestamp(), (d + timedelta(days=1)).timestamp()


def pretty_day(day: str) -> str:
    d = datetime.strptime(day, "%Y-%m-%d")
    return d.strftime("%a %d %b %Y")


def today() -> str:
    return day_of(time.time())


def fmt_dur(seconds: float) -> str:
    minutes = int(round((seconds or 0) / 60.0))
    h, m = divmod(minutes, 60)
    if h:
        return "%dh %02dm" % (h, m)
    return "%dm" % m


def parse_clock(s: str) -> Optional[tuple]:
    """'18:00' -> (18, 0)."""
    try:
        h, m = s.strip().split(":")[:2]
        h, m = int(h), int(m)
        if 0 <= h < 24 and 0 <= m < 60:
            return h, m
    except Exception:
        pass
    return None


# ---------------------------------------------------------------- misc helpers

def clip(s: Any, n: int) -> str:
    s = "" if s is None else str(s)
    s = " ".join(s.split())
    return s if len(s) <= n else s[: max(0, n - 1)] + "…"


def as_str_list(v: Any, limit: int = 8, item_len: int = 80) -> List[str]:
    if v is None:
        return []
    if isinstance(v, str):
        v = [v]
    if not isinstance(v, (list, tuple)):
        return []
    out: List[str] = []
    for x in v:
        if x is None:
            continue
        s = clip(x, item_len)
        if s and s not in out:
            out.append(s)
        if len(out) >= limit:
            break
    return out


def jdump(v: Any) -> str:
    return json.dumps(v, ensure_ascii=False)


def jload(s: Optional[str], default: Any = None) -> Any:
    if not s:
        return default
    try:
        return json.loads(s)
    except Exception:
        return default


def host_of(url: Optional[str]) -> str:
    if not url:
        return ""
    u = url.strip().lower()
    if "://" in u:
        u = u.split("://", 1)[1]
    u = u.split("/", 1)[0].split("?", 1)[0].split("#", 1)[0]
    u = u.rsplit("@", 1)[-1].split(":", 1)[0]
    if u.startswith("www."):
        u = u[4:]
    return u


def uniq(items: Iterable[str]) -> List[str]:
    seen = set()
    out = []
    for x in items:
        k = x.lower()
        if k not in seen:
            seen.add(k)
            out.append(x)
    return out
