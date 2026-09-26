#!/usr/bin/env python3
"""Does the panel stay lit for minutes when we talk to it the way the Windows
app does? (docs/windows-capture/PROTOCOL.md)

Sequence: [GET_REPORT] -> DIS -> 0.39s -> LIG 50 -> 0.08s -> frames (~16 fps,
a counter + moving bar so a frozen or dark panel is obvious) with a CONNECT
every 10 s between frames, and NO reconnects or bus resets for the whole run.
Everything is logged (console + windows_stream_test.log): USB errors, write
stalls, and every packet the device sends on the IN endpoint 0x82 - Windows saw
`00 00` every 3 s while the device was idle and none once the session began, so
whether they stop after DIS tells us if the firmware accepted it.

Stop the service first:   systemctl --user stop risemode-screen.service
Then:                     ./venv/bin/python3 windows_stream_test.py
Control run (old start):  ./venv/bin/python3 windows_stream_test.py --no-dis
"""
import argparse
import io
import logging
import sys
import threading
import time

import usb.core
import usb.util
from PIL import Image, ImageDraw, ImageFont

from risemode_driver import (
    CONNECT_INTERVAL_S, CONNECT_PACKET, DIS_PACKET, DIS_TO_LIG_S, EP_IN, EP_OUT,
    GET_REPORT_TO_DIS_S, LIG_TO_FIRST_FRAME_S, PACKET_SIZE, build_brightness_packet,
    find_device, read_firmware_string, send_frame,
)

WIDTH, HEIGHT = 462, 1920
FONT_PATH = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
LOG_PATH = "windows_stream_test.log"
SLOW_WRITE_S = 0.25  # a frame taking longer than this to send is logged as a stall

log = logging.getLogger("test")


def make_background():
    """A busy gradient + noise so each JPEG is ~50 KB like Windows' frames
    (14-81 KB) rather than a tiny flat-colour one."""
    across = Image.linear_gradient("L").rotate(90).resize((WIDTH, HEIGHT))
    down = Image.linear_gradient("L").resize((WIDTH, HEIGHT))
    diag = Image.blend(across, down, 0.5)
    img = Image.merge("RGB", (across, down, diag))
    noise = Image.effect_noise((WIDTH, HEIGHT), 40).convert("RGB")
    return Image.blend(img, noise, 0.06)


class FrameMaker:
    def __init__(self):
        self.bg = make_background()
        self.big = ImageFont.truetype(FONT_PATH, 120)
        self.mid = ImageFont.truetype(FONT_PATH, 48)

    def jpeg(self, frame_no, elapsed):
        img = self.bg.copy()
        d = ImageDraw.Draw(img)
        d.rectangle([20, 60, WIDTH - 20, 420], fill=(0, 0, 0))
        d.text((40, 80), f"{frame_no}", font=self.big, fill=(255, 255, 255))
        d.text((40, 260), f"{int(elapsed // 60)}:{elapsed % 60:04.1f}", font=self.big,
               fill=(255, 220, 0))
        # a bar sweeping across so motion is visible even between counter changes
        pos = int((elapsed * 200) % (WIDTH - 40))
        d.rectangle([20 + pos, 460, 20 + pos + 30, 560], fill=(255, 0, 0))
        d.text((30, 600), "if you can read this\nthe panel is alive", font=self.mid,
               fill=(255, 255, 255))
        img = img.rotate(180)  # the panel wants its image rotated 180 degrees
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=85)
        return buf.getvalue()


