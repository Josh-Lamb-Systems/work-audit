"""Runs Claude Code (`claude -p`) or Codex (`codex exec`) headless, using your plan."""
from __future__ import annotations

import base64
import json
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence

from .util import data_dir, log


class LLMError(Exception):
    def __init__(self, message: str, kind: str = "error"):
        super().__init__(message)
        self.kind = kind  # rate_limit | auth | model | timeout | missing | error


@dataclass
class LLMResult:
    text: str
    backend: str
    model: str = ""
    seconds: float = 0.0
    usage: Dict[str, Any] = field(default_factory=dict)


def classify_error(text: str) -> str:
    t = (text or "").lower()
    if any(s in t for s in ("rate limit", "rate_limit", "usage limit", "limit reached", "too many requests",
                            " 429", "quota", "resets at", "limit will reset", "out of extra usage")):
        return "rate_limit"
    if any(s in t for s in ("not logged in", "authentication", "unauthorized", " 401", "/login", "log in",
                            "login required", "invalid api key", "invalid x-api-key", "credentials")):
        return "auth"
    if any(s in t for s in ("model_not_found", "not_found_error", "invalid model", "unknown model",
                            "model not found", "does not exist", "not available for your", "model is not supported")):
        return "model"
    return "error"


def extract_json(text: str) -> Optional[Any]:
    """Find the first JSON object in a model reply (tolerates code fences and stray prose)."""
    if not text:
        return None
    s = text.strip()
    s = re.sub(r"^```(?:json)?\s*", "", s)
    s = re.sub(r"\s*```\s*$", "", s)
    try:
        return json.loads(s)
    except ValueError:
        pass
    start = s.find("{")
    while start != -1:
        depth, in_str, esc = 0, False, False
        for i in range(start, len(s)):
            c = s[i]
            if in_str:
                if esc:
                    esc = False
                elif c == "\\":
                    esc = True
                elif c == '"':
                    in_str = False
                continue
            if c == '"':
                in_str = True
            elif c == "{":
                depth += 1
            elif c == "}":
                depth -= 1
                if depth == 0:
                    try:
                        return json.loads(s[start:i + 1])
                    except ValueError:
                        break
        start = s.find("{", start + 1)
    return None


def make_env(extra_path: str = "") -> Dict[str, str]:
    env = dict(os.environ)
    if extra_path:
        env["PATH"] = extra_path + os.pathsep + env.get("PATH", "")
    env["NO_COLOR"] = "1"
    env["CLAUDE_CODE_SKIP_PROMPT_HISTORY"] = "1"   # don't fill your Claude history with audit calls
    return env


def _work_dir() -> str:
    d = data_dir() / "tmp" / "cli"
    d.mkdir(parents=True, exist_ok=True)
    return str(d)


def _resolve(path: str, name: str, env: Dict[str, str]) -> Optional[str]:
    if path and os.path.isfile(os.path.expanduser(path)) and os.access(os.path.expanduser(path), os.X_OK):
        return os.path.expanduser(path)
    return shutil.which(name, path=env.get("PATH"))


class _Backend:
    name = "?"
    help_args: List[str] = []

    def __init__(self, path: str, env: Dict[str, str]):
        self.env = env
        self.path = _resolve(path, self.name, env)
        self._flags: Optional[set] = None
        self._lock = threading.Lock()

    def available(self) -> bool:
        return bool(self.path)

    def flags(self) -> set:
        with self._lock:
            if self._flags is None:
                out = ""
                try:
                    p = subprocess.run([self.path] + self.help_args, capture_output=True, text=True, timeout=60,
                                       env=self.env, cwd=_work_dir())
                    out = (p.stdout or "") + (p.stderr or "")
                except Exception as e:  # pragma: no cover
                    log.warning("%s --help failed: %s", self.name, e)
                self._flags = set(re.findall(r"--[a-zA-Z][a-zA-Z0-9-]+", out))
            return self._flags


