"""
Shared panel rendering and user-configurable settings for the Risemode smart
screen: which sensors to show and what background image to use. Used by both
risemode_driver.py (renders the frames actually sent to the panel) and
risemode_gui.py (renders an identical live preview from unsaved settings).

Kept separate from risemode_driver.py so the GUI doesn't need to touch
anything USB/device-specific to render a preview.
"""
import colorsys
import glob
import hashlib
import io
import json
import os
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import deque

import psutil
from PIL import Image, ImageDraw, ImageFont

WIDTH, HEIGHT = 462, 1920  # the physical panel's fixed native buffer size -
                           # always portrait, regardless of ORIENTATION below

ORIENTATIONS = ("vertical", "horizontal")
ORIENTATION_LABELS = {
    "vertical": "Vertical (portrait)",
    "horizontal": "Horizontal (landscape)",
}
# The logical canvas each orientation is laid out and rendered at.
# "horizontal" renders a wide 1920x462 image with a row-based layout, then
# render_stats_image() rotates that into the panel's fixed portrait buffer
# above right before sending - the physical panel itself never changes
# size, only how the content is composed before being rotated to fit it.
CANVAS_SIZES = {
    "vertical": (WIDTH, HEIGHT),
    "horizontal": (HEIGHT, WIDTH),
}

MANGOHUD_LOG_DIR = os.path.expanduser("~/.local/share/mangohud_logs")
MANGOHUD_STALE_S = 3  # ignore logs that haven't been touched recently -
                       # means no game/GL app is currently running

BG_DIM_ALPHA = 140  # 0-255; darkens the wallpaper so stat text stays legible
                    # over bright/busy photos
BG_FALLBACK = (15, 15, 25)

STEAMGRIDDB_API_BASE = "https://www.steamgriddb.com/api/v2"
# Grids (portrait, 600x900) suit the vertical orientation's tall panel;
# heroes (wide, up to 3840x1240) suit horizontal's - see resolve_background_
# path(), which picks between them based on the config's orientation.
GRID_CACHE_DIR = os.path.expanduser("~/.cache/risemode-screen/grids")
HERO_CACHE_DIR = os.path.expanduser("~/.cache/risemode-screen/heroes")
HERO_4K_WIDTH = 3840  # SteamGridDB's "4K" hero dimension is 3840x1240,
                      # vs. 1920x620 for the standard size
GAME_DETECT_INTERVAL_S = 5  # how often to re-scan for a running Steam game -
                            # doesn't need checking every frame like the
                            # panel's own render loop

ART_CACHE_DIR = os.path.expanduser("~/.cache/risemode-screen/art")
ART_CACHE_KEEP = 200  # album art churns per-track (unlike game art), so the
                      # cache dir is pruned to its most-recently-used N files
ART_FETCH_RETRY_S = 30  # cooldown before retrying a failed remote-art fetch
MUSIC_INFO_INTERVAL_S = 1  # how often to re-run playerctl - track metadata
                           # doesn't change fast enough to poll every frame

CONFIG_PATH = os.path.expanduser("~/.config/risemode-screen/config.json")
# The base background is always one of these two - Game Mode isn't a third
# alternative to them, it's an independent overlay (see GAME_MODE_LABEL)
# that swaps in a grid/hero image on top of whichever of these is picked,
# only while a game is actually running.
BACKGROUND_MODES = ("desktop", "custom")
BACKGROUND_MODE_LABELS = {
    "desktop": "Desktop wallpaper (auto-updates)",
    "custom": "Custom image",
}
GAME_MODE_LABEL = "Game Mode"
DEFAULT_SENSORS = {
    "cpu": True, "cpu_temp": True, "ram": True,
    "gpu": True, "gpu_temp": True, "gpu_vram": True, "gpu_power": True,
    "fps": True, "frametime": True,
    "clock": True,
    "music": True,
}
SENSOR_LABELS = {
    "cpu": "CPU usage",
    "cpu_temp": "CPU temperature",
    "ram": "RAM usage",
    "gpu": "GPU usage",
    "gpu_temp": "GPU temperature",
    "gpu_vram": "GPU VRAM usage",
    "gpu_power": "GPU power draw",
    "fps": "FPS / 1% low",
    "frametime": "Frame time (stutter)",
    "clock": "Clock / date",
    "music": "Now playing",
}

# The whole panel draws through just 4 color roles - every sensor block
# uses the same label/value/secondary scheme (e.g. GPU temp, the clock's
# date) rather than each having its own, so that's the level these are
# customizable at.
DEFAULT_COLORS = {
    "label": [0, 200, 255],      # e.g. "CPU", "GPU", "FPS"
    "value": [255, 255, 255],    # the big numbers, and the clock's time
    "secondary": [255, 150, 0],  # temps, VRAM, power, the clock's date
    "separator": [60, 60, 80],   # the line above the FPS/frame time section
}
COLOR_LABELS = {
    "label": "Labels",
    "value": "Values",
    "secondary": "Secondary readings",
    "separator": "Separator line",
}

COLOR_MODES = ("custom", "auto")
COLOR_MODE_LABELS = {
    "custom": "Custom",
    "auto": "Auto (from background)",
}


def load_config():
    """Reads the GUI-editable config from disk. A missing file, missing
    keys, or a corrupt file all just fall back to sane defaults, so a fresh
    install or an older config written before a new sensor was added both
    work without special-casing."""
    try:
        with open(CONFIG_PATH) as f:
            data = json.load(f)
    except (OSError, ValueError):
        data = {}
    sensors = dict(DEFAULT_SENSORS)
    sensors.update(data.get("sensors", {}))
    colors = dict(DEFAULT_COLORS)
    colors.update(data.get("colors", {}))
    color_mode = data.get("color_mode", "custom")
    if color_mode not in COLOR_MODES:
        color_mode = "custom"
    # Configs saved before Game Mode existed have no background_mode at all -
    # infer it from wallpaper instead of silently discarding a custom image.
    background_mode = data.get(
        "background_mode", "custom" if data.get("wallpaper") else "desktop"
    )
    game_mode_enabled = bool(data.get("game_mode_enabled", False))
    if background_mode == "game":
        # Migrates configs from when Game Mode was its own third radio
        # option instead of an overlay on top of desktop/custom - falls
        # back to whichever of those the wallpaper implies, same as a
        # config with no background_mode at all.
        background_mode = "custom" if data.get("wallpaper") else "desktop"
        game_mode_enabled = True
    if background_mode not in BACKGROUND_MODES:
        background_mode = "desktop"
    orientation = data.get("orientation", "vertical")
    if orientation not in ORIENTATIONS:
        orientation = "vertical"
    return {
        "wallpaper": data.get("wallpaper"),
        "background_mode": background_mode,
        "game_mode_enabled": game_mode_enabled,
        "steamgriddb_api_key": data.get("steamgriddb_api_key", ""),
        "sensors": sensors,
        "colors": colors,
        "color_mode": color_mode,
        "orientation": orientation,
    }


