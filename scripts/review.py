#!/usr/bin/env python3
"""
Local review server for De-Haiku-ifier daily puzzles.

Usage:
    python scripts/review.py

Opens http://localhost:8000 with a review interface to pick daily puzzles
from generated candidates.
"""

import http.server
import json
import re
import subprocess
import sys
import webbrowser
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
CANDIDATES_DIR = REPO_ROOT / "candidates"
PUZZLES_DIR = REPO_ROOT / "puzzles"
CONFIG_DIR = REPO_ROOT / "config"
PORT = 8000


# ── Data helpers ─────────────────────────────────────────────


def get_review_status():
    """Return list of {date, approved, count} for all days with candidates."""
    if not CANDIDATES_DIR.exists():
        return []
    days = []
    for d in sorted(CANDIDATES_DIR.iterdir()):
        if not d.is_dir() or not re.match(r"\d{4}-\d{2}-\d{2}$", d.name):
            continue
        y, m, day = d.name.split("-")
        approved = (PUZZLES_DIR / y / m / f"{day}.json").exists()
        count = len(list(d.glob("*.json")))
        if count > 0:
            days.append({"date": d.name, "approved": approved, "count": count})
    return days


def get_candidates(day_str):
    """Return all candidate puzzles for a given day."""
    day_dir = CANDIDATES_DIR / day_str
    if not day_dir.exists():
        return []
    candidates = []
    for f in sorted(day_dir.glob("*.json"), key=lambda p: int(p.stem)):
        with open(f) as fh:
            data = json.load(fh)
        data["_num"] = int(f.stem)
        candidates.append(data)
    return candidates


def approve_candidate(day_str, pick_num):
    """Approve candidate N for the given day. Writes to puzzles/ and updates banned words."""
    src = CANDIDATES_DIR / day_str / f"{pick_num}.json"
    if not src.exists():
        return {"ok": False, "message": f"Candidate {pick_num} not found for {day_str}"}

    with open(src) as f:
        puzzle = json.load(f)

    # Clean internal fields
    puzzle.pop("_num", None)

    y, m, d = day_str.split("-")
    out_dir = PUZZLES_DIR / y / m
    out_dir.mkdir(parents=True, exist_ok=True)
    out_file = out_dir / f"{d}.json"

    with open(out_file, "w") as f:
        json.dump(puzzle, f, indent=2)
        f.write("\n")

    # Update banned words (mirrors logic from generate.py)
    _update_banned_words(puzzle)

    return {"ok": True, "message": f"Approved → puzzles/{y}/{m}/{d}.json"}


def _update_banned_words(puzzle):
    """Add the approved haiku's first word to the banned list."""
    banned_file = CONFIG_DIR / "banned-words.json"
    with open(banned_file) as f:
        data = json.load(f)

    banned = data["words"]
    max_size = data["max_size"]

    first_line = puzzle["haiku"].split("\n")[0]
    words = first_line.split()
    if words:
        first_word = re.sub(r"[^a-z]", "", words[0].lower())
        if first_word and first_word not in banned:
            banned.append(first_word)
    if len(banned) > max_size:
        banned = banned[-max_size:]

    with open(banned_file, "w") as f:
        json.dump({"words": banned, "max_size": max_size}, f, indent=2)
        f.write("\n")


def regenerate_candidates(day_str, seeds_csv="", theme=""):
    """Re-run generate.py for a specific day. Optionally with seed words and/or theme."""
    cmd = [
        sys.executable,
        str(REPO_ROOT / "scripts" / "generate.py"),
        "--day", day_str,
        "--force",
    ]
    if seeds_csv.strip():
        cmd.extend(["--seeds", seeds_csv.strip()])
    if theme.strip():
        cmd.extend(["--themes", theme.strip()])

    try:
        result = subprocess.run(
            cmd, cwd=REPO_ROOT, capture_output=True, text=True, timeout=600
        )
        if result.returncode == 0:
            return {"ok": True, "message": "Regenerated candidates.", "log": result.stdout}
        else:
            return {
                "ok": False,
                "message": "Generation failed.",
                "log": result.stderr or result.stdout,
            }
    except subprocess.TimeoutExpired:
        return {"ok": False, "message": "Generation timed out (10 min limit)."}


