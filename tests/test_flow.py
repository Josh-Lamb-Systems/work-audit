"""End-to-end: simulated morning of work -> capture -> AI descriptions (fake CLIs) -> check-ins -> reports."""
import os
import random
from datetime import datetime
from pathlib import Path

from PIL import Image, ImageDraw

from workaudit import reports, selftest
from workaudit.capture import CaptureWorker, Shared
from workaudit.checkins import CheckinManager
from workaudit.describe import Describer
from workaudit.llm import LLMError, Router
from workaudit.util import data_dir

DAY = datetime(2026, 10, 19)
T0 = datetime(2026, 10, 19, 9, 0).timestamp()


def at(h, m, s=0):
    return datetime(2026, 10, 19, h, m, s).timestamp()


# (start, end, scenario)
SCRIPT = [
    (at(9, 0), at(9, 20), dict(app="Google Chrome", bundle="com.google.Chrome", title="Inbox (3) - Gmail",
                               url="https://mail.google.com/mail/u/0/#inbox?token=SECRET", content="gmail", churn=30)),
    (at(9, 20), at(9, 22), dict(app="WhatsApp", bundle="net.whatsapp.WhatsApp", title="Mom", content="wa")),
    (at(9, 22), at(9, 50), dict(app="Microsoft Excel", bundle="com.microsoft.Excel", title="Q4 leads.xlsx",
                                content="excel", churn=0)),
    (at(9, 50), at(10, 20), dict(app="zoom.us", bundle="us.zoom.xos", title="Zoom Meeting", content="zoom")),
    (at(10, 20), at(10, 25), dict(app="Google Chrome", bundle="com.google.Chrome", title="Chase", content="bank",
                                  url="https://secure.chase.com/web/auth/dashboard")),
    (at(10, 25), at(10, 40), dict(idle=True)),
    (at(10, 40), at(11, 0), dict(app="Notion", bundle="notion.id", title="Proposal – Acme", content="notion", churn=20)),
]


