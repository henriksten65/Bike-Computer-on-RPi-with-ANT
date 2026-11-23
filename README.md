# Bike Computer on RPi with ANT+

A Raspberry Pi bike computer for power-meter pedals. The Pi receives power and cadence over
**ANT+** (USB stick) or **Bluetooth LE**, and serves a live dashboard you open in a browser on
your phone, tablet or laptop. You don't need an app or a cloud account.

It was built for the **Magene P715** pedals on a Raspberry Pi 4. The program reads the standard
ANT+ Bicycle Power profile and the Bluetooth Cycling Power Service (0x1818), so other power
meters should work too.

```
power meter ──ANT+──► USB stick ─────────► Raspberry Pi ─► Wi-Fi ─► browser (phone / tablet / laptop)
            └─BLE (alternative)─► built-in Bluetooth ─┘
```

## Features

- **Power**: total, left and right, in big numbers with an L/R balance bar
- **Cadence** and **moving time**, which pauses on its own when you stop pedalling
- **Scrolling power graph** with a 1, 5 or 10 minute window or the whole ride, and optional
  left/right lines
- **Ride stats**: average and max power, average L/R balance, average cadence, and work (kJ)
- **One CSV file per ride**, saved in `rides/`
- Works in portrait and landscape, follows the system's light/dark setting, and keeps the phone
  screen on (Wake Lock API)
- **Simulator mode**: try the dashboard on any computer with plain Python and nothing else
  installed

## Quick start (no bike needed)

```bash
git clone https://github.com/henriksten65/Bike-Computer-on-RPi-with-ANT.git
cd Bike-Computer-on-RPi-with-ANT
python3 bikecomputer.py --source sim
```

Open http://localhost:8080. Simulator mode needs only the Python standard library.

## What you need

- A Raspberry Pi with Wi-Fi. Tested on a Pi 4 with Raspberry Pi OS Lite (64-bit).
- An **ANT+ USB stick**. Most sticks use Dynastream/Garmin IDs `0fcf:1008` or `0fcf:1009`.
  You don't need one if you use Bluetooth only.
- A power meter that sends ANT+ Bicycle Power or Bluetooth Cycling Power.

## Install on the Raspberry Pi

Flash **Raspberry Pi OS Lite (64-bit)** with Raspberry Pi Imager. In the Imager settings, enter
your Wi-Fi details and enable SSH. Then, on the Pi:

```bash
sudo apt update && sudo apt install -y git python3-venv libusb-1.0-0
git clone https://github.com/henriksten65/Bike-Computer-on-RPi-with-ANT.git
cd Bike-Computer-on-RPi-with-ANT
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt

# let a normal user open the ANT+ stick
sudo cp deploy/99-ant-usb.rules /etc/udev/rules.d/
sudo udevadm control --reload-rules && sudo udevadm trigger
```

Plug in the ANT+ stick and check that it shows up:

```bash
lsusb      # expect something like "ID 0fcf:1008 Dynastream Innovations"
```

If your stick reports a different ID, add a line for it to `deploy/99-ant-usb.rules` and copy
the file again.

## Ride

```bash
.venv/bin/python bikecomputer.py --source ant
```

Start pedalling to wake the pedals. The log prints `ANT+ power meter found`. On your phone, open
`http://<pi-hostname>.local:8080` or `http://<pi-ip>:8080`. Use **Add to Home Screen** for a
full-screen, app-like view.

### Start automatically at boot

Edit `deploy/bikecomputer.service` first: set `User`, `WorkingDirectory` and `ExecStart` to
your user name and the folder you cloned into. Then:

```bash
sudo cp deploy/bikecomputer.service /etc/systemd/system/
sudo systemctl enable --now bikecomputer
journalctl -u bikecomputer -f      # watch the log
```

## Options

| Option | Meaning |
|---|---|
| `--source ant\|ble\|sim` | ANT+ stick (default), the Pi's Bluetooth, or simulated data |
| `--ant-device-id N` | Pair only with ANT+ device number N, useful when another rider's power meter is nearby. 0 = first one found |
| `--ble-name NAME` / `--ble-address AA:BB:..` | Choose which Bluetooth device to connect to |
| `--unknown-balance-side left\|right` | Change this if **left and right look reversed** (see below) |
| `--port 8080` | Web server port |
| `--log-dir DIR` | Where to save ride CSV files (default: `rides/`) |
| `--no-log` | Don't save CSV files |

### About the left/right split

Power meters don't send separate left and right watts. They send the **total** plus a
**balance percentage**, and the program calculates the split from those. ANT+ marks the
percentage as "right pedal" or "unknown". Bluetooth marks it as "left pedal" or "unknown".
When the meter says "unknown", the program assumes right for ANT+ and left for Bluetooth.

Check it once: push mostly with one leg. If the wrong side goes up, restart with
`--unknown-balance-side` set to the other side.

### ANT+ or Bluetooth?

Use **ANT+**, the default. Many devices can listen to ANT+ at once, so you can still record the
ride on a Garmin or in Zwift at the same time. Pedals usually accept **only one Bluetooth
connection**, so while the Pi is connected, an app on your phone can't connect over Bluetooth.

## Adding a screen to the Pi

Any HDMI or DSI screen on the Pi can show the same dashboard. Install a minimal desktop and run
Chromium in kiosk mode:

```bash
chromium-browser --kiosk --noerrdialogs http://localhost:8080
```

## Project layout

| Path | What it is |
|---|---|
| `bikecomputer.py` | Main program: web server, live updates and CSV logging |
| `sources.py` | Data sources: ANT+, Bluetooth LE and the simulator |
| `ride.py` | Ride state: 1-second samples, moving time and stats |
| `static/index.html` | The dashboard (a single page) |
| `deploy/` | systemd service and udev rule for the Pi |
