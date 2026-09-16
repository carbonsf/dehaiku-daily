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
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import webbrowser
from datetime import date
from pathlib import Path
from urllib.parse import parse_qs, urlparse

REPO_ROOT = Path(__file__).resolve().parent.parent
CANDIDATES_DIR = REPO_ROOT / "candidates"
PUZZLES_DIR = REPO_ROOT / "puzzles"
CONFIG_DIR = REPO_ROOT / "config"
PORT = 8000


# ── Data helpers ─────────────────────────────────────────────


def purge_past_candidates():
    """Remove candidate dirs for dates before today."""
    if not CANDIDATES_DIR.exists():
        return
    today_str = date.today().isoformat()
    for d in list(CANDIDATES_DIR.iterdir()):
        if not d.is_dir() or not re.match(r"\d{4}-\d{2}-\d{2}$", d.name):
            continue
        if d.name < today_str:
            shutil.rmtree(d)


def get_review_status():
    """Return list of {date, approved, count} for today + future days."""
    today_str = date.today().isoformat()
    seen = set()
    days = []

    # Days with candidates
    if CANDIDATES_DIR.exists():
        for d in sorted(CANDIDATES_DIR.iterdir()):
            if not d.is_dir() or not re.match(r"\d{4}-\d{2}-\d{2}$", d.name):
                continue
            if d.name < today_str:
                continue
            y, m, day = d.name.split("-")
            approved = (PUZZLES_DIR / y / m / f"{day}.json").exists()
            count = len(list(d.glob("*.json")))
            if count > 0:
                days.append({"date": d.name, "approved": approved, "count": count})
                seen.add(d.name)

    # Approved days without candidate dirs (candidates purged after commit)
    if PUZZLES_DIR.exists():
        for year_dir in sorted(PUZZLES_DIR.iterdir()):
            if not year_dir.is_dir() or not re.match(r"\d{4}$", year_dir.name):
                continue
            for month_dir in sorted(year_dir.iterdir()):
                if not month_dir.is_dir() or not re.match(r"\d{2}$", month_dir.name):
                    continue
                for pf in sorted(month_dir.glob("*.json")):
                    ds = f"{year_dir.name}-{month_dir.name}-{pf.stem}"
                    if ds < today_str or ds in seen:
                        continue
                    days.append({"date": ds, "approved": True, "count": 0})

    days.sort(key=lambda x: x["date"])
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


def get_approved_puzzle(day_str):
    """Return the approved puzzle for a day, or None."""
    y, m, d = day_str.split("-")
    puzzle_file = PUZZLES_DIR / y / m / f"{d}.json"
    if not puzzle_file.exists():
        return None
    with open(puzzle_file) as f:
        return json.load(f)


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


def unapprove_day(day_str):
    """Remove the approved puzzle for a day so it can be re-picked."""
    y, m, d = day_str.split("-")
    puzzle_file = PUZZLES_DIR / y / m / f"{d}.json"
    if not puzzle_file.exists():
        return {"ok": False, "message": f"No approved puzzle for {day_str}"}
    puzzle_file.unlink()
    # Clean up empty parent dirs
    for parent in [puzzle_file.parent, puzzle_file.parent.parent]:
        if parent != PUZZLES_DIR and parent.is_dir() and not any(parent.iterdir()):
            parent.rmdir()
    return {"ok": True, "message": f"Unapproved {day_str} — pick again"}


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


# ── Background generation job ────────────────────────────────
# generate.py runs as a subprocess in a background thread. Its stdout is
# captured line by line so the UI can poll /api/regenerate/status and
# render live progress. One job at a time. Because it is a fresh
# subprocess each time, it always runs the current generate.py on disk.

JOB_LOCK = threading.Lock()
JOB = {
    "running": False, "date": None, "lines": [], "done": False,
    "ok": None, "message": "", "started": 0.0, "proc": None,
}


