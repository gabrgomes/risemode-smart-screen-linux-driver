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
- [x] Marquee scroll for long titles — continuous left-scroll loop with a blank gap, clipped to the line window; short text stays static (centered in vertical). Wall-clock paced (~55px/s). GUI preview refresh bumped to 250ms so it looks live there too.
- [x] CJK / Hangul font fallback — characters outside DejaVu Sans's coverage resolve via `fc-match` (the system's own fontconfig, no bundled/hardcoded font) to whatever's installed, cached per character; text is split into per-font runs so mixed Latin+CJK lines (and the marquee) render correctly. `install.sh` prompts for `fonts-noto-cjk`. Emoji remain tofu - the common installed emoji font is a colour bitmap-strike font Pillow can't rasterize at arbitrary sizes; a plain/monochrome emoji font would work if added manually, but isn't installed by default and wasn't worth forcing on everyone for this
- [ ] 💡 "Show while paused" option (v1 shows it while playing *or* paused)
- [ ] 💡 Move the `playerctl` call + art fetch off the render thread (first fetch of a new track's remote art can block up to ~6s; a title with lots of not-yet-seen CJK characters has a smaller one-time `fc-match` cost too, ~500ms for ~17 characters in testing, then free once cached)

### OpenRGB integration

Sync case/component RGB lighting to whatever's currently driving the panel's own background - "ambilight for the whole PC." Designed 2026-09-11; not started, no OpenRGB install on the dev machine yet to test against.

**Design**
- Priority, same precedence Game Mode already uses for the panel background: game running (its Grid/Hero art's dominant colour) > music playing (album art's dominant colour) > desktop wallpaper's dominant colour.
- Direct color push (`device.set_mode("direct")` + `set_color()`) via [`openrgb-python`](https://github.com/jath03/openrgb-python) (pip, confirmed current: 0.3.6) - not OpenRGB's own saved profiles, which are static snapshots and can't follow the current image. This makes it an all-or-nothing takeover of whatever effect the user has running in OpenRGB's own UI while the toggle is on.
- v1 pushes one color to every detected device - no per-device/zone picker yet.
- New `_dominant_color(img)` util, extracted from `_compute_auto_colors()`'s own averaging step - shared with the music widget's parked "dynamic tint" idea above.
- Own background thread in `risemode_driver.py` (alongside the existing IN-endpoint `poller` thread), own ~1-2s throttle (not every render frame - RGB doesn't need 6fps and fast SDK writes can flicker some LED chains), fully non-fatal if the SDK server isn't running - retries quietly, never touches the panel's own render loop.
- GUI: new "RGB (OpenRGB)" section, enable switch + status line, config key `openrgb_enabled` (default off).

**Animation - revised per user's suggestion**: instead of building any audio capture/analysis ourselves, lean on the community "OpenRGB Effects Plugin" (Fawtytoo) - it already has audio-reactive effects that read straight from a system audio device, no DSP work needed on our side. Plan: while music is playing, activate that plugin's audio-reactive effect instead of pushing a static album-art color; keep the static push for the idle/game states.
- [ ] Verify the SDK can actually select a plugin-contributed effect the same way it selects a built-in mode (`device.set_mode(name)`) - plugin effects show up in OpenRGB's own UI, but it's unconfirmed whether openrgb-python's mode list/selection mirrors plugin-added effects too. Needs testing against a real OpenRGB install (none available yet) before committing to this path.
- Requires the user to install the Effects plugin separately (not bundled with OpenRGB core) and pick/configure a suitable audio-reactive effect + correct input device (a monitor/loopback source, not a mic) themselves in OpenRGB's own UI - not something we can set up on their behalf.
- Fallback if plugin-mode-selection doesn't pan out: a gentle wall-clock-paced brightness pulse over the album-art color while playing (same pacing technique as the marquee).

**Known challenges**
- OpenRGB's SDK server has to actually be running (`openrgb --server` or autostart) - external setup step, needs a README callout.
- Direct-mode takeover conflicts with manually-set OpenRGB effects.
- Device/zone granularity deferred past v1.
- Thread safety / graceful degradation when the server is absent or restarts mid-session.

---

## Backlog / ideas
- 💡 Use system fonts
- [x] Only show FPS-related info when a game is detected — "Only while a game is running" toggle in the MangoHud Sensors section
- 💡 Improve UI
- 💡 Find a way to generate vertical images for games
- 💡 Add other automatic color schemes
- 💡 Group sensors into widgets for easier configuration
- 💡 Add more flexibility for widget positioning
- 💡 Brightness control — the `LIG` command exists in the protocol but only flashes, no persistent set on this firmware
- 💡 More MangoHud-derived metrics — the CSV already carries swap, per-core load, etc.
- 💡 Weather / notification / alert widgets
