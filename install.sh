#!/bin/bash
# Work Audit installer. Run it from Terminal inside the unzipped folder:
#   bash install.sh
# Safe to run again (e.g. after moving the folder or updating Claude Code / Codex).
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT"
APP="$ROOT/Work Audit.app"
BUNDLE_ID="com.workaudit.app"

bold() { printf "\033[1m%s\033[0m\n" "$*"; }
ok()   { printf "  \033[32m✓\033[0m %s\n" "$*"; }
warn() { printf "  \033[33m!\033[0m %s\n" "$*"; }
die()  { printf "\n\033[31m✗ %s\033[0m\n" "$*" >&2; exit 1; }
ask()  {  # prompt on the terminal; with no terminal, return "" (= the default answer)
  local a=""
  if (exec </dev/tty) 2>/dev/null; then read -r -p "$1 " a </dev/tty || a=""; fi
  printf "%s" "$a"
}

bold "Work Audit installer"
[[ "$(uname -s)" == "Darwin" ]] || die "Work Audit runs on macOS only."
[[ "$(id -u)" != "0" ]] || die "Don't run this with sudo."

# ---------------------------------------------------------------- location
case "$ROOT/" in
  "$HOME/Desktop/"*|"$HOME/Documents/"*|"$HOME/Downloads/"*|"$HOME/Library/Mobile Documents/"*)
    warn "This folder is inside Desktop/Documents/Downloads/iCloud. macOS would then ask Work Audit"
    warn "for extra folder permissions. Recommended: move it to your home folder first, e.g."
    warn "    mv \"$ROOT\" ~/work-audit && bash ~/work-audit/install.sh"
    a="$(ask "Continue here anyway? [y/N]")"
    [[ "$a" =~ ^[Yy] ]] || exit 0
    ;;
esac
xattr -dr com.apple.quarantine "$ROOT" 2>/dev/null || true
chmod +x "$ROOT/bin/workaudit" "$ROOT/install.sh" "$ROOT/uninstall.sh" 2>/dev/null || true
mkdir -p "$ROOT/data/logs"

# ------------------------------------------------------------ build tools
bold "1/5 Checking Apple's command line tools"
if ! xcode-select -p >/dev/null 2>&1 || ! command -v clang >/dev/null 2>&1; then
  warn "Apple's command line tools are needed (to build the small Work Audit app and for Python)."
  xcode-select --install >/dev/null 2>&1 || true
  die "Finish the install window that just opened, then run: bash install.sh"
fi
ok "found"

# ---------------------------------------------------------------- python
bold "2/5 Setting up Python"
PY=""
if command -v uv >/dev/null 2>&1; then
  if [ ! -x .venv/bin/python ]; then
    uv venv --quiet --python 3.12 .venv 2>/dev/null || uv venv --quiet .venv
  fi
  uv pip install --quiet --python .venv/bin/python -r requirements.txt
  ok "Python environment ready (uv)"
else
  for c in /opt/homebrew/bin/python3 /usr/local/bin/python3 python3.13 python3.12 python3.11 python3.10 python3; do
    if command -v "$c" >/dev/null 2>&1 && "$c" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)' 2>/dev/null; then
      PY="$(command -v "$c")"; break
    fi
  done
  [ -n "$PY" ] || die "Python 3.9 or newer not found. Install it with 'brew install python' (or install uv) and run again."
  if [ ! -x .venv/bin/python ]; then "$PY" -m venv .venv; fi
  .venv/bin/python -m pip install --quiet --upgrade pip
  .venv/bin/python -m pip install --quiet -r requirements.txt
  ok "Python environment ready ($("$PY" --version 2>&1))"
fi
.venv/bin/python -c "import rumps, Quartz, AppKit, PIL" || die "Python packages failed to import; see the messages above."

# ------------------------------------------------------- find claude/codex
bold "3/5 Finding Claude Code and Codex"
LOGIN_SHELL="${SHELL:-/bin/zsh}"
if [[ "$(basename "$LOGIN_SHELL")" == "fish" ]]; then
  PATH_CMD='printf "__WAPATH__%s\n" (string join : $PATH)'
else
  PATH_CMD='printf "__WAPATH__%s\n" "$PATH"'
fi
LOGIN_PATH="$("$LOGIN_SHELL" -lic "$PATH_CMD" </dev/null 2>/dev/null | sed -n 's/^__WAPATH__//p' | tail -n 1 || true)"
[ -n "$LOGIN_PATH" ] || LOGIN_PATH="$PATH"
find_bin() {
  local name="$1" p=""
  p="$(PATH="$LOGIN_PATH" command -v "$name" 2>/dev/null || true)"
  if [ -z "$p" ]; then
    for c in "$HOME/.local/bin/$name" "$HOME/.claude/local/$name" "/opt/homebrew/bin/$name" "/usr/local/bin/$name" \
             "$HOME/.npm-global/bin/$name" "$HOME/.bun/bin/$name" "$HOME/.volta/bin/$name"; do
      if [ -x "$c" ]; then p="$c"; break; fi
    done
  fi
  printf "%s" "$p"
}
CLAUDE_BIN="$(find_bin claude)"
CODEX_BIN="$(find_bin codex)"
EXTRA_PATH="$LOGIN_PATH"
for b in "$CLAUDE_BIN" "$CODEX_BIN"; do
  [ -n "$b" ] && EXTRA_PATH="$(dirname "$b"):$EXTRA_PATH"
