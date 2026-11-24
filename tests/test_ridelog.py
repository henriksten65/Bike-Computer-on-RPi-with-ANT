"""Ride files: writing, keeping the newest N, failed saves, reading back for the ride list."""

import csv
import os
import tempfile
import time
import unittest
import xml.etree.ElementTree as ET
from datetime import datetime
from unittest import mock

import ridelog
from ridelog import CsvLogger, list_rides, summarize


def sample(t, p=200, l=100, r=100, c=90, h=None, lap=1, wall=1795430000.0):
    return {"t": t, "wall": wall + t, "p": p, "l": l, "r": r, "c": c, "h": h, "lap": lap}


def path_of(lg: CsvLogger) -> str:
    """The open ride file's path (fails the test if there is none)."""
    assert lg.path is not None, "no ride file"
    return lg.path


class CsvLoggerTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = self.tmp.name

    def tearDown(self):
        os.chmod(self.dir, 0o755)
        self.tmp.cleanup()

    def test_writes_header_and_rows(self):
        lg = CsvLogger(self.dir)
        lg.write(sample(1, h=140), ftp=250)
        lg.write(sample(2))
        lg.new_file()
        with open(path_of(lg), newline="") as f:
            rows = list(csv.reader(f))
        self.assertEqual(rows[0], ridelog.COLUMNS)
        self.assertEqual(rows[1][1:], ["1", "200", "100", "100", "90", "140", "1", "250"])
        self.assertEqual(rows[2][-3], "")      # no heart rate
        self.assertEqual(rows[2][-1], "")      # no FTP set

    def test_keeps_only_newest_rides_and_leaves_other_files(self):
        for d in range(10, 22):
            open(os.path.join(self.dir, f"ride-202611{d}-120000.csv"), "w").close()
        open(os.path.join(self.dir, "notes.txt"), "w").close()
        lg = CsvLogger(self.dir, keep=10)
        lg.write(sample(1))
        names = sorted(os.listdir(self.dir))
        rides = [n for n in names if n.startswith("ride-")]
        self.assertEqual(len(rides), 10)
        self.assertIn("notes.txt", names)
        self.assertIn(os.path.basename(path_of(lg)), rides)
        self.assertNotIn("ride-20261112-120000.csv", rides)
        lg.new_file()

    def test_keep_zero_keeps_all(self):
        for d in range(10, 22):
            open(os.path.join(self.dir, f"ride-202611{d}-120000.csv"), "w").close()
        lg = CsvLogger(self.dir, keep=0)
        lg.write(sample(1))
        lg.new_file()
        self.assertEqual(len(os.listdir(self.dir)), 13)

    @unittest.skipIf(os.geteuid() == 0, "root can always write")
    def test_failed_save_is_reported_and_retried(self):
        os.chmod(self.dir, 0o555)
        lg = CsvLogger(self.dir)
        lg.write(sample(1))                       # must not raise
        self.assertIn("Can't save the ride", lg.error or "")
        os.chmod(self.dir, 0o755)
        lg.write(sample(2))
        self.assertIsNone(lg.error)
        self.assertTrue(os.path.exists(path_of(lg)))
        lg.new_file()


class SummaryTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = self.tmp.name

    def tearDown(self):
        self.tmp.cleanup()

    def write(self, name, header, rows):
        path = os.path.join(self.dir, name)
        with open(path, "w", newline="") as f:
            w = csv.writer(f)
            w.writerow(header)
            w.writerows(rows)
        return path

    def test_summary_of_a_ride(self):
        rows = [[f"2026-11-23T10:00:{i % 60:02d}", i + 1, 200, 80, 120, 90, 140 + i % 2] for i in range(60)]
        st = summarize(self.write("ride-20261123-100000.csv", ridelog.COLUMNS, rows))
        self.assertEqual(st["start"], "2026-11-23T10:00:00")
        self.assertEqual((st["moving_s"], st["avg_p"], st["np"], st["max_p"]), (60, 200, 200, 200))
        self.assertEqual((st["avg_c"], st["avg_hr"], st["max_hr"], st["balance_l"], st["kj"]),
                         (90, 140, 141, 40, 12.0))

    def test_old_files_without_heart_rate_column(self):
        old = ["time_iso", "elapsed_s", "power_w", "left_w", "right_w", "cadence_rpm"]
        st = summarize(self.write("ride-20261123-100000.csv", old,
                                  [["2026-11-23T10:00:00", 1, 150, "", "", 80]]))
        self.assertEqual((st["avg_p"], st["avg_hr"], st["balance_l"], st["np"]), (150, None, None, None))

    def test_empty_file(self):
        st = summarize(self.write("ride-20261123-100000.csv", ridelog.COLUMNS, []))
        self.assertEqual((st["moving_s"], st["avg_p"], st["max_p"]), (0, None, None))

    def test_list_newest_first_and_marks_current(self):
        a = self.write("ride-20261122-100000.csv", ridelog.COLUMNS, [])
        b = self.write("ride-20261123-100000.csv", ridelog.COLUMNS, [])
        self.write("other.csv", ridelog.COLUMNS, [])
        rides = list_rides(self.dir, current=b)
        self.assertEqual([r["name"] for r in rides], [os.path.basename(b), os.path.basename(a)])
        self.assertEqual([r["in_progress"] for r in rides], [True, False])

    def test_missing_folder(self):
        self.assertEqual(list_rides(os.path.join(self.dir, "nope")), [])