def save_config(config):
    os.makedirs(os.path.dirname(CONFIG_PATH), exist_ok=True)
    tmp = CONFIG_PATH + ".tmp"
    with open(tmp, "w") as f:
        json.dump(config, f, indent=2)
    os.replace(tmp, CONFIG_PATH)  # atomic - never leaves a half-written file


_config_cache = {"mtime": None, "data": load_config()}


def get_config():
    """Cached, auto-reloading read of the on-disk config. The driver's run
    loop calls this every frame so changes applied from the GUI take effect
    live, without needing to restart the service."""
    try:
        mtime = os.path.getmtime(CONFIG_PATH)
    except OSError:
        mtime = None
    if mtime != _config_cache["mtime"]:
        _config_cache["data"] = load_config()
        _config_cache["mtime"] = mtime
    return _config_cache["data"]


def get_gpu_stats():
    """Returns (load%, temp_c, vram_used_mb, vram_total_mb, power_w), all
    None if nvidia-smi isn't available - these are always-on system stats
    (like CPU/RAM), not tied to any particular game being logged."""
    try:
        out = subprocess.check_output(
            ["nvidia-smi",
             "--query-gpu=utilization.gpu,temperature.gpu,memory.used,"
             "memory.total,power.draw",
             "--format=csv,noheader,nounits"],
            timeout=1,
        ).decode().strip()
        load, temp, vram_used, vram_total, power = out.split(",")
        return float(load), float(temp), float(vram_used), float(vram_total), float(power)
    except (subprocess.SubprocessError, OSError, ValueError):
        return None, None, None, None, None


def get_cpu_temp():
    """Best-effort CPU package temperature via psutil/lm-sensors. Chip
    names vary by vendor/kernel (k10temp on AMD, coretemp on Intel) - prefer
    those, otherwise fall back to whatever's first available."""
    try:
        temps = psutil.sensors_temperatures()
    except (AttributeError, OSError):
        return None
    for chip in ("k10temp", "coretemp"):
        entries = temps.get(chip)
        if entries:
            for e in entries:
                if e.label in ("Tctl", "Package id 0"):
                    return e.current
            return entries[0].current
    for entries in temps.values():
        if entries:
            return entries[0].current
    return None


_mangohud_columns_cache = {"path": None, "columns": None}


def _mangohud_columns(path):
    """MangoHud's CSV opens with a 2-line system-info block, then a header
    row naming the actual per-frame columns - their order depends on
    MangoHud's config/version, so it can't be hardcoded. Cached per log
    file to avoid re-reading it every frame."""
    if _mangohud_columns_cache["path"] == path:
        return _mangohud_columns_cache["columns"]
    try:
        with open(path, errors="ignore") as f:
            f.readline()  # system-info header (os,cpu,gpu,ram,...)
            f.readline()  # system-info values
            columns = f.readline().strip().split(",")
    except OSError:
        columns = None
    _mangohud_columns_cache.update(path=path, columns=columns)
    return columns


