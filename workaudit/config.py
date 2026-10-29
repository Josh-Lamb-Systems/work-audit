from __future__ import annotations

import copy
import os
import re
from pathlib import Path
from typing import Any, Dict, List

try:  # Python 3.11+
    import tomllib  # type: ignore
except ImportError:  # pragma: no cover - Python 3.9/3.10
    import tomli as tomllib  # type: ignore

from .util import ROOT, log

DEFAULTS: Dict[str, Dict[str, Any]] = {
    "audit": {
        "days": 7,              # stop recording after this many calendar days (0 = never)
        "about_me": "",         # filled in from your first check-in answer if empty
    },
    "capture": {
        "interval_seconds": 5,
        "scope": "window",      # "window" (front window only) or "display" (whole screen it's on)
        "max_side": 1440,       # longest side in pixels after compression
        "jpeg_quality": 60,
        "idle_after_seconds": 300,
        "change_threshold": 1.5,  # 0-255 mean pixel change below which a frame counts as unchanged
        "refresh_seconds": 60,    # store a fresh frame at least this often while you're active
        "keep_all_frames": False, # False = keep only the screenshots the AI looked at
    },
    "describe": {
        "backend": "claude",      # "claude" or "codex"
        "fallback": "codex",      # used when the main one fails ("" for none)
        "window_minutes": 5,      # one AI call per window, only if something changed
        "max_images": 5,
        "carry_minutes": 15,      # reuse the last description this long while nothing really changes
        "claude_model": "haiku",
        "claude_effort": "low",
        "codex_model": "",        # empty = your Codex default
        "codex_effort": "low",
        "timeout_seconds": 300,
        "cooldown_minutes": 30,   # pause a backend this long after a usage-limit error
    },
    "checkins": {
        "enabled": True,
        "max_per_day": 15,
        "min_gap_minutes": 20,
        "dry_spell_minutes": 75,
        "expire_minutes": 30,
        "typing_pause_seconds": 4,
    },
    "reports": {
        "daily_at": "18:00",
        "backend": "claude",
        "fallback": "codex",
        "daily_model": "opus",
        "daily_effort": "high",
        "audit_model": "best",    # 'best' = Fable where your plan has it, otherwise Opus
        "audit_effort": "max",
        "codex_model": "",
        "codex_effort": "high",
        "timeout_seconds": 3600,
    },
    "privacy": {
        "skip_password_managers_and_banking": True,
        "skip_personal_messaging": True,
        "skip_call_screens": True,
        "skip_password_fields": True,
        "extra_apps": [],
        "extra_domains": [],
        "extra_title_keywords": [],
        "allow_apps": [],
        "allow_domains": [],
    },
    "paths": {
        "claude": "",
        "codex": "",
        "extra_path": "",
    },
}


class NS:
    """Read-only attribute view over a config section."""

    def __init__(self, d: Dict[str, Any]):
        self.__dict__["_d"] = d

    def __getattr__(self, k: str) -> Any:
        try:
            return self._d[k]
        except KeyError:
            raise AttributeError(k)

    def __setattr__(self, k: str, v: Any) -> None:  # pragma: no cover
        raise AttributeError("config is read-only")

    def get(self, k: str, default: Any = None) -> Any:
        return self._d.get(k, default)


def _coerce(default: Any, value: Any, where: str, errors: List[str]) -> Any:
    if isinstance(default, bool):
        if isinstance(value, bool):
            return value
    elif isinstance(default, int) and not isinstance(default, bool):
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return int(value) if isinstance(default, int) and float(value).is_integer() else value
    elif isinstance(default, float):
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return float(value)
    elif isinstance(default, str):
        if isinstance(value, str):
            return value
    elif isinstance(default, list):
        if isinstance(value, list):
            return [str(x) for x in value]
    errors.append("%s: expected %s, got %r (using default)" % (where, type(default).__name__, value))
    return default


