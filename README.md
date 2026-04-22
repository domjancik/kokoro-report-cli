# kokoro-report-cli

Cross-platform queued speech reporter for [Kokoro-FastAPI](https://github.com/remsky/Kokoro-FastAPI).

## Features

- `say` command: generate audio from Kokoro and enqueue it
- queued playback so reports do not overlap
- detached worker launch for background playback
- cross-platform playback backend:
  - Windows: `winsound`
  - macOS: `afplay`
  - Linux: `paplay` / `aplay` / `ffplay`
- hook-friendly CLI command for agent/tool integrations

## Requirements

- Python 3.10+
- Running Kokoro API (default: `http://127.0.0.1:8880`)

## Usage

```bash
python kokoro_report.py say "Task complete: implemented feature X and validation passed."
```

Options:

```bash
python kokoro_report.py say "Done." \
  --api-base http://127.0.0.1:8880 \
  --voice am_echo \
  --speed 1.0 \
  --lang-code a
```

Manual worker run:

```bash
python kokoro_report.py worker --once
```

## Hook Prompt Append Example

Use this as a prompt appendix for agent hooks (Claude Code, Vibe Kanban, etc.):

```text
After every turn, run this to speak a one-sentence completion summary:

  python /path/to/kokoro_report.py say "<summary>"

Summary requirements: exactly 1 sentence, past tense, includes what changed + validation status, and contains no placeholders, quotes, or markdown.

Skip report on successful commit only steps.
```

## Queue/State Directory

- Windows: `%LOCALAPPDATA%\\kokoro-report`
- macOS/Linux: `$XDG_STATE_HOME/kokoro-report` or `~/.local/state/kokoro-report`
