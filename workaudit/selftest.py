"""Checks that Claude Code / Codex can describe a screenshot from this machine."""
from __future__ import annotations

import time
from pathlib import Path
from typing import List, Optional, Tuple

from .describe import SYSTEM_PROMPT, build_user_text, parse_reply
from .llm import LLMError, Router
from .util import clip, data_dir

_FONT_CANDIDATES = [
    "/System/Library/Fonts/Supplemental/Arial.ttf",
    "/System/Library/Fonts/Helvetica.ttc",
    "/System/Library/Fonts/SFNS.ttf",
    "/Library/Fonts/Arial.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
]


def _font(size: int):
    from PIL import ImageFont
    for p in _FONT_CANDIDATES:
        try:
            return ImageFont.truetype(p, size)
        except Exception:
            continue
    try:
        return ImageFont.load_default(size=size)
    except TypeError:  # Pillow < 10.1
        return ImageFont.load_default()


def synthetic_screenshot(path: Path) -> Path:
    """A fake mail window with readable text, so the test never needs a real screenshot."""
    from PIL import Image, ImageDraw
    img = Image.new("RGB", (1280, 800), (255, 255, 255))
    d = ImageDraw.Draw(img)
    big, mid, small = _font(28), _font(21), _font(18)
    d.rectangle([0, 0, 1280, 64], fill=(242, 242, 247))
    d.text((24, 18), "Inbox — Mail", fill=(20, 20, 20), font=big)
    d.rectangle([0, 64, 380, 800], fill=(248, 248, 250))
    items = [("Dana Ruiz", "Re: Proposal for Q4 lead-gen campaign"), ("Stripe", "Your payout is on the way"),
             ("Calendly", "New event: Discovery call with Omar"), ("Instantly", "Campaign 'SaaS founders' stats")]
    y = 84
    for i, (who, subj) in enumerate(items):
        if i == 0:
            d.rectangle([0, y - 8, 380, y + 62], fill=(220, 232, 252))
        d.text((20, y), who, fill=(20, 20, 20), font=mid)
        d.text((20, y + 28), subj[:34], fill=(90, 90, 90), font=small)
        y += 84
    x = 410
    d.text((x, 90), "From: Dana Ruiz <dana@example.com>", fill=(20, 20, 20), font=mid)
    d.text((x, 124), "Subject: Re: Proposal for Q4 lead-gen campaign", fill=(20, 20, 20), font=mid)
    body = ["Hi Sam,", "", "Thanks for the proposal. Two questions before we sign:", "",
            "1. Can you include LinkedIn outreach in the 3,000-lead package?",
            "2. What reply rate should we expect in month one?", "",
            "If you can send updated pricing by Friday we can start Nov 3.", "", "Dana"]
    yy = 180
    for line in body:
        d.text((x, yy), line, fill=(40, 40, 40), font=small)
        yy += 30
    d.rectangle([x, 560, x + 820, 760], outline=(200, 200, 200), width=2)
    d.text((x + 16, 576), "Hi Dana, great questions. Yes, LinkedIn outreach can be added for", fill=(40, 40, 40),
           font=small)
    path.parent.mkdir(parents=True, exist_ok=True)
    img.save(str(path), "JPEG", quality=80)
    return path


def selftest_text(now: float, app: str = "Mail", title: str = "Re: Proposal for Q4 lead-gen campaign") -> str:
    rs = [{"group": "active", "app": app, "title": title, "domain": None, "reason": None,
           "start": now - 120, "end": now}]
    chosen = [{"ts": now - 5, "app": app, "title": title, "domain": None}]
    return build_user_text(about_me="(self-test)", ws=now - 300, we=now, interval=5, rs=rs, chosen=chosen,
                           other_windows=[], previous=[], task_names=[], answers=[])


def test_backend(router: Router, name: str, image: Path, text: str) -> Tuple[bool, str]:
    b = router.backends[name]
    if not b.available():
        return False, "%s: not found (set [paths] %s in config.toml)" % (name, name)
    prm = router._params("describe", name)
    t0 = time.time()
    try:
        res = b.run(SYSTEM_PROMPT, text, [str(image)], model=prm["model"], effort=prm["effort"],
                    timeout=int(prm["timeout"]))
    except LLMError as e:
        hint = {"auth": " → sign in first (claude: run `claude` once and /login; codex: `codex login`)",
                "rate_limit": " → usage limit reached; try again later",
                "model": " → model not available on your plan; change the model in config.toml"}.get(e.kind, "")
        return False, "%s: FAILED (%s) %s%s" % (name, e.kind, clip(str(e), 400), hint)
    parsed = parse_reply(res.text)
    if not parsed:
        return False, "%s: replied but not with the expected JSON: %s" % (name, clip(res.text, 300))
    s = parsed["segments"][0]
    return True, "%s/%s OK in %.0fs → \"%s\" — %s" % (name, res.model, time.time() - t0, s["task"],
                                                       clip(s["summary"], 200))


def run(router: Router, which: Optional[List[str]] = None, image: Optional[Path] = None) -> Tuple[bool, List[str]]:
    img = image or synthetic_screenshot(data_dir() / "tmp" / "selftest.jpg")
    text = selftest_text(time.time())
    names = which or [n for n in ("claude", "codex")]
    lines: List[str] = []
    ok_any = False
    for n in names:
        ok, line = test_backend(router, n, img, text)
        ok_any = ok_any or ok
        lines.append(("✓ " if ok else "✗ ") + line)
    return ok_any, lines
