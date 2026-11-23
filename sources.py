"""Data sources: ANT+ (USB stick), Bluetooth LE, and a simulator for testing.

Each source runs in its own thread and calls `ride.add(Reading(...))`.
"""

import asyncio
import logging
import math
import random
import struct
import threading
import time

from ride import Reading

log = logging.getLogger("sources")


# --------------------------------------------------------------------------- ANT+

ANTPLUS_NETWORK_KEY = [0xB9, 0xA5, 0x21, 0xFB, 0xBD, 0x72, 0xC3, 0x45]
ANT_DEVICE_TYPE_POWER = 11
ANT_POWER_PERIOD = 8182      # 4.00 Hz
ANT_RF_FREQ = 57             # 2457 MHz
ANT_DEVICE_TYPE_HR = 120
ANT_HR_PERIOD = 8070         # 4.06 Hz
ANT_USB_VENDOR = 0x0FCF      # Dynastream; used by nearly all ANT+ USB sticks


def _ant_error_text(e):
    """A short, actionable message for the page about an ANT+ failure."""
    name = type(e).__name__
    msg = str(e)
    if name == "DriverNotFound" or "Device not found" in msg:
        return "ANT+ stick not found. Is it plugged in?"
    if getattr(e, "errno", None) == 13 or "Access denied" in msg or "Permission" in msg:
        return "No permission to open the ANT+ stick. Install the udev rule (see README)."
    if getattr(e, "errno", None) == 16 or "Resource busy" in msg:
        return "The ANT+ stick is in use by another program."
    return f"ANT+ error: {name}: {msg}"


def _ant_stick_present():
    """True if an ANT+ USB stick is plugged in (None if we can't tell)."""
    try:
        import usb.core
        return usb.core.find(idVendor=ANT_USB_VENDOR) is not None
    except Exception:
        return None