if __name__ == "__main__":
    unittest.main()


class TcxBase(unittest.TestCase):
    NS = {"t": "http://www.garmin.com/xmlschemas/TrainingCenterDatabase/v2",
          "x": "http://www.garmin.com/xmlschemas/ActivityExtension/v2"}

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        # CSV times are the Pi's local time; pin the zone so the UTC result is known.
        self.old_tz = os.environ.get("TZ")
        os.environ["TZ"] = "Europe/Stockholm"
        time.tzset()

    def tearDown(self):
        if self.old_tz is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = self.old_tz
        time.tzset()
        self.tmp.cleanup()

    def tcx(self, rows, header=ridelog.COLUMNS):
        path = os.path.join(self.tmp.name, "ride-20260701-100000.csv")
        with open(path, "w", newline="") as f:
            w = csv.writer(f)
            w.writerow(header)
            w.writerows(rows)
        return ET.fromstring(ridelog.to_tcx(path, "0.4.0"))


class TcxTest(TcxBase):

    def test_trackpoints_and_lap(self):
        doc = self.tcx([["2026-07-01T10:00:00", 1, 200, 100, 100, 90, 140],
                        ["2026-07-01T10:00:01", 2, 300, 150, 150, 92, 142]])
        tps = doc.findall(".//t:Trackpoint", self.NS)
        self.assertEqual(len(tps), 2)
        # 10:00 summer time in Stockholm is 08:00 UTC
        self.assertEqual(tps[0].find("t:Time", self.NS).text, "2026-07-01T08:00:00Z")
        self.assertEqual(tps[1].find("t:HeartRateBpm/t:Value", self.NS).text, "142")
        self.assertEqual(tps[1].find("t:Cadence", self.NS).text, "92")
        self.assertEqual(tps[1].find(".//x:Watts", self.NS).text, "300")
        act = doc.find(".//t:Activity", self.NS)
        self.assertEqual(act.get("Sport"), "Biking")
        self.assertEqual(act.find("t:Id", self.NS).text, "2026-07-01T08:00:00Z")
        lap = act.find("t:Lap", self.NS)
        self.assertEqual(lap.get("StartTime"), "2026-07-01T08:00:00Z")
        self.assertEqual(lap.find("t:TotalTimeSeconds", self.NS).text, "2")
        self.assertEqual(lap.find("t:MaximumHeartRateBpm/t:Value", self.NS).text, "142")
        self.assertEqual(lap.find(".//x:AvgWatts", self.NS).text, "250")
        self.assertEqual(lap.find(".//x:MaxWatts", self.NS).text, "300")
        self.assertEqual(act.find("t:Creator/t:Name", self.NS).text, "Bike computer")
        self.assertEqual(act.find("t:Creator/t:Version/t:VersionMinor", self.NS).text, "4")

    def test_winter_time(self):
        path = os.path.join(self.tmp.name, "ride-20261123-162208.csv")
        with open(path, "w", newline="") as f:
            f.write("time_iso,elapsed_s,power_w,left_w,right_w,cadence_rpm\n2026-11-23T16:22:08,1.0,36,,,21\n")
        doc = ET.fromstring(ridelog.to_tcx(path))
        self.assertEqual(doc.findtext(".//t:Trackpoint/t:Time", namespaces=self.NS), "2026-11-23T15:22:08Z")

    def test_missing_values_are_left_out(self):
        doc = self.tcx([["2026-07-01T10:00:00", 1, 200, "", "", "", ""]])
        tp = doc.find(".//t:Trackpoint", self.NS)
        self.assertIsNone(tp.find("t:HeartRateBpm", self.NS))
        self.assertIsNone(tp.find("t:Cadence", self.NS))
        lap = doc.find(".//t:Lap", self.NS)
        self.assertIsNone(lap.find("t:AverageHeartRateBpm", self.NS))
        self.assertIsNone(lap.find("t:Cadence", self.NS))

    def test_old_files_without_heart_rate_column(self):
        old = ["time_iso", "elapsed_s", "power_w", "left_w", "right_w", "cadence_rpm"]
        doc = self.tcx([["2026-07-01T10:00:00", 1, 150, 75, 75, 80]], header=old)
        self.assertEqual(len(doc.findall(".//t:Trackpoint", self.NS)), 1)

    def test_cadence_is_capped_at_tcx_limit(self):
        doc = self.tcx([["2026-07-01T10:00:00", 1, 200, "", "", 300, ""]])
        self.assertEqual(doc.find(".//t:Trackpoint/t:Cadence", self.NS).text, "254")

    def test_element_order_follows_the_schema(self):
        doc = self.tcx([["2026-07-01T10:00:00", 1, 200, 100, 100, 90, 140]])
        lap = doc.find(".//t:Lap", self.NS)
        names = [e.tag.split("}")[1] for e in lap]
        self.assertEqual(names, ["TotalTimeSeconds", "DistanceMeters", "Calories", "AverageHeartRateBpm",
                                 "MaximumHeartRateBpm", "Intensity", "Cadence", "TriggerMethod",
                                 "Track", "Extensions"])
        tp = doc.find(".//t:Trackpoint", self.NS)
        self.assertEqual([e.tag.split("}")[1] for e in tp], ["Time", "HeartRateBpm", "Cadence", "Extensions"])

    def test_empty_ride_cannot_be_exported(self):
        with self.assertRaises(ValueError):
            self.tcx([])


