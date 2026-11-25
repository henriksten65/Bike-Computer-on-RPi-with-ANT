# Changelog

## 0.10.1 (2026-11-25)

### Changed
- With the balance set to **Dial**, it moves up next to Time, Power, Cadence and Heart rate on
  screens wider than 1100 px, where the numbers in that row get a little smaller to make room.
  On narrower screens, and with the bar, it keeps its own row.

## 0.10.0 (2026-11-25)

### New
- **Left/right balance as a dial**: the **Bar | Dial** switch under the balance shows it as a
  needle on a half circle instead of the bar. Straight up is 50/50, and the needle goes all
  the way over at 70 % on one side, so small differences show. The choice is remembered.

### Changed
- The time-in-zones bar on the Rides page now looks like the dashboard's: a little thicker,
  with the times in bold.
- Saved rides are summed up by the same code as the live ride, so the ride list always shows
  the numbers the dashboard did. The two pages share their colours, styles and helpers
  (`static/common.css` and `static/common.js`).

### Fixed
- Removing old rides only touches files named like a ride (`ride-YYYYMMDD-HHMMSS.csv`). Before,
  another file starting with `ride-` and ending in `.csv` could be removed too.

## 0.9.0 (2026-11-24)

### New
- **Interval timer for HIIT**: when you start riding hard, a red **Hard** timer appears under
  the ride time and shows how long the interval has lasted. It goes away when you ease off.
  With an FTP set, hard means above threshold (105 % of FTP); without one, 1.5 times what you
  were riding before (at least 150 W). It takes 3 s at that level to start, so a spike
  doesn't count, and 5 s below 85 % of it to end, so a gear change doesn't either.
- **20 minute window** for the graph, next to 1m, 5m and 10m.

### Changed
- The Python code is type-checked with mypy (`mypy.ini`), locally and on GitHub. A few type
  hints were added; nothing changes for riders.
- The graph's scales fit what it shows: power goes up to the highest shown value in the window
  instead of at least 300 W, and heart rate to its own range in the window instead of a fixed
  40–200 bpm. Hiding the left or right line rescales the graph if it was the highest.

## 0.8.0 (2026-11-24)

### New
- **Burned (kcal)**: an estimate of the food energy burned, next to Work on the dashboard and
  for each saved ride. It assumes 24% efficiency by default, so it comes out close to the kJ
  number. Set your own efficiency (15–30 %) under **Training** on the Rides page.

## 0.7.1 (2026-11-23)

### Fixed
- Powering off while the pedals were still sending left a few-second extra ride file, which
  could push a real ride out of the newest 10. Power off and shutdown now stop recording first.
- Reset closed the ride file from another thread while it could be being written.
- A web site could reach the Pi through the browser with DNS rebinding and reset the ride,
  change settings, or power off without a PIN. Buttons that change something now only work at
  the Pi's own addresses; `--allow-host` adds another name.
- A phone set to the wrong time could change the Pi's correct internet time. The phone's time
  is now only used while the Pi has no internet time.
- PIN lockout: after 5 wrong PINs the pause now doubles each time (1, 2, 4 minutes, … up to an
  hour) instead of always 1 minute. A 4-digit PIN now takes weeks to guess, not hours.
- The Rides page could keep a wrong PIN and send it again by itself. A PIN is now remembered
  only once the Pi has accepted it.
- The lap table showed "null rpm" for a lap without cadence.
- Ride files store times with their UTC offset, so rides during the autumn clock change get
  the right times in TCX.
- Keeping the newest 10 rides could, with a wrong clock, delete the ride being recorded.
- An unreadable request header, or a too-short Bluetooth power packet, caused an error instead
  of being ignored.

## 0.7.0 (2026-11-23)

### New
- **Time in zones**: a bar below the stats on the dashboard, and one for each saved ride.
- **PIN** for power off, restart and settings, set on the Rides page. Lap and Reset never need
  it. After 5 wrong PINs, PINs are refused for a minute.
