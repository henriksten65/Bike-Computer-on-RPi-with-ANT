# Changelog

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
