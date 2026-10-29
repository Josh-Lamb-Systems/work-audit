from __future__ import annotations

import sqlite3
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence

from .util import day_bounds, day_of, jdump

# Frame states. ACTIVE = you were at the computer working in a capturable window.
ACTIVE_STATES = ("captured", "unchanged", "nowindow", "error")
SCHEMA = """
CREATE TABLE IF NOT EXISTS frames (
  id INTEGER PRIMARY KEY,
  ts REAL NOT NULL,
  day TEXT NOT NULL,
  state TEXT NOT NULL,          -- captured | unchanged | nowindow | error | private | call
  reason TEXT,
  app TEXT, bundle_id TEXT, title TEXT, url TEXT, domain TEXT,
  window_id INTEGER,
  image TEXT,                   -- path relative to the data folder (captured frames only)
  diff REAL,
  visible TEXT                  -- JSON list of other open windows (privacy-filtered)
);
CREATE INDEX IF NOT EXISTS frames_ts ON frames(ts);
CREATE INDEX IF NOT EXISTS frames_day ON frames(day);

CREATE TABLE IF NOT EXISTS batches (
  id INTEGER PRIMARY KEY,
  start_ts REAL NOT NULL UNIQUE,
  end_ts REAL NOT NULL,
  day TEXT NOT NULL,
  status TEXT NOT NULL,         -- done | carried | skipped (nothing capturable) | failed
  backend TEXT,
  attempts INTEGER NOT NULL DEFAULT 0,
  error TEXT,
  images TEXT,
  new_task INTEGER,
  question TEXT,
  question_used INTEGER NOT NULL DEFAULT 0,
  raw TEXT,
  seconds REAL,
  created_at REAL NOT NULL,
  updated_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS batches_day ON batches(day);

CREATE TABLE IF NOT EXISTS segments (
  id INTEGER PRIMARY KEY,
  batch_id INTEGER NOT NULL,
  day TEXT NOT NULL,
  start_ts REAL NOT NULL,
  end_ts REAL NOT NULL,
  seconds REAL NOT NULL DEFAULT 0,
  task TEXT, summary TEXT, category TEXT,
  apps TEXT, systems TEXT, artifacts TEXT, people TEXT,
  is_work INTEGER, automation TEXT,
  source TEXT NOT NULL          -- model | carried | metadata | private | call
);
CREATE INDEX IF NOT EXISTS segments_day ON segments(day);
CREATE INDEX IF NOT EXISTS segments_batch ON segments(batch_id);

CREATE TABLE IF NOT EXISTS checkins (
  id INTEGER PRIMARY KEY,
  day TEXT NOT NULL,
  created_at REAL NOT NULL,
  asked_at REAL,
  answered_at REAL,
  kind TEXT NOT NULL,           -- task_switch | dry_spell | manual | followup | profile | note
  task TEXT,
  question TEXT,
  answer TEXT,
  status TEXT NOT NULL          -- pending | answered | skipped | expired
);
CREATE INDEX IF NOT EXISTS checkins_day ON checkins(day);

CREATE TABLE IF NOT EXISTS followups (
  id INTEGER PRIMARY KEY,
  created_at REAL NOT NULL,
  day TEXT,
  question TEXT NOT NULL,
  used INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS events (
  id INTEGER PRIMARY KEY,
  ts REAL NOT NULL,
  day TEXT NOT NULL,
  kind TEXT NOT NULL,
  detail TEXT
);

CREATE TABLE IF NOT EXISTS reports (
  id INTEGER PRIMARY KEY,
  kind TEXT NOT NULL,           -- daily | audit
  day TEXT,
  path TEXT NOT NULL,
  backend TEXT,
  created_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
"""


