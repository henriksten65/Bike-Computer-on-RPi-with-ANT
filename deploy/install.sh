#!/usr/bin/env bash
# Set up the bike computer on a Raspberry Pi. Run it from the project folder as
# your normal user (not root); it asks for your password where root is needed:
#
#     deploy/install.sh
#
# It can be run again at any time, for example after moving the folder.
#
#  1. installs the system packages it needs
#  2. creates the Python virtual environment in .venv and installs the libraries
#  3. lets normal users open the ANT+ USB stick (udev rule)
#  4. lets this user power off / restart the Pi from the dashboard (sudoers rule)
#  5. installs and starts the systemd service, filled in for this user and folder
set -euo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
USER_NAME="$(id -un)"

if [[ "$USER_NAME" == root ]]; then
    echo "Run this as your normal user, not with sudo. It asks for your password when needed." >&2
    exit 1
fi
if [[ "$DIR" == *$'\n'* || "$DIR" == *'"'* || "$DIR" == *'\'* || "$DIR" == *'%'* ]]; then
    echo "The project folder path contains a character systemd can't handle: $DIR" >&2
    exit 1
fi
cd "$DIR"
echo "Installing the bike computer for user '$USER_NAME' in '$DIR'"

echo "==> System packages"
sudo apt-get update -q
sudo apt-get install -y -q python3-venv libusb-1.0-0

echo "==> Python environment (.venv)"
[[ -x .venv/bin/python ]] || python3 -m venv .venv
.venv/bin/pip install -q --upgrade pip
.venv/bin/pip install -q -r requirements.txt

echo "==> ANT+ USB stick access (udev rule)"
sudo install -m 644 deploy/99-ant-usb.rules /etc/udev/rules.d/99-ant-usb.rules
sudo udevadm control --reload-rules
sudo udevadm trigger

echo "==> Allow power off / restart from the dashboard (sudoers rule)"
tmp="$(mktemp)"
trap 'rm -f "$tmp"' EXIT
printf '%s ALL=(root) NOPASSWD: /usr/bin/systemctl poweroff, /usr/bin/systemctl reboot\n' "$USER_NAME" > "$tmp"
sudo visudo -c -q -f "$tmp"              # never install a broken sudoers file
sudo install -m 440 -o root -g root "$tmp" /etc/sudoers.d/bikecomputer

echo "==> systemd service"
# Escape the path for sed (& and the | delimiter).
dir_sed="$(printf '%s' "$DIR" | sed 's/[&|]/\\&/g')"
sed -e "s|@USER@|$USER_NAME|g" -e "s|@DIR@|$dir_sed|g" -e '/^# Template/,/^# \/etc/d' \
    deploy/bikecomputer.service > "$tmp"
sudo install -m 644 "$tmp" /etc/systemd/system/bikecomputer.service
sudo systemctl daemon-reload
sudo systemctl enable bikecomputer
sudo systemctl restart bikecomputer

port=8080
host="$(hostname)"
echo
echo "Done. The bike computer is running and starts at every boot."
echo "Open http://$host.local:$port on your phone (same Wi-Fi)."
echo "Log:  journalctl -u bikecomputer -f"
