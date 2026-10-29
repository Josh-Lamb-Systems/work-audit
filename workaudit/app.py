"""The menu bar app: wires capture, AI descriptions, check-ins and reports together."""
from __future__ import annotations

import fcntl
import os
import plistlib
import queue
import subprocess
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Dict, Optional

import rumps
from AppKit import NSApplication, NSApplicationActivationPolicyAccessory, NSBundle, NSWorkspace

from . import __version__, privacy, reports, selftest
from .capture import CaptureWorker, MacProbe, Shared
from .checkins import CheckinManager
from .config import Config
from .db import DB
from .describe import Describer
from .llm import Router
from .util import ROOT, clip, data_dir, fmt_dur, hm, log, parse_clock, setup_logging, today

ICONS = {"starting": "◉", "recording": "◉", "paused": "⏸", "idle": "◎", "locked": "◎", "private": "◌",
         "call": "◌", "permission": "⚠︎", "done": "✓"}
LABELS = {"starting": "Starting", "recording": "Recording", "paused": "Paused", "idle": "Idle (you're away)",
          "locked": "Screen locked", "private": "Private window — not capturing", "call": "On a call — not capturing",
          "permission": "Needs Screen Recording permission", "done": "Audit period finished — not recording"}
AGENT_LABEL = "com.workaudit.app"
LAUNCH_AGENT = Path.home() / "Library" / "LaunchAgents" / (AGENT_LABEL + ".plist")
SCREEN_SETTINGS = "x-apple.systempreferences:com.apple.preference.security?Privacy_ScreenCapture"


def _activate() -> Any:
    """Bring our app forward for a dialog; returns the app that had focus."""
    prev = None
    try:
        prev = NSWorkspace.sharedWorkspace().frontmostApplication()
    except Exception:
        pass
    try:
        NSApplication.sharedApplication().activateIgnoringOtherApps_(True)
    except Exception:
        pass
    return prev


def _restore(prev: Any) -> None:
    try:
        if prev is not None and int(prev.processIdentifier()) != os.getpid():
            prev.activateWithOptions_(2)  # NSApplicationActivateIgnoringOtherApps
    except Exception:
        pass
    try:
        NSApplication.sharedApplication().hide_(None)
    except Exception:
        pass


def alert(title: str, message: str, ok: str = "OK", cancel: Optional[str] = None, other: Optional[str] = None) -> int:
    prev = _activate()
    try:
        return rumps.alert(title=title, message=message, ok=ok, cancel=cancel, other=other)
    finally:
        _restore(prev)


