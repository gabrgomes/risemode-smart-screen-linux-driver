#!/usr/bin/env python3
"""Turns on MangoHud logging for games launched from the Heroic Games
Launcher, so the panel's FPS / 1% low / frame time sensors work for them
like they do for Steam games (see "GPU FPS via MangoHud" in the README).

Heroic has its own "Enable MangoHud" setting rather than reading Steam's
launch options, so this sets that up:
  1. (Flatpak Heroic only) installs the MangoHud Flatpak extension matching
     Heroic's runtime - Heroic looks for `mangohud` on the sandbox's PATH,
     and the host's isn't visible in there - and lets the sandbox write to
     the MangoHud log directory the driver reads.
  2. In Heroic's config.json (and any per-game settings that override it):
     turns "Enable MangoHud" on and adds the same MANGOHUD_CONFIG the Steam
     launch options use (autostart logging, tiny off-screen HUD).

Close Heroic first - it rewrites its config on exit, which would throw the
changes away. Originals are saved next to the files as *.bak-risemode the
first time. Safe to re-run; --dry-run just prints what it would do.
"""
import argparse
import json
import os
import shutil
import subprocess
import sys

FLATPAK_ID = "com.heroicgameslauncher.hgl"
FLATPAK_CONFIG = os.path.expanduser(f"~/.var/app/{FLATPAK_ID}/config/heroic")
NATIVE_CONFIG = os.path.expanduser("~/.config/heroic")
LOG_DIR = os.path.expanduser("~/.local/share/mangohud_logs")
EXTENSION = "org.freedesktop.Platform.VulkanLayer.MangoHud"

# Same options as the Steam launch options in the README.
MANGOHUD_CONFIG = (
    f"output_folder={LOG_DIR},autostart_log=1,log_duration=999999,log_interval=200,"
    "fps_only,font_size=10,hud_no_margin,background_alpha=0,alpha=0.15,position=top-left,"
    "text_color=000000,fps_color_change=0,engine_color=000000,offset_x=-3000,offset_y=-3000"
)


def env_options(is_flatpak):
    opts = {"MANGOHUD_CONFIG": MANGOHUD_CONFIG}
    if is_flatpak:
        # Proton runs in its own container inside Heroic's sandbox, which
        # can't see the extension or the log dir unless told (the same
        # pressure-vessel issue the README describes for Steam).
        opts["PRESSURE_VESSEL_FILESYSTEMS_RO"] = "/usr/lib/extensions/vulkan/MangoHud"
        opts["PRESSURE_VESSEL_FILESYSTEMS_RW"] = LOG_DIR
    return opts


def heroic_running():
    out = subprocess.run(["pgrep", "-f", r"/app/bin/heroic/heroic|/opt/Heroic/heroic"],
                         capture_output=True, text=True).stdout.split()
    return [p for p in out if int(p) != os.getpid()]


def merge_env(existing, wanted):
    """Heroic's env option list ([{"key", "value"}, ...]) with `wanted` set."""
    out = [e for e in (existing or []) if e.get("key") not in wanted]
    out += [{"key": k, "value": v} for k, v in wanted.items()]
    return out


def patch_settings(settings, wanted):
    """Applies the MangoHud settings to one settings dict; True if it changed."""
    before = json.dumps(settings, sort_keys=True)
    settings["showMangohud"] = True
    settings["enviromentOptions"] = merge_env(settings.get("enviromentOptions"), wanted)
    return json.dumps(settings, sort_keys=True) != before


def write_json(path, data, dry_run):
    if dry_run:
        return
    backup = path + ".bak-risemode"
    if not os.path.exists(backup):
        shutil.copy2(path, backup)
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f, indent=2)
    os.replace(tmp, path)


def patch_config_dir(config_dir, wanted, dry_run):
    path = os.path.join(config_dir, "config.json")
    with open(path) as f:
        data = json.load(f)
    changed = []
    if patch_settings(data.setdefault("defaultSettings", {}), wanted):
        write_json(path, data, dry_run)
        changed.append("config.json (default settings for all games)")

    games_dir = os.path.join(config_dir, "GamesConfig")
    for name in sorted(os.listdir(games_dir)) if os.path.isdir(games_dir) else ():
        game_path = os.path.join(games_dir, name)
        if not name.endswith(".json"):
            continue
        try:
            with open(game_path) as f:
                game = json.load(f)
        except (OSError, ValueError):
            continue
        app_name = name[:-5]
        settings = game.get(app_name)
        # {} / no entry = the game just uses the defaults above; only games
        # with their own saved settings need patching (those override them)
        if isinstance(settings, dict) and settings and patch_settings(settings, wanted):
            write_json(game_path, game, dry_run)
            changed.append(f"GamesConfig/{name}")
    return changed


def flatpak_steps(dry_run):
    info = subprocess.run(["flatpak", "info", FLATPAK_ID], capture_output=True, text=True).stdout
    runtime = next((line.split("/")[-1].strip() for line in info.splitlines()
                    if line.strip().startswith("Runtime:")), None)
    if not runtime:
        sys.exit("Couldn't read Heroic's Flatpak runtime version (flatpak info failed).")
    installed = subprocess.run(["flatpak", "list", "--runtime", "--columns=application,branch"],
                               capture_output=True, text=True).stdout
    if f"{EXTENSION}\t{runtime}" in installed:
        print(f"MangoHud Flatpak extension ({runtime}) already installed.")
    else:
        print(f"Installing {EXTENSION} //{runtime} ...")
        if not dry_run:
            subprocess.run(["flatpak", "install", "--user", "-y", "flathub",
                            f"{EXTENSION}//{runtime}"], check=True)
    print(f"Allowing Heroic's sandbox to write {LOG_DIR} ...")
    if not dry_run:
        os.makedirs(LOG_DIR, exist_ok=True)
        subprocess.run(["flatpak", "override", "--user", f"--filesystem={LOG_DIR}", FLATPAK_ID],
                       check=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--dry-run", action="store_true", help="only print what would change")
    parser.add_argument("--force", action="store_true", help="patch even though Heroic is running")
    parser.add_argument("--config-dir", help="Heroic config dir (default: Flatpak's, else native)")
    args = parser.parse_args()

    config_dir = args.config_dir or (
        FLATPAK_CONFIG if os.path.isdir(FLATPAK_CONFIG) else NATIVE_CONFIG)
    if not os.path.isfile(os.path.join(config_dir, "config.json")):
        sys.exit(f"No Heroic config.json in {config_dir}")
    is_flatpak = config_dir.startswith(os.path.expanduser("~/.var/app/")) or (
        args.config_dir is None and config_dir == FLATPAK_CONFIG)

    if heroic_running() and not (args.force or args.dry_run or args.config_dir):
        sys.exit("Heroic is running - close it completely (including its tray icon) first, "
                 "or it will overwrite the changes when it exits. (--force to skip this check.)")

    if is_flatpak and not args.config_dir:
        flatpak_steps(args.dry_run)
    elif not is_flatpak and not shutil.which("mangohud"):
        print("Note: `mangohud` isn't on PATH - install it (sudo apt install mangohud).")

    changed = patch_config_dir(config_dir, env_options(is_flatpak), args.dry_run)
    verb = "Would update" if args.dry_run else "Updated"
    for item in changed:
        print(f"{verb} {item}")
    if not changed:
        print("Heroic's config already has MangoHud set up.")
    elif not args.dry_run:
        print("Done. Start Heroic and launch a game - the panel should pick up FPS from its "
              f"MangoHud log in {LOG_DIR}.")


if __name__ == "__main__":
    main()