def get_game_stats():
    """Reads the latest per-frame row MangoHud logged for whatever game/GL
    app is currently running (see README for setup), keyed by MangoHud's
    own column names - fps, frametime, gpu_vram_used, cpu_temp, and
    whatever else the current config logs. Empty dict if nothing is
    currently logging."""
    try:
        logs = [
            p for p in glob.glob(os.path.join(MANGOHUD_LOG_DIR, "*.csv"))
            if not p.endswith("_summary.csv")
        ]
        if not logs:
            return {}
        latest = max(logs, key=os.path.getmtime)
        if time.time() - os.path.getmtime(latest) > MANGOHUD_STALE_S:
            return {}
        columns = _mangohud_columns(latest)
        if not columns:
            return {}
        with open(latest, "rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            f.seek(max(0, size - 4096))
            lines = f.read().decode(errors="ignore").splitlines()
        for line in reversed(lines):
            if line and line[0].isdigit():
                stats = {}
                for name, value in zip(columns, line.split(",")):
                    try:
                        stats[name] = float(value)
                    except ValueError:
                        pass
                return stats
        return {}
    except (OSError, ValueError):
        return {}


_MUSIC_FIELDS = ("status", "xesam:artist", "xesam:title", "mpris:artUrl",
                 "position", "mpris:length")

_music_info_cache = {"time": 0.0, "info": {}}


def _read_music_info():
    """One `playerctl metadata` call for the currently active MPRIS player.
    Returns {} when nothing is playing/paused, no player is running, or
    playerctl isn't installed - the widget just won't be drawn in any of
    those cases, same as Game Mode when no game is running."""
    fmt = "\t".join("{{" + f + "}}" for f in _MUSIC_FIELDS)
    try:
        out = subprocess.check_output(
            ["playerctl", "metadata", "--format", fmt],
            timeout=1, stderr=subprocess.DEVNULL,
        ).decode("utf-8", "replace").strip("\n")
    except (subprocess.SubprocessError, OSError):
        return {}
    parts = out.split("\t")
    if len(parts) < len(_MUSIC_FIELDS):
        return {}
    status, artist, title, art_url, position, length = parts[:6]
    if status.strip().lower() not in ("playing", "paused"):
        return {}

    def _us_to_s(v):
        try:
            return float(v) / 1_000_000
        except ValueError:
            return None

    return {
        "status": status.strip().lower(),
        "artist": artist.strip(),
        "title": title.strip(),
        "art_url": art_url.strip() or None,
        "position": _us_to_s(position),
        "length": _us_to_s(length),
        "read_at": time.time(),
    }


def get_music_info():
    """Cached, throttled wrapper around _read_music_info() - spawning
    playerctl on every one of the render loop's ~6 frames/sec is wasteful,
    and track metadata doesn't change anywhere near that fast."""
    now = time.time()
    if now - _music_info_cache["time"] > MUSIC_INFO_INTERVAL_S:
        _music_info_cache["info"] = _read_music_info()
        _music_info_cache["time"] = now
    return _music_info_cache["info"]


def _music_progress(music):
    """0..1 fraction of the track elapsed, or None if the player doesn't
    report a length (live streams). While playing, the last polled position
    is advanced by the wall time since that poll so the bar moves smoothly
    between the 1s metadata refreshes."""
    length = music.get("length")
    position = music.get("position")
    if not length or position is None:
        return None
    if music.get("status") == "playing":
        position += time.time() - music.get("read_at", time.time())
    return max(0.0, min(1.0, position / length))


def load_font(size):
    for path in (
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    ):
        try:
            return ImageFont.truetype(path, size)
        except OSError:
            continue
    return ImageFont.load_default()


_font_cache = {}


def _font(size):
    """Memoized load_font() - the music block picks font sizes relative to
    its own box height (which differs by orientation), so it can't just use
    the fixed FONT_* constants; this keeps it from re-reading the TTF on
    every frame."""
    f = _font_cache.get(size)
    if f is None:
        f = _font_cache[size] = load_font(size)
    return f

FONT_DATE = load_font(38)
FONT_BIG = load_font(64)
FONT_MED = load_font(36)

# Horizontal orientation's canvas is only WIDTH (462px) tall, vs. vertical's
# HEIGHT (1920px) - a label/value/secondary column has to fit in far less
# vertical space, hence the smaller sizes.
FONT_LABEL_H = load_font(24)
FONT_VALUE_H = load_font(46)
FONT_SECONDARY_H = load_font(22)


def get_wallpaper_path():
    """Reads the current GNOME desktop wallpaper file path via gsettings.
    Returns None if not on GNOME, nothing is set, or it isn't a local file."""
    for key in ("picture-uri-dark", "picture-uri"):
        try:
            uri = subprocess.check_output(
                ["gsettings", "get", "org.gnome.desktop.background", key],
                timeout=1,
            ).decode().strip().strip("'")
        except (subprocess.SubprocessError, OSError):
            return None
        if not uri:
            continue
        parsed = urllib.parse.urlparse(uri)
        if parsed.scheme == "file":
            return urllib.request.url2pathname(parsed.path)
    return None


def _detect_running_game_appid():
    """Best-effort detection of a currently-running Steam game, via the
    SteamAppId environment variable Steam sets on every game process it
    launches - this hands us the game's Steam AppID directly (which is
    exactly what SteamGridDB's API keys off), with no need to guess it from
    a process/window name. Only catches games actually launched through
    Steam (including Proton); returns None otherwise, or if nothing is
    currently running."""
    try:
        for proc in psutil.process_iter():
            try:
                appid = proc.environ().get("SteamAppId")
            except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess, OSError):
                continue
            if appid and appid.isdigit():
                return appid
    except OSError:
        pass
    return None


_game_appid_cache = {"time": 0.0, "appid": None}


def get_running_game_appid():
    """Cached, throttled wrapper around _detect_running_game_appid() -
    scanning every running process's environ is too heavy to redo every
    single frame like the rest of the panel's stats."""
    now = time.time()
    if now - _game_appid_cache["time"] > GAME_DETECT_INTERVAL_S:
        _game_appid_cache["appid"] = _detect_running_game_appid()
        _game_appid_cache["time"] = now
    return _game_appid_cache["appid"]


GAME_IMAGE_FETCH_RETRY_S = 30  # cooldown before retrying a failed fetch
                               # (bad/missing key, no image, offline, ...)
                               # - without this, "game" mode's per-second
                               # preview refresh would hammer the API every
                               # tick while a game is running and the fetch
                               # keeps failing

# Separate failure caches per image type/appid, not a shared one keyed by
# appid alone - a grid-fetch failure shouldn't cool down a hero fetch for
# the same game (they're independent SteamGridDB endpoints/results).
_grid_fetch_failures = {}
_hero_fetch_failures = {}

_USER_AGENT = "risemode-smart-screen-driver/1.0"
# Without a real User-Agent, urllib's default ("Python-urllib/x.y") gets
# blocked outright by SteamGridDB's Cloudflare WAF - a 403 with Cloudflare
# error 1010 ("browser/client denied"), not an auth or rate-limit problem
# at all, before the request even reaches their API or CDN.


def _fetch_game_image(appid, api_key, *, endpoint, query, cache_dir, failures, rank_key):
    """Shared fetch/cache logic behind _fetch_game_grid_path() and
    _fetch_game_hero_path(): queries SteamGridDB's `endpoint` for `appid`,
    picks the best entry per `rank_key`, and downloads it to `cache_dir` -
    caching to disk on first use, since the art doesn't change, so every
    later call for the same game is just a cache hit, not a repeat
    API/network round-trip. Returns None on any failure (no API key
    configured, no network, no image available for this game, ...) so
    callers can fall back cleanly to the base background instead."""
    os.makedirs(cache_dir, exist_ok=True)
    for ext in ("png", "jpg", "jpeg", "webp"):
        cached = os.path.join(cache_dir, f"{appid}.{ext}")
        if os.path.isfile(cached):
            return cached
    if not api_key:
        return None
    last_failure = failures.get(appid)
    if last_failure is not None and time.time() - last_failure < GAME_IMAGE_FETCH_RETRY_S:
        return None

    def _fail():
        failures[appid] = time.time()
        return None

    url = f"{STEAMGRIDDB_API_BASE}/{endpoint}/steam/{appid}?{query}"
    request = urllib.request.Request(url, headers={
        "Authorization": f"Bearer {api_key}",
        "User-Agent": _USER_AGENT,
    })
    try:
        with urllib.request.urlopen(request, timeout=4) as resp:
            payload = json.loads(resp.read().decode())
    except (urllib.error.URLError, OSError, ValueError):
        return _fail()

    items = payload.get("data") or []
    if not items:
        return _fail()
    best = max(items, key=rank_key)
    image_url = best.get("url")
    if not image_url:
        return _fail()

    ext = image_url.rsplit(".", 1)[-1].split("?")[0].lower()
    if ext not in ("png", "jpg", "jpeg", "webp"):
        ext = "png"
    dest = os.path.join(cache_dir, f"{appid}.{ext}")
    image_request = urllib.request.Request(image_url, headers={"User-Agent": _USER_AGENT})
    try:
        with urllib.request.urlopen(image_request, timeout=6) as resp:
            image_bytes = resp.read()
        with open(dest, "wb") as f:
            f.write(image_bytes)
    except (urllib.error.URLError, OSError):
        return _fail()
    return dest


