"""Ride state: turns raw power-meter readings into 1-second samples, timer and stats."""

import os
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Optional

# If nothing new arrives from the meter for this long we treat the rider as stopped.
STALE_AFTER_S = 3.0
# Keep this many 1 s samples in memory so a reloaded page can redraw the graph (12 h:
# the whole ride; a few MB at most).
HISTORY_LEN = 12 * 60 * 60
# Normalized power uses a rolling average over this many seconds of riding.
NP_WINDOW_S = 30
# Heart rate older than this is treated as "no heart rate".
HR_STALE_AFTER_S = 5.0
# Correct the clock when the browser's time differs from the Pi's by more than this.
CLOCK_TOLERANCE_S = 60
# systemd-timesyncd creates this file once the clock is set from the internet.
TIMESYNC_FLAG = "/run/systemd/timesync/synchronized"


def system_clock_synced():
    return os.path.exists(TIMESYNC_FLAG)


class WallClock:
    """Wall-clock time with an optional correction taken from the browser.

    The Pi has no battery-backed clock. Without internet at boot its time can be
    far off, which would give ride files wrong names and timestamps. The phone's
    clock is nearly always right, so the page sends it and we keep the difference.
    Once the Pi's own clock is set from the internet, it wins: a phone set to
    the wrong time must not change a correct clock.
    """

    def __init__(self, synced=system_clock_synced):
        self._synced = synced
        self.offset = 0.0
        self._lock = threading.Lock()
        self._base = self._system_base()
        # How far times reported so far were off, for fixing the open ride file.
        self._shift = 0.0

    @staticmethod
    def _system_base():
        # Changes only when the system clock is set (for example by NTP).
        return time.time() - time.monotonic()

    def _check_system_jump(self):
        base = self._system_base()
        if abs(base - self._base) > CLOCK_TOLERANCE_S:
            # The system clock was set (internet came up); trust it again. Times so far
            # were (old system time + offset); the right time was (old system time + jump).
            self._shift += (base - self._base) - self.offset
            self._base = base
            self.offset = 0.0

    def now(self) -> float:
        with self._lock:
            self._check_system_jump()
            return time.time() + self.offset

    def correct(self, browser_now: float) -> bool:
        """Take the browser's time (Unix seconds). Returns True if the offset changed."""
        with self._lock:
            self._check_system_jump()
            diff = browser_now - time.time()
            new = diff if abs(diff) > CLOCK_TOLERANCE_S and not self._synced() else 0.0
            if abs(new - self.offset) <= CLOCK_TOLERANCE_S:
                return False
            self._shift += new - self.offset
            self.offset = new
            return True

    def pop_shift(self) -> float:
        """Seconds to add to times reported since the last call (0 if none were off)."""
        with self._lock:
            self._check_system_jump()
            shift, self._shift = self._shift, 0.0
            return shift


clock = WallClock()


# --------------------------------------------------------------------------- training numbers

# Power zones as a share of FTP (Coggan): (number, name, upper limit as a fraction of FTP).
ZONES = [
    (1, "Active recovery", 0.55),
    (2, "Endurance", 0.75),
    (3, "Tempo", 0.90),
    (4, "Threshold", 1.05),
    (5, "VO2 max", 1.20),
    (6, "Anaerobic", 1.50),
    (7, "Neuromuscular", None),
]


def zone(power, ftp):
    """Zone number (1-7) for a power value, or None without an FTP."""
    if not ftp:
        return None
    for n, _name, upper in ZONES:
        if upper is None or power <= upper * ftp:
            return n


def intensity_factor(np_w, ftp):
    """IF = NP / FTP. About 0.75 for an endurance ride, 1.0 for an all-out hour."""
    if not ftp or np_w is None:
        return None
    return round(np_w / ftp, 2)


def training_stress(seconds, np_w, ftp):
    """TSS = hours x IF^2 x 100. One hour at FTP = 100."""
    if not ftp or np_w is None:
        return None
    return round(seconds / 3600 * (np_w / ftp) ** 2 * 100)


