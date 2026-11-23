"""Ride state: turns raw power-meter readings into 1-second samples, timer and stats."""

import threading
import time
from dataclasses import dataclass, field
from typing import Optional

# If nothing new arrives from the meter for this long we treat the rider as stopped.
STALE_AFTER_S = 3.0
# Keep this many 1 s samples in memory so a reloaded page can redraw the graph (2 h).
HISTORY_LEN = 2 * 60 * 60


@dataclass
class Reading:
    """One update from a power meter, already decoded by a source."""
    power: int                      # total instantaneous power, W
    cadence: Optional[int] = None   # rpm, None if the meter didn't send it
    balance_pct: Optional[float] = None  # share of total power from `balance_side`, 0-100
    balance_side: Optional[str] = None   # 'left', 'right' or None (meter says "unknown")
    ts: float = field(default_factory=time.monotonic)


class Ride:
    def __init__(self, unknown_balance_side: str = "right", logger=None):
        # Which pedal an "unknown reference" balance value refers to. ANT+ dual-sided
        # meters normally flag it as right; BLE ones normally flag it as left.
        self.unknown_balance_side = unknown_balance_side
        self.logger = logger
        self._lock = threading.Lock()
        self.reset()

    def reset(self):
        with self._lock:
            self._bucket = []          # readings received during the current second
            self._last_reading = None  # last Reading, for stale detection
            self.elapsed = 0.0         # moving time, seconds
            self.history = []          # list of sample dicts
            self.sum_power = self.sum_left = self.sum_right = 0.0
            self.sum_cadence = 0.0
            self.n_moving = 0
            self.max_power = 0
            self.energy_j = 0.0
            self.started_wall = None
        if self.logger:
            self.logger.new_file()

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
                splits = [self._split(r) for r in bucket]
                splits = [s for s in splits if s[0] is not None]
                if splits:
                    left = sum(s[0] for s in splits) / len(splits)
                    right = sum(s[1] for s in splits) / len(splits)
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

            moving = connected and (power > 0 or (cadence or 0) > 0)
            if moving:
                if self.started_wall is None:
                    self.started_wall = time.time()
                self.elapsed += dt
                self.n_moving += 1
                self.sum_power += power
                self.sum_cadence += cadence or 0
                self.sum_left += left or 0
                self.sum_right += right or 0
                self.max_power = max(self.max_power, power)
                self.energy_j += power * dt

            sample = {
                "t": round(self.elapsed, 1),
                "wall": time.time(),
                "p": round(power),
                "l": None if left is None else round(left),
                "r": None if right is None else round(right),
                "c": None if cadence is None else round(cadence),
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
            sample["stats"] = self._stats()
            return sample

    def _stats(self) -> dict:
        n = self.n_moving or 1
        return {
            "avg_p": round(self.sum_power / n),
            "avg_l": round(self.sum_left / n),
            "avg_r": round(self.sum_right / n),
            "avg_c": round(self.sum_cadence / n),
            "max_p": round(self.max_power),
            "kj": round(self.energy_j / 1000, 1),
            "elapsed": round(self.elapsed, 1),
        }

    def snapshot(self) -> dict:
        with self._lock:
            return {"history": list(self.history), "stats": self._stats()}