def _fetch_game_grid_path(appid, api_key):
    """The given Steam appid's top-voted portrait grid (600x900) - fits
    vertical orientation's tall panel far better than a wide hero image
    would."""
    return _fetch_game_image(
        appid, api_key,
        endpoint="grids", query="dimensions=600x900&types=static",
        cache_dir=GRID_CACHE_DIR, failures=_grid_fetch_failures,
        rank_key=lambda g: g.get("score", 0),  # most-voted
    )


def _fetch_game_hero_path(appid, api_key):
    """The given Steam appid's best-rated hero image (SteamGridDB's wide
    background art) - fits horizontal orientation's wide panel far better
    than a portrait grid would. Prefers their 4K/3840x1240 size over the
    standard 1920x620 one when available."""
    return _fetch_game_image(
        appid, api_key,
        endpoint="heroes", query="types=static",
        cache_dir=HERO_CACHE_DIR, failures=_hero_fetch_failures,
        # Prefer 4K art over the standard size regardless of score - only
        # breaking ties by score (most-voted) within whichever size tier
        # is actually available, since most entries carry a score of 0
        # anyway (SteamGridDB's voting is sparse) and a 4K image is the
        # more meaningful "best" here.
        rank_key=lambda h: (h.get("width", 0) >= HERO_4K_WIDTH, h.get("score", 0)),
    )


_art_fetch_failures = {}


def _prune_cache_dir(path, keep):
    """Deletes all but the `keep` most-recently-modified files in `path`."""
    try:
        files = [os.path.join(path, f) for f in os.listdir(path)]
        files = [f for f in files if os.path.isfile(f)]
        if len(files) <= keep:
            return
        files.sort(key=os.path.getmtime)
        for f in files[: len(files) - keep]:
            try:
                os.remove(f)
            except OSError:
                pass
    except OSError:
        pass


def _fetch_album_art(art_url):
    """Resolves an MPRIS `mpris:artUrl` to a local file path. `file://`
    URLs (Spotify's Linux cache, some local players) are used directly;
    `http(s)://` ones (browsers, streaming players) are downloaded once and
    cached to disk keyed by a hash of the URL, so the same track never
    re-fetches. Returns None on anything unusable - the widget then draws a
    plain note-glyph placeholder instead."""
    if not art_url:
        return None
    parsed = urllib.parse.urlparse(art_url)
    if parsed.scheme == "file":
        path = urllib.request.url2pathname(parsed.path)
        return path if os.path.isfile(path) else None
    if parsed.scheme not in ("http", "https"):
        return None

    os.makedirs(ART_CACHE_DIR, exist_ok=True)
    key = hashlib.sha1(art_url.encode()).hexdigest()
    for ext in ("jpg", "jpeg", "png", "webp"):
        cached = os.path.join(ART_CACHE_DIR, f"{key}.{ext}")
        if os.path.isfile(cached):
            return cached

    last_failure = _art_fetch_failures.get(key)
    if last_failure is not None and time.time() - last_failure < ART_FETCH_RETRY_S:
        return None

    ext = parsed.path.rsplit(".", 1)[-1].lower()
    if ext not in ("jpg", "jpeg", "png", "webp"):
        ext = "jpg"
    dest = os.path.join(ART_CACHE_DIR, f"{key}.{ext}")
    try:
        request = urllib.request.Request(art_url, headers={"User-Agent": _USER_AGENT})
        with urllib.request.urlopen(request, timeout=6) as resp:
            data = resp.read()
        with open(dest, "wb") as f:
            f.write(data)
    except (urllib.error.URLError, OSError):
        _art_fetch_failures[key] = time.time()
        return None
    _prune_cache_dir(ART_CACHE_DIR, ART_CACHE_KEEP)
    return dest


def resolve_background_path(config):
    """Figures out which image path load_background() should actually
    display. Starts from the base background_mode:
      - "custom" uses the saved wallpaper path as-is.
      - "desktop" (or anything unset) returns None, i.e. always follow the
        live wallpaper.

    Game Mode (game_mode_enabled) isn't a third alternative to those - it's
    an overlay on top of whichever base is picked, swapping in the
    currently-running game's cover art only while a game is actually
    detected and art for it is fetchable - a portrait grid image in
    vertical orientation, a wide hero image in horizontal (each fits its
    panel shape far better than the other would). The instant no game is
    running (or no art could be fetched), this falls straight back through
    to the base path above - so it's never stuck showing stale art from a
    game that has since closed.
    """
    mode = config.get(
        "background_mode", "custom" if config.get("wallpaper") else "desktop"
    )
    base_path = config.get("wallpaper") if mode == "custom" else None

    if config.get("game_mode_enabled"):
        appid = get_running_game_appid()
        if appid:
            api_key = config.get("steamgriddb_api_key", "")
            fetch = (
                _fetch_game_hero_path
                if config.get("orientation", "vertical") == "horizontal"
                else _fetch_game_grid_path
            )
            image_path = fetch(appid, api_key)
            if image_path:
                return image_path

    return base_path


