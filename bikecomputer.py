#!/usr/bin/env python3
"""Bike computer: receives power/cadence and serves a live dashboard over HTTP.

    .venv/bin/python bikecomputer.py --source ant   # Magene USB ANT+ stick
    .venv/bin/python bikecomputer.py --source ble   # Pi's built-in Bluetooth
    python3 bikecomputer.py --source sim            # fake data, no extra libraries

Then open http://<pi-address>:8080 on a phone/tablet/laptop on the same network.
"""

import argparse
import hashlib
import hmac
import json
import logging
import os
import queue
import re
import importlib.util
import ipaddress
import signal
import socket
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

__version__ = "0.10.1"

HERE = os.path.dirname(os.path.abspath(__file__))
log = logging.getLogger("bikecomputer")


class Hub:
    """Fan-out of samples to all connected browsers (Server-Sent Events)."""

    def __init__(self):
        self.clients = set()
        self.lock = threading.Lock()

    def subscribe(self):
        q: queue.Queue = queue.Queue(maxsize=100)
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
    ride.stop_recording()          # close the ride file; no new one while the Pi shuts down
    try:
        r = subprocess.run(POWER_COMMANDS[action], capture_output=True, text=True, timeout=15)
    except (OSError, subprocess.TimeoutExpired) as e:
        ride.resume_recording()
        return False, str(e)
    if r.returncode != 0:
        ride.resume_recording()
        log.error("%s failed: %s", action, r.stderr.strip())
        return False, ("Not allowed to power off the Pi. Run deploy/install.sh once to allow it."
                       if "password" in r.stderr else r.stderr.strip() or f"exit code {r.returncode}")
    log.info("%s requested from the dashboard", action)
    return True, ""


# FTP limits accepted from the page, W.
FTP_MIN, FTP_MAX = 50, 600
# Efficiency (food energy -> work at the pedals) used for the kcal estimate, %.
EFF_MIN, EFF_MAX, EFF_DEFAULT = 15, 30, 24


def int_in(v, lo, hi):
    """True if v is a whole number from lo to hi (JSON true/false don't count)."""
    return isinstance(v, int) and not isinstance(v, bool) and lo <= v <= hi


def load_settings(path):
    try:
        with open(path) as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as e:
        log.warning("Ignoring unreadable settings file %s: %s", path, e)
        return {}


def save_settings(path, settings):
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(settings, f, indent=2)
    os.replace(tmp, path)        # never leave a half-written file


class PinGuard:
    """Optional PIN for power off, restart and settings.

    Stored as a salted PBKDF2 hash in settings.json. After 5 wrong PINs in a row,
    PIN checks are refused for a while: 1 minute, then 2, 4, 8, ... up to an
    hour each time. Guessing a 4-digit PIN (10,000 tries, 5 an hour) then takes
    about 6 weeks on average instead of hours; longer PINs take far longer.
    A right PIN starts over at 1 minute.

    The lockout counts all wrong tries, whoever sends them: someone guessing
    also locks you out for a while. That's the price of a lockout.
    """

    MAX_TRIES = 5
    LOCKOUT_S = 60
    MAX_LOCKOUT_S = 3600
    PIN_RE = re.compile(r"^\d{4,8}$")

    def __init__(self, settings_path):
        self.settings_path = settings_path
        self.lock = threading.Lock()
        self.failures = 0
        self.lockouts = 0            # lockouts in a row, for the doubling
        self.locked_until = 0.0

    @staticmethod
    def _hash(pin, salt):
        return hashlib.pbkdf2_hmac("sha256", pin.encode(), salt, 50_000).hex()

    @staticmethod
    def make(pin):
        salt = os.urandom(16)
        return {"salt": salt.hex(), "hash": PinGuard._hash(pin, salt)}

    def stored(self):
        pin = load_settings(self.settings_path).get("pin") if self.settings_path else None
        return pin if isinstance(pin, dict) and "salt" in pin and "hash" in pin else None

    def is_set(self):
        return self.stored() is not None

    def check(self, given):
        """Returns None if allowed, else (status, message)."""
        stored = self.stored()
        if stored is None:
            return None
        with self.lock:
            wait = self.locked_until - time.monotonic()
            if wait > 0:
                minutes = max(1, round(wait / 60))
                return 429, f"Too many wrong PINs. Try again in {minutes} minute{'s' if minutes > 1 else ''}."
            if not given:
                return 401, "PIN required"
            try:
                ok = hmac.compare_digest(self._hash(given, bytes.fromhex(stored["salt"])), stored["hash"])
            except ValueError:
                ok = False
            if ok:
                self.failures = 0
                self.lockouts = 0
                return None
            self.failures += 1
            if self.failures >= self.MAX_TRIES:
                self.failures = 0
                pause = min(self.MAX_LOCKOUT_S, self.LOCKOUT_S * 2 ** self.lockouts)
                self.lockouts += 1
                self.locked_until = time.monotonic() + pause
                log.warning("%d wrong PINs: PIN checks paused for %d s", self.MAX_TRIES, pause)
            return 401, "Wrong PIN"


