#!/usr/bin/env python3
"""
Native Linux driver for the Risemode RM-SCP-B "Smart Screen 9.2" USB panel
(VID 0x2100 / PID 0x0003, sold as "RT Systems HOTSPOTEKUSB HID DEMO").

Protocol reverse-engineered from a USB capture of the official Windows app:
  - Single HID interrupt OUT endpoint (0x01, 1024 byte packets) carries commands.
  - Panel is 462 x 1920, image expected as a plain JPEG rotated 180 degrees.
  - A frame is sent as: 32-byte header + JPEG bytes, chunked into 1024-byte
    packets (last packet zero-padded).
  - A CONNECT keep-alive must be resent roughly every 10 seconds or the panel
    drops the session and blanks.
  - Do not call set_configuration() when the device is already configured -
    on this firmware it silently caps the session to about a second.

Header layout (32 bytes):
  0-3   "CRT\0"                  magic
  4     0x00
  5-7   "DRA"                    command name ("draw")
  8     0x00
  9-11  big-endian uint24        total length = 32 (header) + len(jpeg)
  12    0xB1                     constant marker
  13-31 0x00                     padding
"""
import glob
import io
import os
import subprocess
import sys
import threading
import time
import urllib.parse
import urllib.request
from collections import deque

import psutil
import usb.core
import usb.util
from PIL import Image, ImageDraw, ImageFont

MANGOHUD_LOG_DIR = os.path.expanduser("~/.local/share/mangohud_logs")
MANGOHUD_STALE_S = 3  # ignore logs that haven't been touched recently -
                       # means no game/GL app is currently running


def get_gpu_stats():
    try:
        out = subprocess.check_output(
            ["nvidia-smi", "--query-gpu=utilization.gpu,temperature.gpu",
             "--format=csv,noheader,nounits"],
            timeout=1,
        ).decode().strip()
        load, temp = out.split(",")
        return float(load), float(temp)
    except (subprocess.SubprocessError, OSError, ValueError):
        return None, None


