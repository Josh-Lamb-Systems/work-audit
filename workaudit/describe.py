"""Turns closed 5-minute windows of frames into described segments using Claude/Codex."""
from __future__ import annotations

import os
import threading
import time
from typing import Any, Dict, List, Optional, Sequence

from . import batching
from .db import ACTIVE_STATES, DB
from .llm import LLMError, Router, extract_json
from .privacy import REASON_LABELS
from .util import as_str_list, clip, data_dir, day_of, hm, hms, jload, log, pretty_day

CATEGORIES = ["deliverable", "sales", "marketing", "communication", "operations", "finance", "planning",
              "research", "meeting", "personal", "other"]
_CAT_SYNONYMS = {
    "client work": "deliverable", "delivery": "deliverable", "production": "deliverable", "build": "deliverable",
    "prospecting": "sales", "lead generation": "sales", "lead gen": "sales", "outreach": "sales",
    "email": "communication", "comms": "communication", "messaging": "communication", "support": "communication",
    "admin": "operations", "ops": "operations", "administration": "operations", "it": "operations",
    "accounting": "finance", "bookkeeping": "finance", "invoicing": "finance",
    "strategy": "planning", "learning": "research", "reading": "research", "call": "meeting", "calls": "meeting",
    "break": "personal", "entertainment": "personal",
}
CARRY_MAX_DIFF = 6.0

SYSTEM_PROMPT = """You are the observer in a one-week work audit. You receive screenshots of one person's Mac, taken every few seconds, plus a log of which app and window was in front. Your descriptions will later be combined into a list of every task, deliverable and process this person handles, so they can decide what to automate with AI.

Rules:
- Be specific and factual. Name the app or website, the document/file/record, the client or company involved (as shown), and the concrete action. Write "Replying in Gmail to a prospect who asked about pricing", not "Using email".
- Only describe what the screenshots and the log support. If something is unclear, say so briefly. Never invent details.
- Never copy passwords, API keys, card or account numbers or other secrets. Refer to personal data generically ("a customer's phone number").
- When the person is continuing a task you were told about, reuse that exact task name so time adds up correctly.
- Reply with a single JSON object only: no prose, no code fences."""

JSON_SPEC = """Return this JSON:
{
  "segments": [
    {
      "start": "HH:MM",
      "end": "HH:MM",
      "task": "short task name; reuse an existing name if it is the same task",
      "summary": "1-2 sentences on exactly what they did",
      "category": "one of: deliverable, sales, marketing, communication, operations, finance, planning, research, meeting, personal, other",
      "apps": ["apps or websites used"],
      "systems": ["tools, services, data sources or accounts involved"],
      "artifacts": ["documents, files, records or outputs touched"],
      "people": ["clients, prospects, colleagues or companies involved"],
      "is_work": true,
      "automation_notes": "what looks repetitive, rule-based or automatable here; empty if nothing"
    }
  ],
  "new_task": true,
  "checkin_question": "string or null"
}
- 1 to 3 segments in time order that together cover the window (leave out the periods marked not captured).
- new_task: true if the person started something different from the previous activity listed above.
- checkin_question: one short, friendly question (max 25 words) whose answer would teach you something the screen cannot show: why they are doing this, what triggered it, how they decide, which systems or information they need, or what happens next. Make it specific to what you see. Use null if nothing is worth asking."""


def normalize_category(c: Any) -> str:
    s = clip(c, 40).lower().strip()
    if s in CATEGORIES:
        return s
    if s in _CAT_SYNONYMS:
        return _CAT_SYNONYMS[s]
    for cat in CATEGORIES:
        if s.startswith(cat):
            return cat
    return "other"


def _truthy(v: Any, default: bool) -> bool:
    if isinstance(v, bool):
        return v
    if v is None:
        return default
    return str(v).strip().lower() in ("true", "yes", "1", "y")