done
EXTRA_PATH="$(printf "%s" "$EXTRA_PATH" | awk -v RS=: -v ORS=: '$0 != "" && !seen[$0]++' | sed 's/:$//')"
GUI_ENV=(env -i HOME="$HOME" USER="${USER:-}" LOGNAME="${LOGNAME:-}" TMPDIR="${TMPDIR:-/tmp}" PATH="$EXTRA_PATH" LANG="${LANG:-en_US.UTF-8}")

if [ -n "$CLAUDE_BIN" ]; then
  ok "claude: $CLAUDE_BIN ($("${GUI_ENV[@]}" "$CLAUDE_BIN" --version 2>/dev/null | head -n 1))"
  if "${GUI_ENV[@]}" "$CLAUDE_BIN" auth status >/dev/null 2>&1; then
    ok "Claude Code is signed in"
  else
    warn "Couldn't confirm Claude Code is signed in for background use. If the self-test below fails,"
    warn "open Terminal, run 'claude', type /login, then run this installer again."
  fi
else
  warn "claude not found (install Claude Code, or set [paths] claude in config.toml)"
fi
if [ -n "$CODEX_BIN" ]; then
  ok "codex: $CODEX_BIN ($("${GUI_ENV[@]}" "$CODEX_BIN" --version 2>/dev/null | head -n 1))"
  if head -c 64 "$CODEX_BIN" 2>/dev/null | grep -q node && ! PATH="$EXTRA_PATH" command -v node >/dev/null 2>&1; then
    warn "codex needs Node.js, which wasn't found on your shell PATH."
  fi
  if "${GUI_ENV[@]}" "$CODEX_BIN" login status >/dev/null 2>&1; then
    ok "Codex is signed in"
  else
    warn "Couldn't confirm Codex is signed in. If needed, run 'codex login' in Terminal."
  fi
else
  warn "codex not found (optional; used as a backup)"
fi
[ -n "$CLAUDE_BIN$CODEX_BIN" ] || die "Neither claude nor codex was found. Install Claude Code (or Codex) first."

export WORKAUDIT_HOME="$ROOT"
PYTHONPATH="$ROOT" .venv/bin/python -m workaudit setpaths --claude "$CLAUDE_BIN" --codex "$CODEX_BIN" --extra-path "$EXTRA_PATH" >/dev/null
ok "settings saved to config.toml"

# ---------------------------------------------------------- the app bundle
bold "4/5 Building Work Audit.app"
SRC_HASH="$(cat launcher/launcher.c launcher/Info.plist | shasum -a 256 | cut -d' ' -f1)"
if [ -x "$APP/Contents/MacOS/WorkAudit" ] && [ "$(cat "$APP/Contents/Resources/.build-hash" 2>/dev/null)" = "$SRC_HASH" ]; then
  ok "already built (kept as is so macOS keeps your permissions)"
else
  rm -rf "$APP"
  mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources"
  ESC_ROOT="$(printf '%s' "$ROOT" | sed 's/\\/\\\\/g; s/"/\\"/g')"
  clang -O2 -Wall -DWA_ROOT="\"$ESC_ROOT\"" -o "$APP/Contents/MacOS/WorkAudit" launcher/launcher.c
  sed "s|__BUNDLE_ID__|$BUNDLE_ID|" launcher/Info.plist > "$APP/Contents/Info.plist"
  printf "%s" "$SRC_HASH" > "$APP/Contents/Resources/.build-hash"
  codesign --force --sign - "$APP" >/dev/null 2>&1 || warn "ad-hoc signing failed (the app may still work)"
  ok "built $APP"
fi

QROOT="$(printf '%q' "$ROOT")"
QPATH="$(printf '%q' "$EXTRA_PATH:/usr/bin:/bin:/usr/sbin:/sbin")"
cat > bin/run.sh <<EOF
#!/bin/bash
# Generated by install.sh: starts the Work Audit menu bar app (Work Audit.app runs this).
export PATH=$QPATH
export WORKAUDIT_HOME=$QROOT
export PYTHONPATH=$QROOT
[ -f $QROOT/env.sh ] && source $QROOT/env.sh
cd $QROOT || exit 1
mkdir -p $QROOT/data/logs
exec $QROOT/.venv/bin/python -m workaudit run >> $QROOT/data/logs/app-output.log 2>&1
EOF
chmod +x bin/run.sh
ok "launcher script written"

# ---------------------------------------------------------------- self-test
bold "5/5 Quick test"
a="$(ask "Ask the AI to describe a sample screenshot now? Uses a tiny bit of your plan. [Y/n]")"
if [[ ! "$a" =~ ^[Nn] ]]; then
  if "${GUI_ENV[@]}" WORKAUDIT_HOME="$ROOT" PYTHONPATH="$ROOT" "$ROOT/.venv/bin/python" -m workaudit selftest; then
    ok "AI connection works"
  else
    warn "The test failed; see the message above. You can re-run it with: bin/workaudit selftest"
  fi
fi

echo
bold "Installed."
echo "  Next: open Work Audit.app (it's in this folder). macOS will ask for Screen Recording permission:"
echo "  turn on “Work Audit” in System Settings, then open the app again. Look for ◉ in your menu bar."
echo
a="$(ask "Open Work Audit now? [Y/n]")"
if [[ ! "$a" =~ ^[Nn] ]]; then
  open "$APP"
fi
