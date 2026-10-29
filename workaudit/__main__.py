"""Command line: `bin/workaudit <command>` (run `bin/workaudit --help`)."""
from __future__ import annotations

import argparse
import csv
import fcntl
import shutil
import sys
import time
from pathlib import Path

from . import __version__
from .config import Config, set_values
from .db import DB
from .util import ROOT, data_dir, fmt_dur, hm, jload, setup_logging, today


def _day_arg(s: str) -> str:
    if s in ("today", ""):
        return today()
    if s == "yesterday":
        return time.strftime("%Y-%m-%d", time.localtime(time.time() - 86400))
    time.strptime(s, "%Y-%m-%d")
    return s


def _app_running() -> bool:
    p = data_dir() / ".app.lock"
    if not p.exists():
        return False
    with open(str(p), "a") as fh:
        try:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            return True
        fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
    return False


def cmd_status(cfg: Config, db: DB, args) -> int:
    from .llm import Router
    d = today()
    st = db.day_stats(d, float(cfg.capture.interval_seconds))
    print("Work Audit %s — data in %s" % (__version__, data_dir()))
    print("App running: %s" % ("yes" if _app_running() else "no"))
    print("Today: %s active · AI descriptions %d (failed %d) · check-ins %d asked, %d answered · notes %d" % (
        fmt_dur(st["active"]), st["ai_calls"], st["ai_failed"], st["asked"], st["answered"], st["notes"]))
    if st["private"]:
        print("Not captured: " + ", ".join("%s %s" % (k, fmt_dur(v)) for k, v in st["private"].items()))
    if st["call"]:
        print("Calls: %s" % fmt_dur(st["call"]))
    r = Router(cfg)
    for name, s in r.status().items():
        print("%s: %s" % (name, s["path"] or "not found"))
    print("Audit started: %s" % (db.meta_get("audit_started_day") or "not yet"))
    for e in cfg.errors:
        print("config warning: %s" % e)
    return 0


def cmd_describe(cfg: Config, db: DB, args) -> int:
    from .describe import Describer
    from .llm import Router
    if _app_running():
        print("The menu bar app is running and already describes your activity; nothing to do.")
        return 0
    desc = Describer(db, cfg, Router(cfg))
    n = 0
    while desc.process_next(time.time()):
        n += 1
        if desc.paused_until > time.time():
            print("Paused: usage limit reached. %s" % desc.last_error)
            break
        if args.max and n >= args.max:
            break
    print("Processed %d window(s)." % n)
    return 0


def cmd_digest(cfg: Config, db: DB, args) -> int:
    from .reports import build_digest
    sys.stdout.write(build_digest(db, cfg, _day_arg(args.day)))
    return 0


def cmd_report(cfg: Config, db: DB, args) -> int:
    from .llm import Router
    from .reports import generate_daily
    day = _day_arg(args.day)
    print("Writing the report for %s with %s (this can take a few minutes)…" % (day, cfg.reports.backend))
    path, backend = generate_daily(db, cfg, Router(cfg), day)
    print("Done (%s): %s" % (backend, path))
    return 0


def cmd_audit(cfg: Config, db: DB, args) -> int:
    from .llm import Router
    from .reports import generate_audit, generate_daily, missing_daily_reports
    router = Router(cfg)
    for d in missing_daily_reports(db, cfg):
        print("Writing missing daily report for %s…" % d)
        try:
            generate_daily(db, cfg, router, d)
        except Exception as e:
            print("  failed: %s" % e)
    print("Writing the full audit report with %s (%s, effort %s). This can take 10–30 minutes…" % (
        cfg.reports.backend, cfg.reports.audit_model, cfg.reports.audit_effort))
    path, backend = generate_audit(db, cfg, router)
    print("Done (%s): %s" % (backend, path))
    return 0


def cmd_selftest(cfg: Config, db: DB, args) -> int:
    from .llm import Router
    from . import selftest
    which = None if args.backend == "both" else [args.backend]
    print("Asking the AI to describe a sample screenshot (a few seconds to a minute)…")
    ok, lines = selftest.run(Router(cfg), which)
    for ln in lines:
        print(ln)
    return 0 if ok else 1


