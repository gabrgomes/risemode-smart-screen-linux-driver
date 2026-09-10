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

### Music / "Now Playing" widget

Show the current track on the panel, inspired by the [Dynamic Music Pill](https://extensions.gnome.org/extension/9334/dynamic-music-pill/) GNOME extension. Data via MPRIS (`playerctl`), which covers Spotify, VLC, mpv, Rhythmbox, and Chromium/Firefox web audio. Album art fetched + disk-cached the same way as the SteamGridDB art. Display-only (the panel has no input), so it's a readout, not a control.

**v1 — minimal**
- [ ] `get_music_info()` — throttled `playerctl` call → `{status, artist, title, art_url, position, length}`; empty dict when nothing is playing
- [ ] `_fetch_album_art(url)` — clone of `_fetch_game_image()`; cache dir `~/.cache/risemode-screen/art/`; handle `file://` directly and `http(s)://` via download; re-fetch only when the track (artUrl) changes; generic music glyph as fallback
- [ ] `_draw_music_block()` — rounded art thumbnail (PIL mask), title + artist with ellipsis truncation, thin progress bar with interpolated position
- [ ] Slot it into `_render_vertical` and `_render_horizontal`
- [ ] `"music"` sensor toggle; hidden when status is Stopped / no player (like Game Mode's fallback)
- [ ] `install.sh` — optional `playerctl` prompt (like the MangoHud one)
- [ ] README section

**Stretch**
- [ ] 💡 Dynamic tint — color the widget from the art's dominant color (reuse `_compute_auto_colors`)
- [ ] 💡 Marquee scroll for long titles (needs per-frame scroll state; render loop is ~6fps so it may be choppy)
- [ ] 💡 CJK / emoji font fallback (Noto Sans) — PIL does no automatic font fallback
- [ ] 💡 "Show while paused" option

**Known challenges**
- `mpris:artUrl` format is inconsistent — `file://`, `http(s)://`, or missing; browsers are always remote, so the download-and-cache path is mandatory
- Panel space budget, especially landscape (462px total height for everything)
- Text: truncation vs marquee, plus font coverage for non-Latin scripts / emoji
- `Position` isn't reliably signal-pushed — poll it and interpolate between polls while Playing
- Subprocess overhead — throttle metadata to ~1s, re-fetch art only on track change
- Confirm `DBUS_SESSION_BUS_ADDRESS` reaches the `--user` service (should, same as `gsettings` already does)

---

## Backlog / ideas
- 💡 Brightness control — the `LIG` command exists in the protocol but only flashes, no persistent set on this firmware
- 💡 More MangoHud-derived metrics — the CSV already carries swap, per-core load, etc.
- 💡 Weather / notification / alert widgets