def git_commit_and_push():
    """Stage approved puzzles + banned-words, commit, and push."""
    try:
        subprocess.run(
            ["git", "add", "puzzles/", "config/banned-words.json"],
            cwd=REPO_ROOT,
            check=True,
            capture_output=True,
        )
        result = subprocess.run(
            ["git", "diff", "--cached", "--quiet"], cwd=REPO_ROOT
        )
        if result.returncode == 0:
            return {"ok": True, "message": "Nothing new to push."}

        subprocess.run(
            ["git", "commit", "-m", "Approve daily puzzles"],
            cwd=REPO_ROOT,
            check=True,
            capture_output=True,
        )
        subprocess.run(
            ["git", "push"],
            cwd=REPO_ROOT,
            check=True,
            capture_output=True,
        )
        return {"ok": True, "message": "Committed and pushed!"}
    except subprocess.CalledProcessError as e:
        err = e.stderr.decode().strip() if e.stderr else str(e)
        return {"ok": False, "message": f"Git error: {err}"}


# ── HTTP handler ─────────────────────────────────────────────


class ReviewHandler(http.server.BaseHTTPRequestHandler):

    def do_GET(self):
        if self.path == "/":
            self._serve_html()
        elif self.path == "/api/status":
            self._json(get_review_status())
        elif self.path.startswith("/api/candidates/"):
            day = self.path.rsplit("/", 1)[-1]
            self._json(get_candidates(day))
        else:
            self.send_error(404)

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(length)) if length else {}
        if self.path == "/api/approve":
            self._json(approve_candidate(body["date"], body["pick"]))
        elif self.path == "/api/regenerate":
            self._json(regenerate_candidates(
                body["date"], body.get("seeds", ""), body.get("theme", "")
            ))
        elif self.path == "/api/push":
            self._json(git_commit_and_push())
        else:
            self.send_error(404)

    def _json(self, data):
        payload = json.dumps(data).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def _serve_html(self):
        payload = PAGE_HTML.encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, fmt, *args):
        # Keep terminal quiet — only log errors
        if args and str(args[1]).startswith("4"):
            super().log_message(fmt, *args)


# ── HTML ─────────────────────────────────────────────────────


PAGE_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>De-Haiku Review</title>
<style>
:root {
  --bg: #f8f7f5;
  --surface: #fff;
  --border: #e5e2de;
  --accent: #2563eb;
  --accent-h: #1d4ed8;
  --green: #16a34a;
  --green-bg: #dcfce7;
  --green-bdr: #86efac;
  --text: #111;
  --muted: #6b7280;
  --ans-bg: #dbeafe;
  --ans-text: #1e40af;
  --dec-bg: #f3f4f6;
  --dec-text: #4b5563;
}
*{margin:0;padding:0;box-sizing:border-box}
body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;background:var(--bg);color:var(--text);min-height:100vh}

