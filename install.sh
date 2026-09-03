#!/usr/bin/env bash
# Installer for the Risemode RM-SCP-B smart screen Linux driver.
#
# Installs system/Python dependencies, a udev rule for non-root USB access,
# a systemd --user service (auto-starts the driver), and a desktop menu
# shortcut for the settings GUI. Safe to re-run - every step is idempotent.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

echo "== Risemode smart screen driver installer =="
echo "Installing from: $SCRIPT_DIR"
echo

# --- 1. System packages -----------------------------------------------
echo "-- Checking system packages --"
REQUIRED_PKGS=(python3 python3-venv python3-pip python3-tk libusb-1.0-0)
MISSING_PKGS=()
for pkg in "${REQUIRED_PKGS[@]}"; do
    dpkg -s "$pkg" >/dev/null 2>&1 || MISSING_PKGS+=("$pkg")
done
if [ "${#MISSING_PKGS[@]}" -gt 0 ]; then
    echo "Installing missing packages: ${MISSING_PKGS[*]}"
    sudo apt-get update
    sudo apt-get install -y "${MISSING_PKGS[@]}"
else
    echo "All required system packages already installed."
fi

if ! command -v mangohud >/dev/null 2>&1; then
    # Checks the actual command, not dpkg - a manually-built MangoHud
    # (see the mangohud-setup.sh note in the README) won't be dpkg-tracked
    # even though it's fully installed and working.
    echo
    echo "MangoHud isn't installed (optional - needed for the FPS/1% low/"
    echo "frame time sensors; without it the panel just shows -- for those)."
    if [ -t 0 ]; then
        read -rp "Install mangohud now via apt? [y/N] " REPLY
        if [[ "$REPLY" =~ ^[Yy]$ ]]; then
            sudo apt-get install -y mangohud
        fi
    else
        echo "Non-interactive shell - skipping. Install later with: sudo apt install mangohud"
    fi
fi
echo

# --- 2. Python virtual environment --------------------------------------
echo "-- Setting up Python virtual environment --"
if [ ! -d venv ]; then
    python3 -m venv venv
fi
./venv/bin/pip install --upgrade pip -q
./venv/bin/pip install -r requirements.txt -q
echo "Done."
echo

# --- 3. udev rule for non-root device access ----------------------------
echo "-- Checking udev rule --"
UDEV_RULE_PATH="/etc/udev/rules.d/99-risemode-screen.rules"
UDEV_RULE='SUBSYSTEM=="usb", ATTR{idVendor}=="2100", ATTR{idProduct}=="0003", MODE="0660", GROUP="plugdev", TAG+="uaccess"'
if [ -f "$UDEV_RULE_PATH" ] && [ "$(cat "$UDEV_RULE_PATH")" = "$UDEV_RULE" ]; then
    echo "Already installed at $UDEV_RULE_PATH - skipping (needs sudo otherwise)."
else
    echo "Installing udev rule (needs sudo)..."
    echo "$UDEV_RULE" | sudo tee "$UDEV_RULE_PATH" >/dev/null
    sudo udevadm control --reload-rules
    sudo udevadm trigger
    echo "Installed $UDEV_RULE_PATH"
fi

NEED_RELOGIN=0
if ! groups "$USER" | grep -qw plugdev; then
    echo "Adding $USER to the plugdev group..."
    sudo usermod -aG plugdev "$USER"
    NEED_RELOGIN=1
fi
echo

# --- 4. systemd --user service ------------------------------------------
echo "-- Installing systemd user service --"
SERVICE_DIR="$HOME/.config/systemd/user"
mkdir -p "$SERVICE_DIR"
cat > "$SERVICE_DIR/risemode-screen.service" <<EOF
[Unit]
Description=Risemode RM-SCP-B smart screen driver
After=graphical-session.target

[Service]
Type=simple
ExecStart=$SCRIPT_DIR/venv/bin/python3 -u $SCRIPT_DIR/risemode_driver.py
Restart=on-failure
RestartSec=3

[Install]
WantedBy=default.target
EOF

systemctl --user daemon-reload
systemctl --user enable --now risemode-screen.service
echo "Service installed and started."
echo

# --- 5. Desktop menu shortcut for the settings GUI ----------------------
echo "-- Installing desktop menu shortcut --"
APPS_DIR="$HOME/.local/share/applications"
mkdir -p "$APPS_DIR"
cat > "$APPS_DIR/risemode-settings.desktop" <<EOF
[Desktop Entry]
Type=Application
Name=Risemode Smart Screen Settings
Comment=Configure the Risemode smart screen panel (background image, sensors)
Exec=$SCRIPT_DIR/venv/bin/python3 $SCRIPT_DIR/risemode_gui.py
Icon=$SCRIPT_DIR/icon.png
Terminal=false
Categories=Settings;HardwareSettings;
EOF
update-desktop-database "$APPS_DIR" >/dev/null 2>&1 || true
echo "Added \"Risemode Smart Screen Settings\" to your application menu."
echo

# --- Done -----------------------------------------------------------
echo "== Install complete =="
echo "Service status:  systemctl --user status risemode-screen"
echo "Settings GUI:    search your app menu for \"Risemode Smart Screen Settings\","
echo "                 or run $SCRIPT_DIR/venv/bin/python3 $SCRIPT_DIR/risemode_gui.py"
if [ "$NEED_RELOGIN" = "1" ]; then
    echo
    echo "NOTE: you were just added to the 'plugdev' group - log out and back in"
    echo "(or reboot) for USB device access to take effect."
fi
