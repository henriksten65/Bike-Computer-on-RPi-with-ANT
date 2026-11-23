"""Ride state: turns raw power-meter readings into 1-second samples, timer and stats."""

import threading
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Optional

# If nothing new arrives from the meter for this long we treat the rider as stopped.
STALE_AFTER_S = 3.0
# Keep this many 1 s samples in memory so a reloaded page can redraw the graph (2 h).
HISTORY_LEN = 2 * 60 * 60
# Normalized power uses a rolling average over this many seconds of riding.
NP_WINDOW_S = 30
# Heart rate older than this is treated as "no heart rate".
HR_STALE_AFTER_S = 5.0
# Correct the clock when the browser's time differs from the Pi's by more than this.
CLOCK_TOLERANCE_S = 60


class WallClock:
    """Wall-clock time with an optional correction taken from the browser.

    The Pi has no battery-backed clock. Without internet at boot its time can be
    far off, which would give ride files wrong names and timestamps. The phone's
    clock is always right, so the page sends it and we keep the difference.
    """

    def __init__(self):
        self.offset = 0.0
        self._lock = threading.Lock()
        self._base = self._system_base()

    @staticmethod
    def _system_base():
        # Changes only when the system clock is set (for example by NTP).
        return time.time() - time.monotonic()

    def _check_system_jump(self):
        base = self._system_base()
        if abs(base - self._base) > CLOCK_TOLERANCE_S:
            # The system clock was set (internet came up); trust it again.
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
            new = diff if abs(diff) > CLOCK_TOLERANCE_S else 0.0
            if abs(new - self.offset) <= CLOCK_TOLERANCE_S:
                return False
            self.offset = new
            return True


clock = WallClock()


def normalized_power(powers):
    """Normalized power of a list of 1 s power values (moving seconds only).

    4th-power mean of the 30 s rolling average; None for less than 30 s.
    Same calculation as the live value in Ride, used for saved rides.
    """
    if len(powers) < NP_WINDOW_S:
        return None
    window_sum = sum(powers[:NP_WINDOW_S])
    sum4 = (window_sum / NP_WINDOW_S) ** 4
    for i in range(NP_WINDOW_S, len(powers)):
        window_sum += powers[i] - powers[i - NP_WINDOW_S]
        sum4 += (window_sum / NP_WINDOW_S) ** 4
    return round((sum4 / (len(powers) - NP_WINDOW_S + 1)) ** 0.25)


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
        self.max_power = 0
        self.energy_j = 0.0
        self.sum_hr = 0.0
        self.n_hr = 0
        self.max_hr = 0
        self._np_window = deque(maxlen=NP_WINDOW_S)  # last 30 s of power
        self._np_sum4 = 0.0                        # sum of (30 s average) ** 4
        self._np_n = 0

    def add(self, power, cadence, left, right, hr, dt=1.0):
        self.seconds += 1
        self.sum_power += power
        self.sum_cadence += cadence or 0
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
            "avg_c": round(self.sum_cadence / n),
            "max_p": round(self.max_power),
            "np": np_w,
            "if": intensity_factor(np_w, ftp),
            "tss": training_stress(self.seconds, np_w, ftp),
            "avg_hr": round(self.sum_hr / self.n_hr) if self.n_hr else None,
            "max_hr": self.max_hr or None,
            "kj": round(self.energy_j / 1000, 1),
            "elapsed": float(self.seconds),
            "start_t": round(self.start_t, 1),
        }


class Ride:
    def __init__(self, unknown_balance_side: str = "right", logger=None, ftp=None):
        # Which pedal an "unknown reference" balance value refers to. ANT+ dual-sided
        # meters normally flag it as right; BLE ones normally flag it as left.
        self.unknown_balance_side = unknown_balance_side
        self.logger = logger
        self.ftp = ftp               # functional threshold power, W (None = not set)
        self._lock = threading.Lock()
        # What the data source is doing, shown on the page when no data is coming in.
        # level: "searching" (waiting for the pedals) or "error" (something to fix).
        self.source_status = {"level": "searching", "text": "Starting…"}
        self.reset()

    def reset(self):
        with self._lock:
            self._bucket = []          # readings received during the current second
            self._last_reading = None  # last Reading, for stale detection
            self.elapsed = 0.0         # moving time, seconds
            self.history = []          # list of sample dicts
            self.total = Totals()      # the whole ride
            self.lap = Totals()        # the lap in progress
            self.laps = []             # finished laps (Totals)
            self.started_wall = None
            self._hr = None                 # (bpm, monotonic time) of the last heart rate
        if self.logger:
            self.logger.new_file()

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

    def add_heart_rate(self, bpm: int, ts: float = None):
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
            elif connected:
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
                self.total.add(power, cadence, left, right, hr, dt)
                self.lap.add(power, cadence, left, right, hr, dt)

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
                if self.logger:
                    self.logger.write(sample)

            sample["connected"] = connected
            sample["source"] = self.source_status
            sample["log_error"] = getattr(self.logger, "error", None)
            sample["stats"] = self._stats()
            return sample

    def _stats(self) -> dict:
        st = self.total.to_dict(self.ftp)
        st["elapsed"] = round(self.elapsed, 1)
        st["ftp"] = self.ftp
        st["lap"] = self._lap_dict(self.lap, len(self.laps) + 1)
        # As dicts each time, so IF and TSS follow a changed FTP.
        st["laps"] = [self._lap_dict(t, i + 1) for i, t in enumerate(self.laps)]
        return st

    def snapshot(self) -> dict:
        with self._lock:
            return {"history": list(self.history), "stats": self._stats()}
