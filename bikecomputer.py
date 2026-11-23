#!/usr/bin/env python3
"""Bike computer: receives power/cadence and serves a live dashboard over HTTP.

    .venv/bin/python bikecomputer.py --source ant   # Magene USB ANT+ stick
    .venv/bin/python bikecomputer.py --source ble   # Pi's built-in Bluetooth
    python3 bikecomputer.py --source sim            # fake data, no extra libraries

Then open http://<pi-address>:8080 on a phone/tablet/laptop on the same network.
"""

import argparse
import csv
import json
import logging
import os
import queue
import sys
import threading
import time
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

from ride import Ride, clock
import sources

HERE = os.path.dirname(os.path.abspath(__file__))
log = logging.getLogger("bikecomputer")


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
                self.w.writerow(["time_iso", "elapsed_s", "power_w", "left_w", "right_w", "cadence_rpm"])
                log.info("Logging ride to %s", self.path)
                self._prune()
            self.w.writerow([datetime.fromtimestamp(s["wall"]).isoformat(timespec="seconds"),
                             s["t"], s["p"], s["l"], s["r"], s["c"]])
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


class Hub:
    """Fan-out of samples to all connected browsers (Server-Sent Events)."""

    def __init__(self):
        self.clients = set()
        self.lock = threading.Lock()

    def subscribe(self):
        q = queue.Queue(maxsize=100)
        with self.lock:
            self.clients.add(q)
        return q

    def unsubscribe(self, q):
        with self.lock:
            self.clients.discard(q)

    def publish(self, msg):
        data = json.dumps(msg)
        with self.lock:
            for q in list(self.clients):
                try:
                    q.put_nowait(data)
                except queue.Full:
                    pass


def make_handler(ride, hub):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):
            pass

        def _send(self, code, body, ctype):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            path = urlsplit(self.path).path
            if path in ("/", "/index.html"):
                with open(os.path.join(HERE, "static", "index.html"), "rb") as f:
                    self._send(200, f.read(), "text/html; charset=utf-8")
            elif path == "/api/history":
                self._send(200, json.dumps(ride.snapshot()).encode(), "application/json")
            elif path == "/events":
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                q = hub.subscribe()
                try:
                    while True:
                        try:
                            data = q.get(timeout=15)
                            self.wfile.write(f"data: {data}\n\n".encode())
                        except queue.Empty:
                            self.wfile.write(b": keepalive\n\n")
                        self.wfile.flush()
                except OSError:   # browser went away
                    pass
                finally:
                    hub.unsubscribe(q)
            else:
                self._send(404, b"not found", "text/plain")

        def _post_allowed(self):
            """Only accept POSTs from the dashboard itself.

            Another web page open in a browser on the same network could otherwise
            send a POST here (for example to reset the ride). A custom header can't
            be sent cross-site without a CORS preflight, which this server never
            answers, and the Origin check is a second guard.
            """
            if self.headers.get("X-Bike-Computer") != "1":
                return False
            origin = self.headers.get("Origin")
            return not origin or urlsplit(origin).netloc == self.headers.get("Host")

        def _json_body(self):
            n = int(self.headers.get("Content-Length") or 0)
            if not 0 < n <= 1024:
                return {}
            try:
                body = json.loads(self.rfile.read(n))
            except ValueError:
                return {}
            return body if isinstance(body, dict) else {}

        def do_POST(self):
            path = urlsplit(self.path).path
            if path not in ("/api/reset", "/api/clock"):
                self._send(404, b"not found", "text/plain")
                return
            if not self._post_allowed():
                self._send(403, b"forbidden", "text/plain")
                return
            if path == "/api/reset":
                ride.reset()
                hub.publish({"type": "reset"})
            else:
                t = self._json_body().get("now")
                if not isinstance(t, (int, float)) or isinstance(t, bool):
                    self._send(400, b"bad request", "text/plain")
                    return
                if clock.correct(t / 1000.0):
                    log.info("Clock corrected from the browser by %+.0f s", clock.offset)
                    if ride.logger:
                        ride.logger.clock_corrected()
            self._send(200, b"{}", "application/json")

    return Handler


def ticker(ride, hub):
    """Once a second: close the current 1 s bucket and push it to browsers."""
    nxt = time.monotonic()
    while True:
        nxt += 1.0
        time.sleep(max(0, nxt - time.monotonic()))
        try:
            sample = ride.tick(1.0)
            sample["type"] = "sample"
            hub.publish(sample)
        except Exception:
            # Never let one bad second stop the dashboard for the rest of the ride.
            log.exception("Error while updating the ride")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--source", choices=["ant", "ble", "sim"], default="ant")
    ap.add_argument("--port", type=int, default=8080)
    ap.add_argument("--ant-device-id", type=int, default=0,
                    help="ANT+ device number to pair with (0 = first one found)")
    ap.add_argument("--ble-address", help="BLE MAC address of the pedals (default: scan)")
    ap.add_argument("--ble-name", help="only connect to BLE devices whose name contains this")
    ap.add_argument("--unknown-balance-side", choices=["left", "right"], default=None,
                    help="which pedal a balance value refers to when the meter says 'unknown'. "
                         "Default: right for ANT+, left for BLE. Flip it if L/R look swapped.")
    ap.add_argument("--log-dir", default=os.path.join(HERE, "rides"))
    ap.add_argument("--keep-rides", type=int, default=10,
                    help="keep only this many newest ride CSV files (0 = keep all)")
    ap.add_argument("--no-log", action="store_true", help="don't write ride CSV files")
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s: %(message)s")

    # Fail fast with a clear message if the library for this source is missing,
    # instead of a background thread dying while the dashboard keeps running.
    needed = {"ant": "openant", "ble": "bleak"}.get(args.source)
    if needed:
        import importlib.util
        if importlib.util.find_spec(needed) is None:
            raise SystemExit(
                f"Python module '{needed}' is not installed for {sys.executable}.\n"
                f"Run with the project's virtual environment:  .venv/bin/python bikecomputer.py --source {args.source}\n"
                f"or install it there:  .venv/bin/pip install -r requirements.txt")

    side = args.unknown_balance_side or ("left" if args.source == "ble" else "right")
    ride = Ride(unknown_balance_side=side,
                logger=None if args.no_log else CsvLogger(args.log_dir, keep=args.keep_rides))
    hub = Hub()

    if args.source == "ant":
        src = sources.AntSource(ride, device_id=args.ant_device_id)
    elif args.source == "ble":
        src = sources.BleSource(ride, address=args.ble_address, name_filter=args.ble_name)
    else:
        src = sources.SimSource(ride)
    src.start()

    threading.Thread(target=ticker, args=(ride, hub), name="ticker", daemon=True).start()

    server = ThreadingHTTPServer(("0.0.0.0", args.port), make_handler(ride, hub))
    server.daemon_threads = True
    log.info("Dashboard on http://0.0.0.0:%d  (source: %s)", args.port, args.source)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