class FakeProbe:
    def __init__(self):
        self.now = T0
        self.captures = 0

    def cur(self):
        for a, b, sc in SCRIPT:
            if a <= self.now < b:
                return sc
        return dict(idle=True)

    def screen_permission(self):
        return True

    def session_locked(self):
        return False

    def idle_seconds(self):
        return 999 if self.cur().get("idle") else 2

    def seconds_since_key(self):
        return 10

    def snapshot(self):
        sc = self.cur()
        front = {"pid": 4242, "app": sc["app"], "bundle_id": sc["bundle"], "window_id": hash(sc["app"]) % 1000,
                 "title": sc["title"], "bounds": (0, 0, 1200, 800)}
        wins = [dict(front), {"pid": 1, "app": "Messages", "window_id": 7, "title": "Mom", "bounds": (0, 0, 800, 600)},
                {"pid": 2, "app": "Slack", "window_id": 8, "title": "#sales", "bounds": (0, 0, 800, 600)}]
        return front, wins

    def browser_url(self, front):
        return self.cur().get("url"), "normal"

    def bundle_id(self, pid):
        return {1: "com.apple.MobileSMS", 2: "com.tinyspeck.slackmacgap", 4242: self.cur().get("bundle"),
                99: "us.zoom.xos"}.get(pid)

    def secure_input_pid(self):
        return None

    def other_windows(self, wins, front, p, my_pid):
        from workaudit import privacy
        out = []
        for w in wins[1:]:
            lab = privacy.redact_label(w["app"], "com.apple.MobileSMS" if w["app"] == "Messages" else "", w["title"], p)
            if lab:
                out.append(lab)
        return out

    def capture(self, front, scope, out: Path):
        sc = self.cur()
        churn = sc.get("churn", 0)
        version = int((self.now - T0) // churn) if churn else 0
        rnd = random.Random(hash((sc["content"], version)))
        img = Image.new("RGB", (1000, 700), tuple(rnd.randrange(256) for _ in range(3)))
        d = ImageDraw.Draw(img)
        for _ in range(40):
            x, y = rnd.randrange(900), rnd.randrange(600)
            d.rectangle([x, y, x + 100, y + 60], fill=tuple(rnd.randrange(256) for _ in range(3)))
        img.save(str(out), "JPEG", quality=70)
        self.captures += 1
        return True


def simulate(cfg, db, answer=True, until=at(11, 0)):
    probe = FakeProbe()
    shared = Shared()
    cw = CaptureWorker(db, cfg, shared, probe)
    router = Router(cfg)
    ck = CheckinManager(db, cfg)
    desc = Describer(db, cfg, router, ck)
    shown = []
    t = T0
    while t < until + 400:
        probe.now = t
        if t < until:
            cw.tick(t)
        while desc.process_next(t):
            pass
        p = ck.next_to_show(t)
        if p:
            ck.mark_asked(p, t)
            shown.append((t, p["kind"], p["question"]))
            if answer:
                ck.resolve(p, "answered", "Because the client asked yesterday.", t)
            else:
                ck.resolve(p, "skipped", None, t)
        t += cfg.capture.interval_seconds
    return probe, desc, ck, shown


def test_full_morning(env, cfg, db):
    probe, desc, ck, shown = simulate(cfg, db)
    calls = env.calls()
    describes = [c for c in calls if c["kind"] == "describe"]

    # Frames: private/call/active classified, URL query stripped, nothing captured for private states
    states = dict(db.q("SELECT state, COUNT(*) FROM frames GROUP BY state"))
    assert states.get("private", 0) == (2 * 60 + 5 * 60) / 5    # WhatsApp + Chase
    assert states.get("call", 0) == 30 * 60 / 5
    assert db.val("SELECT COUNT(*) FROM frames WHERE state='private' AND (title IS NOT NULL OR url IS NOT NULL)") == 0
    assert db.val("SELECT COUNT(*) FROM frames WHERE url LIKE '%SECRET%'") == 0
    vis = db.val("SELECT visible FROM frames WHERE visible IS NOT NULL LIMIT 1")
    assert "Slack" in vis and "Mom" not in vis

    # Batches: one per 5-minute window that had frames; idle windows produce none
    statuses = [r["status"] for r in db.q("SELECT status FROM batches ORDER BY start_ts")]
    assert len(statuses) == 21, statuses
    assert "carried" in statuses        # static Excel work reused its description
    assert "skipped" in statuses        # Zoom-only windows
    assert len(describes) == statuses.count("done")
    assert all(c["n_images"] >= 1 and c["image_ok"] for c in describes)
    assert all("--safe-mode" in c["argv"] and "--no-session-persistence" in c["argv"] and "--tools" in c["argv"]
               for c in describes)
    assert all(c["model"] == "haiku" for c in describes)

    # Time accounting: every frame's 5 seconds lands in exactly one segment
    total_frames = db.val("SELECT COUNT(*) FROM frames")
    seg_secs = db.val("SELECT SUM(seconds) FROM segments")
    assert abs(seg_secs - total_frames * 5) < 1e-6
    tasks = {r[0] for r in db.q("SELECT DISTINCT task FROM segments")}
    assert "Working in Google Chrome" in tasks and "Working in Microsoft Excel" in tasks
    assert any(t.startswith("Call: Zoom Meeting") for t in tasks)
    assert any(t.startswith("Not captured: personal messaging") for t in tasks)

    # Pruning: only the screenshots the AI saw (plus one per carried window) remain on disk
    kept = list((data_dir() / "frames").rglob("*.jpg"))
    referenced = db.val("SELECT COUNT(*) FROM frames WHERE image IS NOT NULL")
    assert len(kept) == referenced
    assert len(kept) <= 5 * statuses.count("done") + statuses.count("carried")

    # Check-ins: spaced >= 20 minutes apart, under the daily cap, never more than one pending
    times = [t for t, kind, q in shown]
    assert len(shown) >= 2
    assert all(b - a >= 20 * 60 for a, b in zip(times, times[1:]))
    assert db.val("SELECT COUNT(*) FROM checkins WHERE status='answered'") == len(shown)

    # Daily report via fake Claude Opus; open questions become follow-ups
    path, backend = reports.generate_daily(db, cfg, Router(cfg), "2026-10-19")
    assert backend == "claude" and path.exists()
    digest = (path.parent / "2026-10-19.data.md").read_text()
    for section in ("## Totals", "## Time by category", "## Time by task", "## Timeline", "## What the person said",
                    "About the person: I run a small lead-gen agency."):
        assert section in digest, section
    assert "personal messaging" in digest and "Zoom Meeting" in digest
    rep_calls = [c for c in env.calls() if c["kind"] == "daily"]
    assert rep_calls[-1]["model"] == "opus" and "--effort" in rep_calls[-1]["argv"]
    fups = [r["question"] for r in db.q("SELECT question FROM followups")]
    assert fups == ["Which CRM do you log replies in?", "How do you decide which leads to drop?",
                    "Who approves proposals before they go out?"]


def test_audit_report_falls_back_from_best_to_opus(env, cfg, db, monkeypatch):
    simulate(cfg, db, until=at(9, 30))
    monkeypatch.setenv("FAKE_CLAUDE_MODE", "model_best")
    path, backend = reports.generate_audit(db, cfg, Router(cfg))
    assert path.exists() and "Prompt for the next step" in path.read_text()
    audit_calls = [c for c in env.calls() if c["kind"] == "audit"]
    assert [c["model"] for c in audit_calls] == ["best", "opus"]


def test_codex_fallback_and_cooldown(env, cfg, db, monkeypatch):
    monkeypatch.setenv("FAKE_CLAUDE_MODE", "ratelimit")
    simulate(cfg, db, until=at(9, 20))
    calls = env.calls()
    assert any(c["cli"] == "codex" and c["n_images"] >= 1 and c["images_exist"] for c in calls)
    # after the first usage-limit error Claude is not retried during the cooldown
    assert sum(1 for c in calls if c["cli"] == "claude") == 1
    seg = db.one("SELECT * FROM segments WHERE source='model' LIMIT 1")
    assert seg["task"] == "Codex-described work" and seg["category"] == "operations"


def test_both_limited_pauses_without_losing_windows(env, cfg, db, monkeypatch):
    monkeypatch.setenv("FAKE_CLAUDE_MODE", "ratelimit")
    monkeypatch.setenv("FAKE_CODEX_MODE", "ratelimit")
    probe, desc, ck, shown = simulate(cfg, db, until=at(9, 20))
    assert db.val("SELECT COUNT(*) FROM batches") == 0
    assert desc.paused_until > 0
    # limits lift: everything gets described in order
    monkeypatch.delenv("FAKE_CLAUDE_MODE")
    monkeypatch.delenv("FAKE_CODEX_MODE")
    desc.router = Router(cfg)
    desc.paused_until = 0
    n = 0
    while desc.process_next(at(12, 0)):
        n += 1
    assert db.val("SELECT COUNT(*) FROM batches WHERE status='done'") == 4


def test_bad_reply_then_retry(env, cfg, db, monkeypatch):
    monkeypatch.setenv("FAKE_CLAUDE_MODE", "garbage")
    monkeypatch.setenv("FAKE_CODEX_MODE", "ratelimit")
    simulate(cfg, db, until=at(9, 10))
    rows = db.q("SELECT * FROM batches ORDER BY start_ts")
    assert [r["status"] for r in rows] == ["failed", "failed"]
    assert db.val("SELECT COUNT(*) FROM segments WHERE source='metadata'") == 2  # time still accounted
    monkeypatch.delenv("FAKE_CLAUDE_MODE")
    monkeypatch.delenv("FAKE_CODEX_MODE")
    desc = Describer(db, cfg, Router(cfg))
    import time as _t
    later = _t.time() + 3600  # failed windows are retried after a real-time backoff
    assert desc.process_next(later)
    assert desc.process_next(later)
    rows = db.q("SELECT * FROM batches ORDER BY start_ts")
    assert [r["status"] for r in rows] == ["done", "done"]
    assert db.val("SELECT COUNT(*) FROM segments WHERE source='metadata'") == 0


def test_fenced_json_and_style(env, cfg, db, monkeypatch):
    monkeypatch.setenv("FAKE_STYLE", "fenced")
    simulate(cfg, db, until=at(9, 10))
    assert db.val("SELECT COUNT(*) FROM batches WHERE status='done'") == 2


def test_selftest_with_fakes(env, cfg):
    ok, lines = selftest.run(Router(cfg))
    assert ok and lines[0].startswith("✓ claude/haiku OK") and lines[1].startswith("✓ codex")


def test_missing_cli(env, cfg, tmp_path, monkeypatch):
    from workaudit.config import Config
    p = tmp_path / "c2.toml"
    p.write_text('[paths]\nclaude = "/nope/claude"\ncodex = "/nope/codex"\n')
    monkeypatch.setenv("PATH", "/nonexistent")
    c = Config(p)
    r = Router(c)
    try:
        r.call("describe", "s", "t")
        assert False
    except LLMError as e:
        assert e.kind == "missing"


def test_profile_and_manual_questions(env, cfg, db):
    import time as _t
    ck = CheckinManager(db, cfg)
    db.meta_set("about_me", "")
    ck.ensure_profile("")
    now = _t.time()
    p = ck.next_to_show(now)
    assert p and p["kind"] == "profile"
    ck.resolve(p, "answered", "We run cold email for B2B SaaS.", now)
    assert db.meta_get("about_me") == "We run cold email for B2B SaaS."
    assert ck.ask_now(now + 60, task="X")
    p = ck.next_to_show(now + 60)
    assert p["kind"] == "manual"
    ck.snooze(p, 600, now + 60)
    assert ck.next_to_show(now + 120) is None
    assert ck.next_to_show(now + 700)["id"] == p["id"]


def test_call_in_background_blocks_popups_but_not_capture(env, cfg, db):
    """Sharing slides in Chrome during a Zoom call: the slides are recorded, check-in popups are held."""
    probe = FakeProbe()
    probe.now = at(9, 5)
    orig = probe.snapshot

    def snapshot():
        front, wins = orig()
        wins.append({"pid": 99, "app": "zoom.us", "window_id": 55, "title": "zoom share statusbar window",
                     "bounds": (0, 0, 400, 80)})
        return front, wins
    probe.snapshot = snapshot
    shared = Shared()
    cw = CaptureWorker(db, cfg, shared, probe)
    cw.tick(probe.now)
    assert db.val("SELECT state FROM frames ORDER BY id DESC LIMIT 1") == "captured"
    assert shared.call_on_screen == "zoom share statusbar window"