class DB:
    """Small thread-safe wrapper around one SQLite connection (WAL mode)."""

    def __init__(self, path: Path):
        self.path = str(path)
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        self.conn = sqlite3.connect(self.path, timeout=30, isolation_level=None, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        with self.lock:
            self.conn.execute("PRAGMA journal_mode=WAL")
            self.conn.execute("PRAGMA synchronous=NORMAL")
            self.conn.executescript(SCHEMA)

    # ------------------------------------------------------------- basics
    def x(self, sql: str, params: Sequence[Any] = ()) -> sqlite3.Cursor:
        with self.lock:
            return self.conn.execute(sql, params)

    def q(self, sql: str, params: Sequence[Any] = ()) -> List[sqlite3.Row]:
        with self.lock:
            return self.conn.execute(sql, params).fetchall()

    def one(self, sql: str, params: Sequence[Any] = ()) -> Optional[sqlite3.Row]:
        with self.lock:
            return self.conn.execute(sql, params).fetchone()

    def val(self, sql: str, params: Sequence[Any] = (), default: Any = None) -> Any:
        r = self.one(sql, params)
        if r is None or r[0] is None:
            return default
        return r[0]

    @contextmanager
    def tx(self):
        with self.lock:
            self.conn.execute("BEGIN IMMEDIATE")
            try:
                yield self
            except BaseException:
                self.conn.execute("ROLLBACK")
                raise
            else:
                self.conn.execute("COMMIT")

    def close(self) -> None:
        with self.lock:
            self.conn.close()

    # --------------------------------------------------------------- meta
    def meta_get(self, key: str, default: Any = None) -> Any:
        return self.val("SELECT value FROM meta WHERE key=?", (key,), default)

    def meta_set(self, key: str, value: Any) -> None:
        self.x("INSERT INTO meta(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
               (key, None if value is None else str(value)))

    def event(self, kind: str, detail: Any = None, ts: float = None) -> None:
        ts = time.time() if ts is None else ts
        if detail is not None and not isinstance(detail, str):
            detail = jdump(detail)
        self.x("INSERT INTO events(ts,day,kind,detail) VALUES(?,?,?,?)", (ts, day_of(ts), kind, detail))

    # ------------------------------------------------------------- frames
    def insert_frame(self, ts: float, state: str, **f: Any) -> int:
        cols = ["ts", "day", "state"]
        vals: List[Any] = [ts, day_of(ts), state]
        for k in ("reason", "app", "bundle_id", "title", "url", "domain", "window_id", "image", "diff", "visible"):
            if k in f and f[k] is not None:
                cols.append(k)
                v = f[k]
                vals.append(jdump(v) if k == "visible" and not isinstance(v, str) else v)
        sql = "INSERT INTO frames(%s) VALUES(%s)" % (",".join(cols), ",".join("?" * len(cols)))
        return self.x(sql, vals).lastrowid

    def frames_between(self, a: float, b: float) -> List[sqlite3.Row]:
        return self.q("SELECT * FROM frames WHERE ts>=? AND ts<? ORDER BY ts", (a, b))

    def first_frame_from(self, ts: float) -> Optional[sqlite3.Row]:
        return self.one("SELECT * FROM frames WHERE ts>=? ORDER BY ts LIMIT 1", (ts,))

    def last_frame(self) -> Optional[sqlite3.Row]:
        return self.one("SELECT * FROM frames ORDER BY ts DESC LIMIT 1")

    def clear_images(self, frame_ids: Iterable[int]) -> None:
        ids = list(frame_ids)
        if not ids:
            return
        with self.lock:
            for i in range(0, len(ids), 500):
                chunk = ids[i:i + 500]
                self.conn.execute("UPDATE frames SET image=NULL WHERE id IN (%s)" % ",".join("?" * len(chunk)), chunk)

    def image_in_use_elsewhere(self, image: str, exclude_ids: Sequence[int]) -> bool:
        if not exclude_ids:
            return self.val("SELECT COUNT(*) FROM frames WHERE image=?", (image,), 0) > 0
        return self.val("SELECT COUNT(*) FROM frames WHERE image=? AND id NOT IN (%s)" % ",".join("?" * len(exclude_ids)),
                        [image] + list(exclude_ids), 0) > 0

    # ------------------------------------------------------------ batches
    def last_batch_start(self) -> Optional[float]:
        return self.val("SELECT MAX(start_ts) FROM batches")

    def insert_batch(self, start_ts: float, end_ts: float, status: str, **f: Any) -> int:
        now = time.time()
        cols = ["start_ts", "end_ts", "day", "status", "created_at", "updated_at"]
        vals: List[Any] = [start_ts, end_ts, day_of(start_ts), status, now, now]
        for k in ("backend", "attempts", "error", "images", "new_task", "question", "raw", "seconds"):
            if k in f and f[k] is not None:
                cols.append(k)
                v = f[k]
                vals.append(jdump(v) if k == "images" and not isinstance(v, str) else v)
        sql = "INSERT INTO batches(%s) VALUES(%s)" % (",".join(cols), ",".join("?" * len(cols)))
        return self.x(sql, vals).lastrowid

    def update_batch(self, batch_id: int, **f: Any) -> None:
        f["updated_at"] = time.time()
        if "images" in f and f["images"] is not None and not isinstance(f["images"], str):
            f["images"] = jdump(f["images"])
        cols = list(f.keys())
        self.x("UPDATE batches SET %s WHERE id=?" % ",".join("%s=?" % c for c in cols), [f[c] for c in cols] + [batch_id])

    def failed_batches(self, now: float, max_attempts: int = 3, backoff: float = 600.0) -> List[sqlite3.Row]:
        return self.q("SELECT * FROM batches WHERE status='failed' AND attempts<? AND updated_at < ? - attempts*? "
                      "ORDER BY start_ts LIMIT 5", (max_attempts, now, backoff))

    def previous_batch(self, start_ts: float) -> Optional[sqlite3.Row]:
        return self.one("SELECT * FROM batches WHERE start_ts<? AND status IN ('done','carried','failed') "
                        "ORDER BY start_ts DESC LIMIT 1", (start_ts,))

    def latest_unused_question(self, since: float) -> Optional[sqlite3.Row]:
        return self.one("SELECT * FROM batches WHERE question IS NOT NULL AND question!='' AND question_used=0 "
                        "AND end_ts>=? ORDER BY end_ts DESC LIMIT 1", (since,))

    # ----------------------------------------------------------- segments
    def insert_segments(self, batch_id: int, segs: List[Dict[str, Any]]) -> None:
        for s in segs:
            self.x(
                "INSERT INTO segments(batch_id,day,start_ts,end_ts,seconds,task,summary,category,apps,systems,artifacts,"
                "people,is_work,automation,source) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (batch_id, day_of(s["start_ts"]), s["start_ts"], s["end_ts"], s.get("seconds", 0.0), s.get("task"),
                 s.get("summary"), s.get("category"), jdump(s.get("apps") or []), jdump(s.get("systems") or []),
                 jdump(s.get("artifacts") or []), jdump(s.get("people") or []),
                 None if s.get("is_work") is None else int(bool(s.get("is_work"))), s.get("automation") or "",
                 s.get("source") or "model"))

    def delete_segments(self, batch_id: int) -> None:
        self.x("DELETE FROM segments WHERE batch_id=?", (batch_id,))

    def recent_work_segments(self, before_ts: float, limit: int = 3, within: float = 3 * 3600) -> List[sqlite3.Row]:
        rows = self.q("SELECT * FROM segments WHERE start_ts<? AND start_ts>=? AND source IN ('model','carried','metadata') "
                      "ORDER BY start_ts DESC, id DESC LIMIT ?", (before_ts, before_ts - within, limit))
        return list(reversed(rows))

    def last_segment_of_batch(self, batch_id: int) -> Optional[sqlite3.Row]:
        return self.one("SELECT * FROM segments WHERE batch_id=? AND source IN ('model','carried','metadata') "
                        "ORDER BY start_ts DESC, id DESC LIMIT 1", (batch_id,))

    def task_names(self, day: str, before_ts: float, limit: int = 12) -> List[str]:
        rows = self.q("SELECT task, SUM(seconds) s FROM segments WHERE day=? AND start_ts<? AND task IS NOT NULL "
                      "AND source IN ('model','carried') GROUP BY lower(task) ORDER BY s DESC LIMIT ?",
                      (day, before_ts, limit))
        return [r["task"] for r in rows]

    def last_model_time(self, before_ts: float) -> Optional[float]:
        return self.val("SELECT MAX(start_ts) FROM batches WHERE status='done' AND start_ts<?", (before_ts,))

    # ----------------------------------------------------------- checkins
    def insert_checkin(self, kind: str, question: str, task: str = None, status: str = "pending",
                       answer: str = None, ts: float = None) -> int:
        ts = time.time() if ts is None else ts
        asked = ts if status != "pending" else None
        answered = ts if status == "answered" else None
        return self.x("INSERT INTO checkins(day,created_at,asked_at,answered_at,kind,task,question,answer,status) "
                      "VALUES(?,?,?,?,?,?,?,?,?)",
                      (day_of(ts), ts, asked, answered, kind, task, question, answer, status)).lastrowid

    def update_checkin(self, cid: int, **f: Any) -> None:
        cols = list(f.keys())
        self.x("UPDATE checkins SET %s WHERE id=?" % ",".join("%s=?" % c for c in cols), [f[c] for c in cols] + [cid])

    def asked_today(self, day: str) -> int:
        return self.val("SELECT COUNT(*) FROM checkins WHERE day=? AND asked_at IS NOT NULL "
                        "AND kind NOT IN ('note','manual','profile')", (day,), 0)

    def last_question_ts(self) -> Optional[float]:
        return self.val("SELECT MAX(COALESCE(asked_at, created_at)) FROM checkins WHERE kind!='note'")

    def recent_answers(self, before_ts: float, limit: int = 4, within: float = 6 * 3600) -> List[sqlite3.Row]:
        rows = self.q("SELECT * FROM checkins WHERE status='answered' AND answer IS NOT NULL AND answer!='' "
                      "AND COALESCE(answered_at, created_at)<? AND COALESCE(answered_at, created_at)>=? "
                      "ORDER BY answered_at DESC LIMIT ?", (before_ts, before_ts - within, limit))
        return list(reversed(rows))

    def expire_stale_pending(self, older_than: float) -> None:
        self.x("UPDATE checkins SET status='expired' WHERE status='pending' AND created_at<?", (older_than,))

    # ---------------------------------------------------------- followups
    def add_followups(self, day: str, questions: List[str]) -> None:
        now = time.time()
        for q in questions:
            self.x("INSERT INTO followups(created_at,day,question) VALUES(?,?,?)", (now, day, q))

    def pop_followup(self) -> Optional[str]:
        with self.lock:
            r = self.one("SELECT id, question FROM followups WHERE used=0 ORDER BY created_at DESC, id LIMIT 1")
            if not r:
                return None
            self.x("UPDATE followups SET used=1 WHERE id=?", (r["id"],))
            return r["question"]

    # ------------------------------------------------------------ reports
    def add_report(self, kind: str, path: str, day: str = None, backend: str = None) -> None:
        self.x("INSERT INTO reports(kind,day,path,backend,created_at) VALUES(?,?,?,?,?)",
               (kind, day, path, backend, time.time()))

    def report_for(self, kind: str, day: str) -> Optional[sqlite3.Row]:
        return self.one("SELECT * FROM reports WHERE kind=? AND day=? ORDER BY created_at DESC LIMIT 1", (kind, day))

    def days_with_data(self) -> List[str]:
        return [r[0] for r in self.q("SELECT DISTINCT day FROM frames ORDER BY day")]

    # -------------------------------------------------------------- stats
    def day_stats(self, day: str, interval: float) -> Dict[str, Any]:
        rows = self.q("SELECT state, reason, COUNT(*) n FROM frames WHERE day=? GROUP BY state, reason", (day,))
        out: Dict[str, Any] = {"active": 0.0, "private": {}, "call": 0.0}
        for r in rows:
            secs = r["n"] * interval
            if r["state"] in ACTIVE_STATES:
                out["active"] += secs
            elif r["state"] == "private":
                out["private"][r["reason"] or "private"] = out["private"].get(r["reason"] or "private", 0.0) + secs
            elif r["state"] == "call":
                out["call"] += secs
        out["ai_calls"] = self.val("SELECT COUNT(*) FROM batches WHERE day=? AND status='done'", (day,), 0)
        out["ai_failed"] = self.val("SELECT COUNT(*) FROM batches WHERE day=? AND status='failed'", (day,), 0)
        out["asked"] = self.asked_today(day)
        out["answered"] = self.val("SELECT COUNT(*) FROM checkins WHERE day=? AND status='answered' AND kind!='note'",
                                   (day,), 0)
        out["notes"] = self.val("SELECT COUNT(*) FROM checkins WHERE day=? AND kind='note'", (day,), 0)
        a, b = day_bounds(day)
        out["first_ts"] = self.val("SELECT MIN(ts) FROM frames WHERE ts>=? AND ts<?", (a, b))
        out["last_ts"] = self.val("SELECT MAX(ts) FROM frames WHERE ts>=? AND ts<?", (a, b))
        return out
