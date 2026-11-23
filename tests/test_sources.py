"""Decoding of ANT+ and Bluetooth power meter data and ANT+ heart rate."""

import struct
import unittest

import sources


class FakeRide:
    def __init__(self):
        self.readings, self.hr = [], []

    def add(self, r):
        self.readings.append(r)

    def add_heart_rate(self, bpm):
        self.hr.append(bpm)

    def set_source_status(self, level, text):
        self.status = (level, text)


def ant_page(event, balance=0xFF, cadence=90, power=250):
    return bytes([0x10, event, balance, cadence, 0, 0, power & 0xFF, power >> 8])


class AntTest(unittest.TestCase):
    def setUp(self):
        self.ride = FakeRide()
        self.src = sources.AntSource(self.ride)

    def test_power_cadence_and_right_balance(self):
        self.src._on_data(ant_page(1, balance=0x80 | 48, cadence=92, power=300))
        r = self.ride.readings[-1]
        self.assertEqual((r.power, r.cadence, r.balance_pct, r.balance_side), (300, 92, 48, "right"))

    def test_unknown_balance_reference(self):
        self.src._on_data(ant_page(1, balance=52))
        r = self.ride.readings[-1]
        self.assertEqual((r.balance_pct, r.balance_side), (52, None))

    def test_balance_and_cadence_not_available(self):
        self.src._on_data(ant_page(1, balance=0xFF, cadence=0xFF))
        r = self.ride.readings[-1]
        self.assertEqual((r.balance_pct, r.cadence), (None, None))

    def test_power_above_255_w(self):
        self.src._on_data(ant_page(1, power=1234))
        self.assertEqual(self.ride.readings[-1].power, 1234)

    def test_other_pages_ignored(self):
        self.src._on_data(bytes([0x12, 1, 2, 3, 4, 5, 6, 7]))
        self.src._on_data(bytes([0x10, 1, 2]))
        self.assertEqual(self.ride.readings, [])

    def test_repeated_page_means_stopped_after_a_few_seconds(self):
        self.src._on_data(ant_page(5))
        self.src._on_data(ant_page(5))           # same event count: nothing new
        self.assertEqual(len(self.ride.readings), 1)
        self.src._last_event_change -= 4          # pretend 4 s passed
        self.src._on_data(ant_page(5))
        r = self.ride.readings[-1]
        self.assertEqual((r.power, r.cadence), (0, 0))

    def test_heart_rate(self):
        self.src._on_hr_data(bytes([0x04, 0, 0, 0, 0, 0, 12, 148]))
        self.src._on_hr_data(bytes([0x84, 0, 0, 0, 0, 0, 13, 0]))    # 0 = invalid
        self.assertEqual(self.ride.hr, [148])

    def test_error_messages(self):
        class DriverNotFound(Exception):
            pass
        self.assertIn("not found", sources._ant_error_text(DriverNotFound()))
        e = OSError(13, "Access denied (insufficient permissions)")
        self.assertIn("permission", sources._ant_error_text(e))
        self.assertIn("in use", sources._ant_error_text(OSError(16, "Resource busy")))


def ble_packet(power, balance=None, left_ref=False, crank=None):
    flags = 0
    body = b""
    if balance is not None:
        flags |= 0x01 | (0x02 if left_ref else 0)
        body += bytes([int(balance * 2)])
    if crank is not None:
        flags |= 0x20
        body += struct.pack("<HH", *crank)
    return bytearray(struct.pack("<Hh", flags, power) + body)


class BleTest(unittest.TestCase):
    def setUp(self):
        self.ride = FakeRide()
        self.src = sources.BleSource(self.ride)

    def test_power_and_left_balance(self):
        self.src._on_notify(None, ble_packet(240, balance=47.5, left_ref=True))
        r = self.ride.readings[-1]
        self.assertEqual((r.power, r.balance_pct, r.balance_side), (240, 47.5, "left"))

    def test_negative_power_is_zero(self):
        self.src._on_notify(None, ble_packet(-5))
        self.assertEqual(self.ride.readings[-1].power, 0)

    def test_cadence_from_crank_revolutions(self):
        self.src._on_notify(None, ble_packet(200, crank=(10, 1000)))
        self.assertIsNone(self.ride.readings[-1].cadence)      # need two packets
        self.src._on_notify(None, ble_packet(200, crank=(11, 1000 + 683)))   # 1 rev in 0.667 s
        self.assertEqual(self.ride.readings[-1].cadence, 90)

    def test_cadence_counter_wraps_around(self):
        self.src._on_notify(None, ble_packet(200, crank=(0xFFFF, 0xFF00)))
        self.src._on_notify(None, ble_packet(200, crank=(0, (0xFF00 + 683) & 0xFFFF)))
        self.assertEqual(self.ride.readings[-1].cadence, 90)


if __name__ == "__main__":
    unittest.main()