- **Each ride keeps its FTP**: a new `ftp_w` column in the ride files, so IF, TSS and zones of old
  rides don't change when you change your FTP.
- **Heart rate over Bluetooth** (`--heart-rate ble`), with a Bluetooth or an ANT+ power meter.
- **Heart rate line on the graph**, with its own scale and an **HR** button to hide it.
- **Whole ride on the graph**: "All" now shows up to 12 hours instead of the last 2, and stays
  fast on long rides.
- **Wi-Fi hotspot**: `deploy/hotspot.sh on` makes the Pi start its own network when it can't find
  a Wi-Fi it knows.
- **Browser tests** on GitHub: both pages open in headless Chromium on every push, with
  screenshots.
- `--no-power` hides the power buttons, for running on a computer.

### Changed
- Average cadence leaves out coasting, on the dashboard, the ride list and in TCX.
- The graph's window and smoothing buttons moved from the header to the graph, so the header
  fits on one line on a phone.
- Time labels on long rides fall on round times (1:00:00, 2:00:00, …).

### Fixed
- On a phone, the graph tooltip now hides when you lift your finger.
- After the page reconnects, the graph no longer shows a point twice or misses one.
- When the clock is corrected during a ride, the rows already saved get the right time too.
- The Rides page: the download buttons no longer wrap awkwardly on a phone, and a too-low or
  too-high FTP shows the page's own message.

## 0.6.0 (2026-11-23)

### New
- **Lap button** on the dashboard: current lap and a lap table (time, average, NP, max power,
  IF, heart rate, cadence), lap lines on the graph, a `lap` column in the ride files, and one
  lap per lap in the TCX export.
- **FTP setting** on the Rides page, saved on the Pi.
- **Power zones** (Z1–Z7) once FTP is set: the current zone on the power tile and zone bands on
  the graph.
- **IF and TSS** for the ride, each lap, and the saved rides.

## 0.5.0 (2026-11-23)

### New
- **TCX export**: a **TCX** button for each ride on the Rides page, for uploading to Strava,
  Garmin Connect, intervals.icu, TrainingPeaks and others. It includes power, cadence and
  heart rate, and is checked against Garmin's TCX schema.
- Tests run on GitHub on every push (Python 3.10 to 3.13).

## 0.4.0 (2026-11-23)

### New
- **Heart rate** from an ANT+ strap: dashboard tile, average and max in the stats, graph
  tooltip, and a new `heart_rate_bpm` column in the ride files.
- **Normalized power (NP)** in the ride stats.
- **Ride list** at `/rides`, with stats and a CSV download for each saved ride.
- **Power off and restart buttons** on the ride list page.
- **Install script** `deploy/install.sh`, which sets up everything for any user and folder.
- Only the **newest 10 ride files** are kept (`--keep-rides N` to change, 0 = keep all).
- The page says what's wrong, for example "ANT+ stick not found" or "No permission to open
  the ANT+ stick", instead of always "Searching for pedals".
- `--version`, plus tests (`python3 -m unittest`) and an MIT license.

### Fixed
- The program now reconnects when the ANT+ stick is unplugged and plugged back in. Before, it
  waited forever.
- Ride file names and times are right even when the Pi's clock is wrong at boot (no internet):
  the page sends the phone's time.
- If a ride can't be saved (disk full, no permission), the dashboard keeps updating and shows a
  warning. Before, it froze.
- Left + right could add up to more than the total power in some seconds.
- The program no longer crashes (segmentation fault) when it's stopped.
- Other web pages on the same network can no longer reset the ride.

## 0.3 (2026-11-23)
- Set up on the Raspberry Pi: service file and README for this Pi.
- Clear message when `openant` or `bleak` is missing for the chosen source.

## 0.2 (2026-11-23)
- The left and right lines on the graph can be switched off.

## 0.1 (2026-11-23)
- First version: power (total, left, right), cadence, moving time and a scrolling power graph
  from ANT+ or Bluetooth power meter pedals, a simulator, and a CSV file per ride.
