"""Screen capture loop (macOS). MacProbe talks to the OS; CaptureWorker decides what to record."""
from __future__ import annotations

import ctypes
import os
import plistlib
import subprocess
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from . import frames as fr
from . import privacy
from .db import DB
from .util import data_dir, day_of, host_of, log

try:  # macOS only; the module still imports elsewhere so the logic can be tested
    import Quartz  # type: ignore
    from AppKit import NSRunningApplication, NSWorkspace  # type: ignore
    HAVE_MAC = True
except Exception:  # pragma: no cover
    Quartz = None
    NSRunningApplication = NSWorkspace = None
    HAVE_MAC = False

try:  # PyObjC never drains a background thread's autorelease pool on its own: drain it every tick
    from objc import autorelease_pool as _autorelease_pool  # type: ignore
except Exception:  # pragma: no cover
    from contextlib import nullcontext as _autorelease_pool

SCREENCAPTURE = "/usr/sbin/screencapture"
OSASCRIPT = "/usr/bin/osascript"

_CHROMIUM_SCRIPT = '''tell application id "%s"
  if (count of windows) is 0 then return ""
  set w to front window
  set u to URL of active tab of w
  set m to "normal"
  try
    set m to (mode of w) as text
  end try
  return u & linefeed & m
end tell'''
_ARC_SCRIPT = '''tell application id "%s"
  if (count of windows) is 0 then return ""
  set u to URL of active tab of front window
  set m to "normal"
  try
    if incognito of front window then set m to "incognito"
  end try
  return u & linefeed & m
end tell'''
_SAFARI_SCRIPT = '''tell application id "%s"
  if (count of windows) is 0 then return ""
  return (URL of current tab of front window) & linefeed & "normal"
end tell'''

BROWSERS: Dict[str, str] = {}
for _b in ("com.google.Chrome", "com.google.Chrome.beta", "com.google.Chrome.dev", "com.google.Chrome.canary",
           "org.chromium.Chromium", "com.brave.Browser", "com.brave.Browser.beta", "com.brave.Browser.nightly",
           "com.microsoft.edgemac", "com.microsoft.edgemac.Beta", "com.microsoft.edgemac.Dev",
           "com.vivaldi.Vivaldi", "com.operasoftware.Opera", "ai.perplexity.comet"):
    BROWSERS[_b] = _CHROMIUM_SCRIPT
for _b in ("company.thebrowser.Browser",):
    BROWSERS[_b] = _ARC_SCRIPT
for _b in ("com.apple.Safari", "com.apple.SafariTechnologyPreview"):
    BROWSERS[_b] = _SAFARI_SCRIPT


