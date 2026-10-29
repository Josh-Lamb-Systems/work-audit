"""Daily reports and the final audit report (the process inventory for Step 2)."""
from __future__ import annotations

import re
import time
from collections import Counter, OrderedDict
from pathlib import Path
from typing import Any, Dict, List, Tuple

from .db import ACTIVE_STATES, DB
from .llm import Router
from .privacy import REASON_LABELS
from .util import clip, data_dir, day_bounds, fmt_dur, hm, jload, log, pretty_day, today

ANALYST_SYSTEM = """You are a sharp operations analyst helping a business owner audit their own work, so they can automate as much of their non-customer-facing work as possible with AI (Claude skills, agents and scheduled tasks). You write concrete, compact Markdown. You use the real names of tools, clients and documents from the data. You never pad and never invent facts; when the data is thin you say so."""

DAILY_INSTRUCTIONS = """Below is the raw data for one working day. It comes from screenshots taken every few seconds and summarized by a smaller AI model, plus the person's own answers to check-in questions. Treat the person's answers as the most reliable source; the screenshot summaries can be wrong or vague.

Write the day's audit report in Markdown with exactly these sections:

# Work audit — {day_pretty}
## Day at a glance
3–6 bullets: active time, where the time went, main focus blocks, context switching, anything notable.
## Tasks and processes observed
One ### heading per distinct process (merge activity that is really the same process). Under each:
- What it is / deliverable produced
- Time today, and how often it seems to happen
- Trigger (what starts it) and inputs
- Steps as observed
- Tools, systems and data sources
- Output, and who it is for
- Judgment calls the person makes
## What the person told me
The facts from their answers and notes that matter most for automating their work.
## Automation candidates
A table with columns: Process | Fit (automate fully / AI drafts, human approves / keep human) | Why | What would be needed (templates, examples, access). Order by time that could be saved.
## Open questions for tomorrow
Up to 8 specific questions whose answers would most improve the automation plan, one per line, each starting with "- ".

=== RAW DATA ===
{digest}
"""

AUDIT_INSTRUCTIONS = """Below is everything from a {n_days}-day work audit ({first} to {last}): one section per day (the day's report, or its raw data where no report was written), aggregate statistics across all days, and every answer the person gave to check-in questions. The person's own answers are the most reliable source.

Produce the master inventory of this person's work in Markdown with these sections:

# Work audit — {first} to {last}
## Summary
5–8 bullets: what this person's work consists of and where the time goes.
## Where the time goes
A table: Process | Category | Hours over the audit | Estimated hours per week | Share of tracked time.
## Inventory of deliverables and processes
One ### heading per process, ordered by time spent, merging duplicates across days. Under each:
- Description and deliverable
- Frequency and time per week
- Trigger and inputs
- Steps
- Tools, systems and data sources
- Outputs and who receives them
- Decision rules and judgment involved
- Pain points or waste observed
- Automation approach: fully automated and scheduled / AI does it on demand with human approval / keep human — and why
- What Claude would need: context, templates, examples, credentials or access
- How to test it: what a 10/10 output looks like (criteria you could score 10 sample runs against)
## Recommended order of automation
The top 10 by hours saved × ease, with a one-line first step for each.
## Gaps in this audit
What was not observed or is still unclear, and the questions to answer.
## Prompt for the next step
A ready-to-paste prompt inside a fenced code block that starts with "Here's everything I do on a daily basis." It lists every process briefly (with time per week), says the goal is automating the vast majority of non-customer-facing work, asks Claude to help build systems for each, and asks it to request any missing context.

=== AUDIT DATA ===
{body}
"""


# ------------------------------------------------------------------ helpers

def _segments(db: DB, day: str) -> List[Any]:
    return db.q("SELECT * FROM segments WHERE day=? ORDER BY start_ts, id", (day,))


def _merge_blocks(segs: List[Any], gap: float = 180.0) -> List[Dict[str, Any]]:
    blocks: List[Dict[str, Any]] = []
    for s in segs:
        task = (s["task"] or "?").strip()
        if (blocks and blocks[-1]["task"].lower() == task.lower()
                and s["start_ts"] - blocks[-1]["end_ts"] <= gap):
            b = blocks[-1]
            b["end_ts"] = max(b["end_ts"], s["end_ts"])
            b["seconds"] += s["seconds"] or 0.0
            if s["summary"] and s["summary"] not in b["summaries"]:
                b["summaries"].append(s["summary"])
        else:
            blocks.append({"task": task, "category": s["category"] or "other", "start_ts": s["start_ts"],
                           "end_ts": s["end_ts"], "seconds": s["seconds"] or 0.0, "source": s["source"],
                           "summaries": [s["summary"]] if s["summary"] else []})
    return blocks