class ClaudeCLI(_Backend):
    name = "claude"
    help_args = ["--help"]

    def run(self, system: str, text: str, images: Sequence[str] = (), model: str = "", effort: str = "",
            timeout: int = 300) -> LLMResult:
        if not self.path:
            raise LLMError("Claude Code CLI not found", "missing")
        f = self.flags()
        cmd = [self.path, "-p", "--input-format", "stream-json", "--output-format", "stream-json", "--verbose"]
        if model:
            cmd += ["--model", model]
        if effort and "--effort" in f:
            cmd += ["--effort", effort]
        if "--safe-mode" in f:
            cmd.append("--safe-mode")  # skip CLAUDE.md, hooks, plugins, MCP servers: faster, fewer tokens
        if "--strict-mcp-config" in f:
            cmd.append("--strict-mcp-config")
        if "--no-session-persistence" in f:
            cmd.append("--no-session-persistence")
        if "--disable-slash-commands" in f:
            cmd.append("--disable-slash-commands")
        if "--tools" in f:
            cmd += ["--tools", ""]
        if "--disallowedTools" in f:
            cmd += ["--disallowedTools", "mcp__*"]
        if system:
            cmd += ["--system-prompt", system]
        content: List[Dict[str, Any]] = [{"type": "text", "text": text}]
        for p in images:
            with open(p, "rb") as fh:
                data = base64.b64encode(fh.read()).decode("ascii")
            mt = "image/png" if str(p).lower().endswith(".png") else "image/jpeg"
            content.append({"type": "image", "source": {"type": "base64", "media_type": mt, "data": data}})
        msg = {"type": "user", "message": {"role": "user", "content": content}, "parent_tool_use_id": None}
        t0 = time.time()
        try:
            proc = subprocess.run(cmd, input=json.dumps(msg) + "\n", capture_output=True, text=True,
                                  timeout=timeout, env=self.env, cwd=_work_dir())
        except subprocess.TimeoutExpired:
            raise LLMError("claude timed out after %ss" % timeout, "timeout")
        result = None
        for line in (proc.stdout or "").splitlines():
            line = line.strip()
            if not line.startswith("{"):
                continue
            try:
                d = json.loads(line)
            except ValueError:
                continue
            if d.get("type") == "result":
                result = d
        if result is None:
            tail = ((proc.stderr or "") + "\n" + (proc.stdout or "")).strip()[-1200:]
            raise LLMError("claude exited %s: %s" % (proc.returncode, tail), classify_error(tail))
        if result.get("is_error") or result.get("subtype") not in (None, "success"):
            m = "%s %s %s" % (result.get("result") or "", result.get("subtype") or "", result.get("api_error_status") or "")
            raise LLMError("claude: " + m.strip()[:1200], classify_error(m))
        return LLMResult(text=str(result.get("result") or ""), backend="claude", model=model or "default",
                         seconds=time.time() - t0, usage=result.get("usage") or {})


class CodexCLI(_Backend):
    name = "codex"
    help_args = ["exec", "--help"]

    def run(self, system: str, text: str, images: Sequence[str] = (), model: str = "", effort: str = "",
            timeout: int = 300) -> LLMResult:
        if not self.path:
            raise LLMError("Codex CLI not found", "missing")
        f = self.flags()
        t0 = time.time()
        with tempfile.TemporaryDirectory(dir=_work_dir()) as td:
            out = os.path.join(td, "last_message.txt")
            cmd = [self.path, "exec", "--skip-git-repo-check", "--color", "never", "-s", "read-only",
                   "-C", td, "-o", out]
            if "--ephemeral" in f:
                cmd.append("--ephemeral")
            if model:
                cmd += ["-m", model]
            if effort:
                cmd += ["-c", 'model_reasoning_effort="%s"' % effort]
            for p in images:
                cmd.append("--image=%s" % p)
            prompt = (system.strip() + "\n\n" + text) if system else text
            try:
                proc = subprocess.run(cmd, input=prompt, capture_output=True, text=True, timeout=timeout,
                                      env=self.env, cwd=td)
            except subprocess.TimeoutExpired:
                raise LLMError("codex timed out after %ss" % timeout, "timeout")
            reply = ""
            if os.path.exists(out):
                with open(out, encoding="utf-8", errors="replace") as fh:
                    reply = fh.read()
        if proc.returncode != 0 or not reply.strip():
            tail = ((proc.stderr or "") + "\n" + (proc.stdout or "")).strip()[-1200:]
            raise LLMError("codex exited %s: %s" % (proc.returncode, tail), classify_error(tail))
        return LLMResult(text=reply, backend="codex", model=model or "default", seconds=time.time() - t0)