# Local network names the Pi may be reached by. See host_allowed().
LOCAL_SUFFIXES = (".local", ".lan", ".home", ".internal", ".home.arpa", ".localdomain")


def host_allowed(host_header, extra=()):
    """True if a request's Host is an address the Pi is really reached by.

    Protects against DNS rebinding: a web site can point its own name at the
    Pi's IP address, and then the browser treats the Pi as the same site, so
    the header and Origin checks pass. Its Host header still carries that
    site's name, though, so only IP addresses, localhost, local network names,
    the Pi's own name and names given with --allow-host are accepted.
    """
    if not host_header:
        return False
    host = host_header.strip().lower()
    if host.startswith("["):                       # [IPv6]:port
        host = host[1:].split("]", 1)[0]
    elif host.count(":") == 1:                     # name:port or IPv4:port
        host = host.split(":", 1)[0]
    host = host.rstrip(".")
    try:
        ipaddress.ip_address(host)
        return True
    except ValueError:
        pass
    # Names ending in .local, .lan, .home, ... can't be registered in public DNS,
    # so a web site can't point one of them at the Pi.
    return (host in ("localhost", socket.gethostname().lower()) or host.endswith(LOCAL_SUFFIXES)
            or host in {h.lower() for h in extra})


# Files from static/ served as they are: URL path -> (file, content type).
STATIC_FILES = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/index.html": ("index.html", "text/html; charset=utf-8"),
    "/rides": ("rides.html", "text/html; charset=utf-8"),
    "/rides/": ("rides.html", "text/html; charset=utf-8"),
    "/common.css": ("common.css", "text/css; charset=utf-8"),
    "/common.js": ("common.js", "text/javascript; charset=utf-8"),
}


def history_columns(history):
    """Samples as one list per value: about a third of the size for long rides."""
    keys = ("t", "p", "l", "r", "c", "h", "lap")
    return {k: [h.get(k) for h in history] for k in keys}