class InWatcher(threading.Thread):
    """Keeps a read pending on 0x82 and records everything that arrives."""

    def __init__(self, dev, t0):
        super().__init__(daemon=True)
        self.dev, self.t0 = dev, t0
        self.stop = threading.Event()
        self.events = []  # (time, bytes)

    def run(self):
        while not self.stop.is_set():
            try:
                data = bytes(self.dev.read(EP_IN, 512, timeout=1000))
            except usb.core.USBTimeoutError:
                continue
            except usb.core.USBError as e:
                if not self.stop.is_set():
                    log.warning("IN read error: %s", e)
                    time.sleep(0.2)
                continue
            self.events.append((time.time(), data))
            log.info("IN 0x82 <- %s  (t=%.2fs)", data.hex() or "(empty)", time.time() - self.t0)


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--duration", type=float, default=300, help="seconds to stream (300)")
    parser.add_argument("--fps", type=float, default=16, help="frames per second (16)")
    parser.add_argument("--no-dis", action="store_true", help="control run: skip DIS/LIG/GET_REPORT, "
                        "start with CONNECT like the old driver (still no reconnects)")
    parser.add_argument("--no-reset", action="store_true",
                        help="don't bus-reset the device at startup (default: one reset)")
    parser.add_argument("--no-lig", action="store_true", help="send DIS but skip LIG")
    parser.add_argument("--listen", type=float, default=4,
                        help="seconds to listen on 0x82 before opening the session (4)")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s.%(msecs)03d %(levelname)-7s %(message)s",
        datefmt="%H:%M:%S", handlers=[logging.StreamHandler(), logging.FileHandler(LOG_PATH, "w")])

    frames = FrameMaker()
    sample = frames.jpeg(0, 0)
    log.info("test frame ~%.1f KB (Windows: 14-81 KB, avg 54)", len(sample) / 1024)

    dev = find_device(reset=not args.no_reset)
    t0 = time.time()
    watcher = InWatcher(dev, t0)
    watcher.start()
    stats = {"frames": 0, "errors": 0, "stalls": 0, "max_write": 0.0}
    exit_code = 0
    session_t0 = float("inf")  # until the session actually starts

    try:
        if args.listen > 0:
            log.info("listening on 0x82 for %.0fs before starting (device idle: expect `00 00` "
                     "every ~3 s)...", args.listen)
            time.sleep(args.listen)

        if args.no_dis:
            log.info("CONTROL RUN: CONNECT first, no DIS/LIG")
            dev.write(EP_OUT, CONNECT_PACKET)
            time.sleep(0.2)
            session_t0 = time.time()
        else:
            fw = read_firmware_string(dev)
            log.info("GET_REPORT firmware string: %r", fw)
            time.sleep(GET_REPORT_TO_DIS_S)
            dev.write(EP_OUT, DIS_PACKET)
            session_t0 = time.time()
            log.info("sent DIS")
            time.sleep(DIS_TO_LIG_S)
            if not args.no_lig:
                dev.write(EP_OUT, build_brightness_packet(50))
                log.info("sent LIG 50")
            time.sleep(LIG_TO_FIRST_FRAME_S)

        interval = 1 / args.fps
        next_frame = time.time()
        last_connect = session_t0
        last_report = time.time()
        report_frames = 0
        end = session_t0 + args.duration
        log.info("streaming for %.0fs at %.0f fps; no reconnects. Watch the panel.",
                 args.duration, args.fps)
        while time.time() < end:
            now = time.time()
            if now - last_connect >= CONNECT_INTERVAL_S:
                try:
                    dev.write(EP_OUT, CONNECT_PACKET)
                    log.info("CONNECT sent (t=%.2fs since session start)", now - session_t0)
                except usb.core.USBError as e:
                    stats["errors"] += 1
                    log.error("USB error on CONNECT: %s", e)
                last_connect = now

            elapsed = now - session_t0
            jpeg = frames.jpeg(stats["frames"], elapsed)
            t_send = time.time()
            try:
                send_frame(dev, jpeg)
            except usb.core.USBError as e:
                stats["errors"] += 1
                log.error("USB error sending frame %d at t=%.1fs: %s", stats["frames"], elapsed, e)
                time.sleep(0.5)
                if stats["errors"] >= 20:
                    log.error("too many errors, giving up")
                    exit_code = 1
                    break
                continue
            took = time.time() - t_send
            stats["max_write"] = max(stats["max_write"], took)
            if took > SLOW_WRITE_S:
                stats["stalls"] += 1
                log.warning("slow frame write: %.0f ms (frame %d, t=%.1fs)",
                            took * 1000, stats["frames"], elapsed)
            stats["frames"] += 1
            report_frames += 1

            if time.time() - last_report >= 10:
                span = time.time() - last_report
                log.info("t=%3.0fs  frames=%d  %.1f fps  errors=%d  max write %.0f ms  "
                         "IN events since session start: %d",
                         time.time() - session_t0, stats["frames"], report_frames / span,
                         stats["errors"], stats["max_write"] * 1000,
                         sum(1 for t, _ in watcher.events if t >= session_t0))
                last_report, report_frames = time.time(), 0

            next_frame += interval
            delay = next_frame - time.time()
            if delay > 0:
                time.sleep(delay)
            else:
                next_frame = time.time()
    except KeyboardInterrupt:
        log.info("interrupted")
    except usb.core.USBError as e:
        log.error("USB error: %s", e)
        exit_code = 1
    finally:
        watcher.stop.set()
        watcher.join(timeout=2)
        try:
            usb.util.release_interface(dev, 0)
        except usb.core.USBError:
            pass
        usb.util.dispose_resources(dev)

    idle_events = sum(1 for t, _ in watcher.events if t < session_t0)
    after = [e for e in watcher.events if e[0] >= session_t0]
    log.info("=" * 60)
    log.info("frames sent: %d   USB errors: %d   slow writes: %d   max write: %.0f ms",
             stats["frames"], stats["errors"], stats["stalls"], stats["max_write"] * 1000)
    log.info("0x82 packets while idle (before session): %d;  after session start: %d",
             idle_events, len(after))
    if not args.no_dis:
        if idle_events and not after:
            log.info("=> device heartbeat STOPPED after DIS (matches Windows: DIS accepted)")
        elif idle_events and after:
            log.info("=> device kept sending on 0x82 after DIS (Windows saw none): DIS may not "
                     "have taken effect")
        else:
            log.info("=> no idle heartbeat seen before the session, so can't tell either way")
    log.info("Now: was the panel still showing the counter at the end? Did it ever go dark or "
             "freeze, and at what t?  (full log: %s)", LOG_PATH)
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