class Router:
    """Calls the configured backend, falls back to the other one, and backs off after usage limits."""

    def __init__(self, cfg):
        self.cfg = cfg
        self.env = make_env(cfg.paths.extra_path)
        self.backends = {
            "claude": ClaudeCLI(cfg.paths.claude, self.env),
            "codex": CodexCLI(cfg.paths.codex, self.env),
        }
        self.cooldown_until: Dict[str, float] = {}
        self.last_error = ""

    def reload(self, cfg) -> None:
        self.__init__(cfg)

    def status(self) -> Dict[str, Any]:
        now = time.time()
        return {name: {"available": b.available(), "path": b.path,
                       "cooldown_until": self.cooldown_until.get(name, 0) if self.cooldown_until.get(name, 0) > now else 0}
                for name, b in self.backends.items()}

    def _order(self, primary: str, fallback: str) -> List[str]:
        order = [primary] + ([fallback] if fallback and fallback != primary else [])
        return [n for n in order if self.backends[n].available()]

    def _params(self, purpose: str, name: str) -> Dict[str, Any]:
        if purpose == "describe":
            d = self.cfg.describe
            return {"model": d.claude_model if name == "claude" else d.codex_model,
                    "effort": d.claude_effort if name == "claude" else d.codex_effort,
                    "timeout": d.timeout_seconds}
        r = self.cfg.reports
        if name == "codex":
            return {"model": r.codex_model, "effort": r.codex_effort, "timeout": r.timeout_seconds}
        if purpose == "audit":
            return {"model": r.audit_model, "effort": r.audit_effort, "timeout": r.timeout_seconds}
        return {"model": r.daily_model, "effort": r.daily_effort, "timeout": r.timeout_seconds}

    def call(self, purpose: str, system: str, text: str, images: Sequence[str] = ()) -> LLMResult:
        sec = self.cfg.describe if purpose == "describe" else self.cfg.reports
        order = self._order(sec.backend, sec.fallback)
        if not order:
            raise LLMError("Neither Claude Code nor Codex was found. Re-run install.sh or set [paths] in config.toml.",
                           "missing")
        now = time.time()
        errors: List[str] = []
        kinds: List[str] = []
        tried = 0
        for name in order:
            if self.cooldown_until.get(name, 0) > now:
                errors.append("%s: paused after a usage limit until %s" % (
                    name, time.strftime("%H:%M", time.localtime(self.cooldown_until[name]))))
                kinds.append("rate_limit")
                continue
            prm = self._params(purpose, name)
            models = [prm["model"]]
            if name == "claude" and prm["model"] in ("best", "fable"):
                models.append("opus")  # not every plan/version has Fable
            for model in models:
                tried += 1
                try:
                    res = self.backends[name].run(system, text, images, model=model, effort=prm["effort"],
                                                  timeout=int(prm["timeout"]))
                    self.last_error = ""
                    return res
                except LLMError as e:
                    log.warning("%s (%s/%s) failed: %s", purpose, name, model or "default", e)
                    errors.append("%s: %s" % (name, e))
                    kinds.append(e.kind)
                    if e.kind == "rate_limit":
                        self.cooldown_until[name] = time.time() + 60 * self.cfg.describe.cooldown_minutes
                        break
                    if e.kind != "model":
                        break
        self.last_error = errors[-1] if errors else "unknown error"
        kind = "rate_limit" if kinds and all(k == "rate_limit" for k in kinds) else (kinds[-1] if kinds else "error")
        raise LLMError(" | ".join(errors), kind)
