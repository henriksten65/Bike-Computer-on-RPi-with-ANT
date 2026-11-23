"""Ride files: writing one CSV per ride, and reading saved rides back for the ride list."""

import csv
import logging
import os
import re
import time
from datetime import datetime

from ride import clock, normalized_power

log = logging.getLogger("ridelog")

COLUMNS = ["time_iso", "elapsed_s", "power_w", "left_w", "right_w", "cadence_rpm", "heart_rate_bpm"]
# Ride file names; anything else in the folder is never listed, served or deleted.
NAME_RE = re.compile(r"^ride-(\d{8})-(\d{6})\.csv$")


class CsvLogger:
    """Writes one CSV per ride into rides/ (a new file on every reset).

    A failed write (disk full, no permission, ...) never stops the ride: it is
    logged once, shown on the page via `error`, and retried with the next sample.
    """

    def __init__(self, directory, keep=10):
        self.dir = directory
        self.keep = keep   # how many ride files to keep (0 = keep all)
        self.f = None
        self.w = None
        self.path = None
        self.error = None          # message for the page while saving fails
        self._opened_mono = None   # time.monotonic() when the file was opened

    def _prune(self):
        """Delete the oldest ride files so that only the newest `keep` remain."""
        if self.keep <= 0:
            return
        rides = sorted(n for n in os.listdir(self.dir)
                       if n.startswith("ride-") and n.endswith(".csv"))
        for name in rides[:-self.keep]:
            try:
                os.remove(os.path.join(self.dir, name))
                log.info("Deleted old ride file %s", name)
            except OSError as e:
                log.warning("Could not delete %s: %s", name, e)

    def _name(self, wall):
        return os.path.join(self.dir, datetime.fromtimestamp(wall).strftime("ride-%Y%m%d-%H%M%S.csv"))

    def new_file(self):
        if self.f:
            try:
                self.f.close()
            except OSError:
                pass
            self.f = None

    def write(self, s):
        try:
            if self.f is None:
                os.makedirs(self.dir, exist_ok=True)
                self.path = self._name(clock.now())
                self.f = open(self.path, "w", newline="")
                self._opened_mono = time.monotonic()
                self.w = csv.writer(self.f)
                self.w.writerow(COLUMNS)
                log.info("Logging ride to %s", self.path)
                self._prune()
            self.w.writerow([datetime.fromtimestamp(s["wall"]).isoformat(timespec="seconds"),
                             s["t"], s["p"], s["l"], s["r"], s["c"], s.get("h")])
            self.f.flush()
            if self.error:
                log.info("Saving the ride works again")
                self.error = None
        except OSError as e:
            if not self.error:
                log.error("Could not save the ride to %s: %s", self.path or self.dir, e)
            self.error = f"Can't save the ride: {e.strerror or e}"
            self.new_file()   # start over with a fresh file next time

    def clock_corrected(self):
        """Rename the open ride file so its name uses the corrected time."""
        if self.f is None:
            return
        new = self._name(clock.now() - (time.monotonic() - self._opened_mono))
        if new != self.path:
            try:
                os.rename(self.path, new)
                log.info("Renamed %s to %s after the clock was corrected",
                         os.path.basename(self.path), os.path.basename(new))
                self.path = new
            except OSError as e:
                log.warning("Could not rename %s: %s", self.path, e)


def _num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def summarize(path):
    """Stats for one saved ride file (only moving seconds are in the file)."""
    powers, cads, hrs, lefts, rights = [], [], [], [], []
    first = last_elapsed = None
    with open(path, newline="") as f:
        for row in csv.DictReader(f):
            p = _num(row.get("power_w"))
            if p is None:
                continue
            first = first or row.get("time_iso")
            last_elapsed = _num(row.get("elapsed_s"))
            powers.append(p)
            for lst, key in ((cads, "cadence_rpm"), (hrs, "heart_rate_bpm"),
                             (lefts, "left_w"), (rights, "right_w")):
                v = _num(row.get(key))
                if v is not None:
                    lst.append(v)
    n = len(powers)
    avg = lambda xs: round(sum(xs) / len(xs)) if xs else None
    bal = sum(lefts) + sum(rights)
    return {
        "start": first,
        "moving_s": round(last_elapsed or 0),
        "avg_p": avg(powers),
        "np": normalized_power(powers),
        "max_p": round(max(powers)) if n else None,
        "avg_c": avg(cads),
        "avg_hr": avg(hrs),
        "max_hr": round(max(hrs)) if hrs else None,
        "balance_l": round(100 * sum(lefts) / bal) if bal > 0 else None,
        "kj": round(sum(powers) / 1000, 1),   # 1 s samples: W x 1 s = J
    }


_cache = {}   # path -> ((mtime, size), summary)


def list_rides(directory, current=None):
    """All saved rides, newest first, with stats. `current` = path being written now."""
    try:
        names = [n for n in os.listdir(directory) if NAME_RE.match(n)]
    except FileNotFoundError:
        return []
    out = []
    for name in sorted(names, reverse=True):
        path = os.path.join(directory, name)
        try:
            st = os.stat(path)
            key = (st.st_mtime, st.st_size)
            cached = _cache.get(path)
            if cached and cached[0] == key:
                summary = cached[1]
            else:
                summary = summarize(path)
                _cache[path] = (key, summary)
        except (OSError, csv.Error, UnicodeDecodeError) as e:
            log.warning("Could not read %s: %s", name, e)
            continue
        out.append({"name": name, "size": st.st_size,
                    "in_progress": current is not None and os.path.abspath(path) == os.path.abspath(current),
                    **summary})
    return out
