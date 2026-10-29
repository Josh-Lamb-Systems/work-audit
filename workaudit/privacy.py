"""Decides, for the window in front, whether it may be screenshotted.

Verdicts:
  ok      -> capture normally
  private -> no screenshot, no window title, no URL; only the app name and a reason are logged
  call    -> no screenshot; app + window title logged so the audit knows a call happened
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterable, Optional, Sequence

from .util import host_of

PASSWORD_BUNDLES = {
    "com.1password.1password", "com.agilebits.onepassword7", "com.agilebits.onepassword-osx",
    "com.agilebits.onepassword4", "com.bitwarden.desktop", "com.lastpass.lastpass",
    "com.lastpass.lastpassmacdesktop", "com.dashlane.dashlane", "org.keepassxc.keepassxc",
    "com.keepassium.mac", "in.sinew.enpass-desktop", "com.apple.keychainaccess", "com.apple.passwords",
    "me.proton.pass.electron", "com.nordpass.macos.nordpass", "com.siber.roboform",
    "com.callpod.keepermac.lite", "com.strongbox.mac",
}
PASSWORD_NAMES = {
    "1password", "1password 7", "bitwarden", "lastpass", "dashlane", "keepassxc", "keepassium", "enpass",
    "keychain access", "passwords", "proton pass", "nordpass", "roboform", "keeper password manager", "strongbox",
}

# Banks, brokerages, payment apps, credit bureaus, tax. Matched as domain suffixes.
BANK_DOMAINS = {
    "chase.com", "bankofamerica.com", "wellsfargo.com", "citi.com", "citibank.com", "capitalone.com",
    "usbank.com", "pnc.com", "truist.com", "td.com", "tdbank.com", "schwab.com", "fidelity.com",
    "vanguard.com", "etrade.com", "robinhood.com", "merrilledge.com", "ml.com", "morganstanley.com",
    "ally.com", "sofi.com", "discover.com", "americanexpress.com", "amex.com", "synchrony.com",
    "barclaysus.com", "barclays.co.uk", "regions.com", "huntington.com", "key.com", "citizensbank.com",
    "firstcitizens.com", "navyfederal.org", "usaa.com", "penfed.org", "becu.org", "chime.com", "varo.com",
    "marcus.com", "goldmansachs.com", "wealthfront.com", "betterment.com", "m1.com", "acorns.com",
    "stash.com", "coinbase.com", "kraken.com", "gemini.com", "binance.us", "binance.com", "crypto.com",
    "paypal.com", "venmo.com", "cash.app", "zellepay.com", "wise.com", "revolut.com", "mercury.com",
    "relayfi.com", "novo.co", "bluevine.com", "brex.com", "ramp.com", "creditkarma.com", "experian.com",
    "equifax.com", "transunion.com", "annualcreditreport.com", "irs.gov", "treasurydirect.gov",
    "interactivebrokers.com", "ibkr.com", "public.com", "webull.com", "tastytrade.com", "monzo.com",
    "starlingbank.com", "lloydsbank.com", "natwest.com", "santander.com", "santander.co.uk", "hsbc.com",
    "rbc.com", "rbcroyalbank.com", "scotiabank.com", "bmo.com", "cibc.com", "commbank.com.au",
    "westpac.com.au", "anz.com", "nab.com.au", "n26.com", "bunq.com", "apple.com/apple-card",
}
BANK_WORDS = ("bank", "banking", "creditunion")

MESSAGING_BUNDLES = {
    "com.apple.mobilesms", "net.whatsapp.whatsapp", "desktop.whatsapp", "org.whispersystems.signal-desktop",
    "ru.keepcoder.telegram", "org.telegram.desktop", "com.facebook.archon", "com.facebook.archon.developerid",
    "com.facebook.messenger", "com.automattic.beeper.desktop", "com.viber.osx", "jp.naver.line.mac",
    "com.tencent.xinwechat", "com.apple.facetime",
}
MESSAGING_NAMES = {
    "messages", "whatsapp", "signal", "telegram", "telegram desktop", "messenger", "beeper", "beeper desktop",
    "viber", "line", "wechat", "facetime",
}
MESSAGING_URLS = (
    "web.whatsapp.com", "messages.google.com", "web.telegram.org", "messenger.com",
    "facebook.com/messages", "instagram.com/direct", "x.com/messages", "twitter.com/messages",
    "linkedin.com/messaging",
)

CALL_APPS = {  # bundle id -> regex the window title must match (None = any window)
    "us.zoom.xos": re.compile(r"zoom meeting|zoom webinar|\bmeeting\b|\bwebinar\b", re.I),
    "com.microsoft.teams2": re.compile(r"\b(meeting|call)\b", re.I),
    "com.microsoft.teams": re.compile(r"\b(meeting|call)\b", re.I),
    "com.webex.meetingmanager": None,
    "cisco-systems.spark": re.compile(r"\b(meeting|call)\b", re.I),
    "com.tinyspeck.slackmacgap": re.compile(r"\bhuddle\b", re.I),
    "com.apple.facetime": None,
    "com.hnc.discord": re.compile(r"voice connected|\bcall\b", re.I),
    "co.teamport.around": None,
    "com.skype.skype": re.compile(r"\bcall\b", re.I),
}
CALL_URL_PATTERNS = (
    re.compile(r"^meet\.google\.com/[a-z]{3,4}-[a-z]{3,4}-[a-z]{3,4}"),
    re.compile(r"^(app\.)?zoom\.us/wc/"),
    re.compile(r"^[a-z0-9-]+\.zoom\.us/wc/"),
    re.compile(r"^meet\.jit\.si/[^/?#]+"),
    re.compile(r"^whereby\.com/[^/?#]+"),
    re.compile(r"^riverside\.(fm|com)/studio"),
    re.compile(r"^streamyard\.com/[a-z0-9]+"),
)
TEAMS_WEB = ("teams.microsoft.com", "teams.live.com", "teams.cloud.microsoft")

PRIVATE_TITLE_MARKERS = ("private browsing", "[inprivate]", "inprivate", "incognito")

# Browsers: their window titles are page titles, so background browser windows are listed
# without titles, and when a page address can't be read the title is checked instead.
BROWSER_BUNDLES = {
    "com.google.chrome", "com.google.chrome.beta", "com.google.chrome.dev", "com.google.chrome.canary",
    "org.chromium.chromium", "com.brave.browser", "com.brave.browser.beta", "com.brave.browser.nightly",
    "com.microsoft.edgemac", "com.microsoft.edgemac.beta", "com.microsoft.edgemac.dev", "com.vivaldi.vivaldi",
    "com.operasoftware.opera", "ai.perplexity.comet", "company.thebrowser.browser", "company.thebrowser.dia",
    "com.apple.safari", "com.apple.safaritechnologypreview", "org.mozilla.firefox",
    "org.mozilla.firefoxdeveloperedition", "app.zen-browser.zen", "com.kagi.kagimacos", "org.torproject.torbrowser",
}
BROWSER_NAMES = {"google chrome", "chromium", "brave browser", "microsoft edge", "vivaldi", "opera", "comet", "arc",
                 "dia", "safari", "safari technology preview", "firefox", "zen", "zen browser", "orion", "tor browser"}
BANK_TITLE_RX = re.compile(
    r"\b(bank|banking|credit union|wells fargo|paypal|venmo|schwab|fidelity|vanguard|robinhood|coinbase|"
    r"american express|amex|capital one|citibank|e\*?trade|merrill|chase online|chase\.com|sofi|mercury|brex|"
    r"revolut|zelle|cash app|credit karma|experian|equifax|transunion)\b", re.I)
MESSAGING_TITLE_RX = re.compile(r"\b(whatsapp|messenger|google messages|telegram web|signal)\b", re.I)
CALL_TITLE_RX = re.compile(r"^meet\s*[-–—:]\s*[a-z]{3,4}-[a-z]{3,4}-[a-z]{3,4}|\bgoogle meet\b.*\b[a-z]{3}-[a-z]{4}-[a-z]{3}\b|"
                           r"\bzoom meeting\b|\bjitsi meet\b|\bwhereby\b", re.I)
# Zoom/Teams/Webex windows that exist while you share your screen during a call
SHARING_TITLE_RX = re.compile(r"share (status|tool)bar|sharing control|you are screen sharing|is sharing|"
                              r"meeting controls|zoom meeting|webinar", re.I)
PRESENTING_TITLE_RX = re.compile(r"slide ?show|presenter view|presentation mode|presenting", re.I)


def is_browser(app: Optional[str], bundle_id: Optional[str]) -> bool:
    return _norm(bundle_id) in BROWSER_BUNDLES or _norm(app) in BROWSER_NAMES


REASON_LABELS = {
    "password_manager": "password manager",
    "banking": "banking / finance site",
    "private_window": "private browser window",
    "password_field": "typing in a password field",
    "messaging": "personal messaging",
    "custom": "excluded by your settings",
}


@dataclass
class Verdict:
    kind: str                # ok | private | call
    reason: Optional[str] = None


def _norm(s: Optional[str]) -> str:
    return (s or "").strip().lower()


def _url_key(url: Optional[str]) -> str:
    """'https://www.facebook.com/messages/t/1' -> 'facebook.com/messages/t/1'"""
    if not url:
        return ""
    u = url.strip()
    if "://" in u:
        u = u.split("://", 1)[1]
    u = u.lower()
    if u.startswith("www."):
        u = u[4:]
    host, _, path = u.partition("/")
    host = host.rsplit("@", 1)[-1].split(":", 1)[0]
    return host + ("/" + path if path else "")


def _domain_match(host: str, url_key: str, entries: Iterable[str]) -> bool:
    for e in entries:
        e = _norm(e)
        if not e:
            continue
        if e.startswith("www."):
            e = e[4:]
        if "/" in e:
            if url_key == e or url_key.startswith(e.rstrip("/") + "/") or url_key.startswith(e):
                return True
            # also allow subdomains: m.facebook.com/messages
            d, _, p = e.partition("/")
            if host.endswith("." + d) and url_key.split("/", 1)[-1].startswith(p):
                return True
        elif host == e or host.endswith("." + e):
            return True
    return False


def is_banking_host(host: str, url_key: str = "") -> bool:
    if not host:
        return False
    if _domain_match(host, url_key or host, BANK_DOMAINS):
        return True
    labels = host.split(".")
    for lab in labels[:-1]:
        if any(w in lab for w in BANK_WORDS):
            return True
        if lab.endswith("fcu") and len(lab) > 3:
            return True
    return False


def _app_match(app: str, bundle: str, entries: Iterable[str]) -> bool:
    for e in entries:
        e = _norm(e)
        if e and (e == app or e == bundle):
            return True
    return False


def is_call(app: str, bundle: str, title: str, url: Optional[str]) -> bool:
    b = _norm(bundle)
    if b in CALL_APPS:
        rx = CALL_APPS[b]
        if rx is None or rx.search(title or ""):
            return True
    if _norm(app) in ("zoom.us", "zoom") and re.search(r"zoom meeting|zoom webinar", title or "", re.I):
        return True
    key = _url_key(url)
    if key:
        for rx in CALL_URL_PATTERNS:
            if rx.search(key):
                return True
        host = key.split("/", 1)[0]
        if host in TEAMS_WEB and re.search(r"\b(meeting|call)\b", title or "", re.I):
            return True
    return False


def classify(app: Optional[str], bundle_id: Optional[str], title: Optional[str], url: Optional[str],
             browser_mode: Optional[str], secure_input_front: bool, p) -> Verdict:
    """p is the [privacy] config section (attribute access)."""
    a, b, t = _norm(app), _norm(bundle_id), _norm(title)
    key = _url_key(url)
    host = host_of(url)

    allow_apps: Sequence[str] = p.allow_apps
    allow_domains: Sequence[str] = p.allow_domains
    if _app_match(a, b, allow_apps):
        return Verdict("ok")
    allowed_site = bool(host) and _domain_match(host, key, allow_domains)

    # Your own exclusions always apply.
    if _app_match(a, b, p.extra_apps):
        return Verdict("private", "custom")
    if host and not allowed_site and _domain_match(host, key, p.extra_domains):
        return Verdict("private", "custom")
    if t and any(_norm(k) and _norm(k) in t for k in p.extra_title_keywords):
        return Verdict("private", "custom")

    browser_no_url = is_browser(app, bundle_id) and not host
    if p.skip_password_managers_and_banking:
        if b in PASSWORD_BUNDLES or a in PASSWORD_NAMES:
            return Verdict("private", "password_manager")
        if _norm(browser_mode) == "incognito" or any(m in t for m in PRIVATE_TITLE_MARKERS):
            return Verdict("private", "private_window")
        if host and not allowed_site and is_banking_host(host, key):
            return Verdict("private", "banking")
        if browser_no_url and BANK_TITLE_RX.search(title or ""):
            return Verdict("private", "banking")

    if p.skip_password_fields and secure_input_front:
        return Verdict("private", "password_field")

    if p.skip_personal_messaging:
        if b in MESSAGING_BUNDLES or a in MESSAGING_NAMES:
            return Verdict("private", "messaging")
        if host and not allowed_site and _domain_match(host, key, MESSAGING_URLS):
            return Verdict("private", "messaging")
        if browser_no_url and MESSAGING_TITLE_RX.search(title or ""):
            return Verdict("private", "messaging")

    if p.skip_call_screens:
        if is_call(a, b, title or "", url):
            return Verdict("call", "call")
        if browser_no_url and CALL_TITLE_RX.search(title or ""):
            return Verdict("call", "call")

    return Verdict("ok")


def redact_label(app: str, bundle_id: str, title: str, p) -> Optional[str]:
    """Label for a background window in the 'other open windows' list, or None to omit it."""
    v = classify(app, bundle_id, title, None, None, False, p)
    if v.kind == "private":
        return None
    t = (title or "").strip()
    if is_browser(app, bundle_id):
        return app  # a background tab title could be anything (a bank, a private window): leave it out
    return "%s — \"%s\"" % (app, t[:80]) if t else app


def call_on_screen(windows, bundle_of) -> Optional[str]:
    """Title of a call/screen-sharing window anywhere on screen, or None. Used to keep check-in
    popups off a screen you might be sharing."""
    for w in windows:
        app, title = w.get("app") or "", w.get("title") or ""
        b = bundle_of(w.get("pid")) or ""
        if is_call(app, b, title, None):
            return title or app
        if is_browser(app, b) and CALL_TITLE_RX.search(title):
            return title
        if _norm(b) in CALL_APPS or _norm(app) in ("zoom.us", "microsoft teams", "webex", "facetime"):
            if SHARING_TITLE_RX.search(title):
                return title or app
    return None


def presenting(app: Optional[str], title: Optional[str]) -> bool:
    return bool(PRESENTING_TITLE_RX.search(title or ""))
