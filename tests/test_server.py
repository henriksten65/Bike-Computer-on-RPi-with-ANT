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


class ServerTest(unittest.TestCase):
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
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0),
                                         bikecomputer.make_handler(cls.ride, bikecomputer.Hub(), cls.dir))
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

    def test_pages(self):
        for path in ("/", "/index.html", "/?x=1", "/rides"):
            r, body = self.request("GET", path)
            self.assertEqual(r.status, 200, path)
            self.assertIn(b"<html", body)
        self.assertEqual(self.request("GET", "/nope")[0].status, 404)

    def test_ride_list(self):
        r, body = self.request("GET", "/api/rides")
        rides = json.loads(body)["rides"]
        self.assertEqual([x["name"] for x in rides], ["ride-20261123-100000.csv"])
        self.assertEqual((rides[0]["avg_p"], rides[0]["avg_hr"]), (210, 130))

    def test_download(self):
        r, body = self.request("GET", "/rides/ride-20261123-100000.csv")
        self.assertEqual(r.status, 200)
        self.assertIn("attachment", r.getheader("Content-Disposition"))
        self.assertTrue(body.startswith(b"time_iso,"))

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
