# Risemode Smart Screen — Roadmap

Progress tracker for the native Linux driver + settings GUI.

Legend: `[x]` done · `[ ]` planned · 💡 idea / maybe

---

## Shipped

### Core driver
- [x] Native USB HID driver — no Windows, no VM (462×1920 JPEG frames over the interrupt OUT endpoint)
- [x] Firmware-wedge recovery — periodic `CONNECT` resend + proactive session reconnect + `dev.reset()` on startup. Confirmed necessary via reverse engineering; **not** a bug, do not remove.
- [x] `systemd --user` service; config hot-reloaded by mtime, no restart needed for `config.json` changes
- [x] `install.sh` — system deps, udev rule, plugdev group, service, menu shortcut (all idempotent)

### Sensors
- [x] CPU usage + temperature, RAM, GPU usage + temperature + VRAM + power, FPS / 1% low, frame time, clock / date
- [x] FPS / frametime from MangoHud CSV logs (header-driven column parsing, no hardcoded order)
- [x] Device-grouped layout (temp under usage; GPU secondaries on one line), °C degree symbols
- [x] Per-sensor toggles; GPU temperature toggles independently of GPU usage

### Now playing (music widget)
- [x] `"music"` sensor — album art + title + artist + progress bar; shown while playing/paused, hidden otherwise
- [x] Data via MPRIS through one throttled `playerctl metadata` call (`get_music_info()`)
- [x] Album art — `file://` used directly, `http(s)://` downloaded + disk-cached (`~/.cache/risemode-screen/art/`, pruned to 200 files), note-glyph placeholder when missing
- [x] Progress bar interpolated between the 1s metadata polls
- [x] Two layouts — a large centered thumbnail (~½ panel width) over centered title/artist above the clock in vertical; a small-thumbnail full-width strip along the bottom in horizontal (stat columns center in the space above it)
- [x] Title and artist share one font size, distinguished by colour
- [x] `install.sh` prompts for `playerctl`; README section

### Background
- [x] Desktop wallpaper (via `gsettings`) or a custom image — center-cropped + dimmed for text legibility
- [x] Crop axis forced by orientation — fit height for vertical, fit width for horizontal
- [x] **Game Mode** — overlays the running Steam game's cover art while it's running, falls back to the base background otherwise
  - [x] Portrait grid for vertical orientation, wide hero (4K preferred) for horizontal
  - [x] Per-type disk cache + failure cooldown; `SteamAppId` env-var detection; Cloudflare-safe `User-Agent`

### Colors
- [x] Four color roles — label / value / secondary / separator
- [x] Custom mode — a swatch picker per role
- [x] Auto mode — derived from the background image via luminance-targeting (keeps the photo's own hue, hits a real perceived-contrast gap)

### Orientation
- [x] Vertical (portrait) and Horizontal (landscape) — genuinely different layouts, not a rotation
  - Vertical: one stacked column
  - Horizontal: a single row of columns on a wide 1920×462 logical canvas, rotated into the physical portrait buffer before sending

### Settings GUI (`risemode_gui.py`)
- [x] Tkinter app; live preview identical to panel output; Apply writes `config.json` (picked up live)
- [x] Resizable; Display / Background / Sensors / Colors / Live preview sections
- [x] Comboboxes for background source and color mode
- [x] Custom `Switch` widget (iOS-style toggle), sized to the row text
- [x] Menu shortcut + dock/taskbar icon association (`WM_CLASS` / `StartupWMClass`)
- [x] Consistent section spacing / margins / alignment across both orientations

### Parked
- 💡 Deeper firmware-wedge root cause — protocol-level diffing against the genuine Windows software found no difference (CONNECT bytes, DRAW header, keepalive cadence, USB pacing all matched). VM + udev reverse-engineering setup remains available. Explicitly deferred.

---

## Next up

### Music / "Now Playing" widget — polish

v1 shipped (see Shipped § Now playing). Remaining:
- [ ] 💡 Dynamic tint — color the widget from the art's dominant color (reuse `_compute_auto_colors`)
- [ ] 💡 Marquee scroll for long titles (needs per-frame scroll state; render loop is ~6fps so it may be choppy) — v1 ellipsis-truncates
- [ ] 💡 CJK / emoji font fallback (Noto Sans) — PIL does no automatic font fallback; non-Latin track names currently render as tofu
- [ ] 💡 "Show while paused" option (v1 shows it while playing *or* paused)
- [ ] 💡 Move the `playerctl` call + art fetch off the render thread (first fetch of a new track's remote art can block up to ~6s)

---

## Backlog / ideas
- 💡 Use system fonts
- 💡 Only show fps related info when game is detected
- 💡 Integrate with OpenRGB - Change profile/send colors
- 💡 Improve UI
- 💡 Find a way to generate vertical images for games
- 💡 Add other automatic color schemes
- 💡 Group sensors into widgets for easier configuration
- 💡 Add more flexibility for widget positioning
- 💡 Brightness control — the `LIG` command exists in the protocol but only flashes, no persistent set on this firmware
- 💡 More MangoHud-derived metrics — the CSV already carries swap, per-core load, etc.
- 💡 Weather / notification / alert widgets