# Intense intervals (HIIT). With an FTP, power above threshold (zone 5 and up) is intense.
# Without one, power well above what the rider was doing before the interval is.
INTENSE_OF_FTP = 1.05
INTENSE_OF_BASELINE = 1.5    # x the average of the last easy riding
INTENSE_MIN_W = 150          # ... but never below this, so a warm-up doesn't count
BASELINE_S = 120             # easy riding to average for the baseline
BASELINE_MIN_S = 60          # no detection without an FTP until there's this much
INTENSE_START_S = 3          # seconds in a row at the level to start an interval
INTENSE_KEEP = 0.85          # an interval goes on while power is at least this x the level
INTENSE_END_S = 5            # seconds in a row below that to end it


class IntenseDetector:
    """Notices when the rider starts an intense interval and times it.

    Starts after INTENSE_START_S seconds in a row at the intense level (counted
    from the first of them), so a single spike doesn't start one. Ends after
    INTENSE_END_S seconds in a row below INTENSE_KEEP x that level, so a gear
    change or a fading last few seconds don't end it. One call per second,
    moving or not: stopping ends an interval.
    """

    def __init__(self):
        self.seconds: Optional[int] = None   # length of the interval going on, or None
        self._above = 0
        self._below = 0
        self._baseline: deque[float] = deque(maxlen=BASELINE_S)

    def level(self, ftp) -> Optional[float]:
        """Power (W) at which an interval starts, or None if it can't be told yet."""
        if ftp:
            return INTENSE_OF_FTP * ftp
        if len(self._baseline) < BASELINE_MIN_S:
            return None
        return max(INTENSE_MIN_W, INTENSE_OF_BASELINE * sum(self._baseline) / len(self._baseline))

    def add(self, power, moving, ftp=None):
        level = self.level(ftp)      # the baseline doesn't change during an interval
        if self.seconds is None:
            if moving and level is not None and power >= level:
                self._above += 1
                if self._above >= INTENSE_START_S:
                    self.seconds, self._below = self._above, 0
            else:
                self._above = 0
                if moving:
                    self._baseline.append(power)
        else:
            self.seconds += 1
            hard = moving and level is not None and power >= INTENSE_KEEP * level
            self._below = 0 if hard else self._below + 1
            if self._below >= INTENSE_END_S:
                self.seconds, self._above = None, 0


@dataclass
class Reading:
    """One update from a power meter, already decoded by a source."""
    power: int                      # total instantaneous power, W
    cadence: Optional[int] = None   # rpm, None if the meter didn't send it
    balance_pct: Optional[float] = None  # share of total power from `balance_side`, 0-100
    balance_side: Optional[str] = None   # 'left', 'right' or None (meter says "unknown")
    ts: float = field(default_factory=time.monotonic)


