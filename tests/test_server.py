"""The web server: pages, ride list and downloads, POST protection, power off."""

import http.client
import json
import os
import socket
import subprocess
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from typing import Any
from unittest import mock

import bikecomputer
from ride import Ride
from ridelog import CsvLogger


class ServerBase(unittest.TestCase):
    """Starts a server on a free port with a temporary rides folder."""

    # Set in setUpClass.
    tmp: tempfile.TemporaryDirectory
    dir: str
    ride: Ride
    settings: str
    server: ThreadingHTTPServer
    handler: Any                   # the Handler class from make_handler
    port: int

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.dir = cls.tmp.name
        with open(os.path.join(cls.dir, "ride-20261123-100000.csv"), "w") as f:
            f.write("time_iso,elapsed_s,power_w,left_w,right_w,cadence_rpm,heart_rate_bpm\n"
                    "2026-11-23T10:00:00,1.0,210,100,110,88,130\n")
        with open(os.path.join(cls.dir, "secret.txt"), "w") as f:
            f.write("not for download")
        cls.ride = Ride(logger=CsvLogger(cls.dir))
        cls.settings = os.path.join(cls.dir, "settings.json")
        cls.handler = bikecomputer.make_handler(cls.ride, bikecomputer.Hub(), cls.dir, cls.settings)
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), cls.handler)
        cls.server.daemon_threads = True
        cls.port = cls.server.server_address[1]
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.ride.stop_recording()       # close the ride file the tests wrote to
        cls.tmp.cleanup()

    def request(self, method, path, body=None, headers=None):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        c.request(method, path, body=None if body is None else json.dumps(body), headers=headers or {})
        r = c.getresponse()
        data = r.read()
        c.close()
        return r, data

    def post(self, path, body=None, origin=None, header=True):
        h = {"Content-Type": "application/json"}
        if header:
            h["X-Bike-Computer"] = "1"
        if origin:
            h["Origin"] = origin
        return self.request("POST", path, body or {}, h)


class ServerTest(ServerBase):

    def test_pages(self):
        for path in ("/", "/index.html", "/?x=1", "/rides"):
            r, body = self.request("GET", path)
            self.assertEqual(r.status, 200, path)
            self.assertIn(b"<html", body)
        self.assertEqual(self.request("GET", "/nope")[0].status, 404)

    def test_ride_list(self):
        r, body = self.request("GET", "/api/rides")
        data = json.loads(body)
        self.assertEqual(data["version"], bikecomputer.__version__)
        rides = data["rides"]
        self.assertEqual([x["name"] for x in rides], ["ride-20261123-100000.csv"])
        self.assertEqual((rides[0]["avg_p"], rides[0]["avg_hr"]), (210, 130))

    def test_download(self):
        r, body = self.request("GET", "/rides/ride-20261123-100000.csv")
        self.assertEqual(r.status, 200)
        self.assertIn("attachment", r.getheader("Content-Disposition"))
        self.assertTrue(body.startswith(b"time_iso,"))

    def test_tcx_download(self):
        r, body = self.request("GET", "/rides/ride-20261123-100000.tcx")
        self.assertEqual(r.status, 200)
        self.assertEqual(r.getheader("Content-Type"), "application/vnd.garmin.tcx+xml")
        self.assertIn('filename="ride-20261123-100000.tcx"', r.getheader("Content-Disposition"))
        self.assertIn(b"<ns3:Watts>210</ns3:Watts>", body)
        minor = bikecomputer.__version__.split(".")[1]
        self.assertIn(f"<VersionMinor>{minor}</VersionMinor>".encode(), body)

    def test_tcx_only_for_existing_rides(self):
        for path in ("/rides/ride-20991231-235959.tcx", "/rides/secret.tcx", "/rides/../ride.tcx"):
            self.assertEqual(self.request("GET", path)[0].status, 404, path)

    def test_download_only_ride_files(self):
        for path in ("/rides/secret.txt", "/rides/../bikecomputer.py", "/rides/%2e%2e/ride.py",
                     "/rides/ride-20991231-235959.csv"):
            self.assertEqual(self.request("GET", path)[0].status, 404, path)

    def test_post_needs_dashboard_header_and_same_origin(self):
        self.assertEqual(self.post("/api/reset", header=False)[0].status, 403)
        self.assertEqual(self.post("/api/reset", origin="http://evil.example")[0].status, 403)
        self.assertEqual(self.post("/api/reset", origin=f"http://127.0.0.1:{self.port}")[0].status, 200)
        self.assertEqual(self.post("/api/reset")[0].status, 200)

    def test_clock_rejects_bad_input(self):
        self.assertEqual(self.post("/api/clock", {"now": "soon"})[0].status, 400)
        self.assertEqual(self.post("/api/clock", {"now": True})[0].status, 400)

    def test_power_off(self):
        ok = subprocess.CompletedProcess([], 0, "", "")
        with mock.patch("bikecomputer.subprocess.run", return_value=ok) as run:
            r, _ = self.post("/api/power", {"action": "poweroff"})
        self.assertEqual(r.status, 200)
        self.assertEqual(run.call_args[0][0], ["sudo", "-n", "/usr/bin/systemctl", "poweroff"])

    def test_power_off_not_allowed(self):
        denied = subprocess.CompletedProcess([], 1, "", "sudo: a password is required")
        with mock.patch("bikecomputer.subprocess.run", return_value=denied):
            r, body = self.post("/api/power", {"action": "reboot"})
        self.assertEqual(r.status, 500)
        self.assertIn("install.sh", json.loads(body)["error"])

    def test_power_needs_known_action_and_header(self):
        with mock.patch("bikecomputer.subprocess.run") as run:
            self.assertEqual(self.post("/api/power", {"action": "rm -rf /"})[0].status, 400)
            self.assertEqual(self.post("/api/power", {"action": "poweroff"}, header=False)[0].status, 403)
        run.assert_not_called()


