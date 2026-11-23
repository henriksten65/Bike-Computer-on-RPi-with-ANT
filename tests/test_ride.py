"""Ride state: left/right split, moving time, stats, normalized power, heart rate, clock."""

import time
import unittest
from unittest import mock

import ride
from ride import Reading, Ride, WallClock, normalized_power


def ride_through(r, watts, **kw):
    """Feed one reading per second and return the last sample."""
    out = None
    for w in watts:
        r.add(Reading(power=w, cadence=90, **kw))
        out = r.tick(1.0)
    return out


class SplitTest(unittest.TestCase):
    def test_right_reference(self):
        s = ride_through(Ride(), [200], balance_pct=40, balance_side="right")
        self.assertEqual((s["l"], s["r"]), (120, 80))

    def test_left_reference(self):
        s = ride_through(Ride(), [200], balance_pct=40, balance_side="left")
        self.assertEqual((s["l"], s["r"]), (80, 120))

    def test_unknown_reference_uses_configured_side(self):
        s = ride_through(Ride(unknown_balance_side="left"), [200], balance_pct=40)
        self.assertEqual((s["l"], s["r"]), (80, 120))

    def test_no_balance(self):
        s = ride_through(Ride(), [200])
        self.assertEqual((s["l"], s["r"]), (None, None))

    def test_left_plus_right_is_total_when_some_readings_lack_balance(self):
        # Regression: a second with [72 W + 50/50 balance, 0 W without balance]
        # used to give total 36 W but left 36 W and right 36 W.
        r = Ride()
        r.add(Reading(power=72, cadence=40, balance_pct=50, balance_side="right"))
        r.add(Reading(power=0, cadence=0))
        s = r.tick(1.0)
        self.assertEqual(s["p"], 36)
        self.assertEqual(s["l"] + s["r"], 36)

    def test_split_is_power_weighted(self):
        r = Ride()
        r.add(Reading(power=300, balance_pct=40, balance_side="right"))   # L 180
        r.add(Reading(power=100, balance_pct=60, balance_side="right"))   # L 40
        s = r.tick(1.0)
        self.assertEqual(s["p"], 200)
        self.assertEqual(s["l"], 110)   # 200 * 220 / 400
        self.assertEqual(s["r"], 90)


class MovingTimeTest(unittest.TestCase):
    def test_counts_only_moving_seconds(self):
        r = Ride()
        ride_through(r, [150] * 5)
        for _ in range(3):                       # stopped pedalling: 0 W, 0 rpm
            r.add(Reading(power=0, cadence=0))
            s = r.tick(1.0)
        self.assertEqual(s["t"], 5.0)
        self.assertFalse(s["moving"])
        self.assertEqual(len(r.history), 5)

    def test_holds_last_values_when_meter_skips_a_second(self):
        r = Ride()
        ride_through(r, [150])
        s = r.tick(1.0)               # nothing arrived, but the meter is still "connected"
        self.assertTrue(s["connected"])
        self.assertEqual(s["p"], 150)
        self.assertEqual(s["t"], 2.0)

    def test_stops_when_meter_goes_silent(self):
        r = Ride()
        ride_through(r, [150])
        r._last_reading.ts -= ride.STALE_AFTER_S + 1
        s = r.tick(1.0)
        self.assertFalse(s["connected"])
        self.assertFalse(s["moving"])
        self.assertEqual(s["p"], 0)
        self.assertEqual(s["t"], 1.0)

    def test_nothing_received_yet(self):
        s = Ride().tick(1.0)
        self.assertFalse(s["connected"])
        self.assertIsNone(s["c"])


class StatsTest(unittest.TestCase):
    def test_averages_max_work(self):
        r = Ride()
        s = ride_through(r, [100, 200, 300])
        st = s["stats"]
        self.assertEqual((st["avg_p"], st["max_p"], st["avg_c"], st["kj"]), (200, 300, 90, 0.6))

    def test_reset_clears_everything(self):
        r = Ride()
        ride_through(r, [200] * 40)
        r.add_heart_rate(150)
        r.tick(1.0)
        r.reset()
        st = r.snapshot()["stats"]
        self.assertEqual(r.history, [])
        self.assertEqual((st["avg_p"], st["np"], st["elapsed"], st["avg_hr"]), (0, None, 0, None))