class Totals:
    """Running totals for the whole ride or one lap, one call per moving second."""

    def __init__(self, start_t=0.0):
        self.start_t = start_t       # ride moving time when this started, s
        self.seconds = 0
        self.sum_power = self.sum_left = self.sum_right = 0.0
        self.sum_cadence = 0.0
        self.n_cadence = 0           # seconds with cadence > 0 (coasting doesn't count)
        self.zone_s = [0] * len(ZONES)   # seconds in each power zone (with an FTP set)
        self.max_power = 0
        self.energy_j = 0.0
        self.sum_hr = 0.0
        self.n_hr = 0
        self.max_hr = 0
        self._np_window: deque[float] = deque(maxlen=NP_WINDOW_S)  # last 30 s of power
        self._np_sum4 = 0.0                        # sum of (30 s average) ** 4
        self._np_n = 0

    def add(self, power, cadence, left, right, hr, dt=1.0, ftp=None):
        self.seconds += 1
        self.sum_power += power
        if cadence:
            self.sum_cadence += cadence
            self.n_cadence += 1
        z = zone(power, ftp)
        if z:
            self.zone_s[z - 1] += 1
        self.sum_left += left or 0
        self.sum_right += right or 0
        self.max_power = max(self.max_power, power)
        self.energy_j += power * dt
        if hr is not None:
            self.sum_hr += hr
            self.n_hr += 1
            self.max_hr = max(self.max_hr, hr)
        # Normalized power: 4th-power mean of the 30 s rolling average,
        # counted once the first full 30 s window is available.
        self._np_window.append(power)
        if len(self._np_window) == NP_WINDOW_S:
            self._np_sum4 += (sum(self._np_window) / NP_WINDOW_S) ** 4
            self._np_n += 1

    def np(self):
        return round((self._np_sum4 / self._np_n) ** 0.25) if self._np_n else None

    def to_dict(self, ftp=None):
        n = self.seconds or 1
        np_w = self.np()
        return {
            "avg_p": round(self.sum_power / n),
            "avg_l": round(self.sum_left / n),
            "avg_r": round(self.sum_right / n),
            "avg_c": round(self.sum_cadence / self.n_cadence) if self.n_cadence else None,
            "max_p": round(self.max_power),
            "np": np_w,
            "if": intensity_factor(np_w, ftp),
            "tss": training_stress(self.seconds, np_w, ftp),
            "avg_hr": round(self.sum_hr / self.n_hr) if self.n_hr else None,
            "max_hr": round(self.max_hr) if self.n_hr else None,
            "kj": round(self.energy_j / 1000, 1),
            "zones": list(self.zone_s) if any(self.zone_s) else None,
            "elapsed": float(self.seconds),
            "start_t": round(self.start_t, 1),
        }


