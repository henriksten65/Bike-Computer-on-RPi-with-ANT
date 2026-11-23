# Bike computer

A Raspberry Pi 4 picks up the Magene P715 pedals over ANT+ (USB stick) or Bluetooth and
serves a live dashboard. Open it in a browser on your phone, tablet or laptop:

- **Power**: total, left and right (big numbers, plus an L/R balance bar)
- **Cadence** and **elapsed time** (moving time: pauses on its own when you stop pedalling)
- **Scrolling graph** of total, left and right power (1, 5 or 10 min window, or the whole ride)
- Ride averages, max power and work (kJ); one CSV file per ride in `rides/`

```
P715 pedals ──ANT+──► USB stick ─► Raspberry Pi 4 ─► Wi-Fi ─► browser (phone / tablet / laptop)
            └─BLE (alternative)──► built-in Bluetooth
```

## 1. Try it on any computer (no bike needed)

```bash
python3 bikecomputer.py --source sim
```
Open http://localhost:8080. Only the Python standard library is needed for this.

## 2. Set up the Raspberry Pi

Flash **Raspberry Pi OS Lite (64-bit)** with Raspberry Pi Imager. In the Imager settings, enter
your Wi-Fi details, enable SSH, set the user name to `pi` and the hostname to `bike`.

```bash
# copy this folder to the Pi (from your PC)
scp -r "Bike computer" pi@bike.local:bike-computer

# on the Pi
ssh pi@bike.local
sudo apt update && sudo apt install -y python3-venv libusb-1.0-0
cd ~/bike-computer
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt

# let a normal user open the ANT+ stick
sudo cp deploy/99-ant-usb.rules /etc/udev/rules.d/
sudo udevadm control --reload-rules && sudo udevadm trigger
```

Plug in the ANT+ stick and check that it shows up:
```bash
lsusb      # expect something like "ID 0fcf:1008 Dynastream Innovations" (or 0fcf:1009)
```
If the Magene stick reports a **different ID**, tell me what `lsusb` prints. It then needs a line
in the udev rule, and `openant` may need to be told about it.

## 3. Ride

```bash
.venv/bin/python bikecomputer.py --source ant
```
Start pedalling to wake the pedals. The log prints `ANT+ power meter found`. On your phone, open
**http://bike.local:8080** (or `http://<pi-ip>:8080`). The phone screen stays on while the
page is open (in browsers that support the Wake Lock API).

Use **Add to Home Screen** for a full-screen app-like view.

### Run it automatically at boot
```bash
sudo cp deploy/bikecomputer.service /etc/systemd/system/
sudo systemctl enable --now bikecomputer
journalctl -u bikecomputer -f      # watch the log
```

## Options

| Option | Meaning |
|---|---|
| `--source ant\|ble\|sim` | ANT+ stick (default), Pi's Bluetooth, or simulated data |
| `--ant-device-id N` | Pair only with ANT+ device number N (useful if a neighbour's power meter is nearby). 0 = first one found |
| `--ble-name P715` / `--ble-address AA:BB:..` | Choose which BLE device to connect to |
| `--unknown-balance-side left\|right` | Swap this if **left and right look reversed** (see below) |
| `--port 8080` | Web port |
| `--no-log` | Don't write CSV files |

### About the left/right split
The pedals don't send separate left and right watts. They send the **total** plus a **balance
percentage**, and the split is calculated from those. ANT+ marks the percentage as "right pedal"
or "unknown". Bluetooth marks it as "left pedal" or "unknown". When the meter says "unknown",
the program assumes right for ANT+ and left for BLE. Check it once: push mostly with one leg.
If the wrong side goes up, restart with `--unknown-balance-side` set to the other side.

### ANT+ or Bluetooth?
Use **ANT+** (the default). ANT+ allows many listeners at once, so you can still record the ride
on a Garmin or Zwift at the same time. Pedals usually accept **only one Bluetooth connection**.
While the Pi holds it, an app on your phone can't connect over BLE.

## Adding a display later
Any HDMI or DSI screen on the Pi can show the same page. Install a minimal desktop and run
Chromium in kiosk mode:
```bash
chromium-browser --kiosk --noerrdialogs http://localhost:8080
```
The page works in both portrait and landscape. It follows the system's light/dark setting.

## Files
- `bikecomputer.py`: web server, ride CSV logging, command-line options
- `sources.py`: ANT+ (page 0x10 Standard Power-Only), BLE Cycling Power Service, simulator
- `ride.py`: 1-second sampling, auto-pause timer, averages
- `static/index.html`: the dashboard (no internet or external libraries needed)
