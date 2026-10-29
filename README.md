# Work Audit

A small macOS menu bar app for **Step 1: Audit your work**. For about a week it:

1. **Watches** the window you're working in: a screenshot every 5 seconds, shrunk to a small but readable JPEG.
2. **Describes** what you're doing: every 5 minutes (only if something changed) it sends a few of those
   screenshots plus the app/window/website log to **Claude Code** (Haiku) or **Codex**, on your existing plan,
   and stores a structured description (task, apps, systems, documents, people, automation notes) in a local
   SQLite database.
3. **Asks you 10–15 short questions a day** right after you switch tasks ("Why are you doing this now?",
   "What systems do you need for this?"), because the screen can't show the *why*.
4. **Writes a daily report** at 18:00 (Claude Opus) and, at the end, a **full audit report**: an inventory of
   every deliverable and process with time per week, triggers, tools, automation fit, and a ready-to-paste
   prompt for Step 2.

Everything stays in this folder. The only things that leave your Mac are the screenshots and text sent to
Claude/Codex through their own CLIs, exactly as when you use them yourself.

---

## Install (5 minutes)

1. Move the folder to your home folder (not Desktop/Documents/Downloads, which need extra permissions):
   ```bash
   mv ~/Downloads/work-audit ~/work-audit
   ```
2. Run the installer in Terminal:
   ```bash
   cd ~/work-audit && bash install.sh
   ```
   It creates a private Python environment, finds `claude` and `codex`, builds **Work Audit.app** in the folder,
   and (if you say yes) asks the AI to describe a sample screenshot to prove the connection works.
3. Open **Work Audit.app**. macOS asks for **Screen Recording** permission. Turn on “Work Audit” in
   System Settings → Privacy & Security → Screen & System Audio Recording, then open the app again.
4. Look for **◉** in the menu bar. The first popup asks one setup question about your business; answer it.
   The first time you use each browser, macOS asks whether Work Audit may control it: click **Allow** (that's
   how it reads the tab's address and spots incognito windows and banking sites).

Requirements: macOS 12+, Apple's command line tools (the installer prompts for them), Python 3.9+ (or `uv`),
and Claude Code and/or Codex signed in.

## Using it

Just work normally. The menu bar icon shows what it's doing:

| Icon | Meaning |
|---|---|
| ◉ | recording |
| ◌ | private window or call: nothing captured |
| ◎ | idle or screen locked |
| ⏸ | paused |
| ✓ | audit period finished |
| `?` after the icon | a question is waiting (you were typing or on a call) |
| `…` after the icon | writing a report |

**Check-ins** pop up after you switch tasks, once you pause typing for a few seconds, at most every 20 minutes
(15 a day). They are held back while a call or screen-share window is open anywhere on screen, or while you're
presenting slides, so they never show up on a screen you're sharing. Type or dictate (press the dictation key or Fn twice). *Skip* is fine; *Ask me
later* snoozes 10 minutes. Volunteered context is the most valuable input: use **Add a note…** whenever you think
"this part is tedious" or "this is why I do it this way."

**Menu**: pause for 15 minutes / an hour / until resumed · ask me a question now · add a note · generate today's
report · generate the full audit report · run a self-test · settings · start at login.

After 7 days recording stops and it offers to write the full audit report (Claude's most capable model at max
effort: 10–30 minutes and a good share of your plan's usage). Take the **Prompt for the next step** section at the
end of that report into a new Claude chat to start Step 2.

## Privacy

Chosen at setup and editable in **Settings…** (`config.toml`):

- **Only the front window** is captured, not your whole screen (set `scope = "display"` for more context).
- **Never captured**: password managers, banking/brokerage/payment sites, incognito/private windows, any app
  while you type in a password field, and personal messaging (Messages, WhatsApp, Signal, Telegram, Messenger…).
  For these only the app name and the reason are logged, so the report can say "12 min personal messaging".
- **Calls** (Zoom, Meet, Teams, Webex, FaceTime, Slack huddles): logged by window title only, no screenshot.
- Add your own: `extra_apps`, `extra_domains`, `extra_title_keywords`. Pause from the menu any time.
- Background windows are listed to the AI by app name and title for context, except browsers (app name only).
- Limitations: Safari can't report private windows (pause for those). Firefox page addresses aren't readable,
  so there (and in any browser you don't allow under Automation) banking, messaging and Meet pages are
  recognised by the page title instead.
- Screenshots the AI didn't look at are deleted as soon as each 5-minute window is described
  (`keep_all_frames = false`), so you keep roughly one image per minute of work (~50–100 MB a day).
- Data lives in `data/`: `audit.db` (SQLite), `frames/` (screenshots), `reports/`, `logs/`.

## Usage of your plan

- Descriptions: about 60–100 small Claude Haiku calls per working day (fewer when you stay in one window), each
  with up to 5 screenshots. If Claude hits a usage limit, it falls back to Codex; if both are limited, it waits and
  catches up later without losing anything.
- Daily report: one Opus call (high effort). Full audit: one call with `best` (Fable if your plan has it,
  otherwise Opus) at max effort.
- To shift the screenshot load to your ChatGPT plan, set `backend = "codex"` under `[describe]`.

## Command line

```bash
bin/workaudit status            # today's numbers, which CLIs it found
bin/workaudit selftest          # describe a sample screenshot with each CLI
bin/workaudit digest            # print today's raw data (no AI)
bin/workaudit report [YYYY-MM-DD]
bin/workaudit audit             # full audit report
bin/workaudit export            # CSVs of every activity segment and answer
bin/workaudit new-period        # start a fresh audit period today
```

## Troubleshooting

- **Nothing is being described / "failed" in the menu**: run `bin/workaudit selftest`. "auth" means sign in
  (`claude` then `/login`, or `codex login`). If Claude Code works in Terminal but not in the app, run
  `claude setup-token` and put `export CLAUDE_CODE_OAUTH_TOKEN=...` in a file named `env.sh` in this folder
  (`chmod 600 env.sh`); the app loads it at start.
- **Permission dialog keeps coming back**: macOS ties the permission to this exact app. If you re-ran the
  installer after changing the launcher, remove “Work Audit” from Screen Recording and add it again.
  macOS may also ask monthly whether Work Audit may keep recording your screen; click Allow.
- **Wrong browser URLs / private windows captured**: allow Work Audit under System Settings → Privacy &
  Security → Automation for your browser.
- Logs: `data/logs/workaudit.log`.

## Uninstall

`bash uninstall.sh` stops the app and removes the login item (and, if you confirm, your recorded data). Then
delete the folder and remove “Work Audit” from the Screen Recording and Automation lists in System Settings.

## How it works (for tinkering)

`workaudit/capture.py` takes the screenshots and applies `privacy.py` · `describe.py` batches 5-minute windows
and calls `llm.py` (headless `claude -p` with your login, no tools or MCP servers, or `codex exec`) ·
`checkins.py` decides when to ask · `reports.py` builds the daily data digest and reports · `app.py` is the menu
bar. **Work Audit.app** is a 60-line C launcher (`launcher/launcher.c`) so macOS permissions attach to a stable
app while the Python code stays easy to change.
