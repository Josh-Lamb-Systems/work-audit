"""Smoke-test the menu bar app logic with stand-ins for rumps/AppKit (which only exist on macOS)."""
import sys
import threading
import time
import types
from collections import deque
from pathlib import Path

import pytest


# ---------------------------------------------------------------- stand-ins
class _Resp:
    def __init__(self, clicked, text):
        self.clicked, self.text = clicked, text


class _Cell:
    def setWraps_(self, v):
        pass

    def setScrollable_(self, v):
        pass


class _Field:
    def cell(self):
        return _Cell()


def make_rumps():
    m = types.ModuleType("rumps")
    m.window_script = deque()
    m.alert_script = deque()
    m.shown = []
    m.alerts = []
    m.quit_called = []

    class App:
        def __init__(self, name, title=None, icon=None, template=None, menu=None, quit_button="Quit"):
            self.name, self.title, self._menu = name, title, []

        @property
        def menu(self):
            return self._menu

        @menu.setter
        def menu(self, items):
            self._menu = list(items)

        def run(self, **kw):
            pass

    class MenuItem:
        def __init__(self, title, callback=None, key=None, icon=None, dimensions=None, template=None):
            self.title, self.callback, self.state = title, callback, 0

    class Timer:
        def __init__(self, cb, interval):
            self.cb, self.interval = cb, interval

        def start(self):
            pass

    class Window:
        def __init__(self, message="", title="", default_text="", ok=None, cancel=None, dimensions=(320, 160)):
            self.message, self.title, self.buttons = message, title, [ok, cancel]
            self._textfield = _Field()

        def add_button(self, name):
            self.buttons.append(name)

        def run(self):
            m.shown.append((self.title, self.message, list(self.buttons)))
            clicked, text = m.window_script.popleft() if m.window_script else (0, "")
            return _Resp(clicked, text)

    def alert(title=None, message="", ok=None, cancel=None, other=None, icon_path=None):
        m.alerts.append((title, message))
        return m.alert_script.popleft() if m.alert_script else 0

    m.App, m.MenuItem, m.Timer, m.Window, m.alert = App, MenuItem, Timer, Window, alert
    m.quit_application = lambda sender=None: m.quit_called.append(True)
    return m


def make_appkit():
    a = types.ModuleType("AppKit")

    class _NSApp:
        def activateIgnoringOtherApps_(self, v):
            pass

        def hide_(self, v):
            pass

        def setActivationPolicy_(self, v):
            pass

    _app = _NSApp()

    class NSApplication:
        @staticmethod
        def sharedApplication():
            return _app

    class _Running:
        def processIdentifier(self):
            return 4242

        def activateWithOptions_(self, o):
            return True

    class _WS:
        def frontmostApplication(self):
            return _Running()

    class NSWorkspace:
        @staticmethod
        def sharedWorkspace():
            return _WS()

    class NSBundle:
        @staticmethod
        def mainBundle():
            class B:
                def infoDictionary(self):
                    return {}
            return B()

    a.NSApplication, a.NSWorkspace, a.NSBundle = NSApplication, NSWorkspace, NSBundle
    a.NSApplicationActivationPolicyAccessory = 1
    a.NSRunningApplication = object
    return a


@pytest.fixture
def appmod(env, monkeypatch, tmp_path):
    rumps = make_rumps()
    monkeypatch.setitem(sys.modules, "rumps", rumps)
    monkeypatch.setitem(sys.modules, "AppKit", make_appkit())
    sys.modules.pop("workaudit.app", None)
    import workaudit.app as app
    from tests.test_flow import FakeProbe
    monkeypatch.setattr(app, "MacProbe", FakeProbe)
    monkeypatch.setattr(app, "LAUNCH_AGENT", tmp_path / "LaunchAgents" / "com.workaudit.app.plist")
    started = []

    class NoThread:
        def __init__(self, target=None, args=(), name=None, daemon=None):
            self.target, self.args, self.name = target, args, name

        def start(self):
            started.append(self.name)
            if self.name == "report":     # run background jobs inline so the test is deterministic
                self.target(*self.args)
    monkeypatch.setattr(app.threading, "Thread", NoThread)
    opened = []
    # replace only app.py's view of subprocess (the CLI runners in llm.py must keep the real one)
    monkeypatch.setattr(app, "subprocess", types.SimpleNamespace(run=lambda cmd, **kw: opened.append(cmd)))
    app._started_threads, app._opened, app._rumps = started, opened, rumps
    yield app
    sys.modules.pop("workaudit.app", None)