class AntSource:
    """Receives the ANT+ Bicycle Power profile (device type 11).

    Uses data page 0x10 "Standard Power-Only", which carries total instantaneous
    power, cadence and the pedal power balance:
        byte 0  0x10
        byte 1  update event count
        byte 2  pedal power: bit7 = 1 -> value is RIGHT pedal %, 0 -> unknown pedal;
                bits 0-6 = percent; 0xFF = balance not used
        byte 3  instantaneous cadence, rpm (0xFF = invalid)
        byte 4-5 accumulated power (uint16 LE)
        byte 6-7 instantaneous power (uint16 LE), W
    """

    def __init__(self, ride, device_id=0, heart_rate=True, hr_device_id=0):
        self.ride = ride
        self.device_id = device_id   # 0 = pair with the first power meter found
        self.heart_rate = heart_rate        # also listen for an ANT+ heart rate strap
        self.hr_device_id = hr_device_id    # 0 = first strap found
        self._hr_found = False
        self._stopping = False
        self._last_event = None
        self._last_event_change = 0.0
        self.node = None

    def start(self):
        threading.Thread(target=self._run, name="ant", daemon=True).start()
        threading.Thread(target=self._watch_stick, name="ant-watch", daemon=True).start()

    def stop(self):
        """Close the ANT+ stick before the program exits.

        Exiting with the openant threads still using the stick crashes Python
        (segmentation fault in libusb).
        """
        self._stopping = True
        node, self.node = self.node, None
        if node is not None:
            try:
                node.stop()
            except Exception as e:
                log.warning("Error closing the ANT+ stick: %s", e)

    def _watch_stick(self):
        """Restart ANT+ when the stick is unplugged.

        openant only logs USB errors and keeps waiting, so on its own it never
        notices the stick is gone and never reconnects when it's plugged back in.
        """
        while not self._stopping:
            time.sleep(2)
            node = self.node
            if node is not None and _ant_stick_present() is False:
                log.error("ANT+ stick was unplugged")
                self.ride.set_source_status("error", "ANT+ stick not found. Is it plugged in?")
                self.node = None
                try:
                    node.stop()       # makes node.start() in _run return
                except Exception:
                    pass

    def _run(self):
        from openant.easy.node import Node
        from openant.easy.channel import Channel

        while not self._stopping:
            try:
                self.node = None
                self.node = Node()
                self.node.set_network_key(0x00, ANTPLUS_NETWORK_KEY)
                ch = self.node.new_channel(Channel.Type.BIDIRECTIONAL_RECEIVE)
                ch.on_broadcast_data = self._on_data
                ch.on_burst_data = self._on_data
                ch.on_acknowledge_data = self._on_data
                ch.set_period(ANT_POWER_PERIOD)
                ch.set_search_timeout(255)        # search forever
                ch.set_rf_freq(ANT_RF_FREQ)
                ch.set_id(self.device_id, ANT_DEVICE_TYPE_POWER, 0)
                ch.open()
                log.info("ANT+ channel open, searching for power meter (device id %s)",
                         self.device_id or "any")
                if self.heart_rate:
                    hr = self.node.new_channel(Channel.Type.BIDIRECTIONAL_RECEIVE)
                    hr.on_broadcast_data = self._on_hr_data
                    hr.set_period(ANT_HR_PERIOD)
                    hr.set_search_timeout(255)
                    hr.set_rf_freq(ANT_RF_FREQ)
                    hr.set_id(self.hr_device_id, ANT_DEVICE_TYPE_HR, 0)
                    hr.open()
                    self._hr_found = False
                    log.info("ANT+ heart rate channel open (device id %s)", self.hr_device_id or "any")
                self._last_event = None
                self.ride.set_source_status("searching", "Searching for pedals (ANT+)… Start pedalling to wake them.")
                self.node.start()              # blocks until stopped / error
                time.sleep(1)                  # stopped by _watch_stick: retry soon
            except Exception as e:  # stick missing, permissions, ...
                if self._stopping:
                    break
                log.error("ANT+ error: %s: %s; retrying in 5 s", type(e).__name__, e)
                self.ride.set_source_status("error", _ant_error_text(e))
                try:
                    self.node.stop()
                except Exception:
                    pass
                time.sleep(5)

    def _on_hr_data(self, data):
        """ANT+ heart rate: every data page carries the computed heart rate in byte 7."""
        data = bytes(data)
        if len(data) < 8 or data[7] == 0:
            return
        if not self._hr_found:
            log.info("ANT+ heart rate strap found")
            self._hr_found = True
        self.ride.add_heart_rate(data[7])

    def _on_data(self, data):
        data = bytes(data)
        if len(data) < 8 or data[0] != 0x10:
            return
        event = data[1]
        now = time.monotonic()
        if event == self._last_event:
            # Meter repeats the page when nothing changed (e.g. you stopped pedalling).
            # After a few seconds without a new event, report zero.
            if now - self._last_event_change > 3.0:
                self.ride.add(Reading(power=0, cadence=0, ts=now))
            return
        if self._last_event is None:
            log.info("ANT+ power meter found")
            self.ride.set_source_status("searching", "Pedals found (ANT+) but they stopped sending. Start pedalling to wake them.")
        self._last_event = event
        self._last_event_change = now

        bal_raw = data[2]
        cadence = data[3] if data[3] != 0xFF else None
        power = data[6] | (data[7] << 8)
        if bal_raw == 0xFF:
            bal_pct, bal_side = None, None
        else:
            bal_pct = bal_raw & 0x7F
            bal_side = "right" if bal_raw & 0x80 else None
            if bal_pct > 100:
                bal_pct, bal_side = None, None
        self.ride.add(Reading(power=power, cadence=cadence,
                              balance_pct=bal_pct, balance_side=bal_side, ts=now))


# --------------------------------------------------------------------------- BLE

CPS_SERVICE = "00001818-0000-1000-8000-00805f9b34fb"
CPS_MEASUREMENT = "00002a63-0000-1000-8000-00805f9b34fb"