def _pause_seconds(db: DB, day: str) -> float:
    a, b = day_bounds(day)
    evs = db.q("SELECT ts, kind, detail FROM events WHERE ts>=? AND ts<? AND kind IN ('pause','resume') ORDER BY ts",
               (a, b))
    total, start, until = 0.0, None, None
    end_cap = min(b, time.time())
    for e in evs:
        if e["kind"] == "pause":
            if start is None:
                start = e["ts"]
                d = jload(e["detail"], {}) or {}
                until = d.get("until")
        elif e["kind"] == "resume" and start is not None:
            stop = e["ts"] if not until else min(e["ts"], until)
            total += max(0.0, stop - start)
            start, until = None, None
    if start is not None:
        stop = min(end_cap, until) if until else end_cap
        total += max(0.0, stop - start)
    return total


def build_digest(db: DB, cfg, day: str) -> str:
    interval = float(cfg.capture.interval_seconds)
    st = db.day_stats(day, interval)
    segs = _segments(db, day)
    work = [s for s in segs if s["source"] in ("model", "carried", "metadata")]
    L: List[str] = ["# Raw work-audit data — %s" % pretty_day(day), ""]

    # Totals
    L.append("## Totals")
    span = ""
    if st["first_ts"]:
        span = " (first activity %s, last %s)" % (hm(st["first_ts"]), hm(st["last_ts"]))
    L.append("- Active, captured time: %s%s" % (fmt_dur(st["active"]), span))
    if st["private"]:
        L.append("- Not captured for privacy: " + "; ".join(
            "%s %s" % (REASON_LABELS.get(k, k), fmt_dur(v)) for k, v in sorted(st["private"].items(), key=lambda x: -x[1])))
    if st["call"]:
        L.append("- On calls/meetings (screen not captured): %s" % fmt_dur(st["call"]))
    paused = _pause_seconds(db, day)
    if paused >= 60:
        L.append("- Recording paused by the person: %s" % fmt_dur(paused))
    L.append("- AI descriptions: %d (failed: %d). Check-ins asked: %d, answered: %d. Notes: %d." % (
        st["ai_calls"], st["ai_failed"], st["asked"], st["answered"], st["notes"]))
    pending = db.val("SELECT COUNT(*) FROM frames WHERE day=? AND ts >= COALESCE((SELECT MAX(end_ts) FROM batches), 0)",
                     (day,), 0)
    if pending:
        L.append("- Note: the last %s of activity was not described yet." % fmt_dur(pending * interval))
    L.append("")

    # Categories
    cat = Counter()
    for s in segs:
        if s["source"] in ("model", "carried", "metadata", "call"):
            cat[s["category"] or "other"] += s["seconds"] or 0.0
    total = sum(cat.values()) or 1.0
    if cat:
        L.append("## Time by category")
        for c, secs in cat.most_common():
            L.append("- %s: %s (%d%%)" % (c, fmt_dur(secs), round(100 * secs / total)))
        L.append("")

    # Tasks
    tasks: "OrderedDict[str, Dict[str, Any]]" = OrderedDict()
    for s in work:
        k = (s["task"] or "?").strip().lower()
        t = tasks.setdefault(k, {"task": (s["task"] or "?").strip(), "seconds": 0.0, "cats": Counter(),
                                 "apps": Counter(), "systems": Counter(), "artifacts": Counter(), "people": Counter(),
                                 "summaries": [], "automation": [], "blocks": 0})
        t["seconds"] += s["seconds"] or 0.0
        t["cats"][s["category"] or "other"] += 1
        for col in ("apps", "systems", "artifacts", "people"):
            for v in jload(s[col], []) or []:
                t[col][v] += 1
        if s["summary"] and s["source"] != "carried" and len(t["summaries"]) < 4 and s["summary"] not in t["summaries"]:
            t["summaries"].append(s["summary"])
        if s["automation"] and len(t["automation"]) < 3 and s["automation"] not in t["automation"]:
            t["automation"].append(s["automation"])
    if tasks:
        L.append("## Time by task")
        for t in sorted(tasks.values(), key=lambda x: -x["seconds"])[:40]:
            if t["seconds"] < 30:
                continue
            def top(c: Counter, n: int = 6) -> str:
                return ", ".join(v for v, _ in c.most_common(n))
            L.append("### %s — %s [%s]" % (t["task"], fmt_dur(t["seconds"]), t["cats"].most_common(1)[0][0]))
            for lab, col in (("Apps", "apps"), ("Systems", "systems"), ("Artifacts", "artifacts"), ("People", "people")):
                if t[col]:
                    L.append("- %s: %s" % (lab, top(t[col])))
            for sm in t["summaries"]:
                L.append("- Observed: %s" % clip(sm, 300))
            for au in t["automation"]:
                L.append("- Automation note: %s" % clip(au, 300))
        L.append("")

    # Apps and websites, straight from the window log
    a, b = day_bounds(day)
    ph = ",".join("?" * len(ACTIVE_STATES))
    apps = db.q("SELECT app, COUNT(*) n FROM frames WHERE ts>=? AND ts<? AND state IN (%s) AND app IS NOT NULL "
                "GROUP BY app ORDER BY n DESC LIMIT 15" % ph, [a, b] + list(ACTIVE_STATES))
    if apps:
        L.append("## Apps (window log)")
        L.append("; ".join("%s %s" % (r["app"], fmt_dur(r["n"] * interval)) for r in apps))
        L.append("")
    doms = db.q("SELECT domain, COUNT(*) n FROM frames WHERE ts>=? AND ts<? AND state IN (%s) AND domain IS NOT NULL "
                "AND domain!='' GROUP BY domain ORDER BY n DESC LIMIT 20" % ph, [a, b] + list(ACTIVE_STATES))
    if doms:
        L.append("## Websites (window log)")
        L.append("; ".join("%s %s" % (r["domain"], fmt_dur(r["n"] * interval)) for r in doms))
        L.append("")
    calls = db.q("SELECT COALESCE(NULLIF(title,''), app) t, COUNT(*) n FROM frames WHERE ts>=? AND ts<? AND state='call' "
                 "GROUP BY t ORDER BY n DESC LIMIT 15", (a, b))
    if calls:
        L.append("## Calls and meetings (window titles)")
        for r in calls:
            L.append("- %s: %s" % (clip(r["t"], 120), fmt_dur(r["n"] * interval)))
        L.append("")

    # Timeline
    blocks = _merge_blocks([s for s in segs if s["source"] != "private" or (s["seconds"] or 0) >= 60])
    if blocks:
        L.append("## Timeline")
        shown = 0
        small = 0
        for bl in blocks:
            if bl["seconds"] < 60 and bl["source"] in ("model", "carried", "metadata"):
                small += 1
                continue
            if shown >= 160:
                small += 1
                continue
            summ = clip(bl["summaries"][0], 200) if bl["summaries"] else ""
            L.append("- %s–%s (%s) [%s] %s%s" % (hm(bl["start_ts"]), hm(bl["end_ts"]), fmt_dur(bl["seconds"]),
                                                  bl["category"], bl["task"], (" — " + summ) if summ else ""))
            shown += 1
        if small:
            L.append("- (%d short activities under a minute not listed)" % small)
        L.append("")

    # Check-ins and notes
    qa = db.q("SELECT * FROM checkins WHERE day=? AND status IN ('answered','skipped','expired') ORDER BY created_at",
              (day,))
    if qa:
        L.append("## What the person said (check-ins and notes)")
        for c in qa:
            when = hm(c["answered_at"] or c["asked_at"] or c["created_at"])
            ctx = (" [during: %s]" % clip(c["task"], 80)) if c["task"] else ""
            if c["kind"] == "note":
                L.append("- %s NOTE%s: %s" % (when, ctx, clip(c["answer"], 1200)))
            elif c["status"] == "answered":
                L.append("- %s Q%s: %s\n  A: %s" % (when, ctx, clip(c["question"], 300), clip(c["answer"], 1200)))
            else:
                L.append("- %s Q%s: %s (%s)" % (when, ctx, clip(c["question"], 300), c["status"]))
        L.append("")
    about = (cfg.audit.about_me or db.meta_get("about_me") or "").strip()
    if about:
        L.insert(2, "About the person: %s\n" % about)
    return "\n".join(L).rstrip() + "\n"