class NormalizedPowerTest(unittest.TestCase):
    def test_none_before_30_seconds(self):
        self.assertIsNone(ride_through(Ride(), [200] * 29)["stats"]["np"])
        self.assertEqual(ride_through(Ride(), [200] * 30)["stats"]["np"], 200)

    def test_steady_ride_equals_average(self):
        self.assertEqual(ride_through(Ride(), [200] * 600)["stats"]["np"], 200)

    def test_intervals_are_weighted_up(self):
        st = ride_through(Ride(), ([350] * 60 + [50] * 60) * 30)["stats"]
        self.assertEqual(st["avg_p"], 200)
        self.assertEqual(st["np"], 273)

    def test_function_matches_live_value(self):
        watts = [100 + (i * 37) % 250 for i in range(500)]
        live = ride_through(Ride(), watts)["stats"]["np"]
        self.assertEqual(normalized_power(watts), live)
        self.assertIsNone(normalized_power([200] * 29))


class HeartRateTest(unittest.TestCase):
    def test_heart_rate_in_sample_and_stats(self):
        r = Ride()
        for bpm in (120, 130, 140):
            r.add_heart_rate(bpm)
            s = ride_through(r, [200])
        self.assertEqual(s["h"], 140)
        self.assertEqual((s["stats"]["avg_hr"], s["stats"]["max_hr"]), (130, 140))

    def test_old_heart_rate_is_dropped(self):
        r = Ride()
        r.add_heart_rate(120, ts=time.monotonic() - ride.HR_STALE_AFTER_S - 1)
        self.assertIsNone(ride_through(r, [200])["h"])

    def test_no_strap(self):
        st = ride_through(Ride(), [200])["stats"]
        self.assertEqual((st["avg_hr"], st["max_hr"]), (None, None))


class WallClockTest(unittest.TestCase):
    def test_small_difference_is_ignored(self):
        c = WallClock()
        self.assertFalse(c.correct(time.time() + 10))
        self.assertEqual(c.offset, 0)

    def test_large_difference_is_applied(self):
        c = WallClock()
        self.assertTrue(c.correct(time.time() + 3600))
        self.assertAlmostEqual(c.offset, 3600, delta=1)
        self.assertAlmostEqual(c.now(), time.time() + 3600, delta=1)
        self.assertFalse(c.correct(time.time() + 3610))   # already about right

    def test_dropped_when_system_clock_is_set(self):
        c = WallClock()
        c.correct(time.time() + 3600)
        real = time.time()
        with mock.patch("ride.time.time", return_value=real + 3600):   # NTP set the clock
            self.assertAlmostEqual(c.now(), real + 3600, delta=1)
            self.assertEqual(c.offset, 0)


if __name__ == "__main__":
    unittest.main()


class TrainingNumbersTest(unittest.TestCase):
    def test_zones(self):
        ftp = 200
        cases = {0: 1, 110: 1, 111: 2, 150: 2, 151: 3, 180: 3, 181: 4, 210: 4,
                 211: 5, 240: 5, 241: 6, 300: 6, 301: 7, 1500: 7}
        for power, z in cases.items():
            self.assertEqual(ride.zone(power, ftp), z, power)
        self.assertIsNone(ride.zone(200, None))

    def test_if_and_tss(self):
        self.assertEqual(ride.intensity_factor(200, 250), 0.8)
        self.assertEqual(ride.training_stress(3600, 250, 250), 100)     # 1 h at FTP
        self.assertEqual(ride.training_stress(3600, 200, 250), 64)      # 1 h at IF 0.8
        self.assertEqual(ride.training_stress(1800, 250, 250), 50)
        self.assertIsNone(ride.intensity_factor(200, None))
        self.assertIsNone(ride.training_stress(3600, None, 250))

    def test_live_if_and_tss(self):
        r = Ride(ftp=250)
        st = ride_through(r, [250] * 600)["stats"]
        self.assertEqual((st["np"], st["if"], st["tss"], st["ftp"]), (250, 1.0, 17, 250))   # 10 min at FTP

    def test_no_ftp_no_if(self):
        st = ride_through(Ride(), [250] * 60)["stats"]
        self.assertEqual((st["if"], st["tss"], st["ftp"]), (None, None, None))

    def test_ftp_change_applies_to_finished_laps(self):
        r = Ride(ftp=200)
        ride_through(r, [200] * 60)
        r.new_lap()
        r.ftp = 100
        st = r.tick(1.0)["stats"]
        self.assertEqual(st["laps"][0]["if"], 2.0)