def _relative_luminance(rgb):
    """Standard perceived-brightness weighting (ITU-R BT.709), 0 (black) to
    1 (white) - human vision is far more sensitive to green than red or
    blue, so a plain (r+g+b)/3 average would misjudge e.g. a pure-green
    background as darker than it reads."""
    r, g, b = (c / 255 for c in rgb)
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def _color_at_luminance(hue, max_sat, target_luminance):
    """Finds an RGB at the given hue whose perceived luminance lands close
    to target_luminance, searching both value and saturation (up to
    max_sat). A saturated hue alone often can't reach a given target - a
    pure, fully-saturated red only reaches ~0.21 luminance even at HSV
    V=1.0 (red gets a small weight in perceived brightness - see
    _relative_luminance), so pushing V without also being willing to
    desaturate would silently fail to actually get any lighter, e.g.
    trying to make a light accent out of an already-vivid red background.
    Desaturating toward white/black is what actually moves luminance the
    rest of the way once the hue's own headroom runs out."""
    best_rgb, best_diff = None, None
    for i in range(13):
        v = i / 12
        for sat_frac in (1.0, 0.7, 0.4, 0.15, 0.0):
            r, g, b = colorsys.hsv_to_rgb(hue, max_sat * sat_frac, v)
            rgb = (r * 255, g * 255, b * 255)
            diff = abs(_relative_luminance(rgb) - target_luminance)
            if best_diff is None or diff < best_diff:
                best_diff, best_rgb = diff, rgb
    return [round(c) for c in best_rgb]


def _compute_auto_colors(bg_img):
    """Derives a color scheme from the background image, in two parts:

    1. Value/text color: plain black-or-white choice based on the
       background's overall luminance (the standard trick for guaranteed
       legible text on an arbitrary background - whichever of the two
       extremes has more contrast against the average brightness always
       reads clearly, unlike trying to pick some "just right" mid-tone).
    2. Label/secondary accent: the background's *own* average hue, not a
       rotated one - rotating away from it (complementary or triadic, both
       tried first) got contrast by picking a hue foreign to the photo,
       which read as random and clashing rather than belonging to it.
       Keeping the same hue makes the accent read as "a vivid shade of
       this photo's own color" instead. Contrast comes from targeting an
       actual perceived-luminance gap against the background via
       _color_at_luminance() (see there for why raw HSV brightness alone
       isn't enough), light against a dark background or dark against a
       light one. Label and secondary target different luminance gaps of
       that *same* hue rather than different hues, so they stay visually
       related to each other and to the photo.

    The separator blends the value color partway into the average
    background tone, keeping it the same subtle, low-key divider the
    fixed color schemes use rather than a high-contrast line competing
    for attention.
    """
    avg_r, avg_g, avg_b = bg_img.resize((1, 1), Image.LANCZOS).getpixel((0, 0))[:3]
    dark_bg = _relative_luminance((avg_r, avg_g, avg_b)) < 0.5

    value_color = [255, 255, 255] if dark_bg else [20, 20, 20]

    h, s, _v = colorsys.rgb_to_hsv(avg_r / 255, avg_g / 255, avg_b / 255)
    accent_sat = max(s, 0.6)
    label_rgb = _color_at_luminance(h, accent_sat, 0.88 if dark_bg else 0.12)
    secondary_rgb = _color_at_luminance(h, accent_sat, 0.68 if dark_bg else 0.28)

    separator_color = [
        round(0.35 * v + 0.65 * bg) for v, bg in zip(value_color, (avg_r, avg_g, avg_b))
    ]

    return {
        "label": label_rgb,
        "value": value_color,
        "secondary": secondary_rgb,
        "separator": separator_color,
    }


_background_cache = {
    "path": None, "mtime": None, "canvas_size": None, "image": None, "auto_colors": None,
}


def get_auto_colors(wallpaper_override=None, canvas_size=(WIDTH, HEIGHT)):
    """Colors derived from the current background image (see
    _compute_auto_colors) - cached alongside the background itself in
    load_background(), so this is only recomputed when the background
    actually changes, not every frame."""
    load_background(wallpaper_override, canvas_size)  # ensures the cache below is current
    return _background_cache["auto_colors"] or DEFAULT_COLORS


def load_background(wallpaper_override=None, canvas_size=(WIDTH, HEIGHT)):
    """Loads the panel background, center-cropped and scaled to fill the
    given logical canvas size (see CANVAS_SIZES - portrait for "vertical",
    landscape for "horizontal") and dimmed so stat text stays readable over
    it. If wallpaper_override is a readable file, it's used as-is (this is
    how the GUI's chosen image and the saved config's override both flow
    in); otherwise falls back to the desktop wallpaper. Cached and only
    re-decoded if the effective path, its mtime, or the requested
    canvas_size changes (switching orientation needs a differently-cropped
    image, not just a resize). Falls back to a plain dark background if
    nothing is set/found/readable."""
    if wallpaper_override and os.path.isfile(wallpaper_override):
        path = wallpaper_override
    else:
        path = get_wallpaper_path()
    if path is None or not os.path.isfile(path):
        fallback = Image.new("RGB", canvas_size, BG_FALLBACK)
        if _background_cache["path"] is not None or _background_cache["canvas_size"] != canvas_size:
            # was showing a real image (or a different-sized fallback)
            # before, now isn't - keep auto_colors in sync rather than
            # leaving it stale from that last image.
            _background_cache.update(
                path=None, mtime=None, canvas_size=canvas_size, image=fallback,
                auto_colors=_compute_auto_colors(fallback),
            )
        return fallback

    mtime = os.path.getmtime(path)
    if (_background_cache["path"] == path and _background_cache["mtime"] == mtime
            and _background_cache["canvas_size"] == canvas_size):
        return _background_cache["image"]

    try:
        src = Image.open(path).convert("RGB")
        canvas_w, canvas_h = canvas_size
        target_ratio = canvas_w / canvas_h
        # Which axis to fit is forced by the canvas' own shape (portrait
        # canvas -> fit height, landscape -> fit width), not auto-picked by
        # comparing to the source image's own aspect ratio - an unusual
        # source photo (e.g. already portrait-shaped) would otherwise flip
        # which axis gets cropped instead of always cropping the same one
        # for a given orientation.
        if canvas_h >= canvas_w:
            new_width = round(src.height * target_ratio)
            left = (src.width - new_width) // 2
            src = src.crop((left, 0, left + new_width, src.height))
        else:
            new_height = round(src.width / target_ratio)
            top = (src.height - new_height) // 2
            src = src.crop((0, top, src.width, top + new_height))
        src = src.resize(canvas_size, Image.LANCZOS)
        img = Image.blend(src, Image.new("RGB", canvas_size, (0, 0, 0)),
                           BG_DIM_ALPHA / 255)
    except (OSError, ValueError):
        img = Image.new("RGB", canvas_size, BG_FALLBACK)

    _background_cache.update(
        path=path, mtime=mtime, canvas_size=canvas_size, image=img,
        auto_colors=_compute_auto_colors(img),
    )
    return img

