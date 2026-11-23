#!/usr/bin/env python3
"""Bike computer: receives power/cadence and serves a live dashboard over HTTP.

    .venv/bin/python bikecomputer.py --source ant   # Magene USB ANT+ stick
    .venv/bin/python bikecomputer.py --source ble   # Pi's built-in Bluetooth
    python3 bikecomputer.py --source sim            # fake data, no extra libraries

Then open http://<pi-address>:8080 on a phone/tablet/laptop on the same network.
"""

import argparse
import json
import logging
import os
import queue
import signal
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

from ride import Ride, clock
from ridelog import CsvLogger
import ridelog
import sources

__version__ = "0.4.0"

HERE = os.path.dirname(os.path.abspath(__file__))
log = logging.getLogger("bikecomputer")


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


# Power off / restart the Pi. Allowed by the sudoers rule that deploy/install.sh adds.
POWER_COMMANDS = {
    "poweroff": ["sudo", "-n", "/usr/bin/systemctl", "poweroff"],
    "reboot": ["sudo", "-n", "/usr/bin/systemctl", "reboot"],
}


def power_action(action, ride):
    """Run a power command. Returns (ok, message)."""
    if ride.logger:
        ride.logger.new_file()     # close the ride file cleanly first
    try:
        r = subprocess.run(POWER_COMMANDS[action], capture_output=True, text=True, timeout=15)
    except (OSError, subprocess.TimeoutExpired) as e:
        return False, str(e)
    if r.returncode != 0:
        log.error("%s failed: %s", action, r.stderr.strip())
        return False, ("Not allowed to power off the Pi. Run deploy/install.sh once to allow it."
                       if "password" in r.stderr else r.stderr.strip() or f"exit code {r.returncode}")
    log.info("%s requested from the dashboard", action)
    return True, ""


def make_handler(ride, hub, log_dir):
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

        def _page(self, name):
            with open(os.path.join(HERE, "static", name), "rb") as f:
                self._send(200, f.read(), "text/html; charset=utf-8")

        def do_GET(self):
            path = urlsplit(self.path).path
            if path in ("/", "/index.html"):
                self._page("index.html")
            elif path in ("/rides", "/rides/"):
                self._page("rides.html")
            elif path == "/api/rides":
                current = ride.logger.path if ride.logger and ride.logger.f else None
                rides = ridelog.list_rides(log_dir, current=current)
                self._send(200, json.dumps({"rides": rides, "version": __version__}).encode(), "application/json")
            elif path.startswith("/rides/") and ridelog.NAME_RE.match(path[len("/rides/"):]):
                name = path[len("/rides/"):]
                try:
                    with open(os.path.join(log_dir, name), "rb") as f:
                        body = f.read()
                except FileNotFoundError:
                    self._send(404, b"not found", "text/plain")
                    return
                self.send_response(200)
                self.send_header("Content-Type", "text/csv; charset=utf-8")
                self.send_header("Content-Disposition", f'attachment; filename="{name}"')
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(body)
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
            if path not in ("/api/reset", "/api/clock", "/api/power"):
                self._send(404, b"not found", "text/plain")
                return
            if not self._post_allowed():
                self._send(403, b"forbidden", "text/plain")
                return
            if path == "/api/reset":
                ride.reset()
                hub.publish({"type": "reset"})
            elif path == "/api/power":
                action = self._json_body().get("action")
                if action not in POWER_COMMANDS:
                    self._send(400, b"bad request", "text/plain")
                    return
                ok, msg = power_action(action, ride)
                self._send(200 if ok else 500, json.dumps({"error": msg} if not ok else {}).encode(),
                           "application/json")
                return
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
    ap.add_argument("--version", action="version", version=f"bikecomputer {__version__}")
    ap.add_argument("--source", choices=["ant", "ble", "sim"], default="ant")
    ap.add_argument("--port", type=int, default=8080)
    ap.add_argument("--ant-device-id", type=int, default=0,
                    help="ANT+ device number to pair with (0 = first one found)")
    ap.add_argument("--ant-hr-device-id", type=int, default=0,
                    help="ANT+ heart rate strap device number (0 = first one found)")
    ap.add_argument("--no-heart-rate", action="store_true",
                    help="don't listen for an ANT+ heart rate strap")
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
        src = sources.AntSource(ride, device_id=args.ant_device_id,
                                heart_rate=not args.no_heart_rate, hr_device_id=args.ant_hr_device_id)
    elif args.source == "ble":
        src = sources.BleSource(ride, address=args.ble_address, name_filter=args.ble_name)
    else:
        src = sources.SimSource(ride)
    src.start()

    threading.Thread(target=ticker, args=(ride, hub), name="ticker", daemon=True).start()

    server = ThreadingHTTPServer(("0.0.0.0", args.port), make_handler(ride, hub, args.log_dir))
    server.daemon_threads = True
    log.info("Bike computer %s: dashboard on http://0.0.0.0:%d  (source: %s)",
             __version__, args.port, args.source)

    # systemd stops the service with SIGTERM: shut down the same way as Ctrl+C.
    def on_term(*_):
        raise KeyboardInterrupt
    signal.signal(signal.SIGTERM, on_term)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        log.info("Shutting down")
        if hasattr(src, "stop"):
            src.stop()
        if ride.logger:
            ride.logger.new_file()   # close the ride file


if __name__ == "__main__":
    main()