class Config:
    def __init__(self, path: Path = None):
        self.path = Path(path or os.environ.get("WORKAUDIT_CONFIG") or (ROOT / "config.toml"))
        self.data: Dict[str, Dict[str, Any]] = copy.deepcopy(DEFAULTS)
        self.errors: List[str] = []
        self.mtime = None
        self.load()

    def load(self) -> None:
        data = copy.deepcopy(DEFAULTS)
        errors: List[str] = []
        mtime = None
        if self.path.exists():
            mtime = self.path.stat().st_mtime
            try:
                with open(self.path, "rb") as f:
                    raw = tomllib.load(f)
            except Exception as e:
                errors.append("config.toml could not be read (%s); using defaults" % e)
                raw = {}
            for sec, vals in raw.items():
                if sec not in data or not isinstance(vals, dict):
                    errors.append("unknown section [%s] ignored" % sec)
                    continue
                for k, v in vals.items():
                    if k not in data[sec]:
                        errors.append("unknown setting %s.%s ignored" % (sec, k))
                        continue
                    data[sec][k] = _coerce(DEFAULTS[sec][k], v, "%s.%s" % (sec, k), errors)
        self._sanity(data, errors)
        self.data, self.errors, self.mtime = data, errors, mtime
        for e in errors:
            log.warning("config: %s", e)

    @staticmethod
    def _sanity(d: Dict[str, Dict[str, Any]], errors: List[str]) -> None:
        c = d["capture"]
        c["interval_seconds"] = max(2, int(c["interval_seconds"]))
        if c["scope"] not in ("window", "display"):
            errors.append("capture.scope must be 'window' or 'display'")
            c["scope"] = "window"
        c["max_side"] = min(4000, max(480, int(c["max_side"])))
        c["jpeg_quality"] = min(95, max(20, int(c["jpeg_quality"])))
        de = d["describe"]
        if 60 % max(1, int(de["window_minutes"])) != 0:
            errors.append("describe.window_minutes must divide 60 (1,2,3,4,5,6,10,12,15,20,30,60); using 5")
            de["window_minutes"] = 5
        de["max_images"] = min(12, max(1, int(de["max_images"])))
        for sec in ("describe", "reports"):
            if d[sec]["backend"] not in ("claude", "codex"):
                errors.append("%s.backend must be 'claude' or 'codex'" % sec)
                d[sec]["backend"] = "claude"
            if d[sec]["fallback"] not in ("", "claude", "codex"):
                d[sec]["fallback"] = ""

    def changed_on_disk(self) -> bool:
        try:
            return self.path.exists() and self.path.stat().st_mtime != self.mtime
        except OSError:
            return False

    def __getattr__(self, name: str) -> NS:
        data = self.__dict__.get("data")
        if data is not None and name in data:
            return NS(data[name])
        raise AttributeError(name)


# ------------------------------------------------------------ installer helper

def set_values(path: Path, updates: Dict[str, Dict[str, Any]]) -> None:
    """Update simple `key = value` lines inside sections of a TOML file in place,
    keeping comments and layout. Missing keys/sections are appended."""
    text = path.read_text(encoding="utf-8") if path.exists() else ""
    lines = text.splitlines()

    def fmt(v: Any) -> str:
        if isinstance(v, bool):
            return "true" if v else "false"
        if isinstance(v, (int, float)):
            return str(v)
        s = str(v).replace("\\", "\\\\").replace('"', '\\"')
        return '"%s"' % s

    for sec, kv in updates.items():
        # locate section
        start = None
        for i, ln in enumerate(lines):
            if re.match(r"^\s*\[%s\]\s*(#.*)?$" % re.escape(sec), ln):
                start = i
                break
        if start is None:
            lines += ["", "[%s]" % sec]
            start = len(lines) - 1
        end = len(lines)
        for j in range(start + 1, len(lines)):
            if re.match(r"^\s*\[[^\]]+\]", lines[j]):
                end = j
                break
        for k, v in kv.items():
            pat = re.compile(r"^(\s*)%s\s*=\s*[^#]*?(\s*#.*)?$" % re.escape(k))
            done = False
            for j in range(start + 1, end):
                m = pat.match(lines[j])
                if m:
                    lines[j] = "%s%s = %s%s" % (m.group(1), k, fmt(v), m.group(2) or "")
                    done = True
                    break
            if not done:
                lines.insert(end, "%s = %s" % (k, fmt(v)))
                end += 1
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
