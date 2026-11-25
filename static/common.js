// Shared by the dashboard and the Rides page.

// POSTs must carry this header; the server rejects POSTs from other web pages.
const POST_HEADERS = { "Content-Type": "application/json", "X-Bike-Computer": "1" };

// Seconds as 1:05 or 1:02:05.
function fmtTime(s) {
  s = Math.floor(s || 0);
  const h = Math.floor(s / 3600), m = Math.floor(s / 60) % 60, ss = String(s % 60).padStart(2, "0");
  return h ? `${h}:${String(m).padStart(2, "0")}:${ss}` : `${m}:${ss}`;
}

// Food energy burned, estimated from work: 1 kcal = 4.184 kJ and the body turns
// about `eff` % of it into work at the pedals (24 % by default, so kcal ≈ kJ).
// Set on the Rides page, as it varies from rider to rider.
const kcal = (kj, eff) => kj == null ? null : Math.round(kj / (4.184 * (eff || 24) / 100));

// Power zones as a share of FTP: [number, name, upper limit]. Same limits as ride.py.
const ZONES = [[1, "Recovery", .55], [2, "Endurance", .75], [3, "Tempo", .90], [4, "Threshold", 1.05],
               [5, "VO2 max", 1.20], [6, "Anaerobic", 1.50], [7, "Neuromuscular", Infinity]];

// Time in zones: a .zonebar with the bar and a key below it, or "" without any.
// `pct`: also show each zone's share of the time.
function zoneBar(zones, pct) {
  const total = zones ? zones.reduce((a, b) => a + b, 0) : 0;
  if (!total) return "";
  return `<div class="zonebar"><div class="bar" role="img" aria-label="Time in power zones">` +
    zones.map((s, i) => s ? `<div style="flex:${s};background:var(--z${i + 1})" title="Z${i + 1} ${ZONES[i][1]}: ${fmtTime(s)}"></div>` : "").join("") +
    `</div><div class="keys">` +
    zones.map((s, i) => s ? `<span style="--zc:var(--z${i + 1})">Z${i + 1} <b>${fmtTime(s)}</b>` +
      (pct ? ` · ${Math.round(100 * s / total)}%` : "") + `</span>` : "").join("") +
    `</div></div>`;
}
