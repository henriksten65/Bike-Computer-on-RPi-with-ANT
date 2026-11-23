"""The web server: pages, ride list and downloads, POST protection, power off."""

import http.client
import json
import os
import subprocess
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from unittest import mock

import bikecomputer
from ride import Ride
from ridelog import CsvLogger


class ServerBase(unittest.TestCase):
    """Starts a server on a free port with a temporary rides folder."""

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
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0),
                                         bikecomputer.make_handler(cls.ride, bikecomputer.Hub(), cls.dir,
                                                                   cls.settings))
        cls.server.daemon_threads = True
        cls.port = cls.server.server_address[1]
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
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

    def test_settings_file_round_trip(self):
        path = os.path.join(self.dir, "s2.json")
        self.assertEqual(bikecomputer.load_settings(path), {})
        bikecomputer.save_settings(path, {"ftp": 230})
        self.assertEqual(bikecomputer.load_settings(path), {"ftp": 230})
        with open(path, "w") as f:
            f.write("{not json")
        self.assertEqual(bikecomputer.load_settings(path), {})