def get_gpu_fps():
    """Reads the live FPS of whatever game/GL app MangoHud is currently
    logging (see README for setup). Returns None if nothing is running."""
    try:
        logs = [
            p for p in glob.glob(os.path.join(MANGOHUD_LOG_DIR, "*.csv"))
            if not p.endswith("_summary.csv")
        ]
        if not logs:
            return None
        latest = max(logs, key=os.path.getmtime)
        if time.time() - os.path.getmtime(latest) > MANGOHUD_STALE_S:
            return None
        with open(latest, "rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            f.seek(max(0, size - 4096))
            lines = f.read().decode(errors="ignore").splitlines()
        for line in reversed(lines):
            if line and line[0].isdigit():
                return float(line.split(",")[0])
        return None
    except (OSError, ValueError):
        return None

VENDOR_ID = 0x2100
PRODUCT_ID = 0x0003
EP_OUT = 0x01
PACKET_SIZE = 1024
WIDTH, HEIGHT = 462, 1920
CONNECT_INTERVAL_S = 8
SESSION_MAX_S = 20  # proactively reconnect periodically; this firmware
                      # occasionally wedges itself after a while and only a
                      # fresh USB reset (done in find_device()) clears it
FRAME_INTERVAL_S = 0.3

CONNECT_PACKET = bytes.fromhex("4352540000434f4e4e454354") + b"\x00" * (
    PACKET_SIZE - 12
)


def build_brightness_packet(value):
    header = bytearray(13)
    header[0:4] = b"CRT\x00"
    header[4] = 0x00
    header[5:8] = b"LIG"
    header[8] = 0x00
    header[9] = 0x00
    header[10] = 0x00
    header[11] = value
    header[12] = 0x00
    return bytes(header) + b"\x00" * (PACKET_SIZE - 13)


def build_draw_header(jpeg_len):
    total_len = jpeg_len + 32
    header = bytearray(32)
    header[0:4] = b"CRT\x00"
    header[4] = 0x00
    header[5:8] = b"DRA"
    header[8] = 0x00
    header[9:12] = total_len.to_bytes(3, "big")
    header[12] = 0xB1
    return bytes(header)


def find_device():
    dev = usb.core.find(idVendor=VENDOR_ID, idProduct=PRODUCT_ID)
    if dev is None:
        raise ValueError("Risemode device not found (2100:0003)")

    # This firmware accumulates bad internal state across repeated
    # claim/release cycles (e.g. handing the device to/from a VM). A plain
    # USB bus reset clears it; without this the panel will render for only
    # ~1-3 seconds before going dark, even though every USB transfer keeps
    # completing successfully.
    dev.reset()
    time.sleep(0.5)
    dev = usb.core.find(idVendor=VENDOR_ID, idProduct=PRODUCT_ID)

    for cfg in dev:
        for intf in cfg:
            if dev.is_kernel_driver_active(intf.bInterfaceNumber):
                dev.detach_kernel_driver(intf.bInterfaceNumber)

    # Calling set_configuration() when already configured also truncates
    # the session to ~1 second on this firmware - only call it if needed.
    try:
        dev.get_active_configuration()
    except usb.core.USBError:
        dev.set_configuration()

    usb.util.claim_interface(dev, 0)
    return dev


def send_frame(dev, jpeg_bytes):
    payload = build_draw_header(len(jpeg_bytes)) + jpeg_bytes
    pad_len = (-len(payload)) % PACKET_SIZE
    payload += b"\x00" * pad_len
    for i in range(0, len(payload), PACKET_SIZE):
        dev.write(EP_OUT, payload[i : i + PACKET_SIZE])


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

BG_DIM_ALPHA = 140  # 0-255; darkens the wallpaper so stat text stays legible
                    # over bright/busy photos
BG_FALLBACK = (15, 15, 25)


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


_background_cache = {"path": None, "mtime": None, "image": None}


def load_background():
    """Loads the current desktop wallpaper, center-cropped and scaled to
    fill the panel (462x1920, portrait) and dimmed so stat text stays
    readable over it. Cached and only re-decoded if the wallpaper changes.
    Falls back to a plain dark background if none is set/found/readable."""
    path = get_wallpaper_path()
    if path is None or not os.path.isfile(path):
        return Image.new("RGB", (WIDTH, HEIGHT), BG_FALLBACK)

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

    _background_cache.update(path=path, mtime=mtime, image=img)
    return img

_fps_history = deque(maxlen=200)


def render_stats_image():
    fps = get_gpu_fps()
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

    img = load_background().copy()  # copy: we draw on this every frame, the
                                     # cached original must stay untouched
    draw = ImageDraw.Draw(img)

    cpu = psutil.cpu_percent()
    mem = psutil.virtual_memory().percent
    gpu_load, gpu_temp = get_gpu_stats()

    y = 40
    draw.text((20, y), "CPU", font=FONT_MED, fill=(0, 200, 255))
    y += 44
    draw.text((20, y), f"{cpu:.0f}%", font=FONT_BIG, fill=(255, 255, 255))
    y += 90

    draw.text((20, y), "RAM", font=FONT_MED, fill=(0, 200, 255))
    y += 44
    draw.text((20, y), f"{mem:.0f}%", font=FONT_BIG, fill=(255, 255, 255))
    y += 90

    if gpu_load is not None:
        draw.text((20, y), "GPU", font=FONT_MED, fill=(0, 200, 255))
        y += 44
        draw.text((20, y), f"{gpu_load:.0f}%", font=FONT_BIG, fill=(255, 255, 255))
        y += 70
        draw.text((20, y), f"{gpu_temp:.0f}C", font=FONT_MED, fill=(255, 150, 0))
        y += 60

    y += 30
    draw.line([(20, y), (WIDTH - 20, y)], fill=(60, 60, 80), width=2)
    y += 30

    draw.text((20, y), "FPS", font=FONT_MED, fill=(0, 200, 255))
    y += 44
    draw.text((20, y), f"{fps:.1f}" if fps is not None else "--", font=FONT_BIG, fill=(255, 255, 255))
    y += 90

    draw.text((20, y), "1% LOW", font=FONT_MED, fill=(0, 200, 255))
    y += 44
    draw.text((20, y), f"{fps_low1:.1f}" if fps_low1 is not None else "--", font=FONT_BIG, fill=(255, 255, 255))
    y += 90

    y = HEIGHT - 140
    draw.text((20, y), time.strftime("%H:%M:%S"), font=FONT_BIG, fill=(255, 255, 0))
    y += 70
    draw.text((20, y), time.strftime("%d-%m-%Y"), font=FONT_DATE, fill=(200, 200, 200))

    img = img.rotate(180)
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=85)
    return buf.getvalue()


def poll_in_endpoint(dev, stop_event):
    while not stop_event.is_set():
        try:
            dev.read(0x82, 512, timeout=50)
        except usb.core.USBError:
            pass


def run():
    dev = find_device()
    stop_event = threading.Event()
    poller = threading.Thread(target=poll_in_endpoint, args=(dev, stop_event), daemon=True)
    poller.start()

    try:
        print("Device claimed. Sending CONNECT...")
        dev.write(EP_OUT, CONNECT_PACKET)
        time.sleep(0.2)
        session_start = time.time()
        last_connect = session_start
        frame_num = 0
        print("Streaming (Ctrl+C to stop)...")
        while True:
            now = time.time()
            if now - session_start > SESSION_MAX_S:
                print("Proactive periodic reconnect...")
                return
            if now - last_connect > CONNECT_INTERVAL_S:
                dev.write(EP_OUT, CONNECT_PACKET)
                last_connect = now
            jpeg = render_stats_image()
            send_frame(dev, jpeg)
            frame_num += 1
            time.sleep(FRAME_INTERVAL_S)
    finally:
        stop_event.set()
        poller.join(timeout=1)
        try:
            usb.util.release_interface(dev, 0)
        except usb.core.USBError:
            pass
        usb.util.dispose_resources(dev)


def main():
    while True:
        try:
            run()
        except KeyboardInterrupt:
            print("\nStopped.")
            return 0
        except (usb.core.USBError, ValueError) as e:
            print(f"USB error: {e}. Retrying in 3s...")
            time.sleep(3)


if __name__ == "__main__":
    sys.exit(main())