def cmd_export(cfg: Config, db: DB, args) -> int:
    out = Path(args.out or (data_dir() / "export" / time.strftime("%Y-%m-%d-%H%M")))
    out.mkdir(parents=True, exist_ok=True)
    with open(str(out / "segments.csv"), "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["day", "start", "end", "minutes", "task", "category", "summary", "apps", "systems", "artifacts",
                    "people", "is_work", "automation_notes", "source"])
        for s in db.q("SELECT * FROM segments ORDER BY start_ts, id"):
            w.writerow([s["day"], hm(s["start_ts"]), hm(s["end_ts"]), round((s["seconds"] or 0) / 60, 1), s["task"],
                        s["category"], s["summary"], "; ".join(jload(s["apps"], [])), "; ".join(jload(s["systems"], [])),
                        "; ".join(jload(s["artifacts"], [])), "; ".join(jload(s["people"], [])), s["is_work"],
                        s["automation"], s["source"]])
    with open(str(out / "checkins.csv"), "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["day", "time", "kind", "task", "question", "answer", "status"])
        for c in db.q("SELECT * FROM checkins ORDER BY created_at"):
            w.writerow([c["day"], hm(c["answered_at"] or c["asked_at"] or c["created_at"]), c["kind"], c["task"],
                        c["question"], c["answer"], c["status"]])
    print("Exported to %s" % out)
    return 0


def cmd_setpaths(cfg: Config, db: DB, args) -> int:
    """Used by install.sh: record where claude/codex live and pick sensible backends."""
    path = cfg.path
    if not path.exists():
        example = ROOT / "config.example.toml"
        if example.exists():
            shutil.copy(str(example), str(path))
        else:
            path.write_text("", encoding="utf-8")
    claude = args.claude or ""
    codex = args.codex or ""
    upd = {"paths": {"claude": claude, "codex": codex, "extra_path": args.extra_path or ""}}
    cfg.load()
    for sec in ("describe", "reports"):
        backend, fallback = cfg.data[sec]["backend"], cfg.data[sec]["fallback"]
        have = {"claude": bool(claude), "codex": bool(codex)}
        if not have.get(backend):
            other = "codex" if backend == "claude" else "claude"
            if have.get(other):
                upd.setdefault(sec, {})["backend"] = other
                upd[sec]["fallback"] = ""
        elif fallback and not have.get(fallback):
            upd.setdefault(sec, {})["fallback"] = ""
    set_values(path, upd)
    print("Saved settings to %s" % path)
    return 0


def cmd_new_period(cfg: Config, db: DB, args) -> int:
    db.meta_set("audit_started_day", today())
    db.meta_set("audit_extra_days", 0)
    db.meta_set("audit_end_prompted", "")
    print("A new %d-day audit period starts today. Restart the app for it to take effect." % cfg.audit.days)
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="workaudit", description="Work Audit — screen-based work audit for macOS")
    ap.add_argument("--version", action="version", version=__version__)
    sub = ap.add_subparsers(dest="cmd")
    sub.add_parser("run", help="start the menu bar app (normally started by opening Work Audit.app)")
    sub.add_parser("status", help="show today's numbers and setup")
    p = sub.add_parser("describe", help="describe recorded activity now (when the app isn't running)")
    p.add_argument("--max", type=int, default=0)
    p = sub.add_parser("digest", help="print the raw data for a day (no AI)")
    p.add_argument("day", nargs="?", default="today")
    p = sub.add_parser("report", help="write the daily report for a day")
    p.add_argument("day", nargs="?", default="today")
    sub.add_parser("audit", help="write the full audit report (process inventory for the next step)")
    p = sub.add_parser("selftest", help="check that Claude Code / Codex can describe a screenshot")
    p.add_argument("--backend", choices=["claude", "codex", "both"], default="both")
    p = sub.add_parser("export", help="export segments and check-ins as CSV")
    p.add_argument("out", nargs="?")
    p = sub.add_parser("setpaths", help="(used by install.sh) record where claude/codex are installed")
    p.add_argument("--claude", default="")
    p.add_argument("--codex", default="")
    p.add_argument("--extra-path", default="")
    sub.add_parser("new-period", help="start a fresh audit period today")
    args = ap.parse_args(argv)
    if args.cmd in (None, "run"):
        if sys.platform != "darwin":
            print("The menu bar app runs on macOS only.")
            return 1
        from .app import run_app
        return run_app()
    setup_logging(to_stderr=False)
    cfg = Config()
    db = DB(data_dir() / "audit.db")
    handlers = {"status": cmd_status, "describe": cmd_describe, "digest": cmd_digest, "report": cmd_report,
                "audit": cmd_audit, "selftest": cmd_selftest, "export": cmd_export, "setpaths": cmd_setpaths,
                "new-period": cmd_new_period}
    try:
        return handlers[args.cmd](cfg, db, args)
    except KeyboardInterrupt:
        return 130
    except Exception as e:
        print("Error: %s" % e, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