if __name__ == "__main__":
    unittest.main()


class TrainingEndpointsTest(ServerBase):
    """Lap button and FTP setting."""

    def setUp(self):
        self.ride.reset()
        self.ride.ftp = None

    def test_lap(self):
        from ride import Reading
        self.assertEqual(self.post("/api/lap")[0].status, 200)      # empty lap: ignored
        self.assertEqual(self.ride.laps, [])
        self.ride.add(Reading(power=200, cadence=90))
        self.ride.tick(1.0)
        self.assertEqual(self.post("/api/lap")[0].status, 200)
        self.assertEqual(len(self.ride.laps), 1)
        self.assertEqual(self.post("/api/lap", header=False)[0].status, 403)

    def test_set_and_clear_ftp(self):
        self.assertEqual(self.post("/api/settings", {"ftp": 250})[0].status, 200)
        self.assertEqual(self.ride.ftp, 250)
        with open(self.settings) as f:
            self.assertEqual(json.load(f)["ftp"], 250)
        data = json.loads(self.request("GET", "/api/rides")[1])
        self.assertEqual(data["ftp"], 250)
        self.assertEqual(self.post("/api/settings", {"ftp": None})[0].status, 200)
        self.assertIsNone(self.ride.ftp)
        with open(self.settings) as f:
            self.assertIsNone(json.load(f)["ftp"])

    def test_bad_ftp(self):
        for bad in (10, 5000, "250", 250.5, True):
            self.assertEqual(self.post("/api/settings", {"ftp": bad})[0].status, 400, bad)
        self.assertEqual(self.post("/api/settings", {})[0].status, 400)
        self.assertIsNone(self.ride.ftp)

    def test_set_and_clear_efficiency(self):
        try:
            self.assertEqual(self.post("/api/settings", {"efficiency": 20})[0].status, 200)
            self.assertEqual(self.ride.efficiency, 20)
            with open(self.settings) as f:
                self.assertEqual(json.load(f)["efficiency"], 20)
            self.assertEqual(json.loads(self.request("GET", "/api/rides")[1])["efficiency"], 20)
            self.assertEqual(self.ride.snapshot()["stats"]["efficiency"], 20)
            for bad in (5, 50, "20", 20.5, True):
                self.assertEqual(self.post("/api/settings", {"efficiency": bad})[0].status, 400, bad)
            self.assertEqual(self.ride.efficiency, 20)
            self.assertEqual(self.post("/api/settings", {"efficiency": None})[0].status, 200)
            self.assertEqual(self.ride.efficiency, bikecomputer.EFF_DEFAULT)
        finally:
            self.ride.efficiency = bikecomputer.EFF_DEFAULT

    def test_settings_file_round_trip(self):
        path = os.path.join(self.dir, "s2.json")
        self.assertEqual(bikecomputer.load_settings(path), {})
        bikecomputer.save_settings(path, {"ftp": 230})
        self.assertEqual(bikecomputer.load_settings(path), {"ftp": 230})
        with open(path, "w") as f:
            f.write("{not json")
        self.assertEqual(bikecomputer.load_settings(path), {})


