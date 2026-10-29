"""Decides when to ask the 10-15 daily check-in questions. The UI shows them."""
from __future__ import annotations

import threading
import time
from typing import Any, Dict, Optional

from .db import DB
from .util import day_of, log

# The four questions from the audit method, plus a few that help later automation work.
QUESTION_BANK = [
    "What are you working on right now, and what is it for?",
    "Why are you working on this now? What triggered it?",
    "Why did you do that last step the way you did?",
    "What systems, tools or information do you need to make this happen?",
    "What happens next with this: who receives it, or what does it feed into?",
    "How often do you do this, and how long does it usually take?",
    "What would a great result look like here, and how do you judge it?",
]

PROFILE_QUESTION = ("Quick setup question: in a sentence or two, what is your business, your role in it, "
                    "and who are your clients? (This helps the AI describe your screen accurately.)")


class CheckinManager:
    def __init__(self, db: DB, cfg):
        self.db, self.cfg = db, cfg
        self.lock = threading.Lock()
        self.pending: Optional[Dict[str, Any]] = None
        self._restore()

    def _restore(self) -> None:
        # Questions left pending when the app quit are expired, not re-asked days later.
        self.db.expire_stale_pending(time.time() + 1)

    # ---------------------------------------------------------- policy
    def limits_ok(self, now: float, kind: str) -> bool:
        c = self.cfg.checkins
        if kind in ("manual", "profile", "note"):
            return True
        if not c.enabled:
            return False
        if self.db.asked_today(day_of(now)) >= c.max_per_day:
            return False
        last = self.db.last_question_ts()
        if last is not None and now - last < c.min_gap_minutes * 60:
            return False
        return True

    def bank_question(self, now: float) -> str:
        n = self.db.asked_today(day_of(now))
        return QUESTION_BANK[n % len(QUESTION_BANK)]

    # ---------------------------------------------------------- offers
    def offer(self, question: Optional[str], kind: str, task: Optional[str] = None, now: float = None) -> bool:
        now = time.time() if now is None else now
        if not question:
            return False
        with self.lock:
            if self.pending is not None:
                return False
            if not self.limits_ok(now, kind):
                return False
            cid = self.db.insert_checkin(kind, question, task, status="pending", ts=now)
            self.pending = {"id": cid, "question": question, "kind": kind, "task": task,
                            "created_at": now, "not_before": now}
            log.info("check-in queued (%s): %s", kind, question)
            return True

    def maybe_dry_spell(self, now: float, task: Optional[str], model_question: Optional[str] = None) -> bool:
        c = self.cfg.checkins
        last = self.db.last_question_ts()
        if last is not None and now - last < c.dry_spell_minutes * 60:
            return False
        if not self.limits_ok(now, "dry_spell"):
            return False
        if model_question:
            return self.offer(model_question, "dry_spell", task, now)
        fq = self.db.pop_followup()
        if fq:
            return self.offer(fq, "followup", task, now)
        return self.offer(self.bank_question(now), "dry_spell", task, now)

    def ask_now(self, now: float = None, task: Optional[str] = None) -> bool:
        """Menu item 'Ask me a question now'."""
        now = time.time() if now is None else now
        b = self.db.latest_unused_question(now - 20 * 60)
        if b is not None:
            self.db.update_batch(b["id"], question_used=1)
            seg = self.db.last_segment_of_batch(b["id"])
            return self.offer(b["question"], "manual", seg["task"] if seg else task, now)
        q = self.db.pop_followup() or self.bank_question(now)
        return self.offer(q, "manual", task, now)

    def ensure_profile(self, about_me_configured: str) -> None:
        if (about_me_configured or "").strip() or (self.db.meta_get("about_me") or "").strip():
            return
        if self.db.val("SELECT COUNT(*) FROM checkins WHERE kind='profile' AND status='answered'", (), 0):
            return
        self.offer(PROFILE_QUESTION, "profile", None)

    # ---------------------------------------------------------- UI side
    def next_to_show(self, now: float) -> Optional[Dict[str, Any]]:
        with self.lock:
            p = self.pending
            if p is None:
                return None
            if now - p["created_at"] > self.cfg.checkins.expire_minutes * 60 and p["kind"] not in ("profile",):
                self.db.update_checkin(p["id"], status="expired")
                self.pending = None
                return None
            if now < p["not_before"]:
                return None
            return dict(p)

    def mark_asked(self, p: Dict[str, Any], now: float = None) -> None:
        now = time.time() if now is None else now
        row = self.db.one("SELECT asked_at FROM checkins WHERE id=?", (p["id"],))
        if row is not None and row["asked_at"] is None:
            self.db.update_checkin(p["id"], asked_at=now)

    def resolve(self, p: Dict[str, Any], status: str, answer: Optional[str] = None, now: float = None) -> None:
        now = time.time() if now is None else now
        with self.lock:
            fields: Dict[str, Any] = {"status": status}
            if status == "answered":
                fields.update(answer=answer, answered_at=now)
            self.db.update_checkin(p["id"], **fields)
            if p["kind"] == "profile" and status == "answered" and answer:
                self.db.meta_set("about_me", answer.strip())
            if self.pending is not None and self.pending["id"] == p["id"]:
                self.pending = None

    def snooze(self, p: Dict[str, Any], seconds: float, now: float = None) -> None:
        now = time.time() if now is None else now
        with self.lock:
            if self.pending is not None and self.pending["id"] == p["id"]:
                self.pending["not_before"] = now + seconds
                # give a snoozed question a fresh expiry window
                self.pending["created_at"] = max(self.pending["created_at"], now)

    def add_note(self, text: str, task: Optional[str] = None) -> None:
        self.db.insert_checkin("note", "Note", task, status="answered", answer=text.strip())
