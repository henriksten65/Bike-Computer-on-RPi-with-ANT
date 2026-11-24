"""Ride files: writing one CSV per ride, and reading saved rides back for the ride list."""

import csv
import logging
import os
import re
import time
from datetime import datetime, timezone
from typing import Any, Optional, TextIO

from ride import ZONES, clock, intensity_factor, normalized_power, training_stress, zone

log = logging.getLogger("ridelog")

COLUMNS = ["time_iso", "elapsed_s", "power_w", "left_w", "right_w", "cadence_rpm", "heart_rate_bpm", "lap", "ftp_w"]
def _iso(wall):
    """Local time with its UTC offset, e.g. 2026-11-23T16:22:08+01:00.

    The offset keeps times unambiguous when the clocks go back in autumn (the
    hour 02:00-03:00 happens twice). Files from before 0.7.1 have no offset;
    they are read as local time.
    """
    return datetime.fromtimestamp(wall).astimezone().isoformat(timespec="seconds")


# Ride file names; anything else in the folder is never listed, served or deleted.
NAME_RE = re.compile(r"^ride-(\d{8})-(\d{6})\.csv$")
TCX_NAME_RE = re.compile(r"^(ride-\d{8}-\d{6})\.tcx$")


class CsvLogger:
    """Writes one CSV per ride into rides/ (a new file on every reset).

    A failed write (disk full, no permission, ...) never stops the ride: it is
    logged once, shown on the page via `error`, and retried with the next sample.
    """

    def __init__(self, directory, keep=10):
        self.dir = directory
        self.keep = keep   # how many ride files to keep (0 = keep all)
        self.f: Optional[TextIO] = None
        self.w: Any = None         # csv writer for self.f
        self.path: Optional[str] = None
        self.error = None          # message for the page while saving fails
        self._opened_mono = None   # time.monotonic() when the file was opened

    def _prune(self):
        """Delete the oldest ride files so that only the newest `keep` remain."""
        if self.keep <= 0:
            return
        rides = sorted(n for n in os.listdir(self.dir)
                       if n.startswith("ride-") and n.endswith(".csv"))
        current = os.path.basename(self.path) if self.path else None
        for name in rides[:-self.keep]:
            if name == current:
                # Older-looking than the rest only because of a wrong clock: keep it.
                continue
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

    def write(self, s, ftp=None):
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
            self.w.writerow([_iso(s["wall"]),
                             s["t"], s["p"], s["l"], s["r"], s["c"], s.get("h"), s.get("lap"), ftp])
            self.f.flush()
            if self.error:
                log.info("Saving the ride works again")
                self.error = None
        except OSError as e:
            if not self.error:
                log.error("Could not save the ride to %s: %s", self.path or self.dir, e)
            self.error = f"Can't save the ride: {e.strerror or e}"
            self.new_file()   # start over with a fresh file next time

    def shift_times(self, shift):
        """Fix the open ride file after the clock was corrected by `shift` seconds.

        Rows already written get the corrected time, and the file is renamed to
        match. Called from the update loop, so it never runs during a write.
        """
        if self.f is None or self.path is None or not shift:
            return
        old = self.path
        try:
            self.f.close()
            self.f = None
            with open(old, newline="") as f:
                rows = list(csv.reader(f))
            for row in rows[1:]:
                try:
                    t = datetime.fromisoformat(row[0]).timestamp() + shift
                    row[0] = _iso(t)
                except (ValueError, IndexError):
                    pass
            start = datetime.fromisoformat(rows[1][0]).timestamp() if len(rows) > 1 else clock.now()
            new = self._name(start)
            tmp = new + ".tmp"
            with open(tmp, "w", newline="") as f:
                csv.writer(f).writerows(rows)
            os.replace(tmp, new)
            if new != old:
                os.remove(old)
            self.path = new
            self.f = open(new, "a", newline="")
            self.w = csv.writer(self.f)
            log.info("Clock corrected by %+.0f s: times in %s fixed%s", shift, os.path.basename(new),
                     f" (was {os.path.basename(old)})" if new != old else "")
        except (OSError, ValueError, IndexError) as e:
            log.warning("Could not fix the times in %s: %s", old, e)
            self.new_file()
            self.f = None