class MacProbe:
    """Thin wrapper over the macOS APIs the capture loop needs."""

    def __init__(self):
        self._bid_cache: Dict[int, Optional[str]] = {}
        self._url_cache: Dict[Tuple, Tuple[float, Optional[str], Optional[str]]] = {}
        self._denied: Dict[str, float] = {}
        self._asked: set = set()   # browsers we've queried once (the first query may show macOS's consent prompt)
        self._secure_cache: Tuple[float, Optional[int]] = (0.0, None)
        try:
            self._carbon = ctypes.cdll.LoadLibrary("/System/Library/Frameworks/Carbon.framework/Carbon")
            self._carbon.IsSecureEventInputEnabled.restype = ctypes.c_bool
        except Exception:
            self._carbon = None

    # ------------------------------------------------------- permissions
    def screen_permission(self) -> bool:
        try:
            return bool(Quartz.CGPreflightScreenCaptureAccess())
        except Exception:
            return True  # very old macOS: no API, assume granted

    def request_screen_permission(self) -> None:
        try:
            Quartz.CGRequestScreenCaptureAccess()
        except Exception:
            pass

    # ------------------------------------------------------------ state
    def idle_seconds(self) -> float:
        try:
            return float(Quartz.CGEventSourceSecondsSinceLastEventType(1, 0xFFFFFFFF))
        except Exception:
            try:
                out = subprocess.run(["/usr/sbin/ioreg", "-c", "IOHIDSystem"], capture_output=True, text=True,
                                     timeout=3).stdout
                for line in out.splitlines():
                    if "HIDIdleTime" in line:
                        return int(line.rsplit("=", 1)[1].strip()) / 1e9
            except Exception:
                pass
        return 0.0

    def seconds_since_key(self) -> float:
        try:
            return float(Quartz.CGEventSourceSecondsSinceLastEventType(1, 10))  # kCGEventKeyDown
        except Exception:
            return 999.0

    def session_locked(self) -> bool:
        try:
            d = Quartz.CGSessionCopyCurrentDictionary()
            if d is None:
                return True
            if d.get("CGSSessionScreenIsLocked", 0):
                return True
            if not d.get(getattr(Quartz, "kCGSessionOnConsoleKey", "kCGSSessionOnConsoleKey"), True):
                return True
        except Exception:
            pass
        try:
            return bool(Quartz.CGDisplayIsAsleep(Quartz.CGMainDisplayID()))
        except Exception:
            return False

    def secure_input_pid(self) -> Optional[int]:
        """PID of the app with a password field focused (macOS 'secure input'), if any."""
        now = time.time()
        if now - self._secure_cache[0] < 3:
            return self._secure_cache[1]
        pid = None
        try:
            if self._carbon is not None and self._carbon.IsSecureEventInputEnabled():
                out = subprocess.run(["/usr/sbin/ioreg", "-a", "-d", "1", "-k", "IOConsoleUsers"],
                                     capture_output=True, timeout=3).stdout
                pid = _find_key(plistlib.loads(out), "kCGSSessionSecureInputPID") if out else None
                if pid is None:
                    pid = -1
        except Exception:
            pid = None
        self._secure_cache = (now, pid)
        return pid

    # ---------------------------------------------------------- windows
    def bundle_id(self, pid: int) -> Optional[str]:
        return self._bundle_id(pid)

    def _bundle_id(self, pid: int) -> Optional[str]:
        if pid not in self._bid_cache:
            try:
                app = NSRunningApplication.runningApplicationWithProcessIdentifier_(pid)
                self._bid_cache[pid] = str(app.bundleIdentifier()) if app and app.bundleIdentifier() else None
            except Exception:
                self._bid_cache[pid] = None
            if len(self._bid_cache) > 500:
                self._bid_cache.clear()
        return self._bid_cache.get(pid)

    def snapshot(self) -> Tuple[Optional[Dict[str, Any]], List[Dict[str, Any]]]:
        """Return (front window info, list of on-screen normal windows)."""
        wins: List[Dict[str, Any]] = []
        try:
            raw = Quartz.CGWindowListCopyWindowInfo(
                Quartz.kCGWindowListOptionOnScreenOnly | Quartz.kCGWindowListExcludeDesktopElements,
                Quartz.kCGNullWindowID) or []
        except Exception:
            raw = []
        for w in raw:
            try:
                if int(w.get(Quartz.kCGWindowLayer, 0)) != 0:
                    continue
                if float(w.get(Quartz.kCGWindowAlpha, 1.0)) <= 0.01:
                    continue
                b = w.get(Quartz.kCGWindowBounds) or {}
                bounds = (float(b.get("X", 0)), float(b.get("Y", 0)), float(b.get("Width", 0)), float(b.get("Height", 0)))
                if bounds[2] < 80 or bounds[3] < 60:
                    continue
                wins.append({"pid": int(w.get(Quartz.kCGWindowOwnerPID, 0)),
                             "app": str(w.get(Quartz.kCGWindowOwnerName, "") or ""),
                             "window_id": int(w.get(Quartz.kCGWindowNumber, 0)),
                             "title": str(w.get(Quartz.kCGWindowName, "") or ""),
                             "bounds": bounds})
            except Exception:
                continue
        front = None
        try:
            app = NSWorkspace.sharedWorkspace().frontmostApplication()
            if app is not None:
                pid = int(app.processIdentifier())
                front = {"pid": pid, "app": str(app.localizedName() or ""),
                         "bundle_id": str(app.bundleIdentifier()) if app.bundleIdentifier() else None,
                         "window_id": None, "title": "", "bounds": None}
                for w in wins:
                    if w["pid"] == pid:
                        front.update(window_id=w["window_id"], title=w["title"], bounds=w["bounds"])
                        break
        except Exception:
            front = None
        if front is None and wins:
            w = wins[0]
            front = dict(w, bundle_id=self._bundle_id(w["pid"]))
        return front, wins

    def other_windows(self, wins: List[Dict[str, Any]], front: Dict[str, Any], p, my_pid: int) -> List[str]:
        out: List[str] = []
        for w in wins:
            if w["window_id"] == front.get("window_id") or w["pid"] == my_pid:
                continue
            if w["bounds"][2] < 200 or w["bounds"][3] < 150:
                continue
            lab = privacy.redact_label(w["app"], self._bundle_id(w["pid"]) or "", w["title"], p)
            if lab and lab not in out:
                out.append(lab)
            if len(out) >= 6:
                break
        return out

    def browser_url(self, front: Dict[str, Any]) -> Tuple[Optional[str], Optional[str]]:
        bid = front.get("bundle_id") or ""
        script = BROWSERS.get(bid)
        if not script:
            return None, None
        now = time.time()
        if self._denied.get(bid, 0) > now:
            return None, None
        key = (bid, front.get("window_id"), front.get("title"))
        c = self._url_cache.get(key)
        if c and now - c[0] < 30:
            return c[1], c[2]
        url, mode = None, None
        # The first query may trigger macOS's "allow Work Audit to control <browser>?" prompt: give the
        # person time to answer it. After that, a stuck browser must not stall the capture loop.
        first = bid not in self._asked
        self._asked.add(bid)
        try:
            p = subprocess.run([OSASCRIPT, "-e", script % bid], capture_output=True, text=True,
                               timeout=90 if first else 3)
            if p.returncode == 0:
                parts = (p.stdout or "").strip().split("\n")
                url = parts[0].strip() or None
                mode = parts[1].strip() if len(parts) > 1 else None
            else:
                err = p.stderr or ""
                if "-1743" in err or "Not authorized" in err or "not allowed" in err:
                    log.warning("Automation permission for %s was denied; URLs and incognito detection are off "
                                "for it (System Settings → Privacy & Security → Automation).", bid)
                    self._denied[bid] = now + 600
        except subprocess.TimeoutExpired:
            self._denied[bid] = now + 60
        except Exception as e:  # pragma: no cover
            log.debug("browser url failed: %s", e)
        if len(self._url_cache) > 300:
            self._url_cache.clear()
        self._url_cache[key] = (now, url, mode)
        return url, mode

    # ---------------------------------------------------------- capture
    def display_rect(self, bounds: Optional[Tuple[float, float, float, float]]) -> Tuple[int, int, int, int]:
        rects = []
        try:
            err, ids, count = Quartz.CGGetActiveDisplayList(16, None, None)
            for d in list(ids or [])[: int(count or 0)]:
                r = Quartz.CGDisplayBounds(d)
                rects.append((r.origin.x, r.origin.y, r.size.width, r.size.height))
        except Exception:
            pass
        if not rects:
            try:
                r = Quartz.CGDisplayBounds(Quartz.CGMainDisplayID())
                rects = [(r.origin.x, r.origin.y, r.size.width, r.size.height)]
            except Exception:
                return (0, 0, 1440, 900)
        if bounds:
            cx, cy = bounds[0] + bounds[2] / 2.0, bounds[1] + bounds[3] / 2.0
            for x, y, w, h in rects:
                if x <= cx < x + w and y <= cy < y + h:
                    return (int(x), int(y), int(w), int(h))
        x, y, w, h = rects[0]
        return (int(x), int(y), int(w), int(h))

    def capture(self, front: Dict[str, Any], scope: str, out: Path) -> bool:
        try:
            if out.exists():
                out.unlink()
        except OSError:
            pass
        if scope == "window" and front.get("window_id"):
            cmd = [SCREENCAPTURE, "-x", "-o", "-l", str(front["window_id"]), "-t", "jpg", str(out)]
        else:
            x, y, w, h = self.display_rect(front.get("bounds"))
            cmd = [SCREENCAPTURE, "-x", "-R", "%d,%d,%d,%d" % (x, y, w, h), "-t", "jpg", str(out)]
        try:
            p = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
        except subprocess.TimeoutExpired:
            return False
        if p.returncode != 0:
            log.debug("screencapture failed: %s", (p.stderr or "").strip())
            return False
        return out.exists() and out.stat().st_size > 0


