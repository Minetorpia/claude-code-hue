# Security policy

## Supported versions

Security fixes currently target the latest `0.1.x` release.

## Reporting a vulnerability

Do not disclose an unpatched vulnerability in a public issue. Use the
repository's private security-advisory feature. Include the affected version,
reproduction steps, impact, and any suggested mitigation. If private advisories
are unavailable, ask the maintainer for a private reporting channel without
including exploit details.

## Sensitive local data

The Hue API username in `~/.claude/hue-indicator/config.json` is a credential
with local control over the Bridge. The project stores it outside the repository
with owner-only permissions. Never attach this file, `~/.claude/settings.json`,
state files, or logs to an issue without redacting local addresses and
credentials.

Hook payloads contain your prompt and Claude's last reply; the hook reads only
session and prompt IDs and the transcript path from them. Recovery also reads
local Claude Code transcripts. Never publish those files: they can contain
private conversation text, code, and tool outputs. The controller deserializes
only interruption records, uses their IDs only, and keeps conversation contents
out of its logs, state, and Hue requests. State files can contain local
transcript paths and session IDs.

All light-control requests after setup go directly to the configured Bridge on
the local network. Bridge discovery may contact the official Hue discovery
service if local hostname discovery does not find it.
