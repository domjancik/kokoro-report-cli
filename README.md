# kokoro-report-cli

Cross-platform queued speech reporter for [Kokoro-FastAPI](https://github.com/remsky/Kokoro-FastAPI).

## Features

- `say` command: enqueue synthesis + playback job (returns immediately by default)
- queued playback so reports do not overlap
- detached worker launch for background playback
- basic post-processing filters (EQ + cutoff + limiter)
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

The default `say` flow is non-blocking (`--detach`): it queues the job and returns immediately while a detached worker generates and plays audio in sequence.

Options:

```bash
python kokoro_report.py say "Done." \
  --api-base http://127.0.0.1:8880 \
  --voice am_echo \
  --speed 1.0 \
  --lang-code a \
  --bass-db -3 \
  --normalize-peak 0.95
```

Manual worker run:

```bash
python kokoro_report.py worker --once
```

## Basic Filter Set

Available `say` audio filters:

- `--preamp-db` (overall gain before EQ)
- `--bass-db` (low-shelf around ~200 Hz, good for reducing rumble without harsh cutoff)
- `--treble-db` (high-shelf around ~4 kHz)
- `--highpass-hz` (harder bass reduction via cutoff)
- `--lowpass-hz` (top-end smoothing)
- `--normalize-peak` (0..1 peak limiter, attenuates only)

Suggested starting points:

- Gentle bass reduction: `--bass-db -2` to `--bass-db -4`
- Stronger bass cleanup: `--highpass-hz 100` plus `--bass-db -2`
- Keep levels controlled: `--normalize-peak 0.95`

## Local Defaults Config

`kokoro_report.py` auto-loads `kokoro_report.local.json` from the same directory as the script (if present).
Use it so agents do not need to pass tuning flags each run.

- Example template: `kokoro_report.local.example.json`
- Local file is ignored by git: `kokoro_report.local.json`

You can also point to a custom path:

```bash
python kokoro_report.py say "Done." --config /path/to/config.json
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