def _num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def summarize(path, ftp=None):
    """Stats for one saved ride file (only moving seconds are in the file).

    Uses the FTP saved in the file (since 0.7); `ftp` is the fallback for older
    files without one.
    """
    powers: list[float] = []
    cads: list[float] = []
    hrs: list[float] = []
    lefts: list[float] = []
    rights: list[float] = []
    first = last_elapsed = None
    laps = 1
    file_ftp = None
    zone_s = [0] * len(ZONES)
    with open(path, newline="") as f:
        for row in csv.DictReader(f):
            p = _num(row.get("power_w"))
            if p is None:
                continue
            first = first or row.get("time_iso")
            last_elapsed = _num(row.get("elapsed_s"))
            powers.append(p)
            laps = max(laps, int(_num(row.get("lap")) or 1))
            row_ftp = _num(row.get("ftp_w"))
            if row_ftp:
                file_ftp = round(row_ftp)
            z = zone(p, row_ftp or ftp)
            if z:
                zone_s[z - 1] += 1
            for lst, key in ((cads, "cadence_rpm"), (hrs, "heart_rate_bpm"),
                             (lefts, "left_w"), (rights, "right_w")):
                v = _num(row.get(key))
                if v is not None:
                    lst.append(v)
    n = len(powers)
    avg = lambda xs: round(sum(xs) / len(xs)) if xs else None
    cads = [c for c in cads if c > 0]          # coasting doesn't count
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
        "laps": laps,
        "ftp": file_ftp or ftp,
        "ftp_saved": file_ftp is not None,     # False: the current FTP was used
        "zones": zone_s if any(zone_s) else None,
    }


_cache: dict[str, tuple] = {}   # path -> ((mtime, size, fallback ftp), summary)


def list_rides(directory, current=None, ftp=None):
    """All saved rides, newest first, with stats. `current` = path being written now.

    IF, TSS and zones use the FTP saved in each file; `ftp` (the current
    setting) is used only for files from before 0.7, which don't have one.
    """
    try:
        names = [n for n in os.listdir(directory) if NAME_RE.match(n)]
    except FileNotFoundError:
        return []
    out = []
    for name in sorted(names, reverse=True):
        path = os.path.join(directory, name)
        try:
            st = os.stat(path)
            key = (st.st_mtime, st.st_size, ftp)
            cached = _cache.get(path)
            if cached and cached[0] == key:
                summary = cached[1]
            else:
                summary = summarize(path, ftp)
                _cache[path] = (key, summary)
        except (OSError, csv.Error, UnicodeDecodeError) as e:
            log.warning("Could not read %s: %s", name, e)
            continue
        out.append({"name": name, "size": st.st_size,
                    "in_progress": current is not None and os.path.abspath(path) == os.path.abspath(current),
                    **summary,
                    "if": intensity_factor(summary["np"], summary["ftp"]),
                    "tss": training_stress(summary["moving_s"], summary["np"], summary["ftp"])})
    return out


# --------------------------------------------------------------------------- TCX export

def _utc(time_iso):
    """CSV time -> TCX time in UTC. With an offset (0.7.1 and later) it's exact;
    without one (older files) it's read as the Pi's local time."""
    t = datetime.fromisoformat(time_iso)
    if t.tzinfo is None:
        t = t.astimezone()
    return t.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _int_or_none(v, lo, hi):
    n = _num(v)
    if n is None:
        return None
    return max(lo, min(hi, round(n)))