class BleSource:
    """Receives the Bluetooth Cycling Power Service (0x1818) measurement (0x2A63).

    Flags (uint16):
        bit0 pedal power balance present (uint8, unit 1/2 %)
        bit1 balance reference: 1 = left, 0 = unknown
        bit2 accumulated torque present (uint16)
        bit4 wheel revolution data present (uint32 + uint16)
        bit5 crank revolution data present (uint16 revs + uint16 time, 1/1024 s)
    """

    def __init__(self, ride, address=None, name_filter=None):
        self.ride = ride
        self.address = address
        self.name_filter = name_filter
        self._last_crank = None   # (revs, time_1024)
        self._last_crank_change = 0.0
        self._cadence = 0

    def start(self):
        threading.Thread(target=lambda: asyncio.run(self._run()), name="ble",
                         daemon=True).start()

    async def _find(self):
        from bleak import BleakScanner
        if self.address:
            return await BleakScanner.find_device_by_address(self.address, timeout=20)

        def match(dev, adv):
            if CPS_SERVICE not in [u.lower() for u in adv.service_uuids]:
                return False
            if self.name_filter:
                return self.name_filter.lower() in (dev.name or "").lower()
            return True
        return await BleakScanner.find_device_by_filter(match, timeout=20)

    async def _run(self):
        from bleak import BleakClient
        while True:
            try:
                log.info("BLE: scanning for a cycling power meter...")
                self.ride.set_source_status("searching", "Searching for pedals (Bluetooth)… Start pedalling to wake them.")
                dev = await self._find()
                if dev is None:
                    continue
                log.info("BLE: connecting to %s (%s)", dev.name, dev.address)
                self.ride.set_source_status("searching", f"Connecting to {dev.name or dev.address} (Bluetooth)…")
                disconnected = asyncio.Event()
                async with BleakClient(dev, disconnected_callback=lambda c: disconnected.set()) as client:
                    await client.start_notify(CPS_MEASUREMENT, self._on_notify)
                    log.info("BLE: receiving data")
                    self.ride.set_source_status("searching", f"Connected to {dev.name or dev.address} (Bluetooth), waiting for data. Start pedalling.")
                    await disconnected.wait()
                log.warning("BLE: disconnected")
            except Exception as e:
                log.error("BLE error: %s: %s; retrying in 5 s", type(e).__name__, e)
                msg = str(e)
                if "adapter" in msg.lower() or "NotReady" in msg or "PowerOff" in msg:
                    text = "Bluetooth is off or not available on the Pi."
                else:
                    text = f"Bluetooth error: {msg or type(e).__name__}"
                self.ride.set_source_status("error", text)
                await asyncio.sleep(5)

    def _on_notify(self, _char, data: bytearray):
        now = time.monotonic()
        flags, power = struct.unpack_from("<Hh", data, 0)
        off = 4
        bal_pct = bal_side = None
        if flags & 0x01:
            bal_pct = data[off] / 2.0
            bal_side = "left" if flags & 0x02 else None
            off += 1
        if flags & 0x04:
            off += 2
        if flags & 0x10:
            off += 6
        cadence = None
        if flags & 0x20:
            revs, t = struct.unpack_from("<HH", data, off)
            if self._last_crank is not None:
                d_revs = (revs - self._last_crank[0]) & 0xFFFF
                d_t = ((t - self._last_crank[1]) & 0xFFFF) / 1024.0
                if d_revs and d_t > 0:
                    self._cadence = round(60.0 * d_revs / d_t)
                    self._last_crank_change = now
                elif now - self._last_crank_change > 3.0:
                    self._cadence = 0
                cadence = self._cadence
            self._last_crank = (revs, t)
        self.ride.add(Reading(power=max(power, 0), cadence=cadence,
                              balance_pct=bal_pct, balance_side=bal_side, ts=now))


# --------------------------------------------------------------------------- simulator

class SimSource:
    """Fake rider, ~4 updates/s, for testing the display without the bike."""

    def __init__(self, ride):
        self.ride = ride
        self._hr = 100.0

    def start(self):
        self.ride.set_source_status("searching", "Simulator")
        threading.Thread(target=self._run, name="sim", daemon=True).start()

    def _run(self):
        t0 = time.monotonic()
        while True:
            t = time.monotonic() - t0
            # Intervals: 60 s hard / 60 s easy, plus noise; a 10 s stop every 5 min.
            if 290 < t % 300:
                self.ride.add(Reading(power=0, cadence=0, balance_pct=50, balance_side="right"))
            else:
                base = 260 if int(t // 60) % 2 else 160
                power = max(0, int(base + 25 * math.sin(t / 7) + random.gauss(0, 12)))
                cadence = int(88 + 6 * math.sin(t / 11) + random.gauss(0, 1.5))
                right = 50 + 3 * math.sin(t / 23) + random.gauss(0, 1)
                self.ride.add(Reading(power=power, cadence=cadence,
                                      balance_pct=round(right), balance_side="right"))
            # Heart rate follows the effort slowly.
            target = 95 + 0.25 * (260 if int(t // 60) % 2 and t % 300 <= 290 else 160)
            self._hr += (target - self._hr) * 0.01
            self.ride.add_heart_rate(round(self._hr + random.gauss(0, 0.5)))
            time.sleep(0.25)