class AuditApp(rumps.App):
    def __init__(self, cfg: Config, db: DB):
        super().__init__("Work Audit", title=ICONS["starting"], quit_button=None)
        self.cfg, self.db = cfg, db
        self.shared = Shared()
        self.probe = MacProbe()
        self.router = Router(cfg)
        self.checkins = CheckinManager(db, cfg)
        self.describer = Describer(db, cfg, self.router, self.checkins)
        self.capture = CaptureWorker(db, cfg, self.shared, self.probe, audit_over=self.audit_over)
        self.stop_event = threading.Event()
        self.uiq: "queue.Queue[Callable[[], None]]" = queue.Queue()
        self.dialog_open = False
        self.busy_label: Optional[str] = None
        self.started = False
        self.audit_end = self._compute_audit_end()
        self._build_menu()
        self.t_fast = rumps.Timer(self._fast_tick, 2)
        self.t_fast.start()
        self.t_slow = rumps.Timer(self._slow_tick, 30)
        self.t_slow.start()

    # ------------------------------------------------------------ menu
    def _build_menu(self) -> None:
        self.m_status = rumps.MenuItem("Starting…")
        self.m_ai = rumps.MenuItem("AI descriptions: –")
        self.m_checkins = rumps.MenuItem("Check-ins: –")
        self.m_period = rumps.MenuItem("")
        self.m_pause_toggle = rumps.MenuItem("Pause until I resume", callback=self.toggle_pause)
        self.m_login = rumps.MenuItem("Start at login", callback=self.toggle_login)
        self.menu = [
            self.m_status, self.m_ai, self.m_checkins, self.m_period, None,
            rumps.MenuItem("Pause for 15 minutes", callback=lambda _: self.pause(15 * 60)),
            rumps.MenuItem("Pause for 1 hour", callback=lambda _: self.pause(60 * 60)),
            self.m_pause_toggle, None,
            rumps.MenuItem("Ask me a question now", callback=self.ask_now),
            rumps.MenuItem("Add a note…", callback=self.add_note), None,
            rumps.MenuItem("Generate today's report", callback=self.make_daily),
            rumps.MenuItem("Generate full audit report", callback=self.make_audit),
            rumps.MenuItem("Open reports folder", callback=lambda _: self._open(data_dir() / "reports")), None,
            rumps.MenuItem("Run self-test", callback=self.run_selftest),
            rumps.MenuItem("Settings…", callback=lambda _: self._open(self.cfg.path, text=True)),
            rumps.MenuItem("Open data folder", callback=lambda _: self._open(data_dir())),
            self.m_login, None,
            rumps.MenuItem("Quit Work Audit", callback=self.quit),
        ]
        self.m_login.state = 1 if LAUNCH_AGENT.exists() else 0

    # ---------------------------------------------------------- startup
    def _startup(self) -> None:
        self.started = True
        if not self.probe.screen_permission():
            self.probe.request_screen_permission()
            via_terminal = not os.environ.get("WORKAUDIT_APP")
            who = "Terminal (or the app you started it from)" if via_terminal else "“Work Audit”"
            r = alert("Work Audit needs Screen Recording permission",
                      "1. Click Open Settings.\n2. Under Screen & System Audio Recording, turn on %s. If it isn't "
                      "listed, click + and choose “Work Audit” in your work-audit folder.\n3. Open Work Audit again.\n\n"
                      "Work Audit will quit now, because macOS applies this permission on the next launch." % who,
                      ok="Open Settings", cancel="Quit")
            if r == 1:
                subprocess.run(["/usr/bin/open", SCREEN_SETTINGS])
            self.quit(None)
            return
        if not any(b.available() for b in self.router.backends.values()):
            alert("Claude Code / Codex not found",
                  "Work Audit will record, but it can't describe your screen until it finds the `claude` or `codex` "
                  "command. Re-run install.sh, or set the full paths under [paths] in Settings.")
        threading.Thread(target=self.capture.run_forever, args=(self.stop_event,), name="capture", daemon=True).start()
        threading.Thread(target=self.describer.run_forever, args=(self.stop_event,), name="describer",
                         daemon=True).start()
        self.checkins.ensure_profile(self.cfg.audit.about_me)
        self._slow_tick(None)
        missing = reports.missing_daily_reports(self.db, self.cfg)[-3:]
        if missing:
            self._background("Writing missed daily reports", self._catch_up_reports, missing)

    # ------------------------------------------------------------ timers
    def _fast_tick(self, _sender: Any) -> None:
        now = time.time()
        if not self.started:
            self._startup()
            return
        while not self.dialog_open:
            try:
                fn = self.uiq.get_nowait()
            except queue.Empty:
                break
            try:
                fn()
            except Exception:
                log.exception("UI task failed")
        self._update_title(now)
        if not self.dialog_open:
            try:
                self._maybe_show_checkin(now)
            except Exception:
                log.exception("check-in display failed")

    def _slow_tick(self, _sender: Any) -> None:
        now = time.time()
        try:
            if self.cfg.changed_on_disk():
                self.cfg.load()
                self.router.reload(self.cfg)
                self.audit_end = self._compute_audit_end()
                log.info("settings reloaded")
            self._update_menu(now)
            self._maybe_daily_report(now)
            self._maybe_audit_finished(now)
            self.m_login.state = 1 if LAUNCH_AGENT.exists() else 0
        except Exception:
            log.exception("slow tick failed")

    def _update_title(self, now: float) -> None:
        st = "paused" if self.shared.paused(now) else self.shared.status
        icon = ICONS.get(st, "◉")
        if self.checkins.pending is not None:
            icon += "?"
        if self.busy_label:
            icon += "…"
        if self.title != icon:
            self.title = icon

    def _update_menu(self, now: float) -> None:
        interval = float(self.cfg.capture.interval_seconds)
        st = self.db.day_stats(today(), interval)
        status = "paused" if self.shared.paused(now) else self.shared.status
        line = "%s · %s recorded today" % (LABELS.get(status, status), fmt_dur(st["active"]))
        if status == "paused" and self.shared.paused_until != float("inf"):
            line += " (until %s)" % hm(self.shared.paused_until)
        self.m_status.title = line
        ai = "AI descriptions: %d today" % st["ai_calls"]
        if st["ai_failed"]:
            ai += " · %d failed" % st["ai_failed"]
        if self.describer.paused_until > now:
            ai += " · paused (usage limit) until %s" % hm(self.describer.paused_until)
        else:
            behind = self.describer.behind_seconds(now)
            if behind > 600:
                ai += " · catching up (%s behind)" % fmt_dur(behind)
        if self.busy_label:
            ai = self.busy_label + "…"
        self.m_ai.title = ai
        self.m_checkins.title = "Check-ins: %d of %d today · %d answered" % (
            st["asked"], self.cfg.checkins.max_per_day, st["answered"])
        start = self.db.meta_get("audit_started_day") or today()
        if self.audit_end:
            day_n = (datetime.strptime(today(), "%Y-%m-%d") - datetime.strptime(start, "%Y-%m-%d")).days + 1
            total = int(round((self.audit_end - datetime.strptime(start, "%Y-%m-%d").timestamp()) / 86400))
            if now >= self.audit_end:
                self.m_period.title = "Audit period finished"
            else:
                self.m_period.title = "Audit day %d of %d" % (max(1, day_n), max(1, total))
        else:
            self.m_period.title = "Audit started %s (no end date)" % start
        self.m_pause_toggle.title = "Resume recording" if self.shared.paused(now) else "Pause until I resume"

    # ------------------------------------------------------- audit period
    def _compute_audit_end(self) -> float:
        days = int(self.cfg.audit.days)
        if days <= 0:
            return 0.0
        start = self.db.meta_get("audit_started_day") or today()
        extra = int(self.db.meta_get("audit_extra_days") or 0)
        return (datetime.strptime(start, "%Y-%m-%d") + timedelta(days=days + extra)).timestamp()

    def audit_over(self, now: float) -> bool:
        return bool(self.audit_end) and now >= self.audit_end

    def _maybe_audit_finished(self, now: float) -> None:
        if not self.audit_over(now):
            return
        if self.db.meta_get("audit_end_prompted") == str(int(self.audit_end)):
            return
        self.db.meta_set("audit_end_prompted", str(int(self.audit_end)))

        def ask() -> None:
            r = alert("Your work audit is complete",
                      "Recording has stopped. Generate the full audit report now? It uses Claude's most capable model "
                      "at maximum effort, which can take 10–30 minutes and a good share of your plan's usage.",
                      ok="Generate report", cancel="Later", other="Record one more day")
            if r == 1:
                self.make_audit(None)
            elif r == -1:
                self.db.meta_set("audit_extra_days", int(self.db.meta_get("audit_extra_days") or 0) + 1)
                self.audit_end = self._compute_audit_end()
        self.uiq.put(ask)

    # ------------------------------------------------------------ check-ins
    def _maybe_show_checkin(self, now: float) -> None:
        p = self.checkins.next_to_show(now)
        if not p:
            return
        if p["kind"] not in ("manual", "profile"):
            if self.shared.paused(now) or self.shared.status != "recording":
                return
            if self.shared.call_on_screen or self.shared.presenting:
                return  # you might be sharing your screen
            if self.probe.seconds_since_key() < self.cfg.checkins.typing_pause_seconds:
                return
            if self.probe.idle_seconds() > 60:
                return
        self._show_checkin(p, now)

    def _show_checkin(self, p: Dict[str, Any], now: float) -> None:
        self.dialog_open = True
        prev = _activate()
        try:
            self.checkins.mark_asked(p, now)
            if p["kind"] in ("manual", "profile"):
                title = "Work Audit"
            else:
                title = "Work Audit — quick question (%d of %d today)" % (
                    self.db.asked_today(today()), self.cfg.checkins.max_per_day)
            msg = p["question"]
            if p.get("task"):
                msg += "\n\nAbout: %s" % clip(p["task"], 120)
            msg += "\n\nType or dictate your answer (press the dictation key or Fn twice). Option-Return adds a new line."
            w = rumps.Window(message=msg, title=title, default_text="", ok="Save", cancel="Skip", dimensions=(460, 120))
            w.add_button("Ask me later")
            try:
                w._textfield.cell().setWraps_(True)
                w._textfield.cell().setScrollable_(False)
            except Exception:
                pass
            r = w.run()
            text = str(r.text or "").strip()
            if r.clicked == 1 and text:
                self.checkins.resolve(p, "answered", text)
            elif r.clicked == 2:
                self.checkins.snooze(p, 10 * 60)
            else:
                self.checkins.resolve(p, "skipped")
        finally:
            self.dialog_open = False
            _restore(prev)

    def ask_now(self, _sender: Any) -> None:
        if self.checkins.pending is None:
            self.checkins.ask_now(task=self._current_task())
        p = self.checkins.next_to_show(time.time())
        if p:
            self._show_checkin(p, time.time())

    def add_note(self, _sender: Any) -> None:
        self.dialog_open = True
        prev = _activate()
        try:
            w = rumps.Window(message="Anything the audit should know: what you're doing and why, what's tedious, "
                                     "what you'd love to hand off. Dictation works here too.",
                             title="Add a note", default_text="", ok="Save", cancel="Cancel", dimensions=(460, 140))
            try:
                w._textfield.cell().setWraps_(True)
            except Exception:
                pass
            r = w.run()
            if r.clicked == 1 and str(r.text or "").strip():
                self.checkins.add_note(str(r.text), task=self._current_task())
        finally:
            self.dialog_open = False
            _restore(prev)

    def _current_task(self) -> Optional[str]:
        row = self.db.one("SELECT task FROM segments WHERE source IN ('model','carried') ORDER BY start_ts DESC, id DESC "
                          "LIMIT 1")
        return row["task"] if row else None

    # ------------------------------------------------------------- pause
    def pause(self, seconds: Optional[float]) -> None:
        now = time.time()
        self.shared.paused_until = (now + seconds) if seconds else float("inf")
        self.db.event("pause", {"until": (now + seconds) if seconds else None})
        self._update_title(now)
        self._update_menu(now)

    def toggle_pause(self, _sender: Any) -> None:
        if self.shared.paused(time.time()):
            self.shared.paused_until = 0.0   # the capture loop logs the resume
            self._update_menu(time.time())
        else:
            self.pause(None)

    # ----------------------------------------------------------- reports
    def _background(self, label: str, fn: Callable, *args: Any) -> bool:
        if self.busy_label:
            self.uiq.put(lambda: alert("Busy", "Still working on: %s. Try again when it finishes." % self.busy_label))
            return False

        def runner() -> None:
            try:
                fn(*args)
            except Exception:
                log.exception("%s failed", label)
            finally:
                self.busy_label = None
        self.busy_label = label
        threading.Thread(target=runner, name="report", daemon=True).start()
        return True

    def _report_done(self, path: Path, title: str) -> None:
        if alert(title, str(path), ok="Open", cancel="Close") == 1:
            self._open(path, text=True)

    def _daily_job(self, day: str) -> None:
        try:
            path, _ = reports.generate_daily(self.db, self.cfg, self.router, day)
            self.uiq.put(lambda path=path: self._report_done(path, "Daily report ready"))
        except Exception as e:
            msg = clip(str(e), 700)
            self.uiq.put(lambda msg=msg: alert("Couldn't write the daily report", msg))

    def _audit_job(self) -> None:
        try:
            path, _ = reports.generate_audit(self.db, self.cfg, self.router)
            self.uiq.put(lambda path=path: self._report_done(path, "Your full audit report is ready"))
        except Exception as e:
            msg = clip(str(e), 700)
            self.uiq.put(lambda msg=msg: alert("Couldn't write the audit report", msg))

    def _catch_up_reports(self, days) -> None:
        for d in days:
            try:
                reports.generate_daily(self.db, self.cfg, self.router, d)
            except Exception:
                log.exception("catch-up report for %s failed", d)

    def make_daily(self, _sender: Any) -> None:
        self._background("Writing today's report", self._daily_job, today())

    def make_audit(self, _sender: Any) -> None:
        missing = reports.missing_daily_reports(self.db, self.cfg)

        def job() -> None:
            for d in missing:   # daily reports make the final synthesis better
                try:
                    reports.generate_daily(self.db, self.cfg, self.router, d)
                except Exception:
                    log.exception("daily report for %s failed", d)
            self._audit_job()
        self._background("Writing the full audit report", job)

    def _maybe_daily_report(self, now: float) -> None:
        hmv = parse_clock(self.cfg.reports.daily_at)
        if not hmv or self.busy_label:
            return
        target = datetime.now().replace(hour=hmv[0], minute=hmv[1], second=0, microsecond=0).timestamp()
        d = today()
        if now < target or self.db.report_for("daily", d) is not None or self.db.meta_get("daily_auto_" + d):
            return
        if self.db.day_stats(d, float(self.cfg.capture.interval_seconds))["active"] < 1800:
            return
        self.db.meta_set("daily_auto_" + d, int(now))
        self._background("Writing today's report", self._daily_job, d)

    # ------------------------------------------------------------ misc
    def run_selftest(self, _sender: Any) -> None:
        def job() -> None:
            lines = []
            perm = self.probe.screen_permission()
            lines.append(("✓" if perm else "✗") + " Screen Recording permission")
            image = None
            try:
                front, _ = self.probe.snapshot()
                if perm and front and front.get("pid") != os.getpid() and front.get("window_id"):
                    v = privacy.classify(front["app"], front.get("bundle_id"), front.get("title"), None, None, False,
                                         self.cfg.privacy)
                    if v.kind == "ok":
                        raw = data_dir() / "tmp" / "selftest-window.jpg"
                        if self.probe.capture(front, "window", raw):
                            image = raw
                            lines.append("✓ Captured the %s window" % front["app"])
                        else:
                            lines.append("✗ Could not screenshot the %s window" % front["app"])
            except Exception as e:
                lines.append("✗ Screenshot test failed: %s" % e)
            ok, more = selftest.run(self.router, image=image)
            lines += more
            msg = "\n".join(lines)
            self.uiq.put(lambda msg=msg: alert("Work Audit self-test", msg))
        self._background("Running self-test", job)

    def toggle_login(self, sender: Any) -> None:
        app_path = os.environ.get("WORKAUDIT_APP") or str(ROOT / "Work Audit.app")
        try:
            if LAUNCH_AGENT.exists():
                LAUNCH_AGENT.unlink()
            else:
                LAUNCH_AGENT.parent.mkdir(parents=True, exist_ok=True)
                with open(str(LAUNCH_AGENT), "wb") as f:
                    plistlib.dump({"Label": AGENT_LABEL, "ProgramArguments": ["/usr/bin/open", "-a", app_path],
                                   "RunAtLoad": True}, f)
        except OSError as e:
            alert("Couldn't change the login setting", str(e))
        sender.state = 1 if LAUNCH_AGENT.exists() else 0

    @staticmethod
    def _open(path: Path, text: bool = False) -> None:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        if not Path(path).exists() and not text:
            Path(path).mkdir(parents=True, exist_ok=True)
        subprocess.run(["/usr/bin/open"] + (["-t"] if text else []) + [str(path)])

    def quit(self, _sender: Any) -> None:
        self.stop_event.set()
        try:
            self.db.event("app_quit")
        except Exception:
            pass
        rumps.quit_application()


def _single_instance_lock():
    lock_path = data_dir() / ".app.lock"
    fh = open(str(lock_path), "w")
    try:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        fh.close()
        return None
    fh.write(str(os.getpid()))
    fh.flush()
    return fh


def run_app() -> int:
    setup_logging()
    log.info("Work Audit %s starting (pid %d)", __version__, os.getpid())
    try:
        info = NSBundle.mainBundle().infoDictionary()
        if info is not None:
            info["LSUIElement"] = "1"   # no Dock icon
    except Exception:
        pass
    nsapp = NSApplication.sharedApplication()
    nsapp.setActivationPolicy_(NSApplicationActivationPolicyAccessory)
    lock = _single_instance_lock()
    if lock is None:
        alert("Work Audit is already running", "Look for ◉ in your menu bar.")
        return 1
    cfg = Config()
    db = DB(data_dir() / "audit.db")
    if not db.meta_get("audit_started_day"):
        db.meta_set("audit_started_day", today())
    db.event("app_start", {"version": __version__})
    app = AuditApp(cfg, db)
    app.run()
    return 0