def _lap_xml(points):
    """One TCX <Lap> for a list of (time, watts, cadence, hr) points."""
    watts = [p for _, p, _, _ in points]
    cads = [c for _, _, c, _ in points if c]          # coasting doesn't count
    hrs = [h for _, _, _, h in points if h is not None]
    out = [f'<Lap StartTime="{points[0][0]}">',
           f'<TotalTimeSeconds>{len(points)}</TotalTimeSeconds>',
           '<DistanceMeters>0</DistanceMeters>',
           # On the bike, kJ of work is about the same number as kcal burned.
           f'<Calories>{round(sum(watts) / 1000)}</Calories>']
    if hrs:
        out.append(f'<AverageHeartRateBpm><Value>{round(sum(hrs) / len(hrs))}</Value></AverageHeartRateBpm>')
        out.append(f'<MaximumHeartRateBpm><Value>{max(hrs)}</Value></MaximumHeartRateBpm>')
    out.append('<Intensity>Active</Intensity>')
    if cads:
        out.append(f'<Cadence>{min(254, round(sum(cads) / len(cads)))}</Cadence>')
    out.append('<TriggerMethod>Manual</TriggerMethod>')
    out.append('<Track>')
    for t, p, c, h in points:
        tp = [f'<Trackpoint><Time>{t}</Time>']
        if h is not None:
            tp.append(f'<HeartRateBpm><Value>{h}</Value></HeartRateBpm>')
        if c is not None:
            tp.append(f'<Cadence>{c}</Cadence>')
        tp.append(f'<Extensions><ns3:TPX><ns3:Watts>{p}</ns3:Watts></ns3:TPX></Extensions></Trackpoint>')
        out.append("".join(tp))
    out.append('</Track>')
    out.append('<Extensions><ns3:LX>'
               f'<ns3:AvgWatts>{round(sum(watts) / len(watts))}</ns3:AvgWatts>'
               f'<ns3:MaxWatts>{max(watts)}</ns3:MaxWatts>'
               '</ns3:LX></Extensions>')
    out.append('</Lap>')
    return out


def to_tcx(path, version="0.0.0"):
    """Convert a saved ride (CSV) to TCX for Strava, Garmin Connect, intervals.icu, ...

    One TCX lap per lap pressed on the dashboard, one trackpoint per second of
    moving time with power, cadence and heart rate. No GPS or distance: the
    uploader treats it as an indoor ride. Pauses aren't in the file, so they
    show up as gaps in the time.
    """
    laps: dict[int, list] = {}   # lap number -> points, in file order
    with open(path, newline="") as f:
        for row in csv.DictReader(f):
            if _num(row.get("power_w")) is None or not row.get("time_iso"):
                continue
            try:
                t = _utc(row["time_iso"])
            except ValueError:
                continue
            lap = int(_num(row.get("lap")) or 1)
            laps.setdefault(lap, []).append((
                t,
                _int_or_none(row.get("power_w"), 0, 65535),
                _int_or_none(row.get("cadence_rpm"), 0, 254),       # TCX limit
                _int_or_none(row.get("heart_rate_bpm"), 1, 255)))
    if not laps:
        raise ValueError("the ride has no data")

    first = laps[min(laps)][0][0]
    out = ['<?xml version="1.0" encoding="UTF-8"?>',
           '<TrainingCenterDatabase'
           ' xmlns="http://www.garmin.com/xmlschemas/TrainingCenterDatabase/v2"'
           ' xmlns:ns3="http://www.garmin.com/xmlschemas/ActivityExtension/v2"'
           ' xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">',
           '<Activities>',
           '<Activity Sport="Biking">',
           f'<Id>{first}</Id>']
    for n in sorted(laps):
        out += _lap_xml(laps[n])
    # The recording device, shown by Strava and others as "Bike computer".
    major, minor, *_ = (version.split(".") + ["0", "0"])
    out.append('<Creator xsi:type="Device_t"><Name>Bike computer</Name>'
               '<UnitId>0</UnitId><ProductID>0</ProductID>'
               f'<Version><VersionMajor>{int(major)}</VersionMajor><VersionMinor>{int(minor)}</VersionMinor></Version>'
               '</Creator>')
    out.append('</Activity>')
    out.append('</Activities>')
    out.append('</TrainingCenterDatabase>')
    return ("\n".join(out) + "\n").encode("utf-8")