class LapsInFilesTest(TcxBase):
    def rows(self):
        # lap 1: 2 s at 100 W, lap 2: 3 s at 300 W
        return ([[f"2026-07-01T10:00:0{i}", i + 1, 100, 50, 50, 80, 120, 1] for i in range(2)] +
                [[f"2026-07-01T10:00:0{i}", i + 1, 300, 150, 150, 95, 150, 2] for i in range(2, 5)])

    def test_tcx_has_one_lap_per_lap(self):
        doc = self.tcx(self.rows())
        laps = doc.findall(".//t:Lap", self.NS)
        self.assertEqual(len(laps), 2)
        self.assertEqual(laps[0].get("StartTime"), "2026-07-01T08:00:00Z")
        self.assertEqual(laps[1].get("StartTime"), "2026-07-01T08:00:02Z")
        self.assertEqual([l.find("t:TotalTimeSeconds", self.NS).text for l in laps], ["2", "3"])
        self.assertEqual([l.find(".//x:AvgWatts", self.NS).text for l in laps], ["100", "300"])
        self.assertEqual([len(l.findall(".//t:Trackpoint", self.NS)) for l in laps], [2, 3])
        self.assertEqual(laps[1].find("t:AverageHeartRateBpm/t:Value", self.NS).text, "150")

    def test_summary_counts_laps(self):
        path = os.path.join(self.tmp.name, "ride-20260701-100000.csv")
        with open(path, "w", newline="") as f:
            w = csv.writer(f)
            w.writerow(ridelog.COLUMNS)
            w.writerows(self.rows())
        self.assertEqual(summarize(path)["laps"], 2)

    def test_ride_list_if_and_tss_use_the_given_ftp(self):
        path = os.path.join(self.tmp.name, "ride-20260701-100000.csv")
        with open(path, "w", newline="") as f:
            w = csv.writer(f)
            w.writerow(ridelog.COLUMNS)
            w.writerows([[f"2026-07-01T10:{i // 60:02d}:{i % 60:02d}", i + 1, 200, "", "", 90, "", 1]
                         for i in range(1800)])
        ride = list_rides(self.tmp.name, ftp=250)[0]
        self.assertEqual((ride["np"], ride["if"], ride["tss"]), (200, 0.8, 32))   # 30 min at IF 0.8
        ride = list_rides(self.tmp.name)[0]
        self.assertEqual((ride["if"], ride["tss"]), (None, None))


class FtpInFilesTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()

    def tearDown(self):
        self.tmp.cleanup()

    def write(self, rows, header=ridelog.COLUMNS):
        path = os.path.join(self.tmp.name, "ride-20260701-100000.csv")
        with open(path, "w", newline="") as f:
            w = csv.writer(f)
            w.writerow(header)
            w.writerows(rows)
        return path

    def test_ride_keeps_its_own_ftp(self):
        self.write([["2026-07-01T10:00:00", i + 1, 200, "", "", 90, "", 1, 250] for i in range(1800)])
        ride = list_rides(self.tmp.name, ftp=300)[0]        # the current FTP is ignored
        self.assertEqual((ride["ftp"], ride["ftp_saved"], ride["if"], ride["tss"]), (250, True, 0.8, 32))
        self.assertEqual(ride["zones"], [0, 0, 1800, 0, 0, 0, 0])    # 200 W = 80 % of 250: Z3

    def test_old_files_use_the_current_ftp(self):
        old = ridelog.COLUMNS[:-1]
        self.write([["2026-07-01T10:00:00", i + 1, 200, "", "", 90, "", 1] for i in range(60)], header=old)
        ride = list_rides(self.tmp.name, ftp=250)[0]
        self.assertEqual((ride["ftp"], ride["ftp_saved"]), (250, False))
        ride = list_rides(self.tmp.name)[0]
        self.assertEqual((ride["ftp"], ride["zones"], ride["if"]), (None, None, None))

    def test_average_cadence_leaves_out_coasting(self):
        path = self.write([["2026-07-01T10:00:00", 1, 200, "", "", 90, "", 1, ""],
                           ["2026-07-01T10:00:01", 2, 50, "", "", 0, "", 1, ""]])
        self.assertEqual(summarize(path)["avg_c"], 90)


class ShiftTimesTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()

    def tearDown(self):
        self.tmp.cleanup()

    def test_rows_and_name_are_corrected(self):
        lg = CsvLogger(self.tmp.name)
        wall = datetime(2026, 1, 1, 10, 0, 0).timestamp()     # the Pi thought it was 1 January
        with mock.patch("ridelog.clock.now", return_value=wall):
            lg.write(sample(1, wall=wall - 1))
            lg.write(sample(2, wall=wall - 1))
        self.assertTrue(os.path.basename(path_of(lg)).startswith("ride-20260101-10"))
        lg.shift_times(86400)                                  # it was really 2 January
        lg.write(sample(3, wall=wall - 1 + 86400))
        lg.new_file()
        names = os.listdir(self.tmp.name)
        self.assertEqual(names, [os.path.basename(path_of(lg))])
        self.assertTrue(names[0].startswith("ride-20260102-10"), names)
        with open(path_of(lg), newline="") as f:
            rows = list(csv.reader(f))
        self.assertEqual([r[0][:19] for r in rows[1:]],
                         ["2026-01-02T10:00:00", "2026-01-02T10:00:01", "2026-01-02T10:00:02"])

    def test_no_open_file_is_fine(self):
        CsvLogger(self.tmp.name).shift_times(3600)
        self.assertEqual(os.listdir(self.tmp.name), [])


class FixesTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old_tz = os.environ.get("TZ")
        os.environ["TZ"] = "Europe/Stockholm"
        time.tzset()

    def tearDown(self):
        if self.old_tz is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = self.old_tz
        time.tzset()
        self.tmp.cleanup()

    def test_prune_never_deletes_the_file_being_written(self):
        # 10 rides with names in the future (the clock was ahead when they were saved)
        for d in range(10, 20):
            open(os.path.join(self.tmp.name, f"ride-209901{d}-120000.csv"), "w").close()
        lg = CsvLogger(self.tmp.name, keep=10)
        lg.write(sample(1, wall=time.time()))
        self.assertTrue(os.path.exists(path_of(lg)))
        lg.write(sample(2, wall=time.time()))
        lg.new_file()
        with open(path_of(lg)) as f:
            self.assertEqual(len(f.readlines()), 3)

    def test_times_are_saved_with_their_utc_offset(self):
        lg = CsvLogger(self.tmp.name)
        wall = datetime(2026, 7, 1, 10, 0, 0).timestamp()
        with mock.patch("ridelog.clock.now", return_value=wall):
            lg.write(sample(0, wall=wall))
        lg.new_file()
        with open(path_of(lg), newline="") as f:
            self.assertEqual(list(csv.reader(f))[1][0], "2026-07-01T10:00:00+02:00")

    def test_tcx_times_stay_apart_when_the_clocks_go_back(self):
        # 25 October 2026: 02:30 happens twice in Stockholm, first in summer time.
        path = os.path.join(self.tmp.name, "ride-20261025-023000.csv")
        with open(path, "w", newline="") as f:
            w = csv.writer(f)
            w.writerow(ridelog.COLUMNS)
            w.writerow(["2026-10-25T02:30:00+02:00", 1, 200, "", "", 90, "", 1, ""])
            w.writerow(["2026-10-25T02:30:00+01:00", 2, 200, "", "", 90, "", 1, ""])
        doc = ET.fromstring(ridelog.to_tcx(path))
        times = [t.text for t in doc.iter("{http://www.garmin.com/xmlschemas/TrainingCenterDatabase/v2}Time")]
        self.assertEqual(times, ["2026-10-25T00:30:00Z", "2026-10-25T01:30:00Z"])