class LapTest(unittest.TestCase):
    def test_laps_split_the_ride(self):
        r = Ride()
        ride_through(r, [100] * 40)
        self.assertTrue(r.new_lap())
        s = ride_through(r, [300] * 50)
        st = s["stats"]
        self.assertEqual(s["lap"], 2)
        self.assertEqual(len(st["laps"]), 1)
        first, now = st["laps"][0], st["lap"]
        self.assertEqual((first["n"], first["elapsed"], first["avg_p"], first["np"], first["start_t"]),
                         (1, 40.0, 100, 100, 0.0))
        self.assertEqual((now["n"], now["elapsed"], now["avg_p"], now["np"], now["start_t"]),
                         (2, 50.0, 300, 300, 40.0))
        self.assertEqual((st["avg_p"], st["elapsed"]), (211, 90.0))          # whole ride: 19000 W·s / 90 s

    def test_lap_number_in_history(self):
        r = Ride()
        ride_through(r, [100] * 3)
        r.new_lap()
        ride_through(r, [100] * 2)
        self.assertEqual([h["lap"] for h in r.history], [1, 1, 1, 2, 2])

    def test_empty_lap_is_ignored(self):
        r = Ride()
        self.assertFalse(r.new_lap())          # nothing ridden yet
        ride_through(r, [100] * 5)
        self.assertTrue(r.new_lap())
        self.assertFalse(r.new_lap())          # pressed twice
        self.assertEqual(len(r.laps), 1)

    def test_paused_seconds_dont_count_in_the_lap(self):
        r = Ride()
        ride_through(r, [100] * 5)
        for _ in range(3):
            r.add(Reading(power=0, cadence=0))
            r.tick(1.0)
        self.assertEqual(r.lap.seconds, 5)

    def test_reset_clears_laps(self):
        r = Ride()
        ride_through(r, [100] * 5)
        r.new_lap()
        r.reset()
        st = r.snapshot()["stats"]
        self.assertEqual((st["laps"], st["lap"]["n"]), ([], 1))


class CadenceAndZonesTest(unittest.TestCase):
    def test_average_cadence_leaves_out_coasting(self):
        r = Ride()
        for cad in (90, 90, 0, 0):                     # 2 s pedalling, 2 s coasting at 100 W
            r.add(Reading(power=100, cadence=cad))
            st = r.tick(1.0)["stats"]
        self.assertEqual(st["avg_c"], 90)

    def test_no_cadence_at_all(self):
        r = Ride()
        r.add(Reading(power=100))
        self.assertIsNone(r.tick(1.0)["stats"]["avg_c"])

    def test_time_in_zones(self):
        r = Ride(ftp=200)
        st = ride_through(r, [100] * 3 + [200] * 2 + [400])["stats"]
        self.assertEqual(st["zones"], [3, 0, 0, 2, 0, 0, 1])
        self.assertEqual(st["lap"]["zones"], [3, 0, 0, 2, 0, 0, 1])

    def test_no_zones_without_ftp(self):
        self.assertIsNone(ride_through(Ride(), [200] * 5)["stats"]["zones"])


class ClockShiftTest(unittest.TestCase):
    def test_browser_correction_is_reported_once(self):
        c = WallClock()
        c.correct(time.time() + 3600)
        self.assertAlmostEqual(c.pop_shift(), 3600, delta=1)
        self.assertEqual(c.pop_shift(), 0)
        c.correct(time.time())                         # back to the system clock
        self.assertAlmostEqual(c.pop_shift(), -3600, delta=1)

    def test_system_clock_set_by_ntp(self):
        c = WallClock()
        real = time.time()
        with mock.patch("ride.time.time", return_value=real + 7200):   # clock was 2 h behind
            self.assertAlmostEqual(c.pop_shift(), 7200, delta=1)

    def test_ntp_after_browser_correction(self):
        c = WallClock()
        c.correct(time.time() + 7200)                  # the phone already fixed it
        c.pop_shift()
        real = time.time()
        with mock.patch("ride.time.time", return_value=real + 7200):
            self.assertAlmostEqual(c.pop_shift(), 0, delta=1)

    def test_tick_passes_the_shift_to_the_ride_file(self):
        logger = mock.Mock(error=None)
        r = Ride(logger=logger)
        with mock.patch("ride.clock.pop_shift", return_value=120.0):
            r.tick(1.0)
        logger.shift_times.assert_called_once_with(120.0)