def test_app_flow(appmod, cfg, db, env):
    app = appmod
    a = app.AuditApp(cfg, db)
    titles = [getattr(i, "title", None) for i in a.menu if i is not None]
    assert "Quit Work Audit" in titles and "Generate full audit report" in titles

    # first tick: startup (permission OK in the fake) starts capture + describer and queues the profile question
    db.meta_set("about_me", "")
    a.cfg.data["audit"]["about_me"] = ""
    a._fast_tick(None)
    assert app._started_threads[:2] == ["capture", "describer"]
    assert a.checkins.pending and a.checkins.pending["kind"] == "profile"

    # second tick shows it right away (profile questions don't wait for a typing pause)
    app._rumps.window_script.append((1, "We do outbound for SaaS companies."))
    a._fast_tick(None)
    assert app._rumps.shown and "what is your business" in app._rumps.shown[-1][1]
    assert db.meta_get("about_me") == "We do outbound for SaaS companies."
    assert a.checkins.pending is None

    # a task-switch question waits while you're on a call, then shows once you're recording again
    # (the 20-minute gap after the setup question is covered in test_flow; disable it here)
    a.cfg.data["checkins"]["min_gap_minutes"] = 0
    a.shared.set("call")
    assert a.checkins.offer("Why this lead list now?", "task_switch", "Cleaning leads")
    a._fast_tick(None)
    assert len(app._rumps.shown) == 1 and a.title.endswith("?")
    a.shared.set("recording")
    app._rumps.window_script.append((2, ""))          # "Ask me later"
    a._fast_tick(None)
    assert len(app._rumps.shown) == 2 and "(1 of 15 today)" in app._rumps.shown[-1][0]
    assert a.checkins.pending is not None              # snoozed, not dropped

    # notes, pause/resume, menu text
    app._rumps.window_script.append((1, "Lead cleaning is the most tedious part of my week."))
    a.add_note(None)
    assert db.val("SELECT COUNT(*) FROM checkins WHERE kind='note'") == 1
    a.pause(15 * 60)
    a._slow_tick(None)
    assert a.m_status.title.startswith("Paused") and a.m_pause_toggle.title == "Resume recording"
    a.toggle_pause(None)
    assert not a.shared.paused(time.time())
    a._slow_tick(None)
    assert a.m_checkins.title.startswith("Check-ins:") and a.m_period.title.startswith("Audit day 1 of 7")

    # reports run in the background and report back through the UI queue
    from tests.test_flow import at, simulate
    simulate(cfg, db, until=at(9, 20))
    a.make_daily(None)
    assert a.busy_label is None
    app._rumps.alert_script.append(1)                  # "Open"
    a._fast_tick(None)
    assert any("Daily report ready" == t for t, _ in app._rumps.alerts) or any(
        "Couldn't write" in (t or "") for t, _ in app._rumps.alerts)

    # self-test through the router with fake CLIs
    a.run_selftest(None)
    a._fast_tick(None)
    st = [m for t, m in app._rumps.alerts if t == "Work Audit self-test"]
    assert st and "✓ claude/haiku OK" in st[-1]

    # start at login toggles a LaunchAgent plist
    item = a.m_login
    a.toggle_login(item)
    assert app.LAUNCH_AGENT.exists() and item.state == 1
    a.toggle_login(item)
    assert not app.LAUNCH_AGENT.exists() and item.state == 0

    # end of the audit period: recording stops and it offers the final report once
    a.audit_end = time.time() - 1
    app._rumps.alert_script.append(-1)                 # "Record one more day"
    a._slow_tick(None)
    a._fast_tick(None)
    assert any(t == "Your work audit is complete" for t, _ in app._rumps.alerts)
    assert a.audit_end > time.time()

    a.quit(None)
    assert app._rumps.quit_called


def test_permission_missing_quits(appmod, cfg, db):
    app = appmod
    a = app.AuditApp(cfg, db)
    a.probe.screen_permission = lambda: False
    a.probe.request_screen_permission = lambda: None
    app._rumps.alert_script.append(1)
    a._fast_tick(None)
    assert app._rumps.quit_called
    assert any("Privacy_ScreenCapture" in " ".join(c) for c in app._opened)
    assert "capture" not in app._started_threads
