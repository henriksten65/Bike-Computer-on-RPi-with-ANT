# Bike Computer on RPi with ANT+

[![Tests](https://github.com/henriksten65/Bike-Computer-on-RPi-with-ANT/actions/workflows/tests.yml/badge.svg)](https://github.com/henriksten65/Bike-Computer-on-RPi-with-ANT/actions/workflows/tests.yml)

A Raspberry Pi bike computer for power-meter pedals. The Pi receives power and cadence over
**ANT+** (USB stick) or **Bluetooth LE**, and heart rate from an ANT+ strap. It serves a live
dashboard you open in a browser on your phone, tablet or laptop. You don't need an app or a
cloud account.

It was built for the **Magene P715** pedals on a Raspberry Pi 4. The program reads the standard
ANT+ Bicycle Power profile and the Bluetooth Cycling Power Service (0x1818), so other power
meters should work too.

```
power meter ──ANT+──────────► USB stick ──────────────┐
            └─BLE (alternative)─► built-in Bluetooth ─┤
heart rate strap ──ANT+───────► USB stick ────────────┴─► Raspberry Pi ─► Wi-Fi ─► browser
                                                                         (phone / tablet / laptop)
```

## Features

- **Power**: total, left and right, in big numbers with an L/R balance bar
- **Cadence** and **moving time**, which pauses on its own when you stop pedalling
- **Scrolling power graph** with a 1, 5 or 10 minute window or the whole ride, and optional
  left/right lines
- **Heart rate** from an ANT+ chest strap or watch (the tile appears once a strap is found)
- **Ride stats**: average, normalized (NP) and max power, average L/R balance, average cadence,
  average and max heart rate, and work (kJ)
- **Training numbers** once you enter your FTP: the current **power zone** (Z1–Z7) on the power
  tile, zone bands on the graph, and **IF** and **TSS** for the ride and each lap
- **Lap button**: a lap table with time, average, NP, max power, IF, heart rate and cadence per
  lap, lap lines on the graph, and one lap per lap in the TCX file
- **One CSV file per ride**, saved in `rides/` (the newest 10 are kept)
- **Ride list** at `/rides`: past rides with their stats, and downloads as **TCX** (upload to
  Strava, Garmin Connect, intervals.icu, TrainingPeaks, …) or **CSV** (for a spreadsheet)
- **Power off and restart buttons** on the ride list page, so you can shut the Pi down safely
  without a keyboard or SSH
- Clear status messages when something is wrong, for example "ANT+ stick not found". The
  program reconnects on its own when the stick is plugged back in.
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
- Optional: an ANT+ heart rate strap, or a watch that broadcasts heart rate over ANT+. Heart
  rate needs the ANT+ stick; it isn't read over Bluetooth.

## Install on the Raspberry Pi

Flash **Raspberry Pi OS Lite (64-bit)** with Raspberry Pi Imager. In the Imager settings, enter
your Wi-Fi details and enable SSH. Then, on the Pi, as your normal user:

```bash
sudo apt update && sudo apt install -y git
git clone https://github.com/henriksten65/Bike-Computer-on-RPi-with-ANT.git
cd Bike-Computer-on-RPi-with-ANT
deploy/install.sh
```

The install script asks for your password where it needs root. It:

1. installs the system packages (`python3-venv`, `libusb-1.0-0`)
2. creates the Python environment in `.venv` and installs the libraries
3. lets normal users open the ANT+ stick (udev rule)
4. lets your user power off and restart the Pi from the dashboard (a sudoers rule that allows
   only `systemctl poweroff` and `systemctl reboot`)
5. installs the `bikecomputer` service for your user and folder, and starts it at every boot

You can run it again at any time, for example after moving the folder. To use Bluetooth or
other options in the service, edit the `ExecStart` line in `deploy/bikecomputer.service` before
running the script.

Plug in the ANT+ stick and check that it shows up:

```bash
lsusb      # expect something like "ID 0fcf:1008 Dynastream Innovations"
```

If your stick reports a different ID, add a line for it to `deploy/99-ant-usb.rules` and run
the install script again.

## Ride

The service is already running. Start pedalling to wake the pedals. On your phone, open
`http://<pi-hostname>.local:8080` or `http://<pi-ip>:8080`. Use **Add to Home Screen** for a
full-screen, app-like view.

```bash
journalctl -u bikecomputer -f       # watch the log ("ANT+ power meter found")
sudo systemctl restart bikecomputer # after changing the code or the service
```

To run it by hand instead (stop the service first with `sudo systemctl stop bikecomputer`):

```bash
.venv/bin/python bikecomputer.py --source ant
```

### FTP, zones, IF and TSS

Open **Rides** and enter your **FTP** (functional threshold power: the power you can hold for
about an hour) under **Training**. The FTP is saved on the Pi, in `settings.json`, so it applies
on every phone or laptop. Then the dashboard shows:

| Number | Meaning |
|---|---|
| Zone | Z1 Recovery (up to 55 % of FTP), Z2 Endurance (75 %), Z3 Tempo (90 %), Z4 Threshold (105 %), Z5 VO2 max (120 %), Z6 Anaerobic (150 %), Z7 Neuromuscular (above) |
| IF | Intensity Factor = NP ÷ FTP. About 0.75 for an endurance ride, 1.0 for an all-out hour |
| TSS | Training Stress Score = hours × IF² × 100. One hour at FTP = 100 |

IF and TSS need NP, so they appear after 30 seconds of riding. The ride list shows them for
saved rides too, worked out with the FTP set now.

### Laps

Tap **Lap** to finish the current lap and start the next. Once you have pressed it, the
dashboard shows the current lap and a table of all laps. Laps are saved in the ride file and
become separate laps in the TCX file.

When you're done, open **Rides** on the dashboard and tap **Power off**. Wait until the Pi's
green light stops flashing before you unplug it.

### If something is wrong

The status line at the top of the dashboard says what's happening:

| Message | What to do |
|---|---|
| Searching for pedals… | Start pedalling to wake the pedals |
| ANT+ stick not found | Plug in the stick. The program reconnects on its own |
| No permission to open the ANT+ stick | Run `deploy/install.sh` (it installs the udev rule) |
| Lost connection to the bike computer | The phone can't reach the Pi: check the Wi-Fi, or whether the Pi is on |
| No data from the bike computer | The program stopped updating: check `journalctl -u bikecomputer` |

A red banner means the ride can't be saved, for example because the SD card is full. The
dashboard keeps working.

## Ride files

Each ride is saved as `rides/ride-YYYYMMDD-HHMMSS.csv`, one row per second of moving time:

`time_iso, elapsed_s, power_w, left_w, right_w, cadence_rpm, heart_rate_bpm, lap`

Empty values mean the meter didn't send them (no balance, no strap). A new file starts on
**Reset** and whenever the program starts. Only the newest 10 are kept, so download the ones
you want to keep from the **Rides** page.

### Uploading to Strava and other sites

On the **Rides** page, tap **TCX** next to a ride and upload the file:

- **Strava:** the **+** button, then **Upload activity**, then **File** (on the website)
- **Garmin Connect:** **Import Data** (on the website)
- **intervals.icu** and **TrainingPeaks:** drag the file onto the upload area

The file has power, cadence and heart rate for every second of moving time, in UTC. It has no
GPS or distance, so the sites treat it as an indoor ride. Pauses show up as gaps.

The Pi has no battery-backed clock. Without internet at boot its time can be wrong, so the
dashboard sends the phone's time and the Pi uses that for file names and timestamps.

## Updating

```bash
cd Bike-Computer-on-RPi-with-ANT
git pull
deploy/install.sh        # installs new libraries and restarts the service
```

See [CHANGELOG.md](CHANGELOG.md) for what changed.

## Options

Pass these to `bikecomputer.py`. For the service, add them to the `ExecStart` line in
`deploy/bikecomputer.service` and run `deploy/install.sh` again.

| Option | Meaning |
|---|---|
| `--source ant\|ble\|sim` | ANT+ stick (default), the Pi's Bluetooth, or simulated data |
| `--ant-device-id N` | Pair only with ANT+ device number N, useful when another rider's power meter is nearby. 0 = first one found |
| `--ant-hr-device-id N` | The same for the ANT+ heart rate strap |
| `--no-heart-rate` | Don't listen for an ANT+ heart rate strap |
| `--ble-name NAME` / `--ble-address AA:BB:..` | Choose which Bluetooth device to connect to |
| `--unknown-balance-side left\|right` | Change this if **left and right look reversed** (see below) |
| `--port 8080` | Web server port |
| `--log-dir DIR` | Where to save ride CSV files (default: `rides/`) |
| `--keep-rides N` | Keep only the newest N ride CSV files and delete older ones (default 10, 0 = keep all) |
| `--no-log` | Don't save CSV files |
| `--settings FILE` | Where to keep settings changed on the page, such as FTP (default: `settings.json`) |
| `--version` | Show the version |

### About the left/right split

Power meters don't send separate left and right watts. They send the **total** plus a
**balance percentage**, and the program calculates the split from those. ANT+ marks the
percentage as "right pedal" or "unknown". Bluetooth marks it as "left pedal" or "unknown".
When the meter says "unknown", the program assumes right for ANT+ and left for Bluetooth.

Check it once: push mostly with one leg. If the wrong side goes up, add
`--unknown-balance-side` with the other side to the options.

### ANT+ or Bluetooth?

Use **ANT+**, the default. Many devices can listen to ANT+ at once, so you can still record the
ride on a Garmin or in Zwift at the same time. Pedals usually accept **only one Bluetooth
connection**, so while the Pi is connected, an app on your phone can't connect over Bluetooth.

## Adding a screen to the Pi

Any HDMI or DSI screen on the Pi can show the same dashboard. Install a minimal desktop and run
Chromium in kiosk mode:

```bash
chromium --kiosk --noerrdialogs http://localhost:8080
```

On older Raspberry Pi OS versions the command is `chromium-browser`.

## Project layout

| Path | What it is |
|---|---|
| `bikecomputer.py` | Main program: web server, live updates, power off |
| `sources.py` | Data sources: ANT+ (power and heart rate), Bluetooth LE and the simulator |
| `ride.py` | Ride state: 1-second samples, moving time, laps, stats, NP, zones, IF and TSS |
| `ridelog.py` | Ride CSV files: writing, keeping the newest 10, reading them back for the ride list, and TCX export |
| `static/index.html` | The dashboard |
| `static/rides.html` | The ride list, with downloads and the power off button |
| `deploy/` | Install script, systemd service template and udev rule for the Pi |
| `CHANGELOG.md` | What changed in each version |
| `tests/` | Tests |
| `.github/workflows/tests.yml` | Runs the tests on GitHub on every push |

## Tests

The tests need only Python, not the Pi or the ANT+ stick:

```bash
python3 -m unittest
```

GitHub runs them on every push, on Python 3.10 to 3.13 (see the badge at the top).

## License

[MIT](LICENSE)
