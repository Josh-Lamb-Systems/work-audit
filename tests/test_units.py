import time
from datetime import datetime

from workaudit import batching
from workaudit.config import Config, set_values
from workaudit.describe import normalize_category, parse_reply
from workaudit.llm import classify_error, extract_json
from workaudit.privacy import classify, is_banking_host, is_call


class P:
    skip_password_managers_and_banking = True
    skip_personal_messaging = True
    skip_call_screens = True
    skip_password_fields = True
    extra_apps = ["Notes"]
    extra_domains = ["reddit.com", "mail.google.com/mail/u/1"]
    extra_title_keywords = ["salary"]
    allow_apps = []
    allow_domains = ["mercury.com"]


def k(app, bundle=None, title="", url=None, mode=None, secure=False, p=P):
    return classify(app, bundle, title, url, mode, secure, p).kind, classify(app, bundle, title, url, mode, secure, p).reason


def test_privacy_rules():
    assert k("1Password", "com.1password.1password") == ("private", "password_manager")
    assert k("Keychain Access") == ("private", "password_manager")
    assert k("Google Chrome", "com.google.Chrome", "Chase", "https://secure.chase.com/web/auth") == ("private", "banking")
    assert k("Google Chrome", "com.google.Chrome", "x", "https://www.firstnationalbank.com/login") == ("private", "banking")
    assert k("Google Chrome", "com.google.Chrome", "x", "https://alliantcreditunion.org") == ("private", "banking")
    assert k("Google Chrome", "com.google.Chrome", "x", "https://app.mercury.com") == ("ok", None)  # allowed
    assert k("Google Chrome", "com.google.Chrome", "New Tab", "https://google.com", mode="incognito") == ("private", "private_window")
    assert k("Firefox", "org.mozilla.firefox", "Bank — Mozilla Firefox Private Browsing") == ("private", "private_window")
    assert k("Messages", "com.apple.MobileSMS", "Mom") == ("private", "messaging")
    assert k("Google Chrome", "com.google.Chrome", "WhatsApp", "https://web.whatsapp.com/") == ("private", "messaging")
    assert k("Google Chrome", "com.google.Chrome", "x", "https://www.facebook.com/messages/t/123") == ("private", "messaging")
    assert k("Google Chrome", "com.google.Chrome", "x", "https://www.facebook.com/groups/1") == ("ok", None)
    assert k("zoom.us", "us.zoom.xos", "Zoom Meeting") == ("call", "call")
    assert k("zoom.us", "us.zoom.xos", "Zoom Workplace") == ("ok", None)
    assert k("Slack", "com.tinyspeck.slackmacgap", "Huddle with Ana") == ("call", "call")
    assert k("Slack", "com.tinyspeck.slackmacgap", "#sales - Acme") == ("ok", None)
    assert k("Google Chrome", "com.google.Chrome", "Meet", "https://meet.google.com/abc-defg-hij") == ("call", "call")
    assert k("Google Chrome", "com.google.Chrome", "Meet", "https://meet.google.com/landing") == ("ok", None)
    assert k("Notes", "com.apple.Notes") == ("private", "custom")
    assert k("Google Chrome", "com.google.Chrome", "r/x", "https://old.reddit.com/r/x") == ("private", "custom")
    assert k("Google Chrome", "com.google.Chrome", "x", "https://mail.google.com/mail/u/1/#inbox") == ("private", "custom")
    assert k("Google Chrome", "com.google.Chrome", "x", "https://mail.google.com/mail/u/0/#inbox") == ("ok", None)
    assert k("Numbers", None, "2026 salary review") == ("private", "custom")
    assert k("Safari", "com.apple.Safari", "Login", "https://app.hubspot.com", secure=True) == ("private", "password_field")
    assert k("Excel", "com.microsoft.Excel", "Q4 leads.xlsx") == ("ok", None)
    assert is_banking_host("bankofamerica.com") and not is_banking_host("hubspot.com")
    assert is_call("Microsoft Teams", "com.microsoft.teams2", "Meeting with Acme | Microsoft Teams", None)


def test_privacy_switches_off():
    class Off(P):
        skip_password_managers_and_banking = False
        skip_personal_messaging = False
        skip_call_screens = False
        extra_apps, extra_domains, extra_title_keywords = [], [], []
    assert k("1Password", "com.1password.1password", p=Off)[0] == "ok"
    assert k("Messages", "com.apple.MobileSMS", p=Off)[0] == "ok"
    assert k("zoom.us", "us.zoom.xos", "Zoom Meeting", p=Off)[0] == "ok"


def test_extract_json_variants():
    assert extract_json('{"a": 1}') == {"a": 1}
    assert extract_json('```json\n{"a": {"b": "}"}}\n```') == {"a": {"b": "}"}}
    assert extract_json('Sure! Here it is: {"x": [1, 2]} hope that helps') == {"x": [1, 2]}
    assert extract_json("no json here") is None
    assert extract_json('{bad} then {"ok": true}') == {"ok": True}


def test_parse_reply_coercion():
    r = parse_reply('{"segments": {"task": "T", "summary": "S", "category": "Admin", "apps": "Excel", '
                    '"is_work": "no"}, "new_task": "true", "checkin_question": "null"}')
    assert r["segments"][0]["category"] == "operations"
    assert r["segments"][0]["apps"] == ["Excel"]
    assert r["segments"][0]["is_work"] is False
    assert r["new_task"] is True and r["question"] is None
    assert parse_reply('{"segments": []}') is None
    assert parse_reply("nope") is None
    assert normalize_category("Client work") == "deliverable"
    assert normalize_category("weird") == "other"