class Ride:
    def __init__(self, unknown_balance_side: str = "right", logger=None, ftp=None, efficiency=24):
        # Which pedal an "unknown reference" balance value refers to. ANT+ dual-sided
        # meters normally flag it as right; BLE ones normally flag it as left.
        self.unknown_balance_side = unknown_balance_side
        self.logger = logger
        self.ftp = ftp               # functional threshold power, W (None = not set)
        self.efficiency = efficiency # % of food energy that becomes work at the pedals (for kcal)
        self._lock = threading.Lock()
        # What the data source is doing, shown on the page when no data is coming in.
        # level: "searching" (waiting for the pedals) or "error" (something to fix).
        self.source_status = {"level": "searching", "text": "Starting…"}
        self._recording = True       # False while powering off / shutting down
        self.reset()

    def reset(self):
        with self._lock:
            self._bucket = []          # readings received during the current second
            self._last_reading: Optional[Reading] = None  # last Reading, for stale detection
            self.elapsed = 0.0         # moving time, seconds
            self.history = []          # list of sample dicts
            self.total = Totals()      # the whole ride
            self.lap = Totals()        # the lap in progress
            self.laps = []             # finished laps (Totals)
            self.started_wall = None
            self._hr = None                 # (bpm, monotonic time) of the last heart rate
            self.intense = IntenseDetector()
            # Inside the lock: never close the file while tick() is writing to it.
            if self.logger:
                self.logger.new_file()

    def stop_recording(self):
        """Close the ride file and write no more (before power off or shutdown).

        Without this, the next second would open a new file if the pedals were
        still sending, leaving a few-second "ride" behind.
        """
        with self._lock:
            self._recording = False
            if self.logger:
                self.logger.new_file()

    def resume_recording(self):
        """Undo stop_recording(), for example when powering off failed."""
        with self._lock:
            self._recording = True

    def new_lap(self):
        """Finish the current lap and start the next. Returns False if the lap is empty."""
        with self._lock:
            if self.lap.seconds == 0:
                return False
            self.laps.append(self.lap)
            self.lap = Totals(start_t=self.elapsed)
            return True

    def _lap_dict(self, totals, n):
        d = totals.to_dict(self.ftp)
        d["n"] = n
        return d

    def set_source_status(self, level: str, text: str):
        self.source_status = {"level": level, "text": text}

    def add_heart_rate(self, bpm: int, ts: Optional[float] = None):
        """Heart rate from a strap; kept separately from the power meter's readings."""
        with self._lock:
            self._hr = (bpm, time.monotonic() if ts is None else ts)

    def add(self, r: Reading):
        with self._lock:
            self._bucket.append(r)
            self._last_reading = r

    def _split(self, r: Reading):
        """Return (left, right) watts for a reading, or (None, None) if no balance."""
        if r.balance_pct is None:
            return None, None
        side = r.balance_side or self.unknown_balance_side
        share = r.balance_pct / 100.0
        other = r.power * (1.0 - share)
        mine = r.power * share
        return (mine, other) if side == "left" else (other, mine)

    def tick(self, dt: float) -> dict:
        """Called once per second. Returns the sample for this second."""
        now = time.monotonic()
        with self._lock:
            shift = clock.pop_shift()
            if shift:
                if self.started_wall is not None:
                    self.started_wall += shift
                for h in self.history:
                    h["wall"] += shift
                if self.logger:
                    self.logger.shift_times(shift)
            bucket, self._bucket = self._bucket, []
            last = self._last_reading
            connected = last is not None and (now - last.ts) < STALE_AFTER_S

            if bucket:
                power = sum(r.power for r in bucket) / len(bucket)
                cads = [r.cadence for r in bucket if r.cadence is not None]
                cadence = sum(cads) / len(cads) if cads else None
                # Left/right: power-weighted left share from the readings that carry
                # a balance, applied to this second's total so that L + R = total
                # even when some readings in the second have no balance.
                splits = [(self._split(r)[0], r.power) for r in bucket if r.balance_pct is not None]
                balanced_power = sum(p for _, p in splits)
                if balanced_power > 0:
                    left = power * sum(l for l, _ in splits) / balanced_power
                    right = power - left
                else:
                    left = right = None
            elif connected and last is not None:   # (connected implies a reading)
                # Meter is alive but sent nothing this second: hold last values.
                power = last.power
                cadence = last.cadence
                left, right = self._split(last)
            else:
                power, left, right = 0, None, None
                cadence = 0 if last is not None else None

            hr = self._hr[0] if self._hr and now - self._hr[1] < HR_STALE_AFTER_S else None

            moving = connected and (power > 0 or (cadence or 0) > 0)
            if moving:
                if self.started_wall is None:
                    self.started_wall = clock.now()
                self.elapsed += dt
                self.total.add(power, cadence, left, right, hr, dt, self.ftp)
                self.lap.add(power, cadence, left, right, hr, dt, self.ftp)
            self.intense.add(power, moving, self.ftp)

            sample = {
                "t": round(self.elapsed, 1),
                "wall": clock.now(),
                "p": round(power),
                "l": None if left is None else round(left),
                "r": None if right is None else round(right),
                "c": None if cadence is None else round(cadence),
                "h": hr,
                "lap": len(self.laps) + 1,
                "moving": moving,
            }
            # Only record graph points while moving, so pauses don't flatten the graph.
            if moving:
                self.history.append(dict(sample))
                if len(self.history) > HISTORY_LEN:
                    del self.history[: len(self.history) - HISTORY_LEN]
                if self.logger and self._recording:
                    self.logger.write(sample, ftp=self.ftp)

            sample["connected"] = connected
            sample["source"] = self.source_status
            sample["log_error"] = getattr(self.logger, "error", None)
            sample["stats"] = self._stats()
            return sample

    def _stats(self) -> dict:
        st = self.total.to_dict(self.ftp)
        st["elapsed"] = round(self.elapsed, 1)
        st["ftp"] = self.ftp
        st["efficiency"] = self.efficiency
        st["intense_s"] = self.intense.seconds   # the intense interval going on, or None
        st["lap"] = self._lap_dict(self.lap, len(self.laps) + 1)
        # As dicts each time, so IF and TSS follow a changed FTP.
        st["laps"] = [self._lap_dict(t, i + 1) for i, t in enumerate(self.laps)]
        return st

    def snapshot(self) -> dict:
        with self._lock:
            return {"history": list(self.history), "stats": self._stats()}