def parse_reply(text: str) -> Optional[Dict[str, Any]]:
    d = extract_json(text)
    if isinstance(d, list):
        d = {"segments": d}
    if not isinstance(d, dict):
        return None
    raw_segs = d.get("segments")
    if isinstance(raw_segs, dict):
        raw_segs = [raw_segs]
    if not isinstance(raw_segs, list):
        raw_segs = []
    segs: List[Dict[str, Any]] = []
    for s in raw_segs[:4]:
        if not isinstance(s, dict):
            continue
        task = clip(s.get("task") or s.get("name") or "", 120)
        summary = clip(s.get("summary") or s.get("description") or "", 600)
        if not task and not summary:
            continue
        segs.append({
            "start": s.get("start"), "end": s.get("end"),
            "task": task or clip(summary, 80),
            "summary": summary,
            "category": normalize_category(s.get("category")),
            "apps": as_str_list(s.get("apps")),
            "systems": as_str_list(s.get("systems")),
            "artifacts": as_str_list(s.get("artifacts")),
            "people": as_str_list(s.get("people")),
            "is_work": _truthy(s.get("is_work"), True),
            "automation": clip(s.get("automation_notes") or s.get("automation") or "", 400),
        })
    if not segs:
        return None
    q = d.get("checkin_question")
    if not isinstance(q, str) or q.strip().lower() in ("", "null", "none", "n/a"):
        q = None
    else:
        q = clip(q, 300)
    return {"segments": segs, "new_task": _truthy(d.get("new_task"), False), "question": q}


def build_user_text(*, about_me: str, ws: float, we: float, interval: int, rs: List[Dict[str, Any]],
                    chosen: Sequence[Any], other_windows: List[str], previous: Sequence[Any],
                    task_names: List[str], answers: Sequence[Any]) -> str:
    L: List[str] = []
    L.append("About this person: %s" % (about_me.strip() or "(not provided yet)"))
    L.append("")
    L.append("Time window: %s %s–%s (%d minutes). Screenshots every %ds." % (
        pretty_day(day_of(ws)), hm(ws), hm(we), int(round((we - ws) / 60)), interval))
    L.append("")
    L.append("Front window log:")
    for r in rs[:40]:
        if r["group"] == "active":
            L.append("- %s–%s  %s" % (hms(r["start"]), hms(r["end"]), batching.describe_run(r)))
        elif r["group"] == "private":
            L.append("- %s–%s  (not captured: %s)" % (hms(r["start"]), hms(r["end"]),
                                                       REASON_LABELS.get(r["reason"] or "", "private")))
        elif r["group"] == "call":
            L.append("- %s–%s  (on a call, not captured) %s" % (hms(r["start"]), hms(r["end"]), batching.describe_run(r)))
    if len(rs) > 40:
        L.append("- … %d more short switches" % (len(rs) - 40))
    if other_windows:
        L.append("Other open windows: %s" % "; ".join(other_windows[:6]))
    L.append("")
    L.append("Screenshots attached, in order:")
    for i, f in enumerate(chosen, 1):
        title = (f["title"] or "").strip()
        L.append("%d. %s — %s%s%s" % (i, hms(f["ts"]), f["app"] or "?", (' — "%s"' % title[:100]) if title else "",
                                        (" — %s" % f["domain"]) if f["domain"] else ""))
    L.append("")
    if previous:
        L.append("Previous activity (most recent last):")
        for s in previous:
            L.append('- %s–%s "%s" [%s] %s' % (hm(s["start_ts"]), hm(s["end_ts"]), s["task"] or "?",
                                               s["category"] or "other", clip(s["summary"], 160)))
    else:
        L.append("Previous activity: none in the last few hours.")
    if task_names:
        L.append("Task names used so far today: %s" % "; ".join('"%s"' % t for t in task_names))
    if answers:
        L.append("What the person told me recently:")
        for a in answers:
            L.append("- %s Q: %s A: %s" % (hm(a["answered_at"] or a["created_at"]), clip(a["question"], 160),
                                           clip(a["answer"], 300)))
    L.append("")
    L.append(JSON_SPEC)
    return "\n".join(L)


