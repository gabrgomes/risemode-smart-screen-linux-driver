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
import io
import json
import os
import subprocess
import time
import urllib.parse
import urllib.request
from collections import deque

import psutil
from PIL import Image, ImageDraw, ImageFont

WIDTH, HEIGHT = 462, 1920

MANGOHUD_LOG_DIR = os.path.expanduser("~/.local/share/mangohud_logs")
MANGOHUD_STALE_S = 3  # ignore logs that haven't been touched recently -
                       # means no game/GL app is currently running

BG_DIM_ALPHA = 140  # 0-255; darkens the wallpaper so stat text stays legible
                    # over bright/busy photos
BG_FALLBACK = (15, 15, 25)

CONFIG_PATH = os.path.expanduser("~/.config/risemode-screen/config.json")
DEFAULT_SENSORS = {
    "cpu": True, "cpu_temp": True, "ram": True,
    "gpu": True, "gpu_temp": True, "gpu_vram": True, "gpu_power": True,
    "fps": True, "frametime": True,
    "clock": True,
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
    return {
        "wallpaper": data.get("wallpaper"),
        "sensors": sensors,
        "colors": colors,
        "color_mode": color_mode,
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

FONT_DATE = load_font(54)
FONT_BIG = load_font(64)
FONT_MED = load_font(36)


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


_background_cache = {"path": None, "mtime": None, "image": None, "auto_colors": None}


def get_auto_colors(wallpaper_override=None):
    """Colors derived from the current background image (see
    _compute_auto_colors) - cached alongside the background itself in
    load_background(), so this is only recomputed when the background
    actually changes, not every frame."""
    load_background(wallpaper_override)  # ensures the cache below is current
    return _background_cache["auto_colors"] or DEFAULT_COLORS


def load_background(wallpaper_override=None):
    """Loads the panel background, center-cropped and scaled to fill the
    panel (462x1920, portrait) and dimmed so stat text stays readable over
    it. If wallpaper_override is a readable file, it's used as-is (this is
    how the GUI's chosen image and the saved config's override both flow
    in); otherwise falls back to the desktop wallpaper. Cached and only
    re-decoded if the effective path or its mtime changes. Falls back to a
    plain dark background if nothing is set/found/readable."""
    if wallpaper_override and os.path.isfile(wallpaper_override):
        path = wallpaper_override
    else:
        path = get_wallpaper_path()
    if path is None or not os.path.isfile(path):
        fallback = Image.new("RGB", (WIDTH, HEIGHT), BG_FALLBACK)
        if _background_cache["path"] is not None:
            # was showing a real image before, now isn't - keep auto_colors
            # in sync rather than leaving it stale from that last image.
            _background_cache.update(
                path=None, mtime=None, image=fallback,
                auto_colors=_compute_auto_colors(fallback),
            )
        return fallback

    mtime = os.path.getmtime(path)
    if _background_cache["path"] == path and _background_cache["mtime"] == mtime:
        return _background_cache["image"]

    try:
        src = Image.open(path).convert("RGB")
        target_ratio = WIDTH / HEIGHT
        src_ratio = src.width / src.height
        if src_ratio > target_ratio:
            new_width = round(src.height * target_ratio)
            left = (src.width - new_width) // 2
            src = src.crop((left, 0, left + new_width, src.height))
        else:
            new_height = round(src.width / target_ratio)
            top = (src.height - new_height) // 2
            src = src.crop((0, top, src.width, top + new_height))
        src = src.resize((WIDTH, HEIGHT), Image.LANCZOS)
        img = Image.blend(src, Image.new("RGB", (WIDTH, HEIGHT), (0, 0, 0)),
                           BG_DIM_ALPHA / 255)
    except (OSError, ValueError):
        img = Image.new("RGB", (WIDTH, HEIGHT), BG_FALLBACK)

    _background_cache.update(
        path=path, mtime=mtime, image=img, auto_colors=_compute_auto_colors(img)
    )
    return img

_fps_history = deque(maxlen=200)


def _draw_device_block(draw, y, label, value_text, secondary_parts, colors):
    """Draws one `LABEL` / big-value block with optional smaller secondary
    readings (temperature, VRAM, power, ...) packed onto a single line right
    below it - the shared "device" visual theme CPU and GPU both use.
    secondary_parts is a list of text strings (colors["secondary"] is used
    for all of them - callers used to pass a color per part, but every
    caller always passed the same one anyway); pass [] for none."""
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


def render_stats_pil(config=None):
    """Renders one frame as an upright (non-rotated) PIL Image. config
    defaults to the saved on-disk config; the GUI passes its own in-memory
    (not-yet-applied) selections here to preview them before saving."""
    if config is None:
        config = get_config()
    sensors = config.get("sensors", DEFAULT_SENSORS)

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

    if config.get("color_mode", "custom") == "auto":
        colors = get_auto_colors(config.get("wallpaper"))
    else:
        colors = config.get("colors", DEFAULT_COLORS)
    label_color = tuple(colors["label"])
    value_color = tuple(colors["value"])
    secondary_color = tuple(colors["secondary"])
    separator_color = tuple(colors["separator"])

    img = load_background(config.get("wallpaper")).copy()  # copy: caller
                                     # draws on this, cached original must
                                     # stay untouched
    draw = ImageDraw.Draw(img)

    cpu = psutil.cpu_percent()
    mem = psutil.virtual_memory().percent
    cpu_temp = get_cpu_temp()
    gpu_load, gpu_temp, gpu_vram_used, gpu_vram_total, gpu_power = get_gpu_stats()

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
        draw.line([(20, y), (WIDTH - 20, y)], fill=separator_color, width=2)
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

    if sensors.get("clock", True):
        y = HEIGHT - 140
        draw.text((20, y), time.strftime("%H:%M:%S"), font=FONT_BIG, fill=value_color)
        y += 70
        draw.text((20, y), time.strftime("%d-%m-%Y"), font=FONT_DATE, fill=secondary_color)

    return img


def render_stats_image(config=None):
    """Renders one frame as JPEG bytes, rotated 180 degrees as the panel
    expects (see risemode_driver.py's protocol notes)."""
    img = render_stats_pil(config).rotate(180)
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=85)
    return buf.getvalue()
