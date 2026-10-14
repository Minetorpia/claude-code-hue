# claude-code-hue

Turn a Philips Hue room into a whole-room status indicator for Claude Code. The
room stays steadily blue while Claude is working and briefly turns green
whenever a turn finishes. When all work is finished, each light returns to its
saved state.

This is the Claude Code counterpart of
[codex-hue](https://github.com/Minetorpia/codex-hue). It uses Claude Code hooks
and the Hue Bridge's local HTTPS API. After discovery and pairing, status
changes stay on your local network.

| Claude Code state | Hue behavior |
| --- | --- |
| Work starts | Save the scene and set the whole room to blue |
| Any work remains active | Keep steady blue, without breathing or periodic flashes |
| One of several turns finishes | Green briefly, then return to blue |
| The last active turn finishes | Green briefly, then restore each light's saved state |
| A turn ends because of an API error (`StopFailure`) | Clear that turn without green; restore when nothing remains active |
| You interrupt a turn (Esc) | Detected from the local transcript; clear only that turn |
| A session ends, is cleared, or is resumed later | Clear its active turns; restore the room if it was the last one |

Verified with the `claude` CLI. The Code tab of the Claude desktop app and the
IDE extensions load the same user settings and write transcripts to the same
place.

## Requirements

- Python 3.9 or newer on macOS or Linux
- Claude Code 2.1.x (tested with 2.1.294)
- A Philips Hue Bridge on the same local network
- A Hue room or zone containing color-capable lights

Windows is not currently supported because the concurrency locks use POSIX
`fcntl` file locking.

## Install

Clone or download this repository, enter its directory, create an isolated
Python environment, and install it:

```shell
cd claude-code-hue
python3 -m venv .venv
source .venv/bin/activate
python -m pip install .
```

Pair with your Bridge and select a room:

```shell
claude-code-hue setup
```

The command discovers the Bridge, then explicitly tells you when to press its
round link button. It waits up to 60 seconds for the press and then asks which
room or zone to use. Discovery can be bypassed when necessary:

```shell
claude-code-hue setup --bridge 192.168.1.20 --room "Living Room"
```

Test the selected room, then install the Claude Code hooks:

```shell
claude-code-hue test
claude-code-hue install-hooks
```

Running sessions pick up the new hooks automatically through Claude Code's
settings file watcher; restart a session if it does not. Send a prompt to try
it. Use `/hooks` inside Claude Code to see the installed handlers.

The hook configuration invokes the exact Python environment used to run
`install-hooks`. Keep that environment installed. Re-run `install-hooks` after
moving to a different environment.

## Commands

| Command | Purpose |
| --- | --- |
| `claude-code-hue discover` | Find Hue Bridges on the network |
| `claude-code-hue setup` | Discover, pair, and select a room or zone |
| `claude-code-hue rooms` | List rooms and zones after pairing |
| `claude-code-hue set-room NAME` | Change the selected room or zone |
| `claude-code-hue test` | Pulse green and restore the room |
| `claude-code-hue status` | Show non-secret status and the log path |
| `claude-code-hue reset` | Clear stale work and restore the saved state |
| `claude-code-hue recover` | Start safe recovery checks without clearing running turns |
| `claude-code-hue install-hooks` | Merge the handlers into `~/.claude/settings.json` |
| `claude-code-hue uninstall-hooks` | Remove only this project's handlers |

`install-hooks` preserves every other setting and unrelated hook handler in
`~/.claude/settings.json`, keeps the file's key order and permissions, follows a
symlinked settings file, makes a timestamped backup when the file exists, and is
safe to run more than once. It installs handlers for `SessionStart` (startup,
resume and clear; not compact, which happens mid-turn), `SessionEnd`,
`UserPromptSubmit`, `Stop`, and `StopFailure`.

The hook never prints anything. Claude Code adds the output of
`UserPromptSubmit` and `SessionStart` hooks to Claude's context, so the
indicator stays invisible to the model. Hook calls from subagents are ignored.

## How multiple sessions work

Each `UserPromptSubmit` hook adds a session-and-prompt pair to a shared active
set, using the `prompt_id` that Claude Code gives every hook in a turn. Every
`Stop` hook removes that pair and triggers a green completion pulse. A
file-locked event queue serializes simultaneous hooks, so one completion is not
lost merely because another Claude Code session is running. A repeated `Stop`
for the same prompt (for example, after another `Stop` hook made Claude continue)
does not pulse again.

The default `solid` mode keeps the blue color throughout active work.
One shared snapshot is retained until the last turn finishes. Each completion
turns the room green briefly; it returns to blue when other turns remain active.
The controller does not continuously resend the blue command. Manual lighting
changes made while busy are not adopted into the saved scene and may be
overwritten by the next notification or final restoration.

### Optional breathing mode

All active turns share a single breathing cycle. Additional turns do not speed
it up. Each light fades down to 40% of **its own scene brightness** and then back
to that brightness, not to the bulb's maximum. For example, Hue brightness 80
breathes between 32 and 80; another light at 40 breathes between 16 and 40. The
undimmed baseline is retained across cycles, so the scene does not gradually dim.

Busy updates send only brightness and transition duration, never color or on/off
commands. Off lights stay off; lights turned on during work join the effect.
Manual brightness changes outside the current breathing range are checked at
fade boundaries (about every two seconds) and adopted as the new baseline.
Color changes are left untouched. Bridges can report delayed intermediate fade
values, so readings inside the breathing range are not treated as new baselines;
small manual adjustments within that range cannot be reliably distinguished.
Very dim lights may have too little brightness range for a visible effect.

Green completions take priority. A green notification temporarily uses the whole
room, then restores its current colors/on-off settings and undimmed brightness
baselines. Breathing resumes after a short settling delay if work remains.
Changes made during the brief green notification may be overwritten by its
restoration. Bridge latency and request pacing can slightly lengthen a cycle.

### Interrupted-turn recovery

Claude Code runs no hook when you interrupt a turn (Esc, or rejecting a tool
call). Several mechanisms keep the room from staying blue:

- A new prompt in the same session supersedes that session's older active turn,
  because one session cannot run two of its own turns at once.
- `SessionStart` (startup, resume, clear) and `SessionEnd` clear the session's
  interrupted turns. Other sessions' running turns remain untouched. Cleanup does
  not pulse green, because ending a session is not evidence of completion.
- While any turn is active (or a saved room state still needs restoring), one
  background worker checks the local session transcripts every two seconds. A
  `[Request interrupted by user]` record carrying the turn's prompt ID removes
  only that session-and-prompt pair. Restoration failures retain the saved
  lighting and are retried. The worker exits when idle.

The hook-provided `transcript_path` is used when available; otherwise
transcripts are discovered under `~/.claude/projects/*/<session-id>.jsonl`.
Reads are incremental and bounded (4 MB per check), so very large transcripts
take several checks to catch up after the worker starts.

**Compatibility:** transcript JSONL is an internal Claude Code format, not a
stable public API. Recovery recognizes explicit interruption records only.
Missing files, unknown formats, quiet turns, and turn age never imply completion.
Hooks remain the main integration. A session that is killed without running
`SessionEnd` (for example, a closed terminal) stays active until you start or
resume a session, interrupt is detected, or you run `claude-code-hue reset`.
A new hook restarts the worker after a reboot; use `claude-code-hue recover` to
restart it immediately without sending another prompt.

In Claude Code hook terms, `Stop` means Claude finished responding and returned
control. It is not a guarantee that the underlying work succeeded. Turns that
are waiting for a permission prompt still count as busy.

## Configuration and privacy

Runtime data is stored outside the repository:

```text
~/.claude/hue-indicator/config.json
~/.claude/hue-indicator/state.json
~/.claude/hue-indicator/indicator.log
```

`config.json` contains a Hue API username that can control your Bridge. Treat it
as a credential and never publish it. The directory and files are created with
owner-only permissions. You can relocate runtime data with
`CLAUDE_CODE_HUE_DATA_DIR`. `CLAUDE_CONFIG_DIR` changes where `settings.json` is
read and written and where transcripts are discovered.

Hook payloads include your prompt and Claude's last reply. Only the session ID,
prompt ID, and transcript path are read from them. Recovery reads local
transcript files but deserializes only lines containing an interruption marker,
and uses only their session and prompt IDs. Nothing from your conversations is
sent to the Bridge or a cloud service, or copied into the repository or the
indicator log. Runtime state may include local transcript paths and session
IDs; redact these before sharing diagnostics.

The Bridge certificate's SHA-256 fingerprint is captured during setup and
pinned for later requests. A changed certificate is rejected, although—as with
other trust-on-first-use systems—initial setup should be performed on a trusted
local network.

The defaults in `config.json` use Hue API v1 `hue`, `sat`, and `bri` values. You
may edit `busy`, `complete`, and the timing fields after setup.

Steady blue is the default (`"busy_mode": "solid"`), including when the mode
field is omitted. To opt into scene-relative breathing instead, use:

```json
{
  "busy_mode": "breathe",
  "breathing_min_ratio": 0.4,
  "breathing_period_seconds": 4.0
}
```

Merge these fields into your existing private configuration; do not replace the
whole file. Changes are picked up by the worker automatically. The ratio must be
greater than zero and at most one; the period must be at least two seconds.
Completion timing still uses `completion_hold_seconds`. Restore transition
durations use tenths of a second.

A periodic blue pulse is available with `"busy_mode": "pulse"`. In that mode,
`busy_hold_seconds` defaults to 1 and `busy_interval_seconds` to 10; the
interval is clamped to at least the hold duration plus one second. Blue's
color/brightness comes from `busy`. Those fields are not used by breathing mode.

### Using it alongside codex-hue

Both projects keep their own state and saved scene. Pointed at the same room,
they will overwrite each other's lighting and may restore the wrong scene. Use a
different room or zone for each, or install only one.

## Troubleshooting

- **No Bridge is discovered:** pass its LAN address with `--bridge`. Check your
  router or the Hue app for the address.
- **Pairing times out:** run `setup` again and press the physical Bridge button
  only after the command asks.
- **Hooks do not run:** check `/hooks` in Claude Code, make sure
  `disableAllHooks` is not set, and restart the session. Run
  `claude-code-hue status` and inspect the reported log.
- **Room remains blue:** run `claude-code-hue recover`. It checks for
  interrupted local turns while preserving running work. If that does not help,
  confirm that no Claude Code session is working before using
  `claude-code-hue reset` (which clears all active entries). Active turns never
  expire merely because they are old.
- **Certificate fingerprint changed:** verify that the Bridge was replaced or
  reset before pairing again. Do not blindly modify the fingerprint.
- **Some lights do not show color:** non-color lights can still change brightness
  but cannot display the blue/green status colors.

## Uninstall

```shell
claude-code-hue uninstall-hooks
```

You can then remove the Python environment and, if you no longer need it, the
`~/.claude/hue-indicator` runtime directory. If the room is still blue, stop
your running sessions and run `claude-code-hue reset` before removing the
environment; this also lets the recovery worker exit.

## Development

```shell
python -m pip install -e .
python -m unittest discover -s tests -v
```

See [CONTRIBUTING.md](CONTRIBUTING.md) and [SECURITY.md](SECURITY.md) before
opening a change or reporting a security issue.

Useful upstream references:

- [Claude Code hooks reference](https://code.claude.com/docs/en/hooks)
- [Philips Hue local API getting started guide](https://developers.meethue.com/develop/get-started-2/)

This community project is not affiliated with, endorsed by, or sponsored by
Anthropic, Signify, or Philips Hue. Claude, Claude Code, and Philips Hue are
trademarks of their respective owners.