def _find_key(obj: Any, key: str) -> Optional[int]:
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k == key and isinstance(v, int):
                return v
            r = _find_key(v, key)
            if r is not None:
                return r
    elif isinstance(obj, list):
        for v in obj:
            r = _find_key(v, key)
            if r is not None:
                return r
    return None


class Shared:
    """State shared between the capture thread and the menu bar UI."""

    def __init__(self):
        self.lock = threading.Lock()
        self.paused_until = 0.0     # 0 = recording; float('inf') = until resumed
        self.status = "starting"    # recording | paused | idle | locked | private | call | permission | done
        self.detail = ""
        self.front_kind = "ok"
        self.last_capture_ts = 0.0
        self.capture_errors = 0
        self.call_on_screen: Optional[str] = None   # a call or screen-share window is open somewhere
        self.presenting = False

    def paused(self, now: float) -> bool:
        return self.paused_until > now

    def set(self, status: str, detail: str = "", front_kind: str = None) -> None:
        with self.lock:
            self.status, self.detail = status, detail
            if front_kind is not None:
                self.front_kind = front_kind


class CaptureWorker:
    def __init__(self, db: DB, cfg, shared: Shared, probe=None, audit_over=None):
        self.db, self.cfg, self.shared = db, cfg, shared
        self.probe = probe or MacProbe()
        self.audit_over = audit_over or (lambda now: False)
        self.my_pid = os.getpid()
        self.tmp = data_dir() / "tmp"
        self.tmp.mkdir(parents=True, exist_ok=True)
        self.last_sig: Optional[bytes] = None
        self.last_key: Optional[Tuple] = None
        self.last_saved_ts = 0.0
        self._was_paused = False
        self._perm_checked = 0.0
        self._perm_ok = True

    def reset(self) -> None:
        self.last_sig, self.last_key = None, None

    def run_forever(self, stop: threading.Event) -> None:
        while not stop.is_set():
            t0 = time.time()
            try:
                with _autorelease_pool():
                    self.tick(t0)
            except Exception:
                log.exception("capture tick failed")
            stop.wait(max(0.5, self.cfg.capture.interval_seconds - (time.time() - t0)))

    def tick(self, now: float) -> None:
        sh, cfg, db, probe = self.shared, self.cfg, self.db, self.probe
        if sh.paused(now):
            self._was_paused = True
            sh.set("paused")
            self.reset()
            return
        if self._was_paused:
            self._was_paused = False
            sh.paused_until = 0.0
            db.event("resume", {"auto": True})
        if self.audit_over(now):
            sh.set("done")
            return
        if now - self._perm_checked > 60:
            self._perm_checked = now
            self._perm_ok = probe.screen_permission()
        if not self._perm_ok:
            sh.set("permission")
            return
        if probe.session_locked():
            sh.set("locked")
            self.reset()
            return
        idle = probe.idle_seconds()
        if idle >= cfg.capture.idle_after_seconds:
            sh.set("idle")
            self.reset()
            return
        front, wins = probe.snapshot()
        try:
            sh.call_on_screen = privacy.call_on_screen(wins, probe.bundle_id) if cfg.privacy.skip_call_screens else None
        except Exception:
            sh.call_on_screen = None
        if front is None or front.get("pid") == self.my_pid:
            return
        sh.presenting = privacy.presenting(front.get("app"), front.get("title"))
        url, mode = (None, None)
        if front.get("bundle_id") in BROWSERS:
            url, mode = probe.browser_url(front)
        secure = False
        if cfg.privacy.skip_password_fields:
            spid = probe.secure_input_pid()
            secure = spid is not None and spid == front["pid"]
        v = privacy.classify(front["app"], front.get("bundle_id"), front.get("title"), url, mode, secure, cfg.privacy)
        domain = host_of(url) or None
        if url:  # keep the page address but drop query strings/fragments, which can hold tokens
            url = url.split("#", 1)[0].split("?", 1)[0][:300]
        base = {"app": front["app"] or None, "bundle_id": front.get("bundle_id")}
        if v.kind == "private":
            db.insert_frame(now, "private", reason=v.reason, **base)
            sh.set("private", privacy.REASON_LABELS.get(v.reason or "", ""), "private")
            self.reset()
            return
        if v.kind == "call":
            db.insert_frame(now, "call", reason="call", title=front.get("title") or None, domain=domain, **base)
            sh.set("call", front.get("title") or front["app"], "call")
            self.reset()
            return
        info = dict(base, title=front.get("title") or None, url=url, domain=domain, window_id=front.get("window_id"))
        if cfg.capture.scope == "window" and not front.get("window_id"):
            db.insert_frame(now, "nowindow", **base)
            sh.set("recording", front["app"], "ok")
            return
        raw = self.tmp / "capture.jpg"
        if not probe.capture(front, cfg.capture.scope, raw):
            sh.capture_errors += 1
            db.insert_frame(now, "error", reason="capture_failed", **info)
            self._perm_checked = 0.0  # re-check permission on the next tick
            sh.set("recording", "screenshot failed", "ok")
            return
        try:
            img = fr.shrink(fr.load(raw), cfg.capture.max_side)
        except Exception as e:
            log.warning("could not read screenshot: %s", e)
            db.insert_frame(now, "error", reason="bad_image", **info)
            return
        finally:
            try:
                raw.unlink()
            except OSError:
                pass
        sig = fr.signature(img)
        key = (front.get("window_id"), front.get("title"), domain)
        d = fr.diff(sig, self.last_sig) if key == self.last_key else 255.0
        since_save = now - self.last_saved_ts
        refresh = since_save >= cfg.capture.refresh_seconds and idle < since_save
        if key == self.last_key and d < cfg.capture.change_threshold and not refresh:
            db.insert_frame(now, "unchanged", diff=d, **info)
        else:
            day = day_of(now)
            rel = "frames/%s/%s-%s.jpg" % (day, time.strftime("%H%M%S", time.localtime(now)), front.get("window_id") or 0)
            fr.save_jpeg(img, data_dir() / rel, cfg.capture.jpeg_quality)
            visible = probe.other_windows(wins, front, cfg.privacy, self.my_pid)
            db.insert_frame(now, "captured", image=rel, diff=None if d >= 255 else round(d, 2), visible=visible, **info)
            self.last_sig, self.last_key, self.last_saved_ts = sig, key, now
        sh.last_capture_ts = now
        sh.set("recording", front["app"], "ok")
