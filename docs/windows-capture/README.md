# Risemode Windows capture — Linux handoff

| File | What |
|------|------|
| `PROMPT.md` | Paste the part below the line into Claude Code on the Linux machine |
| `PROTOCOL.md` | Protocol notes from the Windows capture |
| `windows-command-timeline.tsv` | Every command/header report the Windows app sent (DIS, LIG, CONNECT, DRA + size), with timestamps |
| `windows-startup-47s.pcapng` | First 47 s of the capture: idle device → app startup → 3 CONNECT keep-alives (26 MB) |

The full 5.6-minute capture (304 MB, including theme changes) is in `..\captures\` if you ever need it.

## Steps
1. Copy this folder into the driver repo on the Linux machine as `docs/windows-capture/`.
2. `cd` to the repo, run `claude`, and paste the prompt from `PROMPT.md`.
3. Optional: `sudo apt install tshark` so Claude can read the pcapng directly.