def start_regeneration(day_str, seeds_csv="", theme=""):
    """Kick off generate.py for one day in the background."""
    with JOB_LOCK:
        if JOB["running"]:
            return {"ok": False, "message": f"Already generating {JOB['date']}."}

    # Resolve the key here so a missing key is a clear message instead of
    # a traceback, and pass it explicitly — this server may have been
    # started from a shell that never exported it.
    from generate import load_api_key
    api_key = load_api_key()
    if not api_key:
        return {
            "ok": False,
            "message": (
                "ANTHROPIC_API_KEY not set. Export it in the shell that runs "
                "review.py, or put ANTHROPIC_API_KEY=sk-ant-... in a .env "
                "file at the repo root, then restart review.py."
            ),
        }

    # -u = unbuffered stdout so lines arrive as they are printed
    cmd = [
        sys.executable, "-u",
        str(REPO_ROOT / "scripts" / "generate.py"),
        "--day", day_str,
        "--force",
    ]
    if seeds_csv.strip():
        cmd.extend(["--seeds", seeds_csv.strip()])
    if theme.strip():
        cmd.extend(["--themes", theme.strip()])

    proc = subprocess.Popen(
        cmd, cwd=REPO_ROOT,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, bufsize=1,
        env={**os.environ, "ANTHROPIC_API_KEY": api_key},
    )
    with JOB_LOCK:
        JOB.update(
            running=True, date=day_str, lines=[], done=False, ok=None,
            message="", started=time.time(), proc=proc,
        )

    def pump():
        for line in proc.stdout:
            with JOB_LOCK:
                JOB["lines"].append(line.rstrip("\n"))
        code = proc.wait()
        with JOB_LOCK:
            JOB["running"] = False
            JOB["done"] = True
            JOB["ok"] = code == 0
            if code == 0:
                JOB["message"] = "Regenerated candidates."
            elif code < 0:
                JOB["message"] = "Generation cancelled."
            else:
                JOB["message"] = f"Generation failed (exit {code})."

    threading.Thread(target=pump, daemon=True).start()
    return {"ok": True, "message": f"Generating {day_str}…"}


def regeneration_status(since=0):
    """Snapshot of the current/last job; `since` skips lines already sent."""
    with JOB_LOCK:
        return {
            "running": JOB["running"],
            "date": JOB["date"],
            "done": JOB["done"],
            "ok": JOB["ok"],
            "message": JOB["message"],
            "elapsed": (time.time() - JOB["started"]) if JOB["started"] else 0,
            "lines": JOB["lines"][since:],
            "total": len(JOB["lines"]),
        }


def cancel_regeneration():
    with JOB_LOCK:
        proc = JOB["proc"] if JOB["running"] else None
    if proc is None:
        return {"ok": False, "message": "Nothing is generating."}
    proc.terminate()
    return {"ok": True, "message": "Stopping…"}


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
        elif self.path.startswith("/api/puzzle/"):
            day = self.path.rsplit("/", 1)[-1]
            puzzle = get_approved_puzzle(day)
            self._json(puzzle if puzzle else {"error": "not found"})
        elif self.path.startswith("/api/regenerate/status"):
            qs = parse_qs(urlparse(self.path).query)
            since = int(qs.get("since", ["0"])[0] or 0)
            self._json(regeneration_status(since))
        else:
            self.send_error(404)

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(length)) if length else {}
        if self.path == "/api/approve":
            self._json(approve_candidate(body["date"], body["pick"]))
        elif self.path == "/api/unapprove":
            self._json(unapprove_day(body["date"]))
        elif self.path == "/api/regenerate":
            self._json(start_regeneration(
                body["date"], body.get("seeds", ""), body.get("theme", "")
            ))
        elif self.path == "/api/regenerate/cancel":
            self._json(cancel_regeneration())
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
.card.approved-card{border-color:var(--green-bdr);background:var(--green-bg);max-width:420px}
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