/* ── Header ─────────────────────────────── */
header{background:#111;color:#fff;padding:14px 24px;display:flex;align-items:center;justify-content:space-between;position:sticky;top:0;z-index:100}
header h1{font-size:16px;font-weight:600;letter-spacing:.02em}
.push-btn{padding:7px 18px;background:var(--accent);color:#fff;border:none;border-radius:6px;font-size:13px;font-weight:600;cursor:pointer;transition:background .15s}
.push-btn:hover{background:var(--accent-h)}
.push-btn:disabled{opacity:.5;cursor:not-allowed}

/* ── Day nav ────────────────────────────── */
.day-nav{padding:12px 24px;display:flex;gap:6px;overflow-x:auto;background:var(--surface);border-bottom:1px solid var(--border)}
.day-pill{padding:5px 14px;border:1px solid var(--border);border-radius:20px;background:var(--surface);font-size:13px;cursor:pointer;white-space:nowrap;transition:all .15s;color:var(--text);font-family:inherit}
.day-pill:hover{border-color:var(--accent)}
.day-pill.active{background:var(--accent);color:#fff;border-color:var(--accent)}
.day-pill.done{background:var(--green-bg);border-color:var(--green-bdr);color:#166534}
.day-pill.active.done{background:var(--green);color:#fff;border-color:var(--green)}

/* ── Date header ────────────────────────── */
.date-hdr{text-align:center;padding:28px 24px 8px}
.date-hdr h2{font-size:22px;font-weight:700}
.date-hdr .meta{color:var(--muted);font-size:14px;margin-top:4px}

/* ── Grid ───────────────────────────────── */
.grid{display:grid;grid-template-columns:repeat(2,1fr);gap:16px;padding:16px 24px 48px;max-width:1100px;margin:0 auto}
@media(max-width:720px){.grid{grid-template-columns:1fr}}

/* ── Card ───────────────────────────────── */
.card{background:var(--surface);border:2px solid var(--border);border-radius:12px;padding:20px;display:flex;flex-direction:column;transition:border-color .15s,box-shadow .15s}
.card:hover{border-color:#b0b0b0;box-shadow:0 2px 12px rgba(0,0,0,.06)}
.card-num{font-size:11px;font-weight:700;text-transform:uppercase;letter-spacing:.06em;color:var(--muted)}
.haiku{font-family:Georgia,'Times New Roman',serif;font-style:italic;font-size:15px;line-height:2;text-align:center;padding:14px 8px;margin:10px 0 14px;border-top:1px solid var(--border);border-bottom:1px solid var(--border);white-space:pre-line}
.sec-label{font-size:10px;font-weight:700;text-transform:uppercase;letter-spacing:.06em;color:var(--muted);margin-bottom:5px}
.pills{display:flex;flex-wrap:wrap;gap:4px;margin-bottom:12px}
.pill{padding:2px 9px;border-radius:10px;font-size:12px;font-weight:500}
.pill.ans{background:var(--ans-bg);color:var(--ans-text)}
.pill.dec{background:var(--dec-bg);color:var(--dec-text)}
.pick-btn{margin-top:auto;padding:10px;background:var(--accent);color:#fff;border:none;border-radius:8px;font-size:14px;font-weight:600;cursor:pointer;transition:background .15s;font-family:inherit}
.pick-btn:hover{background:var(--accent-h)}

/* ── States ─────────────────────────────── */
.state{grid-column:1/-1;text-align:center;padding:60px 24px;color:var(--muted);font-size:15px;line-height:1.7}
.state .icon{font-size:48px;margin-bottom:12px}
.state.ok{color:var(--green)}

/* ── Toast ──────────────────────────────── */
.toast{position:fixed;bottom:24px;left:50%;transform:translateX(-50%);padding:10px 24px;background:#111;color:#fff;border-radius:8px;font-size:14px;font-weight:500;z-index:200;opacity:0;transition:opacity .3s;pointer-events:none}
.toast.show{opacity:1}

/* ── Progress ───────────────────────────── */
.progress{text-align:center;padding:0 24px;font-size:13px;color:var(--muted)}

/* ── Regen bar ─────────────────────────── */
.regen{display:flex;align-items:center;gap:8px;justify-content:center;padding:12px 24px;flex-wrap:wrap}
.regen input{padding:6px 12px;border:1px solid var(--border);border-radius:6px;font-size:13px;width:260px;font-family:inherit}
.regen input:focus{outline:none;border-color:var(--accent)}
.regen-btn{padding:6px 16px;background:#f97316;color:#fff;border:none;border-radius:6px;font-size:13px;font-weight:600;cursor:pointer;font-family:inherit;transition:background .15s}
.regen-btn:hover{background:#ea580c}
.regen-btn:disabled{opacity:.5;cursor:not-allowed}
.regen .hint{font-size:11px;color:var(--muted);width:100%;text-align:center}
</style>
</head>
<body>

<header>
  <h1>De-Haiku Review</h1>
  <button class="push-btn" onclick="pushToGithub()">Commit &amp; Push</button>
</header>

<nav class="day-nav" id="dayNav"></nav>
<div class="date-hdr" id="dateHdr"></div>
<div class="progress" id="progress"></div>
<div class="regen" id="regen" style="display:none">
  <input type="text" id="themeInput" placeholder="Optional theme override (e.g. winter wonderland)">
  <input type="text" id="seedInput" placeholder="Optional seed words (e.g. tree, gift, snow)">
  <button class="regen-btn" id="regenBtn" onclick="regenerate()">Regenerate</button>
  <div class="hint">Re-rolls all 8 candidates. Theme overrides the rotation; seed words get mixed into the 12-word pool.</div>
</div>
<div class="grid" id="grid"></div>
<div class="toast" id="toast"></div>

<script>
const S = { days: [], cur: null };

/* ── API ────────────────────────────────── */
async function api(path, opts) {
  const r = await fetch(path, opts);
  return r.json();
}

/* ── Init ───────────────────────────────── */
async function init() {
  S.days = await api('/api/status');
  renderNav();
  const first = S.days.find(d => !d.approved) || S.days[0];
  if (first) selectDay(first.date);
  else {
    document.getElementById('dateHdr').innerHTML = '';
    document.getElementById('grid').innerHTML =
      '<div class="state">No candidates found.<br>Run: <code>python scripts/generate.py</code></div>';
  }
  renderProgress();
}

/* ── Nav ────────────────────────────────── */
function renderNav() {
  document.getElementById('dayNav').innerHTML = S.days.map(d => {
    const cls = ['day-pill'];
    if (d.date === S.cur) cls.push('active');
    if (d.approved) cls.push('done');
    const label = shortDate(d.date);
    return '<button class="' + cls.join(' ') + '" onclick="selectDay(\'' + d.date + '\')">'
      + (d.approved ? '✓ ' : '') + label + '</button>';
  }).join('');
}

function renderProgress() {
  const total = S.days.length;
  const done = S.days.filter(d => d.approved).length;
  const el = document.getElementById('progress');
  if (total === 0) { el.textContent = ''; return; }
  el.textContent = done + ' / ' + total + ' days approved';
}

/* ── Select day ─────────────────────────── */
async function selectDay(day) {
  S.cur = day;
  renderNav();
  const info = S.days.find(d => d.date === day);
  const hdr = document.getElementById('dateHdr');
  const grid = document.getElementById('grid');

  var regenBar = document.getElementById('regen');

  if (info && info.approved) {
    hdr.innerHTML = '<h2>' + longDate(day) + '</h2><div class="meta">Approved</div>';
    grid.innerHTML = '<div class="state ok"><div class="icon">✓</div>Puzzle approved for this day.</div>';
    regenBar.style.display = 'none';
    return;
  }

  regenBar.style.display = 'flex';
  const candidates = await api('/api/candidates/' + day);
  if (!candidates.length) {
    hdr.innerHTML = '<h2>' + longDate(day) + '</h2>';
    grid.innerHTML = '<div class="state">No candidates for this day.</div>';
    return;
  }

  const theme = candidates[0].theme || '';
  hdr.innerHTML = '<h2>' + longDate(day) + '</h2>'
    + '<div class="meta">Theme: ' + esc(theme) + ' · ' + candidates.length + ' options</div>';

  grid.innerHTML = candidates.map(function(c) {
    const n = c._num;
    return '<div class="card">'
      + '<div class="card-num">Option ' + n + '</div>'
      + '<div class="haiku">' + esc(c.haiku) + '</div>'
      + '<div class="sec-label">Answers (' + c.words.length + ')</div>'
      + '<div class="pills">' + c.words.map(function(w) { return '<span class="pill ans">' + esc(w) + '</span>'; }).join('') + '</div>'
      + '<div class="sec-label">Decoys (' + c.decoys.length + ')</div>'
      + '<div class="pills">' + c.decoys.map(function(w) { return '<span class="pill dec">' + esc(w) + '</span>'; }).join('') + '</div>'
      + '<button class="pick-btn" onclick="pick(\'' + day + '\',' + n + ')">Pick #' + n + '</button>'
      + '</div>';
  }).join('');
}

/* ── Pick ───────────────────────────────── */
async function pick(day, num) {
  if (!confirm('Approve option ' + num + ' for ' + longDate(day) + '?')) return;
  const res = await api('/api/approve', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({date: day, pick: num})
  });
  if (res.ok) {
    toast(res.message);
    S.days = await api('/api/status');
    renderNav();
    renderProgress();
    const next = S.days.find(function(d) { return !d.approved; });
    selectDay(next ? next.date : day);
  } else {
    alert('Error: ' + res.message);
  }
}

/* ── Regenerate ─────────────────────────── */
async function regenerate() {
  const day = S.cur;
  if (!day) return;
  const seeds = document.getElementById('seedInput').value.trim();
  const theme = document.getElementById('themeInput').value.trim();
  const parts = [];
  if (theme) parts.push('theme "' + theme + '"');
  if (seeds) parts.push('seeds "' + seeds + '"');
  const label = parts.length ? ' with ' + parts.join(' and ') : '';
  if (!confirm('Regenerate all candidates for ' + longDate(day) + label + '?\n\nThis calls the Claude API and may take a minute.')) return;
  const btn = document.getElementById('regenBtn');
  btn.textContent = 'Generating…';
  btn.disabled = true;
  const res = await api('/api/regenerate', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({date: day, seeds: seeds, theme: theme})
  });
  btn.textContent = 'Regenerate';
  btn.disabled = false;
  if (res.ok) {
    toast(res.message);
    S.days = await api('/api/status');
    renderNav();
    renderProgress();
    selectDay(day);
  } else {
    alert('Generation failed:\n\n' + (res.log || res.message));
  }
}

/* ── Push ───────────────────────────────── */
async function pushToGithub() {
  if (!confirm('Commit and push all approved puzzles to GitHub?')) return;
  const btn = document.querySelector('.push-btn');
  btn.textContent = 'Pushing…';
  btn.disabled = true;
  const res = await api('/api/push', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: '{}'
  });
  btn.textContent = 'Commit & Push';
  btn.disabled = false;
  toast(res.message);
}

/* ── Helpers ────────────────────────────── */
function shortDate(s) {
  return new Date(s + 'T12:00:00').toLocaleDateString('en-US', {month:'short', day:'numeric'});
}
function longDate(s) {
  return new Date(s + 'T12:00:00').toLocaleDateString('en-US', {weekday:'long', month:'long', day:'numeric', year:'numeric'});
}
function esc(s) {
  const d = document.createElement('div');
  d.textContent = s;
  return d.innerHTML;
}
function toast(msg) {
  const el = document.getElementById('toast');
  el.textContent = msg;
  el.classList.add('show');
  clearTimeout(toast._t);
  toast._t = setTimeout(function() { el.classList.remove('show'); }, 3000);
}

init();
</script>
</body>
</html>
"""


# ── Main ─────────────────────────────────────────────────────


if __name__ == "__main__":
    if not CANDIDATES_DIR.exists() or not any(CANDIDATES_DIR.iterdir()):
        print("No candidates found. Run 'python scripts/generate.py' first.")
        print("Starting server anyway...\n")

    print(f"Review server → http://localhost:{PORT}")
    print("Press Ctrl+C to stop.\n")

    server = http.server.HTTPServer(("127.0.0.1", PORT), ReviewHandler)
    webbrowser.open(f"http://localhost:{PORT}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")