def parse_open_questions(md: str, limit: int = 8) -> List[str]:
    out: List[str] = []
    lines = md.splitlines()
    inside = False
    for ln in lines:
        if re.match(r"^\s*#{1,3}\s*open questions", ln, re.I):
            inside = True
            continue
        if inside and re.match(r"^\s*#{1,3}\s+\S", ln):
            break
        if inside:
            m = re.match(r"^\s*(?:[-*]|\d+[.)])\s+(.*\S)", ln)
            if m:
                q = clip(m.group(1).strip().strip("*"), 300)
                if len(q) > 8:
                    out.append(q)
            if len(out) >= limit:
                break
    return out


def _reports_dir(sub: str) -> Path:
    d = data_dir() / "reports" / sub
    d.mkdir(parents=True, exist_ok=True)
    return d


def generate_daily(db: DB, cfg, router: Router, day: str) -> Tuple[Path, str]:
    digest = build_digest(db, cfg, day)
    rd = _reports_dir("daily")
    (rd / ("%s.data.md" % day)).write_text(digest, encoding="utf-8")
    prompt = DAILY_INSTRUCTIONS.format(day_pretty=pretty_day(day), digest=digest)
    res = router.call("daily", ANALYST_SYSTEM, prompt)
    text = res.text.strip()
    path = rd / ("%s.md" % day)
    footer = "\n\n---\n_Generated %s by %s/%s from %s._\n" % (time.strftime("%Y-%m-%d %H:%M"), res.backend, res.model,
                                                             "%s.data.md" % day)
    path.write_text(text + footer, encoding="utf-8")
    qs = parse_open_questions(text)
    if qs and day >= _yesterday():
        db.x("UPDATE followups SET used=1 WHERE used=0")  # newer questions replace older unused ones
        db.add_followups(day, qs)
    db.add_report("daily", str(path), day, res.backend)
    log.info("daily report written: %s", path)
    return path, res.backend


