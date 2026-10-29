import json
import os
import stat
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
FAKES = ROOT / "tests" / "fakes"


@pytest.fixture
def env(tmp_path, monkeypatch):
    """Isolated data dir + config pointing at the fake CLIs."""
    for f in FAKES.iterdir():
        f.chmod(f.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    data = tmp_path / "data"
    cfgp = tmp_path / "config.toml"
    log = tmp_path / "calls.jsonl"
    cfgp.write_text(
        '[paths]\nclaude = "%s"\ncodex = "%s"\n[audit]\nabout_me = "I run a small lead-gen agency."\n'
        % (FAKES / "claude", FAKES / "codex"), encoding="utf-8")
    monkeypatch.setenv("WORKAUDIT_DATA", str(data))
    monkeypatch.setenv("WORKAUDIT_CONFIG", str(cfgp))
    monkeypatch.setenv("FAKE_LOG", str(log))
    for k in ("FAKE_CLAUDE_MODE", "FAKE_CODEX_MODE", "FAKE_STYLE"):
        monkeypatch.delenv(k, raising=False)

    class E:
        pass
    e = E()
    e.data, e.cfgp, e.log, e.tmp = data, cfgp, log, tmp_path

    def calls():
        if not log.exists():
            return []
        return [json.loads(x) for x in log.read_text().splitlines() if x.strip()]
    e.calls = calls
    return e


@pytest.fixture
def cfg(env):
    from workaudit.config import Config
    return Config()


@pytest.fixture
def db(env):
    from workaudit.db import DB
    from workaudit.util import data_dir
    d = DB(data_dir() / "audit.db")
    yield d
    d.close()