class PinTest(ServerBase):
    def setUp(self):
        if os.path.exists(self.settings):
            os.remove(self.settings)
        self.guard_reset()

    def guard_reset(self):
        # Reset the lockout between tests.
        pins = self.handler.pin_guard
        pins.failures, pins.locked_until = 0, 0.0

    def post_pin(self, path, body, pin=None):
        h = {"Content-Type": "application/json", "X-Bike-Computer": "1"}
        if pin is not None:
            h["X-PIN"] = pin
        r, data = self.request("POST", path, body, h)
        return r.status, json.loads(data or b"{}")

    def test_no_pin_set(self):
        self.assertEqual(self.post_pin("/api/settings", {"ftp": 200})[0], 200)
        self.assertFalse(json.loads(self.request("GET", "/api/rides")[1])["pin_set"])

    def test_set_pin_then_settings_need_it(self):
        self.assertEqual(self.post_pin("/api/settings", {"pin": "1234"})[0], 200)
        with open(self.settings) as f:
            stored = json.load(f)["pin"]
        self.assertNotIn("1234", json.dumps(stored))              # only a salted hash
        self.assertTrue(json.loads(self.request("GET", "/api/rides")[1])["pin_set"])
        status, body = self.post_pin("/api/settings", {"ftp": 210})
        self.assertEqual((status, body["error"], body["pin"]), (401, "PIN required", True))
        self.assertEqual(self.post_pin("/api/settings", {"ftp": 210}, "9999")[1]["error"], "Wrong PIN")
        self.assertEqual(self.post_pin("/api/settings", {"ftp": 210}, "1234")[0], 200)
        self.assertEqual(self.ride.ftp, 210)
        # changing or removing the PIN needs the current one
        self.assertEqual(self.post_pin("/api/settings", {"pin": None})[0], 401)
        self.assertEqual(self.post_pin("/api/settings", {"pin": None}, "1234")[0], 200)
        self.assertEqual(self.post_pin("/api/settings", {"ftp": None})[0], 200)

    def test_power_needs_the_pin(self):
        self.post_pin("/api/settings", {"pin": "4321"})
        ok = subprocess.CompletedProcess([], 0, "", "")
        with mock.patch("bikecomputer.subprocess.run", return_value=ok) as run:
            self.assertEqual(self.post_pin("/api/power", {"action": "poweroff"})[0], 401)
            run.assert_not_called()
            self.assertEqual(self.post_pin("/api/power", {"action": "poweroff"}, "4321")[0], 200)
            run.assert_called_once()

    def test_lap_and_reset_need_no_pin(self):
        self.post_pin("/api/settings", {"pin": "4321"})
        self.assertEqual(self.post_pin("/api/lap", {})[0], 200)
        self.assertEqual(self.post_pin("/api/reset", {})[0], 200)

    def test_bad_pins_are_refused(self):
        for bad in ("12", "123456789", "12a4", 1234):
            self.assertEqual(self.post_pin("/api/settings", {"pin": bad})[0], 400, bad)

    def test_lockout_after_five_wrong_pins(self):
        self.post_pin("/api/settings", {"pin": "1234"})
        for _ in range(5):
            self.assertEqual(self.post_pin("/api/settings", {"ftp": 200}, "0000")[0], 401)
        status, body = self.post_pin("/api/settings", {"ftp": 200}, "1234")      # even the right one
        self.assertEqual(status, 429)
        self.assertIn("Too many", body["error"])


class HistoryTest(ServerBase):
    def test_history_is_sent_as_columns(self):
        from ride import Reading
        self.ride.reset()
        for w in (100, 200):
            self.ride.add(Reading(power=w, cadence=90))
            self.ride.tick(1.0)
        hist = json.loads(self.request("GET", "/api/history")[1])["history"]
        self.assertEqual(hist["p"], [100, 200])
        self.assertEqual(hist["t"], [1.0, 2.0])
        self.assertEqual(set(hist), {"t", "p", "l", "r", "c", "h", "lap"})