_fps_history = deque(maxlen=200)


def _ellipsize(draw, text, font, max_width):
    """Trims `text` to fit `max_width`, appending an ellipsis if it had to
    cut anything. Binary search over the cut point rather than measuring one
    character at a time."""
    text = " ".join(text.split())
    if not text or draw.textlength(text, font=font) <= max_width:
        return text
    ell = "…"
    lo, hi = 0, len(text)
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if draw.textlength(text[:mid].rstrip() + ell, font=font) <= max_width:
            lo = mid
        else:
            hi = mid - 1
    return (text[:lo].rstrip() + ell) if lo else ell


def _prep_thumb(art_path, size, colors):
    """(image, mask) for a rounded square album-art thumbnail of the given
    side length - a note-glyph placeholder when art_path is missing or
    unreadable."""
    thumb = None
    if art_path:
        try:
            thumb = Image.open(art_path).convert("RGB").resize((size, size), Image.LANCZOS)
        except (OSError, ValueError):
            thumb = None
    if thumb is None:
        thumb = Image.new("RGB", (size, size),
                          tuple(round(0.28 * c) for c in colors["value"]))
        td = ImageDraw.Draw(thumb)
        nf = _font(round(size * 0.55))
        nb = td.textbbox((0, 0), "♫", font=nf)
        td.text(((size - (nb[2] - nb[0])) / 2 - nb[0],
                 (size - (nb[3] - nb[1])) / 2 - nb[1]),
                "♫", font=nf, fill=tuple(colors["secondary"]))
    mask = Image.new("L", (size, size), 0)
    ImageDraw.Draw(mask).rounded_rectangle(
        [0, 0, size - 1, size - 1], radius=round(size * 0.12), fill=255
    )
    return thumb, mask


def _draw_progress_bar(draw, x, y, w, h, progress, colors):
    draw.rounded_rectangle([x, y, x + w, y + h], radius=h / 2,
                           fill=tuple(colors["separator"]))
    filled = max(0.0, min(1.0, progress)) * w
    if filled > h:
        draw.rounded_rectangle([x, y, x + filled, y + h], radius=h / 2,
                               fill=tuple(colors["label"]))