class Describer:
    def __init__(self, db: DB, cfg, router: Router, checkins=None):
        self.db, self.cfg, self.router, self.checkins = db, cfg, router, checkins
        self.paused_until = 0.0
        self.last_ok = 0.0
        self.last_error = ""
        self.busy = False
        self.lock = threading.Lock()

    # ------------------------------------------------------------ public
    def run_forever(self, stop: threading.Event) -> None:
        while not stop.is_set():
            did = False
            try:
                if time.time() >= self.paused_until:
                    did = self.process_next(time.time())
            except Exception:
                log.exception("describer step failed")
            stop.wait(1.0 if did else 10.0)

    def behind_seconds(self, now: float) -> float:
        W = self.cfg.describe.window_minutes * 60
        last = self.db.last_batch_start()
        since = (last + W) if last is not None else 0
        f = self.db.first_frame_from(since)
        if f is None:
            return 0.0
        return max(0.0, now - batching.window_start(f["ts"], W) - W)

    def process_next(self, now: float = None) -> bool:
        now = time.time() if now is None else now
        with self.lock:
            W = self.cfg.describe.window_minutes * 60
            last = self.db.last_batch_start()
            since = (last + W) if last is not None else 0
            if now < self.paused_until:
                return False  # usage limit: wait, nothing is lost
            f = self.db.first_frame_from(since)
            if f is not None:
                ws = batching.window_start(f["ts"], W)
                we = ws + W
                if we <= now - 3:
                    self.busy = True
                    try:
                        return self._window(ws, we, now)
                    finally:
                        self.busy = False
            return self._retry_failed(now)

    # ----------------------------------------------------------- windows
    def _window(self, ws: float, we: float, now: float, batch_id: int = None) -> bool:
        """Describe one window. Returns False only when it was deferred (usage limit)."""
        cfg, db = self.cfg, self.db
        interval = cfg.capture.interval_seconds
        frames = db.frames_between(ws, we)
        rs = batching.runs(frames, interval)
        active = [f for f in frames if f["state"] in ACTIVE_STATES]
        side = self._side_segments(rs, interval)

        if batch_id is None and not active:
            with db.tx():
                bid = db.insert_batch(ws, we, "skipped", seconds=0.0)
                db.insert_segments(bid, side)
            return True

        prev_batch = db.previous_batch(ws)
        prev_seg = db.last_segment_of_batch(prev_batch["id"]) if prev_batch else None

        if batch_id is None and self._can_carry(ws, frames, active, prev_batch, prev_seg):
            seg = {k: prev_seg[k] for k in ("task", "summary", "category", "automation")}
            seg.update({"apps": jload(prev_seg["apps"], []), "systems": jload(prev_seg["systems"], []),
                        "artifacts": jload(prev_seg["artifacts"], []), "people": jload(prev_seg["people"], []),
                        "is_work": None if prev_seg["is_work"] is None else bool(prev_seg["is_work"]),
                        "start_ts": float(active[0]["ts"]), "end_ts": float(active[-1]["ts"]) + interval,
                        "seconds": len(active) * float(interval), "source": "carried"})
            with db.tx():
                bid = db.insert_batch(ws, we, "carried", seconds=seg["seconds"])
                db.insert_segments(bid, [seg] + side)
            self._prune(frames, keep_last_only=True)
            return True

        with_img = [f for f in active if f["image"] and os.path.exists(str(data_dir() / f["image"]))]
        if not with_img:
            seg = self._metadata_segment(rs, ws, we, active, interval)
            with db.tx():
                if batch_id is None:
                    bid = db.insert_batch(ws, we, "done", backend="metadata", seconds=seg["seconds"])
                else:
                    bid = batch_id
                    db.delete_segments(bid)
                    db.update_batch(bid, status="done", backend="metadata", seconds=seg["seconds"])
                db.insert_segments(bid, [seg] + side)
            return True

        chosen = batching.select_frames(with_img, interval, cfg.describe.max_images)
        about = (cfg.audit.about_me or db.meta_get("about_me") or "").strip()
        other: List[str] = []
        for f in reversed(chosen):
            for lab in jload(f["visible"], []) or []:
                if lab not in other:
                    other.append(lab)
        text = build_user_text(
            about_me=about, ws=ws, we=we, interval=interval, rs=rs, chosen=chosen, other_windows=other,
            previous=db.recent_work_segments(ws, 3), task_names=db.task_names(day_of(ws), ws, 12),
            answers=db.recent_answers(we, 4))
        paths = [str(data_dir() / f["image"]) for f in chosen]

        try:
            res = self.router.call("describe", SYSTEM_PROMPT, text, paths)
            parsed = parse_reply(res.text)
            if parsed is None:
                raise LLMError("reply was not the expected JSON: %s" % clip(res.text, 300), "parse")
        except LLMError as e:
            self.last_error = str(e)
            if e.kind == "rate_limit" and batch_id is None:
                cd = [v for v in self.router.cooldown_until.values() if v > time.time()]
                self.paused_until = min(cd) if cd else time.time() + 60 * cfg.describe.cooldown_minutes
                log.warning("describer paused until %s (usage limit)", hm(self.paused_until))
                return False  # leave the window pending; it will be described after the pause
            seg = self._metadata_segment(rs, ws, we, active, interval)
            with db.tx():
                if batch_id is None:
                    bid = db.insert_batch(ws, we, "failed", attempts=1, error=clip(str(e), 1000),
                                          images=[f["image"] for f in chosen], seconds=seg["seconds"])
                else:
                    bid = batch_id
                    row = db.one("SELECT attempts FROM batches WHERE id=?", (bid,))
                    db.delete_segments(bid)
                    db.update_batch(bid, status="failed", attempts=(row["attempts"] if row else 0) + 1,
                                    error=clip(str(e), 1000), seconds=seg["seconds"])
                db.insert_segments(bid, [seg] + side)
            return True

        segs = batching.place_segments(parsed["segments"], ws, we)
        batching.assign_seconds(segs, active, interval)
        for s in segs:
            s["source"] = "model"
        total = sum(s["seconds"] for s in segs)
        with db.tx():
            if batch_id is None:
                bid = db.insert_batch(ws, we, "done", backend="%s/%s" % (res.backend, res.model),
                                      images=[f["image"] for f in chosen], new_task=int(parsed["new_task"]),
                                      question=parsed["question"], raw=clip(res.text, 8000), seconds=total)
            else:
                bid = batch_id
                db.delete_segments(bid)
                db.update_batch(bid, status="done", backend="%s/%s" % (res.backend, res.model), error=None,
                                images=[f["image"] for f in chosen], new_task=int(parsed["new_task"]),
                                question=parsed["question"], raw=clip(res.text, 8000), seconds=total)
            db.insert_segments(bid, segs + side)
        self.last_ok = time.time()
        self.last_error = ""
        self._prune(frames, keep={f["id"] for f in chosen})

        # Ask a check-in question while the moment is fresh.
        if self.checkins is not None and batch_id is None and (now - we) < 15 * 60:
            task = segs[-1].get("task") if segs else None
            try:
                offered = False
                if parsed["new_task"] and parsed["question"]:
                    offered = self.checkins.offer(parsed["question"], "task_switch", task, now)
                if not offered:
                    offered = self.checkins.maybe_dry_spell(now, task, parsed["question"])
                if offered and parsed["question"]:
                    db.update_batch(bid, question_used=1)
            except Exception:
                log.exception("check-in offer failed")
        return True

    def _retry_failed(self, now: float) -> bool:
        if not any(b.available() for b in self.router.backends.values()):
            return False
        for b in self.db.failed_batches(now):
            log.info("retrying description for %s", hm(b["start_ts"]))
            return self._window(b["start_ts"], b["end_ts"], now, batch_id=b["id"])
        return False

    # ----------------------------------------------------------- helpers
    def _can_carry(self, ws: float, frames: Sequence[Any], active: Sequence[Any], prev_batch, prev_seg) -> bool:
        if prev_batch is None or prev_seg is None:
            return False
        if abs(prev_batch["end_ts"] - ws) > 1:
            return False  # only carry through continuous work
        last_prev = self.db.one("SELECT * FROM frames WHERE ts<? AND ts>=? AND state IN (%s) ORDER BY ts DESC LIMIT 1"
                                % ",".join("?" * len(ACTIVE_STATES)), [ws, prev_batch["start_ts"]] + list(ACTIVE_STATES))
        if last_prev is None:
            return False
        keys = {(f["app"], f["title"], f["domain"]) for f in active}
        if len(keys) != 1 or keys != {(last_prev["app"], last_prev["title"], last_prev["domain"])}:
            return False
        captured = [f for f in active if f["state"] == "captured"]
        if not captured:
            return True  # nothing changed on screen at all
        if any((f["diff"] if f["diff"] is not None else 255.0) >= CARRY_MAX_DIFF for f in captured):
            return False
        last_model = self.db.last_model_time(ws)
        return last_model is not None and ws - last_model < self.cfg.describe.carry_minutes * 60

    @staticmethod
    def _side_segments(rs: List[Dict[str, Any]], interval: int) -> List[Dict[str, Any]]:
        out: List[Dict[str, Any]] = []
        for r in rs:
            if r["group"] == "private":
                reason = r["reason"] or "private"
                label = REASON_LABELS.get(reason, "private")
                cat = "personal" if reason == "messaging" else ("finance" if reason == "banking" else "other")
                seg = {"task": "Not captured: %s" % label, "summary": "", "category": cat, "source": "private",
                       "is_work": None, "apps": [r["app"]] if r["app"] else []}
            elif r["group"] == "call":
                t = (r["title"] or "").strip()
                seg = {"task": "Call: %s" % (t[:100] if t else (r["app"] or "call")), "summary": "", "category": "meeting",
                       "source": "call", "is_work": None, "apps": [r["app"]] if r["app"] else []}
            else:
                continue
            seg.update({"start_ts": r["start"], "end_ts": r["end"], "seconds": len(r["frames"]) * float(interval)})
            if out and out[-1]["task"] == seg["task"] and seg["start_ts"] - out[-1]["end_ts"] < 60:
                out[-1]["end_ts"] = seg["end_ts"]
                out[-1]["seconds"] += seg["seconds"]
            else:
                out.append(seg)
        return out

    @staticmethod
    def _metadata_segment(rs, ws, we, active, interval) -> Dict[str, Any]:
        act = [r for r in rs if r["group"] == "active"]
        best = max(act, key=lambda r: len(r["frames"])) if act else None
        task = batching.describe_run(best) if best else "Unknown activity"
        return {"task": clip(task, 120), "summary": "No AI description available; from the window log only.",
                "category": "other", "apps": [best["app"]] if best and best["app"] else [], "source": "metadata",
                "is_work": None, "start_ts": float(active[0]["ts"]) if active else ws,
                "end_ts": (float(active[-1]["ts"]) + interval) if active else we,
                "seconds": len(active) * float(interval)}

    def _prune(self, frames: Sequence[Any], keep: set = None, keep_last_only: bool = False) -> None:
        if self.cfg.capture.keep_all_frames:
            return
        imgs = [f for f in frames if f["image"]]
        if keep_last_only:
            keep = {imgs[-1]["id"]} if imgs else set()
        keep = keep or set()
        drop = [f for f in imgs if f["id"] not in keep]
        for f in drop:
            try:
                os.remove(str(data_dir() / f["image"]))
            except OSError:
                pass
        self.db.clear_images(f["id"] for f in drop)
