#!/usr/bin/env python3
"""Does the LIG (brightness) command set the panel's backlight persistently
now that sessions start with DIS? (Earlier tests, without DIS, only saw a
flash.) Streams a bright, static test image the Windows way (DIS -> LIG -> ~16
fps, CONNECT every 10 s) and steps the LIG value every STEP_S seconds while
the image itself never changes - so any change in how bright the panel looks
is the backlight. The current value is printed here and drawn on the panel.

Stop the service first:   systemctl --user stop risemode-screen.service
Run:                      ./venv/bin/python3 brightness_test.py
"""
import functools
import io
import sys
import time

import usb.core
import usb.util
from PIL import Image, ImageDraw, ImageFont

from risemode_driver import (
    CONNECT_INTERVAL_S, CONNECT_PACKET, DIS_PACKET, DIS_TO_LIG_S, EP_OUT,
    GET_REPORT_TO_DIS_S, LIG_TO_FIRST_FRAME_S, build_brightness_packet, find_device,
    read_firmware_string, send_frame,
)

WIDTH, HEIGHT = 462, 1920
STEP_S = 10
VALUES = [50, 100, 10, 100, 30, 100]  # 50 is what Windows sends at startup
FONT = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 150)
SMALL = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 44)


@functools.lru_cache(maxsize=None)
def frame_jpeg(value):
    img = Image.new("RGB", (WIDTH, HEIGHT), (255, 255, 255))
    d = ImageDraw.Draw(img)
    for i in range(0, HEIGHT, 120):  # grey ramp bars so dim vs bright is easy to compare
        shade = 255 - i * 255 // HEIGHT
        d.rectangle([0, i, WIDTH, i + 60], fill=(shade, shade, shade))
    d.rectangle([20, 700, WIDTH - 20, 1100], fill=(0, 0, 0))
    d.text((40, 720), f"LIG", font=SMALL, fill=(255, 255, 255))
    d.text((40, 790), f"{value}", font=FONT, fill=(255, 220, 0))
    return _encode(img.rotate(180))


def _encode(img):
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=85)
    return buf.getvalue()


def main():
    dev = find_device(reset=True)
    try:
        print("firmware:", read_firmware_string(dev))
        time.sleep(GET_REPORT_TO_DIS_S)
        dev.write(EP_OUT, DIS_PACKET)
        t0 = time.time()
        time.sleep(DIS_TO_LIG_S)
        current = VALUES[0]
        dev.write(EP_OUT, build_brightness_packet(current))
        print(f"NOW  LIG={current}   (watch the panel's brightness; the image itself never changes)")
        time.sleep(LIG_TO_FIRST_FRAME_S)

        last_connect = t0
        step_start = time.time()
        step = 0
        next_frame = time.time()
        while step < len(VALUES):
            now = time.time()
            if now - last_connect >= CONNECT_INTERVAL_S:
                dev.write(EP_OUT, CONNECT_PACKET)
                last_connect = now
            if now - step_start >= STEP_S:
                step += 1
                if step >= len(VALUES):
                    break
                current = VALUES[step]
                dev.write(EP_OUT, build_brightness_packet(current))  # between frames
                step_start = now
                print(f"NOW  LIG={current}")
            send_frame(dev, frame_jpeg(current))
            next_frame += 0.062
            delay = next_frame - time.time()
            if delay > 0:
                time.sleep(delay)
            else:
                next_frame = time.time()
        print("done")
    except KeyboardInterrupt:
        print("interrupted")
    finally:
        try:
            usb.util.release_interface(dev, 0)
        except usb.core.USBError:
            pass
        usb.util.dispose_resources(dev)


if __name__ == "__main__":
    sys.exit(main())
