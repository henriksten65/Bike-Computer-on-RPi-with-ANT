"""Open the pages in a real (headless) browser with the simulator running.

Catches JavaScript errors and broken page features that the Python tests can't
see. Needs Playwright, so it is skipped unless it's installed:

    pip install playwright && playwright install chromium
    python3 -m unittest tests.test_browser

Set SCREENSHOT_DIR to also save screenshots (phone and desktop, light and dark).
The server runs with --no-power, so the Power off button can never be pressed.
"""

import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.request
from typing import Any, Optional

try:
    from playwright.sync_api import sync_playwright
    HAVE_PLAYWRIGHT = True
except ImportError:          # not installed: skip the whole module
    HAVE_PLAYWRIGHT = False

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PHONE = {"width": 390, "height": 844}
DESKTOP = {"width": 1280, "height": 800}


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


if not HAVE_PLAYWRIGHT and os.environ.get("REQUIRE_BROWSER"):
    raise ImportError("REQUIRE_BROWSER is set but Playwright is not installed")


@unittest.skipIf(not HAVE_PLAYWRIGHT, "Playwright is not installed")
class BrowserTest(unittest.TestCase):
    # Set in setUpClass.
    tmp: tempfile.TemporaryDirectory
    port: int
    base: str
    server: subprocess.Popen
    pw: Any
    browser: Any
    shots: Optional[str]

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.port = free_port()
        cls.base = f"http://127.0.0.1:{cls.port}"
        cls.server = subprocess.Popen(
            [sys.executable, os.path.join(HERE, "bikecomputer.py"), "--source", "sim",
             "--port", str(cls.port), "--no-power",
             "--log-dir", os.path.join(cls.tmp.name, "rides"),
             "--settings", os.path.join(cls.tmp.name, "settings.json")],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        for _ in range(50):                      # wait for the server
            try:
                urllib.request.urlopen(cls.base + "/api/history", timeout=1)
                break
            except OSError:
                time.sleep(0.2)
        cls.pw = sync_playwright().start()
        cls.browser = cls.pw.chromium.launch()
        cls.shots = os.environ.get("SCREENSHOT_DIR")
        if cls.shots:
            os.makedirs(cls.shots, exist_ok=True)

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.pw.stop()
        cls.server.terminate()
        cls.server.wait(timeout=10)
        cls.tmp.cleanup()

    def open(self, path, viewport=PHONE, scheme="light"):
        """Open a page and collect JavaScript errors and console errors."""
        ctx = self.browser.new_context(viewport=viewport, color_scheme=scheme)
        page = ctx.new_page()
        errors = []
        page.on("pageerror", lambda e: errors.append(f"page error: {e}"))
        # The browser logs every non-2xx reply as a console error; the PIN check
        # answers 401 on purpose, so that one is expected.
        page.on("console", lambda m: errors.append(f"console {m.type}: {m.text}")
                if m.type == "error" and "status of 401" not in m.text else None)
        page.goto(self.base + path)
        self.addCleanup(ctx.close)
        self.addCleanup(lambda: self.assertEqual(errors, [], "JavaScript errors on " + path))
        return page

    def post(self, path, body):
        req = urllib.request.Request(self.base + path, data=json.dumps(body).encode(), method="POST",
                                     headers={"Content-Type": "application/json", "X-Bike-Computer": "1"})
        return urllib.request.urlopen(req, timeout=5)

    def shot(self, page, name):
        if self.shots:
            page.screenshot(path=os.path.join(self.shots, name + ".png"), full_page=True)

    # ------------------------------------------------------------------ dashboard

    def test_dashboard_shows_live_data(self):
        page = self.open("/")
        page.wait_for_function("document.getElementById('status').textContent === 'Receiving'", timeout=10000)
        self.assertRegex(page.inner_text("#power"), r"^\d+")
        self.assertRegex(page.inner_text("#cad"), r"^\d+")
        self.assertRegex(page.inner_text("#left"), r"^\d+")
        page.wait_for_selector("#tiles.has-hr", timeout=5000)          # simulator sends HR
        self.assertRegex(page.inner_text("#hr"), r"^\d+")
        self.assertIn("NP", page.inner_text("#stats"))

    def test_graph_draws(self):
        page = self.open("/")
        page.wait_for_function("document.getElementById('status').textContent === 'Receiving'", timeout=10000)
        page.wait_for_timeout(2500)
        # Some pixels in the plot must be drawn (lines, grid, labels).
        drawn = page.evaluate("""() => {
            const c = document.getElementById('cv'), d = c.getContext('2d').getImageData(0, 0, c.width, c.height).data;
            let n = 0; for (let i = 3; i < d.length; i += 4) if (d[i]) n++; return n; }""")
        self.assertGreater(drawn, 1000)

    def test_lap_button(self):
        page = self.open("/")
        page.wait_for_function("document.getElementById('status').textContent === 'Receiving'", timeout=10000)
        page.wait_for_timeout(1500)
        page.click("#lap")
        page.wait_for_selector("#laps:not([hidden]) table", timeout=5000)
        self.assertIn("Lap 2", page.inner_text("#lapline"))

    def test_reset(self):
        page = self.open("/")
        page.wait_for_function("document.getElementById('status').textContent === 'Receiving'", timeout=10000)
        page.on("dialog", lambda d: d.accept())
        page.click("#reset")
        page.wait_for_function("parseInt(document.getElementById('time').textContent.split(':')[1]) < 3",
                               timeout=5000)

    # ------------------------------------------------------------------ rides page

    def test_rides_page_lists_rides_with_downloads(self):
        time.sleep(2)                       # the simulator ride has a file by now
        page = self.open("/rides")
        page.wait_for_selector(".ride", timeout=5000)
        self.assertIn("In progress", page.inner_text("#list"))
        links = page.eval_on_selector_all(".ride a", "els => els.map(e => e.getAttribute('href'))")
        self.assertTrue(any(h.endswith(".tcx") for h in links), links)
        self.assertTrue(any(h.endswith(".csv") for h in links), links)
        tcx = urllib.request.urlopen(self.base + next(h for h in links if h.endswith(".tcx"))).read()
        self.assertIn(b"<TrainingCenterDatabase", tcx)
        self.assertTrue(page.is_hidden("#power"), "power buttons must be hidden with --no-power")
        self.assertRegex(page.inner_text("#version"), r"Bike computer \d+\.\d+\.\d+")

    def test_ftp_setting_turns_on_zones(self):
        page = self.open("/rides")
        page.fill("#ftp", "200")
        page.click("#ftpform button")
        page.wait_for_function("document.getElementById('ftpmsg').textContent.includes('200')", timeout=5000)
        dash = self.open("/")
        dash.wait_for_selector("#zone.show", timeout=10000)
        self.assertRegex(dash.inner_text("#zone"), r"^Z\d ")
        self.assertIn("IF", dash.inner_text("#stats"))
        self.post("/api/settings", {"ftp": None})

    def test_efficiency_setting(self):
        page = self.open("/rides")
        self.assertEqual(page.input_value("#eff"), "24")
        page.fill("#eff", "20")
        page.click("#effform button")
        page.wait_for_function("document.getElementById('effmsg').textContent.includes('20')", timeout=5000)
        page.fill("#eff", "50")
        page.click("#effform button")
        page.wait_for_function("document.getElementById('effmsg').classList.contains('bad')", timeout=3000)
        self.post("/api/settings", {"efficiency": None})

    def test_ftp_rejects_bad_values(self):
        page = self.open("/rides")
        page.fill("#ftp", "5")
        page.click("#ftpform button")
        page.wait_for_function("document.getElementById('ftpmsg').classList.contains('bad')", timeout=3000)

    def test_pin_is_asked_for(self):
        self.post("/api/settings", {"pin": "2468"})
        try:
            page = self.open("/rides")
            answers = []
            def answer(dialog):
                answers.append(dialog.message)
                dialog.accept("2468")
            page.on("dialog", answer)
            page.fill("#ftp", "230")
            page.click("#ftpform button")
            page.wait_for_function("document.getElementById('ftpmsg').textContent.includes('230')", timeout=5000)
            self.assertEqual(answers, ["Enter the PIN:"])
            page.fill("#ftp", "")                  # the PIN is remembered in this tab
            page.click("#ftpform button")
            page.wait_for_function("document.getElementById('ftpmsg').textContent.includes('cleared')", timeout=5000)
            self.assertEqual(len(answers), 1)
            self.assertFalse(page.is_hidden("#pinclear"))
        finally:
            req = urllib.request.Request(self.base + "/api/settings", data=b'{"pin": null}', method="POST",
                                         headers={"Content-Type": "application/json", "X-Bike-Computer": "1",
                                                  "X-PIN": "2468"})
            urllib.request.urlopen(req, timeout=5)

    def test_header_fits_on_one_line_on_a_phone(self):
        page = self.open("/")
        page.wait_for_function("document.getElementById('status').textContent === 'Receiving'", timeout=10000)
        self.assertLess(page.eval_on_selector("header", "e => e.offsetHeight"), 60)

    def test_heart_rate_line_toggle(self):
        page = self.open("/")
        page.wait_for_selector("#showH:not([hidden])", timeout=10000)
        self.assertEqual(page.get_attribute("#showH", "aria-pressed"), "true")
        page.click("#showH")
        self.assertEqual(page.get_attribute("#showH", "aria-pressed"), "false")

    def test_time_in_zones_bar(self):
        self.post("/api/settings", {"ftp": 200})
        try:
            page = self.open("/")
            page.wait_for_selector("#zonebar:not([hidden]) .bar div", timeout=10000)
            self.assertRegex(page.inner_text("#zonebar"), r"Z\d \d+:\d\d")
        finally:
            self.post("/api/settings", {"ftp": None})

    def test_whole_long_ride_on_the_graph(self):
        # A 4-hour ride with a 10-minute pause and 2 laps, served instead of the real history.
        n = 4 * 3600
        t = [i + 1 + (600 if i > n // 2 else 0) for i in range(n)]
        cols = {"t": t, "p": [150 + (i // 300 % 2) * 100 for i in range(n)], "l": [None] * n,
                "r": [None] * n, "c": [90] * n, "h": [120 + (i // 300 % 2) * 30 for i in range(n)],
                "lap": [1 if i < n // 2 else 2 for i in range(n)]}
        body = json.dumps({"history": cols, "stats": {}})
        ctx = self.browser.new_context(viewport=DESKTOP)
        self.addCleanup(ctx.close)
        page = ctx.new_page()
        errors = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.route("**/api/history", lambda route: route.fulfill(body=body, content_type="application/json"))
        page.goto(self.base + "/")
        page.click("#win button[data-v='0']")                    # All
        page.wait_for_timeout(2500)
        self.assertEqual(errors, [])
        ms = page.evaluate("""() => { const t0 = performance.now();
            for (let i = 0; i < 5; i++) window.dispatchEvent(new Event('resize'));
            return (performance.now() - t0) / 5; }""")
        self.assertLess(ms, 250, f"one redraw of a 4 h ride takes {ms:.0f} ms")
        self.shot(page, "dashboard-long-ride")

    def test_twenty_minute_window(self):
        # 30 minutes of riding: almost 10 hard, then 20 easy, with a heart rate. The hard
        # part ends 10 s before the 20 minute window starts, so it's not on the graph.
        n = 1800
        cols = {"t": list(range(1, n + 1)), "p": [400 if i < 590 else 120 for i in range(n)],
                "l": [None] * n, "r": [None] * n, "c": [90] * n,
                "h": [160 if i < 590 else 130 for i in range(n)], "lap": [1] * n}
        body = json.dumps({"history": cols, "stats": {}})
        ctx = self.browser.new_context(viewport=PHONE)
        self.addCleanup(ctx.close)
        page = ctx.new_page()
        errors = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.route("**/api/history", lambda route: route.fulfill(body=body, content_type="application/json"))
        page.goto(self.base + "/")
        page.click("#win button[data-v='1200']")
        self.assertEqual(page.get_attribute("#win button[data-v='1200']", "aria-pressed"), "true")
        self.assertEqual(page.evaluate("localStorage.getItem('bc.win')"), "1200")
        page.wait_for_timeout(1500)
        self.assertEqual(errors, [])
        # The power scale fits the easy 120 W in the window, not the 400 W before it.
        self.assertEqual(page.get_attribute("#cv", "data-y-max"), "150")
        self.shot(page, "dashboard-20-minutes")
        page.reload()                                            # the choice is remembered
        self.assertEqual(page.get_attribute("#win button[data-v='1200']", "aria-pressed"), "true")

    def test_lap_without_cadence_shows_a_dash(self):
        # A sample whose finished lap has no cadence (a meter without it, or all coasting).
        lap = {"n": 1, "elapsed": 60.0, "avg_p": 150, "np": 150, "max_p": 180, "if": None,
               "avg_hr": None, "avg_c": None}
        msg = {"type": "sample", "t": 61, "p": 150, "l": None, "r": None, "c": None, "h": None,
               "lap": 2, "moving": True, "connected": True, "source": {}, "log_error": None,
               "stats": {"avg_p": 150, "elapsed": 61, "avg_c": None, "laps": [lap],
                         "lap": {**lap, "n": 2, "elapsed": 1.0}}}
        ctx = self.browser.new_context(viewport=PHONE)
        self.addCleanup(ctx.close)
        page = ctx.new_page()
        page.route("**/events", lambda route: route.fulfill(
            body=f"data: {json.dumps(msg)}\n\n", content_type="text/event-stream"))
        page.goto(self.base + "/")
        page.wait_for_selector("#laps:not([hidden]) table", timeout=5000)
        table = page.inner_text("#laps")
        self.assertNotIn("null", table)
        self.assertNotIn("undefined", table)

    def test_intense_interval_timer(self):
        def open_with(intense_s):
            msg = {"type": "sample", "t": 1501, "p": 320, "l": None, "r": None, "c": 105, "h": 172,
                   "lap": 1, "moving": True, "connected": True, "source": {}, "log_error": None,
                   "stats": {"avg_p": 180, "elapsed": 1501, "ftp": 250, "intense_s": intense_s,
                             "laps": [], "lap": {"n": 1, "elapsed": 1501.0}}}
            ctx = self.browser.new_context(viewport=PHONE)
            self.addCleanup(ctx.close)
            page = ctx.new_page()
            errors = []
            page.on("pageerror", lambda e: errors.append(str(e)))
            page.route("**/events", lambda route: route.fulfill(
                body=f"data: {json.dumps(msg)}\n\n", content_type="text/event-stream"))
            page.goto(self.base + "/")
            page.wait_for_function("document.getElementById('time').textContent === '25:01'", timeout=5000)
            self.assertEqual(errors, [])
            return page

        page = open_with(72)
        self.assertTrue(page.is_visible("#intense"))
        self.assertEqual(page.inner_text("#intenseT"), "1:12")
        # Fits in the Time tile next to three others (heart rate is showing).
        fits = page.evaluate("""() => {
            const el = document.getElementById('intense'), tile = el.parentElement;
            return el.scrollWidth <= el.clientWidth && el.getBoundingClientRect().right <= tile.getBoundingClientRect().right;
        }""")
        self.assertTrue(fits)
        self.shot(page, "dashboard-intense-interval")
        self.assertFalse(open_with(None).is_visible("#intense"))

    def test_wrong_pin_is_not_kept(self):
        self.post("/api/settings", {"pin": "1357"})
        try:
            page = self.open("/rides")
            answers = iter(["0000", "0000", "0000",          # first try: 3 wrong PINs
                            "0000", "1357"])                 # second try: wrong, then right
            # 3 + 1 = 4 wrong guesses in all: just under the 5-guess lockout.
            page.on("dialog", lambda d: d.accept(next(answers)))
            page.fill("#ftp", "240")
            page.click("#ftpform button")
            page.wait_for_function("document.getElementById('ftpmsg').classList.contains('bad')", timeout=5000)
            # A refused PIN must not be remembered (it would be sent again by itself).
            self.assertIsNone(page.evaluate("sessionStorage.getItem('bc.pin')"))
            page.click("#ftpform button")
            page.wait_for_function("document.getElementById('ftpmsg').textContent.includes('240')", timeout=5000)
        finally:
            req = urllib.request.Request(self.base + "/api/settings", data=b'{"pin": null, "ftp": null}',
                                         method="POST", headers={"Content-Type": "application/json",
                                                                 "X-Bike-Computer": "1", "X-PIN": "1357"})
            urllib.request.urlopen(req, timeout=5)

    # ------------------------------------------------------------------ screenshots

    def test_layout_fits_the_phone(self):
        for path in ("/", "/rides"):
            page = self.open(path)
            page.wait_for_timeout(2500)
            width = page.evaluate("document.documentElement.scrollWidth")
            self.assertLessEqual(width, PHONE["width"], f"{path} scrolls sideways on a phone")

    def test_screenshots(self):
        if not self.shots:
            self.skipTest("SCREENSHOT_DIR not set")
        self.post("/api/settings", {"ftp": 200})
        try:
            for vp_name, vp in (("phone", PHONE), ("desktop", DESKTOP)):
                for scheme in ("light", "dark"):
                    page = self.open("/", vp, scheme)
                    page.wait_for_timeout(6000)
                    self.shot(page, f"dashboard-{vp_name}-{scheme}")
                    page = self.open("/rides", vp, scheme)
                    page.wait_for_selector(".ride", timeout=5000)
                    self.shot(page, f"rides-{vp_name}-{scheme}")
        finally:
            self.post("/api/settings", {"ftp": None})


if __name__ == "__main__":
    unittest.main()