def _draw_music_block(img, draw, box, art_path, title, artist, progress, colors, stacked=False):
    """Draws the "now playing" widget into `box` (x, y, w, h).

    stacked=False (landscape strip): rounded thumbnail on the left, title
    over artist to its right, progress bar under the text - sizes scale off
    the box's height.

    stacked=True (narrow vertical panel): a big centered thumbnail
    (~half the panel width) with centered title/artist and a full-width
    progress bar below it - sizes scale off the box's width, since text
    there gets the whole width rather than a cramped strip beside the art.

    `art_path` None draws a note-glyph placeholder either way."""
    x, y, w, h = box
    title = title or "Unknown"
    artist = artist or ""

    if stacked:
        art_size = round(w * 0.55)
        thumb, mask = _prep_thumb(art_path, art_size, colors)
        img.paste(thumb, (x + (w - art_size) // 2, y), mask)

        cy = y + art_size + round(w * 0.05)
        title_font = artist_font = _font(round(w * 0.083))
        title_line = _ellipsize(draw, title, title_font, w)
        artist_line = _ellipsize(draw, artist, artist_font, w)

        tb = draw.textbbox((0, 0), title_line, font=title_font)
        tw = draw.textlength(title_line, font=title_font)
        draw.text((x + (w - tw) / 2, cy - tb[1]), title_line,
                  font=title_font, fill=tuple(colors["value"]))
        cy += (tb[3] - tb[1]) + round(w * 0.03)
        if artist_line:
            ab = draw.textbbox((0, 0), artist_line, font=artist_font)
            aw = draw.textlength(artist_line, font=artist_font)
            draw.text((x + (w - aw) / 2, cy - ab[1]), artist_line,
                      font=artist_font, fill=tuple(colors["secondary"]))
            cy += (ab[3] - ab[1]) + round(w * 0.045)
        if progress is not None:
            _draw_progress_bar(draw, x, cy, w, max(3, round(w * 0.016)), progress, colors)
        return

    pad = round(h * 0.08)
    art_size = max(1, min(h - 2 * pad, round(w * 0.26)))
    art_x, art_y = x + pad, y + pad
    thumb, mask = _prep_thumb(art_path, art_size, colors)
    img.paste(thumb, (art_x, art_y), mask)

    text_x = art_x + art_size + round(h * 0.10)
    text_w = max(1, x + w - pad - text_x)
    title_font = artist_font = _font(round(h * 0.19))
    title_line = _ellipsize(draw, title, title_font, text_w)
    artist_line = _ellipsize(draw, artist, artist_font, text_w)

    tb = draw.textbbox((0, 0), title_line or "X", font=title_font)
    ab = draw.textbbox((0, 0), artist_line or "X", font=artist_font)
    line_gap = round(h * 0.09)
    bar_h = max(3, round(h * 0.05))
    block_h = (tb[3] - tb[1]) + line_gap + (ab[3] - ab[1]) + line_gap + bar_h
    cy = y + (h - block_h) / 2

    draw.text((text_x, cy - tb[1]), title_line, font=title_font, fill=tuple(colors["value"]))
    cy += (tb[3] - tb[1]) + line_gap
    if artist_line:
        draw.text((text_x, cy - ab[1]), artist_line, font=artist_font,
                  fill=tuple(colors["secondary"]))
    cy += (ab[3] - ab[1]) + line_gap
    if progress is not None:
        _draw_progress_bar(draw, text_x, cy, text_w, bar_h, progress, colors)


def _draw_device_block(draw, y, label, value_text, secondary_parts, colors):
    """Draws one `LABEL` / big-value block with optional smaller secondary
    readings (temperature, VRAM, power, ...) packed onto a single line right
    below it - the shared "device" visual theme CPU and GPU both use in the
    vertical layout (a stack of these, top to bottom). secondary_parts is a
    list of text strings (colors["secondary"] is used for all of them -
    callers used to pass a color per part, but every caller always passed
    the same one anyway); pass [] for none."""
    draw.text((20, y), label, font=FONT_MED, fill=tuple(colors["label"]))
    y += 44
    draw.text((20, y), value_text, font=FONT_BIG, fill=tuple(colors["value"]))
    if secondary_parts:
        y += 70
        x = 20
        for text in secondary_parts:
            draw.text((x, y), text, font=FONT_MED, fill=tuple(colors["secondary"]))
            x += draw.textlength(text, font=FONT_MED) + 24
        y += 60
    else:
        y += 90
    return y


def _render_vertical(img, draw, canvas_w, canvas_h, sensors, cpu, mem, cpu_temp,
                      gpu_load, gpu_temp, gpu_vram_used, gpu_power,
                      fps, fps_low1, frametime, colors, music):
    """The original portrait layout: one column, each enabled block stacked
    top to bottom (CPU, RAM, GPU, a separator, FPS/1% low/frame time), with
    the clock (and the now-playing widget just above it) pinned to the
    bottom regardless of what's above them."""
    label_color = tuple(colors["label"])
    value_color = tuple(colors["value"])
    secondary_color = tuple(colors["secondary"])
    separator_color = tuple(colors["separator"])

    y = 40
    if sensors.get("cpu", True):
        secondary = []
        if sensors.get("cpu_temp", True) and cpu_temp is not None:
            secondary.append(f"{cpu_temp:.0f}°C")
        y = _draw_device_block(draw, y, "CPU", f"{cpu:.0f}%", secondary, colors)

    if sensors.get("ram", True):
        y = _draw_device_block(draw, y, "RAM", f"{mem:.0f}%", [], colors)

    if sensors.get("gpu", True) and gpu_load is not None:
        secondary = []
        if sensors.get("gpu_temp", True) and gpu_temp is not None:
            secondary.append(f"{gpu_temp:.0f}°C")
        if sensors.get("gpu_vram", True) and gpu_vram_used is not None:
            secondary.append(f"{gpu_vram_used / 1024:.1f}GB")
        if sensors.get("gpu_power", True) and gpu_power is not None:
            secondary.append(f"{gpu_power:.0f}W")
        y = _draw_device_block(draw, y, "GPU", f"{gpu_load:.0f}%", secondary, colors)

    if sensors.get("fps", True) or sensors.get("frametime", True):
        y += 30
        draw.line([(20, y), (canvas_w - 20, y)], fill=separator_color, width=2)
        y += 30

    if sensors.get("fps", True):
        draw.text((20, y), "FPS", font=FONT_MED, fill=label_color)
        y += 44
        draw.text((20, y), f"{fps:.1f}" if fps is not None else "--", font=FONT_BIG, fill=value_color)
        y += 90

        draw.text((20, y), "1% LOW", font=FONT_MED, fill=label_color)
        y += 44
        draw.text((20, y), f"{fps_low1:.1f}" if fps_low1 is not None else "--", font=FONT_BIG, fill=value_color)
        y += 90

    if sensors.get("frametime", True):
        draw.text((20, y), "FRAME TIME", font=FONT_MED, fill=label_color)
        y += 44
        draw.text((20, y), f"{frametime:.1f}ms" if frametime is not None else "--", font=FONT_BIG, fill=value_color)
        y += 90

    if sensors.get("music", True) and music:
        bw = canvas_w - 40
        # art (~0.55*w) + room for the two centered text lines and the bar
        block_h = round(bw * 0.55) + round(bw * 0.34)
        block_bottom = (canvas_h - 140 - 40) if sensors.get("clock", True) else (canvas_h - 40)
        _draw_music_block(
            img, draw, (20, block_bottom - block_h, bw, block_h),
            _fetch_album_art(music.get("art_url")),
            music.get("title"), music.get("artist"), _music_progress(music), colors,
            stacked=True,
        )

    if sensors.get("clock", True):
        y = canvas_h - 140
        for text, font, fill in (
            (time.strftime("%H:%M:%S"), FONT_BIG, value_color),
            (time.strftime("%d-%m-%Y"), FONT_DATE, secondary_color),
        ):
            tw = draw.textlength(text, font=font)
            draw.text(((canvas_w - tw) / 2, y), text, font=font, fill=fill)
            y += 70


def _draw_column(draw, x0, col_width, canvas_h, label, value_text, secondary_text, colors):
    """Draws one label/value/secondary group centered (both axes) within a
    column of the given width - the horizontal layout's equivalent of
    _draw_device_block, since a short, wide canvas has room for several of
    these side by side but not stacked on top of each other. label=None
    skips that line entirely (used for the clock, which the vertical layout
    also shows with no label above it, just the time then the date)."""
    lines = []
    if label:
        lines.append((label, FONT_LABEL_H, tuple(colors["label"])))
    lines.append((value_text, FONT_VALUE_H, tuple(colors["value"])))
    if secondary_text:
        lines.append((secondary_text, FONT_SECONDARY_H, tuple(colors["secondary"])))

    gap = 8
    heights = [draw.textbbox((0, 0), text, font=font)[3] for text, font, _ in lines]
    total_h = sum(heights) + gap * (len(lines) - 1)
    y = (canvas_h - total_h) / 2
    for (text, font, color), h in zip(lines, heights):
        w = draw.textlength(text, font=font)
        draw.text((x0 + (col_width - w) / 2, y), text, font=font, fill=color)
        y += h + gap


def _render_horizontal(img, draw, canvas_w, canvas_h, sensors, cpu, mem, cpu_temp,
                        gpu_load, gpu_temp, gpu_vram_used, gpu_power,
                        fps, fps_low1, frametime, colors, music):
    """The landscape layout: since the canvas is short (canvas_h is the
    panel's native WIDTH, 462px) but wide (canvas_w is its native HEIGHT,
    1920px), there's no room to stack blocks vertically the way the
    portrait layout does - instead each enabled reading gets its own
    column, all in a single row, sized to evenly fill the width. Grouped
    the same way the vertical layout's separator line groups them (system
    stats vs. game stats+clock), just as a vertical divider instead. The
    now-playing widget, when shown, takes a full-width strip along the
    bottom and the columns center in the height left above it."""
    music_strip_h = round(canvas_h * 0.32) if (sensors.get("music", True) and music) else 0
    content_h = canvas_h - music_strip_h

    system_cols = []
    if sensors.get("cpu", True):
        secondary = (
            f"{cpu_temp:.0f}°C" if sensors.get("cpu_temp", True) and cpu_temp is not None else None
        )
        system_cols.append(("CPU", f"{cpu:.0f}%", secondary))
    if sensors.get("ram", True):
        system_cols.append(("RAM", f"{mem:.0f}%", None))
    if sensors.get("gpu", True) and gpu_load is not None:
        parts = []
        if sensors.get("gpu_temp", True) and gpu_temp is not None:
            parts.append(f"{gpu_temp:.0f}°C")
        if sensors.get("gpu_vram", True) and gpu_vram_used is not None:
            parts.append(f"{gpu_vram_used / 1024:.1f}GB")
        if sensors.get("gpu_power", True) and gpu_power is not None:
            parts.append(f"{gpu_power:.0f}W")
        system_cols.append(("GPU", f"{gpu_load:.0f}%", " ".join(parts) if parts else None))

    game_cols = []
    if sensors.get("fps", True):
        game_cols.append(("FPS", f"{fps:.1f}" if fps is not None else "--", None))
        game_cols.append(("1% LOW", f"{fps_low1:.1f}" if fps_low1 is not None else "--", None))
    if sensors.get("frametime", True):
        game_cols.append(("FRAME TIME", f"{frametime:.1f}ms" if frametime is not None else "--", None))
    if sensors.get("clock", True):
        game_cols.append((None, time.strftime("%H:%M:%S"), time.strftime("%d-%m-%Y")))

    columns = system_cols + game_cols
    if columns:
        col_width = canvas_w / len(columns)
        for i, (label, value_text, secondary_text) in enumerate(columns):
            _draw_column(draw, i * col_width, col_width, content_h,
                         label, value_text, secondary_text, colors)

        if system_cols and game_cols:
            sep_x = len(system_cols) * col_width
            margin = content_h * 0.2
            draw.line([(sep_x, margin), (sep_x, content_h - margin)],
                      fill=tuple(colors["separator"]), width=2)

    if music_strip_h:
        _draw_music_block(
            img, draw, (20, content_h, canvas_w - 40, music_strip_h - 12),
            _fetch_album_art(music.get("art_url")),
            music.get("title"), music.get("artist"), _music_progress(music), colors,
        )


def render_stats_pil(config=None):
    """Renders one frame as an upright (non-rotated) PIL Image, at whichever
    logical canvas size and layout the config's orientation calls for (see
    CANVAS_SIZES/_render_vertical/_render_horizontal). config defaults to
    the saved on-disk config; the GUI passes its own in-memory (not-yet-
    applied) selections here to preview them before saving."""
    if config is None:
        config = get_config()
    sensors = config.get("sensors", DEFAULT_SENSORS)
    orientation = config.get("orientation", "vertical")
    if orientation not in ORIENTATIONS:
        orientation = "vertical"
    canvas_size = CANVAS_SIZES[orientation]

    game_stats = get_game_stats()
    fps = game_stats.get("fps")
    frametime = game_stats.get("frametime")
    if fps is None:
        _fps_history.clear()
        fps_low1 = None
    else:
        _fps_history.append(fps)
        fps_low1 = fps
        if len(_fps_history) >= 10:
            sample = sorted(_fps_history)
            cutoff = max(1, len(sample) // 100)
            fps_low1 = sum(sample[:cutoff]) / cutoff

    bg_path = resolve_background_path(config)

    if config.get("color_mode", "custom") == "auto":
        colors = get_auto_colors(bg_path, canvas_size)
    else:
        colors = config.get("colors", DEFAULT_COLORS)

    img = load_background(bg_path, canvas_size).copy()  # copy: caller
                                     # draws on this, cached original must
                                     # stay untouched
    draw = ImageDraw.Draw(img)

    cpu = psutil.cpu_percent()
    mem = psutil.virtual_memory().percent
    cpu_temp = get_cpu_temp()
    gpu_load, gpu_temp, gpu_vram_used, gpu_vram_total, gpu_power = get_gpu_stats()
    music = get_music_info() if sensors.get("music", True) else {}

    canvas_w, canvas_h = canvas_size
    render_fn = _render_horizontal if orientation == "horizontal" else _render_vertical
    render_fn(img, draw, canvas_w, canvas_h, sensors, cpu, mem, cpu_temp,
              gpu_load, gpu_temp, gpu_vram_used, gpu_power,
              fps, fps_low1, frametime, colors, music)

    return img


# Landscape logical canvas -> portrait physical buffer. Flip the sign here
# if the panel ends up mounted the other way around in practice - there's
# no way to know which without the physical hardware in hand.
HORIZONTAL_ROTATE_DEGREES = -90


def render_stats_image(config=None):
    """Renders one frame as JPEG bytes, in the physical panel's fixed
    462x1920 buffer size regardless of the logical orientation chosen in
    settings - "horizontal" renders its own wide layout first (see
    render_stats_pil) and gets rotated into the tall physical buffer here.
    That rotation is separate from, and applied before, the unconditional
    180-degree one below, which is a fixed hardware/protocol quirk (see
    risemode_driver.py's protocol notes) unrelated to the chosen
    orientation."""
    if config is None:
        config = get_config()
    img = render_stats_pil(config)
    if config.get("orientation", "vertical") == "horizontal":
        img = img.rotate(HORIZONTAL_ROTATE_DEGREES, expand=True)
    img = img.rotate(180)
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=85)
    return buf.getvalue()
