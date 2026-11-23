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
