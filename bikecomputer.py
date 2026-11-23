#!/usr/bin/env python3
"""Bike computer: receives power/cadence and serves a live dashboard over HTTP.

    python3 bikecomputer.py --source ant      # Magene USB ANT+ stick
    python3 bikecomputer.py --source ble      # Pi's built-in Bluetooth
    python3 bikecomputer.py --source sim      # fake data, for testing

Then open http://<pi-address>:8080 on a phone/tablet/laptop on the same network.
"""

import argparse
import csv
import json
import logging
import os
import queue
import threading
import time
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from ride import Ride
import sources

HERE = os.path.dirname(os.path.abspath(__file__))
log = logging.getLogger("bikecomputer")


class CsvLogger:
    """Writes one CSV per ride into rides/ (a new file on every reset)."""

    def __init__(self, directory):
        self.dir = directory
        self.f = None
        self.w = None

    def new_file(self):
        if self.f:
            self.f.close()
            self.f = None

    def write(self, s):
        if self.f is None:
            os.makedirs(self.dir, exist_ok=True)
            path = os.path.join(self.dir, datetime.now().strftime("ride-%Y%m%d-%H%M%S.csv"))
            self.f = open(path, "w", newline="")
            self.w = csv.writer(self.f)
            self.w.writerow(["time_iso", "elapsed_s", "power_w", "left_w", "right_w", "cadence_rpm"])
            log.info("Logging ride to %s", path)
        self.w.writerow([datetime.fromtimestamp(s["wall"]).isoformat(timespec="seconds"),
                         s["t"], s["p"], s["l"], s["r"], s["c"]])
        self.f.flush()


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
            if self.path in ("/", "/index.html"):
                with open(os.path.join(HERE, "static", "index.html"), "rb") as f:
                    self._send(200, f.read(), "text/html; charset=utf-8")
            elif self.path == "/api/history":
                self._send(200, json.dumps(ride.snapshot()).encode(), "application/json")
            elif self.path == "/events":
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
                except (BrokenPipeError, ConnectionResetError):
                    pass
                finally:
                    hub.unsubscribe(q)
            else:
                self._send(404, b"not found", "text/plain")

        def do_POST(self):
            if self.path == "/api/reset":
                ride.reset()
                hub.publish({"type": "reset"})
                self._send(200, b"{}", "application/json")
            else:
                self._send(404, b"not found", "text/plain")

    return Handler


def ticker(ride, hub):
    """Once a second: close the current 1 s bucket and push it to browsers."""
    nxt = time.monotonic()
    while True:
        nxt += 1.0
        time.sleep(max(0, nxt - time.monotonic()))
        sample = ride.tick(1.0)
        sample["type"] = "sample"
        hub.publish(sample)


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
    ap.add_argument("--no-log", action="store_true", help="don't write ride CSV files")
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s: %(message)s")

    side = args.unknown_balance_side or ("left" if args.source == "ble" else "right")
    ride = Ride(unknown_balance_side=side,
                logger=None if args.no_log else CsvLogger(args.log_dir))
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