def _yesterday() -> str:
    return time.strftime("%Y-%m-%d", time.localtime(time.time() - 86400))


def build_audit_body(db: DB, cfg) -> Tuple[str, List[str]]:
    days = [d for d in db.days_with_data()]
    interval = float(cfg.capture.interval_seconds)
    parts: List[str] = []
    for d in days:
        r = db.report_for("daily", d)
        txt = None
        if r is not None and Path(r["path"]).exists():
            txt = Path(r["path"]).read_text(encoding="utf-8")
            parts.append("\n\n========== DAY %s (daily report) ==========\n%s" % (d, txt))
        else:
            parts.append("\n\n========== DAY %s (raw data, no report) ==========\n%s" % (d, build_digest(db, cfg, d)))
    # Aggregates across days
    rows = db.q("SELECT task, category, SUM(seconds) s, COUNT(DISTINCT day) nd FROM segments "
                "WHERE source IN ('model','carried','metadata') GROUP BY lower(task) ORDER BY s DESC LIMIT 80")
    agg = ["\n\n========== ACROSS ALL DAYS =========="]
    tot_active = db.val("SELECT COUNT(*) FROM frames WHERE state IN (%s)" % ",".join("?" * len(ACTIVE_STATES)),
                        list(ACTIVE_STATES), 0) * interval
    agg.append("Days recorded: %d. Total active captured time: %s." % (len(days), fmt_dur(tot_active)))
    cats = db.q("SELECT category, SUM(seconds) s FROM segments WHERE source IN ('model','carried','metadata','call') "
                "GROUP BY category ORDER BY s DESC")
    if cats:
        agg.append("Time by category: " + "; ".join("%s %s" % (c["category"] or "other", fmt_dur(c["s"])) for c in cats))
    if rows:
        agg.append("Top tasks (task — total time — days seen — category):")
        for r in rows:
            if (r["s"] or 0) < 60:
                continue
            agg.append("- %s — %s — %d day(s) — %s" % (clip(r["task"], 120), fmt_dur(r["s"]), r["nd"], r["category"]))
    qa = db.q("SELECT * FROM checkins WHERE status='answered' AND answer IS NOT NULL AND answer!='' ORDER BY created_at")
    if qa:
        agg.append("\nEvery answer and note from the person:")
        for c in qa:
            when = "%s %s" % (c["day"], hm(c["answered_at"] or c["created_at"]))
            if c["kind"] == "note":
                agg.append("- %s NOTE: %s" % (when, clip(c["answer"], 1000)))
            else:
                agg.append("- %s Q: %s\n  A: %s" % (when, clip(c["question"], 300), clip(c["answer"], 1000)))
    about = (cfg.audit.about_me or db.meta_get("about_me") or "").strip()
    head = ("About the person: %s\n" % about) if about else ""
    return head + "".join(parts) + "\n".join(agg) + "\n", days


def generate_audit(db: DB, cfg, router: Router) -> Tuple[Path, str]:
    body, days = build_audit_body(db, cfg)
    if not days:
        raise RuntimeError("No recorded data yet.")
    rd = _reports_dir("audit")
    stamp = time.strftime("%Y-%m-%d-%H%M")
    (rd / ("audit-%s.data.md" % stamp)).write_text(body, encoding="utf-8")
    prompt = AUDIT_INSTRUCTIONS.format(n_days=len(days), first=pretty_day(days[0]), last=pretty_day(days[-1]), body=body)
    res = router.call("audit", ANALYST_SYSTEM, prompt)
    path = rd / ("audit-%s.md" % stamp)
    path.write_text(res.text.strip() + "\n\n---\n_Generated %s by %s/%s._\n" % (
        time.strftime("%Y-%m-%d %H:%M"), res.backend, res.model), encoding="utf-8")
    db.add_report("audit", str(path), today(), res.backend)
    log.info("audit report written: %s", path)
    return path, res.backend


def missing_daily_reports(db: DB, cfg, min_active_seconds: float = 1800) -> List[str]:
    out = []
    t = today()
    for d in db.days_with_data():
        if d >= t:
            continue
        if db.report_for("daily", d) is not None:
            continue
        if db.day_stats(d, float(cfg.capture.interval_seconds))["active"] >= min_active_seconds:
            out.append(d)
    return out