def test_classify_error():
    assert classify_error("Claude AI usage limit reached. Your limit will reset at 3pm") == "rate_limit"
    assert classify_error("Invalid API key · Please run /login") == "auth"
    assert classify_error("404 model_not_found") == "model"
    assert classify_error("boom") == "error"


def test_place_and_assign():
    base = datetime(2026, 10, 19, 10, 40).timestamp()
    ws, we = base, base + 300
    # valid times: sorted by time, contiguous, cover the window
    placed = batching.place_segments([{"start": "10:43", "task": "B"}, {"start": "10:39", "task": "A"}], ws, we)
    assert [s["task"] for s in placed] == ["A", "B"]
    assert placed[0]["start_ts"] == ws and placed[0]["end_ts"] == placed[1]["start_ts"] == base + 180
    assert placed[-1]["end_ts"] == we
    # an invalid time: keep the model's order, split evenly
    placed = batching.place_segments([{"start": "10:43", "task": "B"}, {"start": "10:40", "task": "A"},
                                      {"start": "99:99", "task": "C"}], ws, we)
    assert [s["task"] for s in placed] == ["B", "A", "C"]
    assert [round(s["end_ts"] - s["start_ts"]) for s in placed] == [100, 100, 100]
    # duplicate times: also an even split
    placed = batching.place_segments([{"start": "10:40", "task": "A"}, {"start": "10:42", "task": "X"},
                                      {"start": "10:42", "task": "Y"}], ws, we)
    assert [round(s["end_ts"] - s["start_ts"]) for s in placed] == [100, 100, 100]
    frames = [{"ts": ws + i * 5, "state": "captured" if i % 3 else "unchanged"} for i in range(60)]
    frames.append({"ts": ws + 10, "state": "private"})
    batching.assign_seconds(placed, frames, 5)
    assert sum(s["seconds"] for s in placed) == 60 * 5


def test_select_frames_prefers_run_ends():
    fr = []
    t = 1000.0
    for i in range(10):
        fr.append({"id": i, "ts": t + i * 5, "state": "captured", "app": "A", "title": "a", "domain": None,
                   "image": "a%d.jpg" % i, "diff": 3.0})
    for i in range(10, 13):
        fr.append({"id": i, "ts": t + i * 5, "state": "captured", "app": "B", "title": "b", "domain": None,
                   "image": "b%d.jpg" % i, "diff": 50.0})
    pick = batching.select_frames(fr, 5, 3)
    ids = [f["id"] for f in pick]
    assert 9 in ids and 12 in ids and len(ids) == 3
    assert ids == sorted(ids)


def test_config_set_values_and_validation(tmp_path):
    p = tmp_path / "c.toml"
    p.write_text('# top\n[capture]\nscope = "everything"  # bad\n[paths]\nclaude = ""  # keep comment\n')
    set_values(p, {"paths": {"claude": "/x/claude", "codex": "/y/codex"}, "describe": {"backend": "codex"}})
    txt = p.read_text()
    assert 'claude = "/x/claude"  # keep comment' in txt and 'codex = "/y/codex"' in txt and "[describe]" in txt
    c = Config(p)
    assert c.paths.claude == "/x/claude" and c.describe.backend == "codex"
    assert c.capture.scope == "window" and any("scope" in e for e in c.errors)


def test_parse_clock_on_handles_seconds():
    ref = datetime(2026, 10, 19, 9, 0).timestamp()
    assert batching.parse_clock_on(ref, "09:05:30") == datetime(2026, 10, 19, 9, 5, 30).timestamp()
    assert batching.parse_clock_on(ref, "25:00") is None
    assert batching.parse_clock_on(ref, None) is None


def test_browser_titles_when_url_unknown():
    # Firefox (no AppleScript URL) or a browser whose Automation permission was denied
    assert k("Firefox", "org.mozilla.firefox", "Chase Online - Account Summary") == ("private", "banking")
    assert k("Firefox", "org.mozilla.firefox", "WhatsApp") == ("private", "messaging")
    assert k("Firefox", "org.mozilla.firefox", "Meet - abc-defg-hij") == ("call", "call")
    assert k("Firefox", "org.mozilla.firefox", "Chase follow-ups - Google Docs") == ("ok", None)
    # with a known URL the title alone doesn't decide
    assert k("Google Chrome", "com.google.Chrome", "Bank reconciliation - Google Sheets",
             "https://docs.google.com/spreadsheets/d/1") == ("ok", None)


def test_background_window_labels_and_calls():
    from workaudit.privacy import call_on_screen, presenting, redact_label
    assert redact_label("Google Chrome", "com.google.Chrome", "Chase Online", P) is None   # looks like a bank
    assert redact_label("Google Chrome", "com.google.Chrome", "Q4 deck - Google Slides", P) == "Google Chrome"
    assert redact_label("Slack", "com.tinyspeck.slackmacgap", "#sales", P) == 'Slack — "#sales"'
    assert redact_label("Messages", "com.apple.MobileSMS", "Mom", P) is None
    wins = [{"pid": 1, "app": "Google Chrome", "title": "Q4 deck - Google Slides"},
            {"pid": 2, "app": "zoom.us", "title": "Zoom Meeting"}]
    bundles = {1: "com.google.Chrome", 2: "us.zoom.xos"}
    assert call_on_screen(wins, bundles.get) == "Zoom Meeting"
    assert call_on_screen(wins[:1], bundles.get) is None
    assert call_on_screen([{"pid": 1, "app": "Google Chrome", "title": "Meet - abc-defg-hij"}], bundles.get)
    assert presenting("Microsoft PowerPoint", "PowerPoint Slide Show - Pitch.pptx")
    assert not presenting("Microsoft PowerPoint", "Pitch.pptx")