class NoPowerTest(unittest.TestCase):
    def test_power_buttons_off(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        server = ThreadingHTTPServer(("127.0.0.1", 0), bikecomputer.make_handler(
            Ride(), bikecomputer.Hub(), tmp.name, None, power_buttons=False))
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        c = http.client.HTTPConnection("127.0.0.1", server.server_address[1], timeout=5)
        with mock.patch("bikecomputer.subprocess.run") as run:
            c.request("POST", "/api/power", body=json.dumps({"action": "poweroff"}),
                      headers={"Content-Type": "application/json", "X-Bike-Computer": "1"})
            self.assertEqual(c.getresponse().status, 403)
        run.assert_not_called()
        c.close()


class HostCheckTest(unittest.TestCase):
    def test_allowed_hosts(self):
        own = socket.gethostname()
        for h in ("192.168.1.20:8080", "10.42.0.1:8080", "[fe80::1]:8080", "localhost:8080",
                  f"{own}:8080", f"{own}.local:8080", "pi.lan", "pi.home.arpa:8080"):
            self.assertTrue(bikecomputer.host_allowed(h), h)

    def test_refused_hosts(self):
        for h in ("evil.example:8080", "evil.example", "localhost.evil.com",
                  "192.168.1.20.evil.com:8080", "", None):
            self.assertFalse(bikecomputer.host_allowed(h), h)

    def test_allow_host_option(self):
        self.assertTrue(bikecomputer.host_allowed("Pi.Example.net:8080", ["pi.example.net"]))


class RebindingTest(ServerBase):
    def test_post_for_a_foreign_host_is_refused(self):
        h = {"Content-Type": "application/json", "X-Bike-Computer": "1",
             "Host": "evil.example:8080", "Origin": "http://evil.example:8080"}
        self.assertEqual(self.request("POST", "/api/lap", {}, h)[0].status, 403)

    def test_bad_content_length_is_a_bad_request(self):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        c.putrequest("POST", "/api/settings")
        c.putheader("X-Bike-Computer", "1")
        c.putheader("Content-Length", "abc")
        c.endheaders()
        r = c.getresponse()
        self.assertEqual(r.status, 400)
        c.close()


class PowerOffRecordingTest(ServerBase):
    def test_failed_power_off_keeps_recording(self):
        denied = subprocess.CompletedProcess([], 1, "", "sudo: a password is required")
        with mock.patch("bikecomputer.subprocess.run", return_value=denied):
            self.assertEqual(self.post("/api/power", {"action": "poweroff"})[0].status, 500)
        self.assertTrue(self.ride._recording)

    def test_power_off_stops_recording(self):
        ok = subprocess.CompletedProcess([], 0, "", "")
        try:
            with mock.patch("bikecomputer.subprocess.run", return_value=ok):
                self.assertEqual(self.post("/api/power", {"action": "poweroff"})[0].status, 200)
            self.assertFalse(self.ride._recording)
        finally:
            self.ride.resume_recording()


class LockoutTest(unittest.TestCase):
    def test_lockout_doubles_and_resets(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = os.path.join(tmp.name, "s.json")
        bikecomputer.save_settings(path, {"pin": bikecomputer.PinGuard.make("1234")})
        g = bikecomputer.PinGuard(path)
        now = [1000.0]
        with mock.patch("bikecomputer.time.monotonic", side_effect=lambda: now[0]):
            pauses = []
            for _ in range(3):
                for _ in range(5):
                    self.assertEqual(g.check("0000")[0], 401)
                pauses.append(g.locked_until - now[0])
                self.assertEqual(g.check("1234")[0], 429)       # locked, even for the right PIN
                now[0] = g.locked_until + 1
            self.assertEqual(pauses, [60, 120, 240])
            self.assertIsNone(g.check("1234"))                  # right PIN: back to 1 minute
            for _ in range(5):
                g.check("0000")
            self.assertEqual(g.locked_until - now[0], 60)

    def test_lockout_is_capped_at_an_hour(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = os.path.join(tmp.name, "s.json")
        bikecomputer.save_settings(path, {"pin": bikecomputer.PinGuard.make("1234")})
        g = bikecomputer.PinGuard(path)
        g.lockouts = 20                                   # many lockouts already
        with mock.patch("bikecomputer.time.monotonic", return_value=1000.0):
            for _ in range(5):
                g.check("0000")
            self.assertEqual(g.locked_until - 1000.0, 3600)