/* ── Live generation panel ─────────────── */
.gen{max-width:720px;margin:4px 24px 8px;padding:14px 18px 12px;background:#111;color:#fff;border-radius:12px;font-size:13px}
@media(min-width:768px){.gen{margin:4px auto 8px}}
.gen[hidden]{display:none}
.gen-top{display:flex;align-items:center;gap:10px;margin-bottom:12px}
.gen-dot{width:8px;height:8px;border-radius:50%;background:#f97316;flex:none;animation:genPulse 1.2s ease-out infinite}
.gen.done .gen-dot{background:var(--green);animation:none}
.gen.fail .gen-dot{background:#dc2626;animation:none}
@keyframes genPulse{0%{box-shadow:0 0 0 0 rgba(249,115,22,.7)}100%{box-shadow:0 0 0 10px rgba(249,115,22,0)}}
.gen-title{font-weight:600;flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.gen-elapsed{font-variant-numeric:tabular-nums;color:#9ca3af}
.gen-stop{padding:4px 12px;background:transparent;color:#9ca3af;border:1px solid #374151;border-radius:6px;font-size:12px;cursor:pointer;font-family:inherit;transition:all .15s}
.gen-stop:hover{color:#fff;border-color:#fff}
.gen-stop[hidden]{display:none}
.gen-segs{display:grid;grid-template-columns:repeat(8,1fr);gap:5px;margin-bottom:10px}
.seg{height:10px;border-radius:5px;background:#27272a;overflow:hidden;position:relative}
.seg .fill{position:absolute;top:0;left:0;bottom:0;width:0;background:#f97316;border-radius:5px;transition:width .6s cubic-bezier(.22,1,.36,1)}
.seg.saved .fill{background:var(--green)}
.seg.active .fill{background:linear-gradient(90deg,#f97316,#fbbf24,#f97316);background-size:200% 100%;animation:genShimmer 1.1s linear infinite}
.seg.active.retry .fill{background:linear-gradient(90deg,#f59e0b,#fde68a,#f59e0b);background-size:200% 100%;animation:genShimmer .5s linear infinite}
@keyframes genShimmer{0%{background-position:200% 0}100%{background-position:-200% 0}}
.gen.done .seg .fill,.gen.fail .seg .fill{animation:none}
.gen.fail .seg.active .fill{background:#dc2626}
.gen-status{display:flex;justify-content:space-between;gap:12px;margin-bottom:8px}
.gen-status .stage{font-weight:500;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.gen-status .count{color:#9ca3af;font-variant-numeric:tabular-nums;flex:none}
.gen-feed{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:11.5px;line-height:1.55;color:#9ca3af;border-top:1px solid #27272a;padding-top:8px}
.gen-feed .ln{white-space:pre-wrap;word-break:break-word;opacity:.6}
.gen-feed .ln:last-child{opacity:1;color:#fff}
.gen-feed .ln.good{color:#4ade80}
.gen-feed .ln.warn{color:#fbbf24}
.gen-feed .ln.bad{color:#f87171}
.gen-feed .ln.verse{font-family:Georgia,'Times New Roman',serif;font-style:italic;font-size:12.5px;color:#e5e7eb;padding-left:12px}

/* ── Unapprove ─────────────────────────── */
.unapprove-btn{margin-top:12px;padding:6px 18px;background:transparent;color:var(--muted);border:1px solid var(--border);border-radius:6px;font-size:13px;cursor:pointer;font-family:inherit;transition:all .15s}
.unapprove-btn:hover{color:#dc2626;border-color:#dc2626}
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
<div class="gen" id="gen" hidden>
  <div class="gen-top">
    <span class="gen-dot"></span>
    <span class="gen-title" id="genTitle">Generating…</span>
    <span class="gen-elapsed" id="genElapsed">0:00</span>
    <button class="gen-stop" id="genStop" onclick="cancelGeneration()">Stop</button>
  </div>
  <div class="gen-segs" id="genSegs"></div>
  <div class="gen-status" id="genStatus"></div>
  <div class="gen-feed" id="genFeed"></div>
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
  // If the page was refreshed mid-generation, pick the panel back up.
  const st = await api('/api/regenerate/status');
  if (st.running) {
    selectDay(st.date);
    beginGenPanel(st.date, st.elapsed);
    pollGeneration();
  }
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
    var puzzle = await api('/api/puzzle/' + day);
    if (puzzle && !puzzle.error) {
      hdr.innerHTML = '<h2>' + longDate(day) + '</h2>'
        + '<div class="meta">Approved · Theme: ' + esc(puzzle.theme || '—') + '</div>';
      grid.innerHTML = '<div class="card approved-card">'
        + '<div class="card-num">Approved Puzzle</div>'
        + '<div class="haiku">' + esc(puzzle.haiku) + '</div>'
        + '<div class="sec-label">Answers (' + puzzle.words.length + ')</div>'
        + '<div class="pills">' + puzzle.words.map(function(w) { return '<span class="pill ans">' + esc(w) + '</span>'; }).join('') + '</div>'
        + '<div class="sec-label">Decoys (' + puzzle.decoys.length + ')</div>'
        + '<div class="pills">' + puzzle.decoys.map(function(w) { return '<span class="pill dec">' + esc(w) + '</span>'; }).join('') + '</div>'
        + '<button class="unapprove-btn" onclick="unapprove(\'' + day + '\')">Unapprove</button>'
        + '</div>';
    } else {
      hdr.innerHTML = '<h2>' + longDate(day) + '</h2><div class="meta">Approved</div>';
      grid.innerHTML = '<div class="state ok"><div class="icon">✓</div>Puzzle approved.'
        + '<br><button class="unapprove-btn" onclick="unapprove(\'' + day + '\')">Unapprove</button></div>';
    }
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

/* ── Regenerate (live progress) ─────────── */
// generate.py runs as a subprocess on the server; we poll its captured
// stdout and parse the lines it prints into a stage + progress fraction.
const G = { timer: null, since: 0, saved: 0, cur: 0, frac: 0, stage: '',
            retry: false, all: [], day: null, start: 0 };
const TOTAL = 8;

async function regenerate() {
  const day = S.cur;
  if (!day) return;
  const seeds = document.getElementById('seedInput').value.trim();
  const theme = document.getElementById('themeInput').value.trim();
  const parts = [];
  if (theme) parts.push('theme "' + theme + '"');
  if (seeds) parts.push('seeds "' + seeds + '"');
  const label = parts.length ? ' with ' + parts.join(' and ') : '';
  if (!confirm('Regenerate all candidates for ' + longDate(day) + label + '?\n\nThis calls the Claude API. Progress shows live below.')) return;
  const res = await api('/api/regenerate', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({date: day, seeds: seeds, theme: theme})
  });
  if (!res.ok) { alert(res.message); return; }
  beginGenPanel(day, 0);
  pollGeneration();
}

function beginGenPanel(day, elapsedSec) {
  Object.assign(G, { since: 0, saved: 0, cur: 0, frac: 0, retry: false, all: [],
                     day: day, stage: 'Starting generator…',
                     start: Date.now() - (elapsedSec || 0) * 1000 });
  const gen = document.getElementById('gen');
  gen.classList.remove('done', 'fail');
  gen.hidden = false;
  document.getElementById('genTitle').textContent = 'Generating ' + TOTAL + ' candidates for ' + longDate(day);
  document.getElementById('genStop').hidden = false;
  document.getElementById('genSegs').innerHTML = '<div class="seg"><div class="fill"></div></div>'.repeat(TOTAL);
  document.getElementById('genFeed').innerHTML = '';
  const btn = document.getElementById('regenBtn');
  btn.textContent = 'Generating…';
  btn.disabled = true;
  document.getElementById('grid').innerHTML =
    '<div class="state">Candidates will appear here as each one passes the gate…</div>';
  renderGen();
  clearInterval(G.timer);
  G.timer = setInterval(tickGen, 250);
  tickGen();
}

function tickGen() {
  const s = Math.max(0, Math.floor((Date.now() - G.start) / 1000));
  document.getElementById('genElapsed').textContent =
    Math.floor(s / 60) + ':' + String(s % 60).padStart(2, '0');
}

async function pollGeneration() {
  let st;
  try { st = await api('/api/regenerate/status?since=' + G.since); }
  catch (e) { setTimeout(pollGeneration, 1500); return; }
  G.since = st.total;
  let landed = false;
  st.lines.forEach(function(l) { if (ingestLine(l)) landed = true; });
  renderGen();
  if (landed) selectDay(G.day);            // cards appear as they land
  if (st.running) { setTimeout(pollGeneration, 600); return; }
  finishGen(st);
}

// Parse one stdout line from generate.py. Returns true when a candidate
// was saved (so the grid can refresh).
function ingestLine(raw) {
  const t = raw.trim();
  if (!t) return false;
  let cls = '', saved = false, m;
  const who = function() { return 'Candidate ' + G.cur + ' · '; };
  if ((m = t.match(/^Candidate (\d+)\/(\d+)/))) {
    G.cur = +m[1]; G.frac = 0.05; G.retry = false; G.stage = who() + 'picking an angle';
  } else if ((m = t.match(/^Word pool attempt (\d+)\/(\d+)/))) {
    G.frac = 0.1; G.retry = +m[1] > 1;
    G.stage = who() + 'drawing 12 words' + (+m[1] > 1 ? ' (fresh pool ' + m[1] + '/' + m[2] + ')' : '');
  } else if (/^Answers:/.test(t)) {
    G.frac = 0.18; G.stage = who() + 'writing the haiku';
  } else if (/^Pool \(12\)|^Decoys:/.test(t)) {
    /* informational */
  } else if (/^Haiku:/.test(t)) {
    G.frac = 0.45; G.retry = false; G.stage = who() + 'haiku written — checking';
  } else if (/^Structure rejected|^Craft probe failed|^Haiku leaked|^Haiku rejected/.test(t)) {
    cls = 'warn'; G.retry = true; G.frac = 0.3;
    const body = t.replace(/\s*\(try \d+\/\d+\):?\s*/, ' ').trim();
    let why;
    if ((m = body.match(/^Structure rejected\s*(.*)/))) why = 'structure: ' + m[1];
    else if ((m = body.match(/^Craft probe failed\s*(.*)/))) why = 'craft: ' + m[1];
    else if ((m = body.match(/^Haiku leaked\s*\[(.*?)\]/))) why = 'leaked ' + m[1].replace(/'/g, '');
    else if ((m = body.match(/Syllable count (\S+)/))) why = 'syllables ' + m[1] + ', need 5/7/5';
    else why = body.replace(/^Haiku rejected\s*/, '');
    G.stage = who() + 'rewriting — ' + why;
  } else if ((m = t.match(/^Gate check \((\d+)\/(\d+)\)/))) {
    G.frac = 0.55 + 0.09 * (+m[1]); G.retry = false;
    G.stage = who() + 'solver probes ' + m[1] + '/' + m[2];
  } else if (/^Gate failed/.test(t)) {
    cls = 'warn'; G.retry = true;
    G.stage = who() + 'gate: ' + t.replace(/^Gate failed\s*[—-]?\s*/, '') + ' — rewriting';
  } else if (/^Gate: fair but too obvious/.test(t)) {
    cls = 'warn'; G.retry = true; G.stage = who() + 'fair but too obvious — rewriting';
  } else if (/^Gate passed/.test(t)) {
    cls = 'good'; G.frac = 0.97; G.retry = false; G.stage = who() + 'passed ✓';
  } else if (/^Gate budget exhausted|^Could not produce|^All pools exhausted/.test(t)) {
    cls = 'warn'; G.retry = true; G.stage = who() + t.charAt(0).toLowerCase() + t.slice(1);
  } else if (/^Saved →/.test(t)) {
    cls = 'good'; G.saved = G.cur; G.frac = 0; saved = true;
  } else if (/^Done\./.test(t)) {
    cls = 'good';
  } else if (/^Traceback|Error/.test(t)) {
    cls = 'bad';
  } else if (/^ {4}\S/.test(raw) && !/^\s*\[/.test(raw)) {
    cls = 'verse';                          // the three printed haiku lines
  }
  G.all.push({ text: t, cls: cls });
  return saved;
}

function renderGen() {
  const segs = document.getElementById('genSegs').children;
  for (let i = 0; i < segs.length; i++) {
    const n = i + 1, seg = segs[i], fill = seg.firstChild;
    let cls = 'seg', w = 0;
    if (n <= G.saved) { cls += ' saved'; w = 100; }
    else if (n === G.cur) { cls += ' active' + (G.retry ? ' retry' : ''); w = Math.round(G.frac * 100); }
    seg.className = cls;
    fill.style.width = w + '%';
  }
  document.getElementById('genStatus').innerHTML =
    '<span class="stage">' + esc(G.stage) + '</span>'
    + '<span class="count">' + G.saved + ' / ' + TOTAL + ' saved</span>';
  const gen = document.getElementById('gen');
  const n = gen.classList.contains('fail') ? 14 : 7;
  document.getElementById('genFeed').innerHTML = G.all.slice(-n).map(function(x) {
    return '<div class="ln' + (x.cls ? ' ' + x.cls : '') + '">' + esc(x.text) + '</div>';
  }).join('');
}

async function finishGen(st) {
  clearInterval(G.timer);
  const gen = document.getElementById('gen');
  gen.classList.add(st.ok ? 'done' : 'fail');
  document.getElementById('genStop').hidden = true;
  G.retry = false;
  if (st.ok) { G.cur = 0; G.stage = 'Done — ' + G.saved + ' candidates ready'; }
  else { G.stage = st.message; }
  renderGen();
  const btn = document.getElementById('regenBtn');
  btn.textContent = 'Regenerate';
  btn.disabled = false;
  S.days = await api('/api/status');
  renderNav();
  renderProgress();
  selectDay(G.day);
  if (st.ok) {
    toast(st.message);
    setTimeout(function() { gen.hidden = true; }, 3000);
  }
}

async function cancelGeneration() {
  const res = await api('/api/regenerate/cancel', { method: 'POST' });
  toast(res.message);
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

/* ── Unapprove ─────────────────────────── */
async function unapprove(day) {
  if (!confirm('Unapprove ' + longDate(day) + '?\n\nYou can re-pick from the candidates.')) return;
  const res = await api('/api/unapprove', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({date: day})
  });
  if (res.ok) {
    toast(res.message);
    S.days = await api('/api/status');
    renderNav();
    renderProgress();
    selectDay(day);
  } else {
    alert('Error: ' + res.message);
  }
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
    purge_past_candidates()

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