def make_handler(ride, hub, log_dir, settings_path=None, power_buttons=True, allow_hosts=()):
    pins = PinGuard(settings_path)

    class Handler(BaseHTTPRequestHandler):
        pin_guard = pins     # for tests

        def log_message(self, fmt, *args):
            pass

        def _send(self, code, body, ctype, download=None):
            """Send a reply; `download` = file name to save it as."""
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            if download:
                self.send_header("Content-Disposition", f'attachment; filename="{download}"')
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _static(self, name, ctype):
            with open(os.path.join(HERE, "static", name), "rb") as f:
                self._send(200, f.read(), ctype)

        def do_GET(self):
            path = urlsplit(self.path).path
            if path in STATIC_FILES:
                self._static(*STATIC_FILES[path])
            elif path == "/api/rides":
                current = ride.logger.path if ride.logger and ride.logger.f else None
                rides = ridelog.list_rides(log_dir, current=current, ftp=ride.ftp)
                self._send(200, json.dumps({"rides": rides, "version": __version__,
                                            "ftp": ride.ftp, "efficiency": ride.efficiency,
                                            "power_buttons": power_buttons,
                                            "pin_set": pins.is_set()}).encode(),
                           "application/json")
            elif path.startswith("/rides/") and ridelog.NAME_RE.match(name := path[len("/rides/"):]):
                try:
                    with open(os.path.join(log_dir, name), "rb") as f:
                        body = f.read()
                except FileNotFoundError:
                    self._send(404, b"not found", "text/plain")
                    return
                self._send(200, body, "text/csv; charset=utf-8", download=name)
            elif path.startswith("/rides/") and (m := ridelog.TCX_NAME_RE.match(name := path[len("/rides/"):])):
                csv_path = os.path.join(log_dir, m.group(1) + ".csv")
                try:
                    body = ridelog.to_tcx(csv_path, __version__)
                except FileNotFoundError:
                    self._send(404, b"not found", "text/plain")
                    return
                except ValueError as e:      # empty ride
                    self._send(409, f"Can't export this ride: {e}".encode(), "text/plain; charset=utf-8")
                    return
                self._send(200, body, "application/vnd.garmin.tcx+xml", download=name)
            elif path == "/api/history":
                snap = ride.snapshot()
                snap["history"] = history_columns(snap["history"])
                self._send(200, json.dumps(snap, separators=(",", ":")).encode(), "application/json")
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
            if not host_allowed(self.headers.get("Host"), allow_hosts):
                log.warning("Refused a POST for host %r (add it with --allow-host if it's yours)",
                            self.headers.get("Host"))
                return False
            origin = self.headers.get("Origin")
            return not origin or urlsplit(origin).netloc == self.headers.get("Host")

        def _json_body(self):
            try:
                n = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                return {}
            if not 0 < n <= 1024:
                return {}
            try:
                body = json.loads(self.rfile.read(n))
            except ValueError:
                return {}
            return body if isinstance(body, dict) else {}

        def _error(self, code, msg, **extra):
            self._send(code, json.dumps({"error": msg, **extra}).encode(), "application/json")

        def _pin_ok(self):
            """Check the X-PIN header when a PIN is set; sends the error reply if not ok."""
            problem = pins.check(self.headers.get("X-PIN"))
            if problem:
                self._error(problem[0], problem[1], pin=True)
                return False
            return True

        def do_POST(self):
            path = urlsplit(self.path).path
            if path not in ("/api/reset", "/api/clock", "/api/power", "/api/lap", "/api/settings"):
                self._send(404, b"not found", "text/plain")
                return
            if not self._post_allowed():
                self._send(403, b"forbidden", "text/plain")
                return
            if path == "/api/reset":
                ride.reset()
                hub.publish({"type": "reset"})
            elif path == "/api/lap":
                if ride.new_lap():
                    log.info("Lap %d started", len(ride.laps) + 1)
            elif path == "/api/settings":
                body = self._json_body()
                changes = {}
                if "ftp" in body:
                    ftp = body["ftp"]
                    if ftp is not None and not int_in(ftp, FTP_MIN, FTP_MAX):
                        self._error(400, f"FTP must be {FTP_MIN} to {FTP_MAX} W")
                        return
                    changes["ftp"] = ftp
                if "efficiency" in body:
                    eff = body["efficiency"]          # None = back to the default
                    if eff is not None and not int_in(eff, EFF_MIN, EFF_MAX):
                        self._error(400, f"Efficiency must be {EFF_MIN} to {EFF_MAX} %")
                        return
                    changes["efficiency"] = eff
                if "pin" in body:
                    pin = body["pin"]
                    if pin is not None and not (isinstance(pin, str) and PinGuard.PIN_RE.match(pin)):
                        self._error(400, "The PIN must be 4 to 8 digits")
                        return
                    changes["pin"] = PinGuard.make(pin) if pin else None
                if not changes:
                    self._error(400, "Nothing to change")
                    return
                if not self._pin_ok():
                    return
                if settings_path:
                    try:
                        save_settings(settings_path, {**load_settings(settings_path), **changes})
                    except OSError as e:
                        log.error("Could not save the settings: %s", e)
                        self._error(500, f"Could not save the settings: {e}")
                        return
                if "ftp" in changes:
                    ride.ftp = changes["ftp"]
                    log.info("FTP set to %s", f"{ride.ftp} W" if ride.ftp else "none")
                if "efficiency" in changes:
                    ride.efficiency = changes["efficiency"] or EFF_DEFAULT
                    log.info("Efficiency set to %d %%", ride.efficiency)
                if "pin" in changes:
                    log.info("PIN %s", "set" if changes["pin"] else "removed")
            elif path == "/api/power":
                action = self._json_body().get("action")
                if action not in POWER_COMMANDS:
                    self._send(400, b"bad request", "text/plain")
                    return
                if not power_buttons:
                    self._error(403, "Power off is turned off (--no-power).")
                    return
                if not self._pin_ok():
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
                    # The ride file's times are fixed in the next update (Ride.tick).
                    log.info("Clock corrected from the browser by %+.0f s", clock.offset)
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
    ap.add_argument("--heart-rate", choices=["auto", "ant", "ble", "off"], default="auto",
                    help="where heart rate comes from: auto = the same as --source")
    ap.add_argument("--no-heart-rate", action="store_true", help="the same as --heart-rate off")
    ap.add_argument("--hr-ble-name", help="only use Bluetooth heart rate devices whose name contains this")
    ap.add_argument("--ble-address", help="BLE MAC address of the pedals (default: scan)")
    ap.add_argument("--ble-name", help="only connect to BLE devices whose name contains this")
    ap.add_argument("--unknown-balance-side", choices=["left", "right"], default=None,
                    help="which pedal a balance value refers to when the meter says 'unknown'. "
                         "Default: right for ANT+, left for BLE. Flip it if L/R look swapped.")
    ap.add_argument("--log-dir", default=os.path.join(HERE, "rides"))
    ap.add_argument("--keep-rides", type=int, default=10,
                    help="keep only this many newest ride CSV files (0 = keep all)")
    ap.add_argument("--no-log", action="store_true", help="don't write ride CSV files")
    ap.add_argument("--no-power", action="store_true",
                    help="hide the power off / restart buttons (for testing on a computer)")
    ap.add_argument("--allow-host", action="append", default=[], metavar="NAME",
                    help="another name the Pi is reached by, for example pi.example.net (can be repeated)")
    ap.add_argument("--settings", default=os.path.join(HERE, "settings.json"),
                    help="where to keep settings changed on the page, such as FTP and efficiency")
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s: %(message)s")

    # Fail fast with a clear message if the library for this source is missing,
    # instead of a background thread dying while the dashboard keeps running.
    needed = {"ant": "openant", "ble": "bleak"}.get(args.source)
    if needed:
        if importlib.util.find_spec(needed) is None:
            raise SystemExit(
                f"Python module '{needed}' is not installed for {sys.executable}.\n"
                f"Run with the project's virtual environment:  .venv/bin/python bikecomputer.py --source {args.source}\n"
                f"or install it there:  .venv/bin/pip install -r requirements.txt")

    side = args.unknown_balance_side or ("left" if args.source == "ble" else "right")
    settings = load_settings(args.settings)
    ftp = settings.get("ftp")
    if not int_in(ftp, FTP_MIN, FTP_MAX):
        ftp = None
    efficiency = settings.get("efficiency")
    if not int_in(efficiency, EFF_MIN, EFF_MAX):
        efficiency = EFF_DEFAULT
    ride = Ride(unknown_balance_side=side, ftp=ftp, efficiency=efficiency,
                logger=None if args.no_log else CsvLogger(args.log_dir, keep=args.keep_rides))
    hub = Hub()

    hr = "off" if args.no_heart_rate else args.heart_rate
    if hr == "auto":
        hr = {"ant": "ant", "ble": "ble"}.get(args.source, "off")
    if hr == "ant" and args.source != "ant":
        raise SystemExit("ANT+ heart rate needs --source ant. Use --heart-rate ble or off.")
    if hr == "ble" and importlib.util.find_spec("bleak") is None:
        raise SystemExit("Bluetooth heart rate needs the 'bleak' module: .venv/bin/pip install -r requirements.txt")

    src: sources.AntSource | sources.BleSource | sources.SimSource
    if args.source == "ant":
        src = sources.AntSource(ride, device_id=args.ant_device_id,
                                heart_rate=hr == "ant", hr_device_id=args.ant_hr_device_id)
    elif args.source == "ble":
        src = sources.BleSource(ride, address=args.ble_address, name_filter=args.ble_name)
    else:
        src = sources.SimSource(ride)
    src.start()
    hr_src = None
    if hr == "ble":
        hr_src = sources.BleHeartRate(ride, name_filter=args.hr_ble_name)
        hr_src.start()

    threading.Thread(target=ticker, args=(ride, hub), name="ticker", daemon=True).start()

    server = ThreadingHTTPServer(("0.0.0.0", args.port), make_handler(ride, hub, args.log_dir, args.settings,
                                                                         power_buttons=not args.no_power,
                                                                         allow_hosts=args.allow_host))
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
        ride.stop_recording()        # close the ride file, and open no new one
        if hasattr(src, "stop"):
            src.stop()


if __name__ == "__main__":
    main()
