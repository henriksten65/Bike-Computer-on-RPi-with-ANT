# Changelog

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
