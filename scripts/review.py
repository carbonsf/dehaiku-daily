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

#
# Two kinds of job share this runner:
#   "regen"  the per-day Regenerate bar: one day, --force
#   "batch"  the + panel: any set of days with every generate.py option
# Only one runs at a time, whichever kind.

GENERATE_PY = str(REPO_ROOT / "scripts" / "generate.py")
MAX_WORKERS = 8

JOB_LOCK = threading.Lock()
JOB = {
    "running": False, "date": None, "lines": [], "done": False,
    "ok": None, "message": "", "started": 0.0, "proc": None,
    "kind": None, "label": None, "plan": [],
}

_NO_KEY_MESSAGE = (
    "ANTHROPIC_API_KEY not set. Export it in the shell that runs "
    "review.py, or put ANTHROPIC_API_KEY=sk-ant-... in a .env "
    "file at the repo root, then restart review.py."
)


def _spawn_job(cmd, kind, label, day_str=None, plan=None, ok_message=""):
    """Run generate.py in the background and stream its output into JOB.
    Returns an error string, or None once the job is running."""
    with JOB_LOCK:
        if JOB["running"]:
            return f"Already generating {JOB['label']}."

    # Resolve the key here so a missing key is a clear message instead of
    # a traceback, and pass it explicitly — this server may have been
    # started from a shell that never exported it.
    from generate import load_api_key
    api_key = load_api_key()
    if not api_key:
        return _NO_KEY_MESSAGE

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
            kind=kind, label=label, plan=plan or [],
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
                JOB["message"] = ok_message
            elif code < 0:
                JOB["message"] = "Generation cancelled."
            else:
                JOB["message"] = f"Generation failed (exit {code})."

    threading.Thread(target=pump, daemon=True).start()
    return None


def start_regeneration(day_str, seeds_csv="", theme="", version=""):
    """Kick off generate.py for one day in the background."""
    # -u = unbuffered stdout so lines arrive as they are printed
    cmd = [
        sys.executable, "-u",
        GENERATE_PY,
        "--day", day_str,
        "--force",
    ]
    if seeds_csv.strip():
        cmd.extend(["--seeds", seeds_csv.strip()])
    if theme.strip():
        cmd.extend(["--themes", theme.strip()])
    if version.strip():
        cmd.extend(["--version", version.strip()])

    err = _spawn_job(cmd, "regen", label=day_str, day_str=day_str,
                     ok_message="Regenerated candidates.")
    if err:
        return {"ok": False, "message": err}
    return {"ok": True, "message": f"Generating {day_str}…"}


# ── Batch generation (the + panel) ───────────────────────────


def generate_options():
    """What the + panel needs to build its form."""
    from generate import VERSIONS, LOOKAHEAD_DAYS, CANDIDATES_PER_DAY, MAX_PLAN_DAYS
    with open(CONFIG_DIR / "themes.json") as f:
        rotation = json.load(f)["rotation"]
    return {
        "today": date.today().isoformat(),
        "versions": list(VERSIONS),
        "default_version": "POLISHED",
        "rotation": rotation,
        "lookahead": LOOKAHEAD_DAYS,
        "per_day": CANDIDATES_PER_DAY,
        "max_days": MAX_PLAN_DAYS,
        "max_workers": MAX_WORKERS,
    }


def _batch_day_args(o):
    """Validate the 'which days / themes / force' part of a request and
    turn it into generate.py arguments. Raises ValueError for the user."""
    today = date.today()

    def future_date(s, what):
        try:
            d = date.fromisoformat(str(s).strip())
        except ValueError:
            raise ValueError(f"{what}: {s!r} is not a valid date") from None
        if d < today:
            raise ValueError(
                f"{what}: {d:%b} {d.day} is in the past — the review app only "
                f"shows today and later"
            )
        return d

    def day_count():
        try:
            n = int(o.get("count", 0))
        except (TypeError, ValueError):
            raise ValueError("Number of days must be a whole number") from None
        if not 1 <= n <= 60:
            raise ValueError("Number of days must be between 1 and 60")
        return n

    args = []
    mode = o.get("mode", "next")
    if mode == "next":
        args += ["--count", str(day_count())]
    elif mode == "from":
        start = future_date(o.get("start", ""), "Start date")
        args += ["--start", start.isoformat(), "--count", str(day_count())]
    elif mode == "dates":
        dates = sorted({future_date(s, "Date") for s in (o.get("dates") or [])})
        if not dates:
            raise ValueError("Add at least one date")
        args += ["--day", ",".join(d.isoformat() for d in dates)]
    else:
        raise ValueError(f"Unknown mode {mode!r}")

    themes = [str(t).strip() for t in (o.get("themes") or []) if str(t).strip()]
    for t in themes:
        if "," in t:
            raise ValueError(f"Themes can't contain commas: {t!r}")
    if themes:
        args += ["--themes", ",".join(themes)]
    if o.get("force"):
        args.append("--force")
    return args


def plan_batch(o):
    """Ask generate.py itself which days a run would touch (--dry-run), so
    the preview is computed by the same code that will run."""
    try:
        args = _batch_day_args(o)
    except ValueError as e:
        return {"ok": False, "error": str(e)}
    r = subprocess.run(
        [sys.executable, GENERATE_PY, "--dry-run", "--json", *args],
        cwd=REPO_ROOT, capture_output=True, text=True, timeout=30,
    )
    try:
        out = json.loads(r.stdout.strip().splitlines()[-1])
    except (ValueError, IndexError):
        err = (r.stderr or r.stdout).strip().splitlines()
        return {"ok": False, "error": err[-1] if err else "Couldn't compute the plan"}
    if "error" in out:
        return {"ok": False, "error": out["error"]}
    return {"ok": True, "plan": out["plan"]}


def _range_label(dates):
    def short(s):
        d = date.fromisoformat(s)
        return f"{d:%b} {d.day}"
    if len(dates) == 1:
        return short(dates[0])
    return f"{len(dates)} days · {short(dates[0])} – {short(dates[-1])}"


def start_batch(o):
    """Start a batch run of exactly the days the preview showed."""
    from generate import VERSIONS
    planned = plan_batch(o)
    if not planned["ok"]:
        return {"ok": False, "message": planned["error"]}
    gen = [p for p in planned["plan"] if p["action"] == "generate"]
    if not gen:
        return {"ok": False, "message": (
            "Nothing to generate — every day in that selection already has an "
            "approved puzzle. Tick “Include approved days” to redo them."
        )}

    version = str(o.get("version") or "POLISHED").strip().upper()
    if version not in VERSIONS:
        return {"ok": False, "message": f"Unknown version {version!r}"}
    try:
        workers = int(o.get("workers", MAX_WORKERS))
    except (TypeError, ValueError):
        workers = 0
    if not 1 <= workers <= MAX_WORKERS:
        return {"ok": False, "message": f"Parallel candidates must be 1–{MAX_WORKERS}"}
    seeds = [w.strip().lower() for w in str(o.get("seeds") or "").split(",") if w.strip()]
    if len(seeds) > 11:
        return {"ok": False, "message": "Seed words: at most 11 (the pool is 12)"}

    # Pin the run to the previewed dates: if a day gets approved between
    # preview and click it is skipped rather than surprising you.
    dates = [p["date"] for p in gen]
    cmd = [sys.executable, "-u", GENERATE_PY, "--day", ",".join(dates),
           "--version", version, "--workers", str(workers)]
    themes = [str(t).strip() for t in (o.get("themes") or []) if str(t).strip()]
    if themes:
        cmd += ["--themes", ",".join(themes)]
    if seeds:
        cmd += ["--seeds", ",".join(seeds)]
    if o.get("force"):
        cmd.append("--force")

    label = _range_label(dates)
    n = len(dates)
    err = _spawn_job(
        cmd, "batch", label=label,
        plan=[{"date": p["date"], "theme": p["theme"]} for p in gen],
        ok_message=f"Generated {n} day{'s' if n != 1 else ''} of candidates.",
    )
    if err:
        return {"ok": False, "message": err}
    return {"ok": True, "message": f"Generating {label}…", "label": label}


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
            "kind": JOB["kind"],
            "label": JOB["label"],
            "plan": JOB["plan"],
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
        elif self.path == "/api/generate/options":
            self._json(generate_options())
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
                body["date"], body.get("seeds", ""), body.get("theme", ""),
                body.get("version", ""),
            ))
        elif self.path == "/api/regenerate/cancel":
            self._json(cancel_regeneration())
        elif self.path == "/api/generate/plan":
            self._json(plan_batch(body))
        elif self.path == "/api/generate":
            self._json(start_batch(body))
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
.regen select{padding:6px 10px;border:1px solid var(--border);border-radius:6px;font-size:13px;font-family:inherit;background:var(--surface);color:var(--text)}
.regen select:focus{outline:none;border-color:var(--accent)}
.ver{margin-left:8px;padding:1px 7px;border-radius:9px;font-size:10px;font-weight:700;letter-spacing:.05em;background:var(--dec-bg);color:var(--dec-text)}
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
.seg.failed .fill{background:#dc2626}
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

/* ── + pill and batch states in the day nav ── */
.add-pill{flex:none;width:30px;height:30px;border-radius:50%;border:1px dashed #a8a29e;background:var(--surface);color:var(--muted);font-size:18px;line-height:1;cursor:pointer;font-family:inherit;transition:all .15s;display:flex;align-items:center;justify-content:center}
.add-pill:hover{border-style:solid;border-color:var(--accent);color:var(--accent);background:#eff6ff}
.day-pill.ghost{border-style:dashed;color:var(--muted);background:transparent}
.day-pill.genning{position:relative;border-color:#f97316;box-shadow:0 0 0 0 rgba(249,115,22,.5);animation:pillPulse 1.4s ease-out infinite}
.day-pill.active.genning{background:#f97316;color:#fff}
@keyframes pillPulse{0%{box-shadow:0 0 0 0 rgba(249,115,22,.45)}100%{box-shadow:0 0 0 7px rgba(249,115,22,0)}}

/* ── Batch day strip in the live panel ── */
.gen-days{display:flex;flex-wrap:wrap;gap:5px;margin:-2px 0 10px}
.gen-days:empty{display:none}
.gday{padding:2px 9px;border-radius:9px;font-size:11px;font-weight:600;background:#27272a;color:#9ca3af;font-variant-numeric:tabular-nums}
.gday.active{background:#f97316;color:#fff}
.gday.done{background:#14532d;color:#86efac}
.gday.partial{background:#713f12;color:#fde68a}
.gday.skipped{text-decoration:line-through;opacity:.6}

/* ── Create dailies panel ── */
.cm-overlay{position:fixed;inset:0;background:rgba(17,17,17,.45);z-index:300;display:flex;align-items:flex-start;justify-content:center;padding:48px 16px;overflow-y:auto}
.cm-overlay[hidden]{display:none}
.cm{background:var(--surface);border-radius:14px;width:100%;max-width:680px;box-shadow:0 20px 60px rgba(0,0,0,.25);display:flex;flex-direction:column}
.cm-head{display:flex;align-items:center;justify-content:space-between;padding:18px 22px 10px}
.cm-head h3{font-size:17px;font-weight:700}
.cm-x{border:none;background:transparent;font-size:22px;line-height:1;color:var(--muted);cursor:pointer;padding:4px 8px;border-radius:6px}
.cm-x:hover{background:var(--dec-bg);color:var(--text)}
.cm-body{padding:4px 22px 8px;display:flex;flex-direction:column;gap:18px}
.cm-sec h4{font-size:11px;font-weight:700;text-transform:uppercase;letter-spacing:.06em;color:var(--muted);margin-bottom:8px}
.cm-hint{font-size:12px;color:var(--muted);margin-top:6px;line-height:1.5}
.cm-row{display:flex;align-items:center;gap:10px;flex-wrap:wrap}
.cm input[type=text],.cm input[type=number],.cm input[type=date],.cm textarea,.cm select{padding:7px 10px;border:1px solid var(--border);border-radius:7px;font-size:13px;font-family:inherit;background:var(--surface);color:var(--text)}
.cm input:focus,.cm textarea:focus,.cm select:focus{outline:none;border-color:var(--accent)}
.cm input[type=number]{width:76px}
.cm textarea{width:100%;min-height:68px;resize:vertical;line-height:1.5}
.cm input.wide{width:100%}
.seg-ctl{display:inline-flex;border:1px solid var(--border);border-radius:8px;overflow:hidden}
.seg-ctl button{border:none;background:var(--surface);padding:7px 14px;font-size:13px;font-family:inherit;cursor:pointer;color:var(--text);border-right:1px solid var(--border)}
.seg-ctl button:last-child{border-right:none}
.seg-ctl button.on{background:var(--accent);color:#fff}
.cm-mode{margin-top:10px}
.cm-mode[hidden]{display:none}
.chips{display:flex;flex-wrap:wrap;gap:6px;margin-top:8px}
.chip{display:inline-flex;align-items:center;gap:6px;padding:3px 6px 3px 10px;border-radius:12px;background:var(--ans-bg);color:var(--ans-text);font-size:12px;font-weight:600}
.chip button{border:none;background:transparent;color:inherit;cursor:pointer;font-size:14px;line-height:1;padding:0 3px;border-radius:50%}
.chip button:hover{background:rgba(30,64,175,.15)}
.cm-check{display:flex;gap:8px;align-items:flex-start;font-size:13px;margin-top:12px;cursor:pointer}
.cm-check input{margin-top:2px}
.recipes{display:grid;grid-template-columns:repeat(2,1fr);gap:8px}
@media(max-width:560px){.recipes{grid-template-columns:1fr}}
.recipe{border:2px solid var(--border);border-radius:10px;padding:10px 12px;cursor:pointer;text-align:left;background:var(--surface);font-family:inherit;color:var(--text)}
.recipe:hover{border-color:#b0b0b0}
.recipe.on{border-color:var(--accent);background:#eff6ff}
.recipe b{display:block;font-size:12px;letter-spacing:.05em;margin-bottom:3px}
.recipe span{font-size:12px;color:var(--muted);line-height:1.4}
.cm-preview{border:1px solid var(--border);border-radius:10px;overflow:hidden}
.cm-preview .sum{padding:9px 12px;font-size:13px;font-weight:600;background:var(--bg);border-bottom:1px solid var(--border);display:flex;justify-content:space-between;gap:10px}
.cm-preview .sum .sub{font-weight:400;color:var(--muted)}
.cm-preview .rows{max-height:240px;overflow-y:auto}
.prow{display:grid;grid-template-columns:92px 1fr auto;gap:10px;padding:7px 12px;font-size:13px;border-bottom:1px solid var(--border);align-items:center}
.prow:last-child{border-bottom:none}
.prow .d{font-variant-numeric:tabular-nums;color:var(--muted)}
.prow .d b{color:var(--text);font-weight:600}
.prow .th{overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.prow.skip{color:var(--muted)}
.prow.skip .th{text-decoration:line-through}
.badge{font-size:10.5px;font-weight:700;padding:2px 8px;border-radius:9px;white-space:nowrap}
.badge.new{background:var(--green-bg);color:#166534}
.badge.repl{background:#fef3c7;color:#92400e}
.badge.redo{background:#fee2e2;color:#991b1b}
.badge.skip{background:var(--dec-bg);color:var(--dec-text)}
.cm-err{padding:10px 12px;font-size:13px;color:#991b1b;background:#fef2f2}
.cm-busy{padding:9px 12px;font-size:13px;background:#fff7ed;color:#9a3412;border-radius:8px}
.cm-busy[hidden]{display:none}
.cm-foot{display:flex;justify-content:flex-end;align-items:center;gap:10px;padding:14px 22px 18px;border-top:1px solid var(--border);margin-top:10px}
.cm-foot .left{margin-right:auto;font-size:12px;color:var(--muted)}
.cm-cancel{padding:8px 16px;border:1px solid var(--border);background:var(--surface);border-radius:7px;font-size:13px;cursor:pointer;font-family:inherit;color:var(--text)}
.cm-go{padding:8px 18px;border:none;background:#f97316;color:#fff;border-radius:7px;font-size:13px;font-weight:700;cursor:pointer;font-family:inherit}
.cm-go:hover{background:#ea580c}
.cm-go:disabled{opacity:.45;cursor:not-allowed}
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
  <select id="versionInput" title="Generation recipe">
    <option value="POLISHED">POLISHED</option>
    <option value="CHOPPED">CHOPPED</option>
    <option value="HYBRID">HYBRID</option>
    <option value="LANDED">LANDED</option>
  </select>
  <button class="regen-btn" id="regenBtn" onclick="regenerate()">Regenerate</button>
  <div class="hint">Re-rolls all 8 candidates. Theme overrides the rotation; seed words get mixed into the 12-word pool. Version picks the recipe (see README); each card shows which one made it.</div>
</div>
<div class="gen" id="gen" hidden>
  <div class="gen-top">
    <span class="gen-dot"></span>
    <span class="gen-title" id="genTitle">Generating…</span>
    <span class="gen-elapsed" id="genElapsed">0:00</span>
    <button class="gen-stop" id="genStop" onclick="cancelGeneration()">Stop</button>
  </div>
  <div class="gen-days" id="genDays"></div>
  <div class="gen-segs" id="genSegs"></div>
  <div class="gen-status" id="genStatus"></div>
  <div class="gen-feed" id="genFeed"></div>
</div>
<div class="grid" id="grid"></div>
<div class="toast" id="toast"></div>

<div class="cm-overlay" id="cmOverlay" hidden onclick="if (event.target === this) closeCreate()">
  <div class="cm" role="dialog" aria-modal="true" aria-labelledby="cmTitle">
    <div class="cm-head">
      <h3 id="cmTitle">Create dailies</h3>
      <button class="cm-x" onclick="closeCreate()" aria-label="Close">×</button>
    </div>
    <div class="cm-body">
      <div class="cm-busy" id="cmBusy" hidden></div>

      <div class="cm-sec">
        <h4>Days</h4>
        <div class="seg-ctl" id="cmModes">
          <button data-mode="next" onclick="setMode('next')">Next open days</button>
          <button data-mode="from" onclick="setMode('from')">Starting from</button>
          <button data-mode="dates" onclick="setMode('dates')">Specific dates</button>
        </div>
        <div class="cm-mode" id="cmMode-next">
          <div class="cm-row"><input type="number" id="cmCountNext" min="1" max="60" value="7" oninput="cmChanged()"> <span>days</span></div>
          <div class="cm-hint">Starts today if today has no puzzle yet, otherwise tomorrow. Days that are already approved are skipped and don't count.</div>
        </div>
        <div class="cm-mode" id="cmMode-from" hidden>
          <div class="cm-row"><input type="date" id="cmStart" oninput="cmChanged()"> <span>for</span> <input type="number" id="cmCountFrom" min="1" max="60" value="7" oninput="cmChanged()"> <span>days</span></div>
          <div class="cm-hint">Approved days from that date on are skipped and don't count.</div>
        </div>
        <div class="cm-mode" id="cmMode-dates" hidden>
          <div class="cm-row"><input type="date" id="cmDateAdd" onkeydown="if (event.key === 'Enter') addDate()"> <button class="cm-cancel" onclick="addDate()">Add date</button></div>
          <div class="chips" id="cmChips"></div>
        </div>
        <label class="cm-check"><input type="checkbox" id="cmForce" onchange="cmChanged()"> <span>Include days that are already approved<br><span class="cm-hint">Makes 8 new candidates for them. The approved puzzle stays live until you unapprove and re-pick.</span></span></label>
      </div>

      <div class="cm-sec">
        <h4>Themes</h4>
        <textarea id="cmThemes" placeholder="One per line (or comma-separated). They cycle across the days.&#10;Leave empty to use the rotation." oninput="cmChanged()"></textarea>
        <div class="cm-hint" id="cmRotation"></div>
      </div>

      <div class="cm-sec">
        <h4>Seed words</h4>
        <input type="text" class="wide" id="cmSeeds" placeholder="Optional, comma-separated (e.g. tree, gift, snow)" oninput="cmChanged()">
        <div class="cm-hint">Mixed into every candidate's 12-word pool — they may land as answers or decoys. Up to 11.</div>
      </div>

      <div class="cm-sec">
        <h4>Recipe</h4>
        <div class="recipes" id="cmRecipes">
          <button class="recipe" data-v="POLISHED" onclick="setRecipe('POLISHED')"><b>POLISHED</b><span>Punctuated sentence with a turn; line-end commas stripped. Default.</span></button>
          <button class="recipe" data-v="CHOPPED" onclick="setRecipe('CHOPPED')"><b>CHOPPED</b><span>The no-commas recipe: bare, terse, imagistic lines.</span></button>
          <button class="recipe" data-v="HYBRID" onclick="setRecipe('HYBRID')"><b>HYBRID</b><span>CHOPPED's prompt on POLISHED's checks.</span></button>
          <button class="recipe" data-v="LANDED" onclick="setRecipe('LANDED')"><b>LANDED</b><span>Built as one plain-spoken sentence with the last line settled first. Aimed at word-salad endings.</span></button>
        </div>
      </div>

      <div class="cm-sec">
        <h4>Speed</h4>
        <div class="cm-row"><select id="cmWorkers" onchange="cmChanged()"></select> <span>candidates generated at once</span></div>
        <div class="cm-hint">8 makes a whole day in one round. Lower it if you hit rate limits. Days themselves run one after another.</div>
      </div>

      <div class="cm-sec">
        <h4>Preview</h4>
        <div class="cm-preview" id="cmPreview"><div class="cm-err">Loading…</div></div>
      </div>
    </div>
    <div class="cm-foot">
      <span class="left" id="cmFootNote"></span>
      <button class="cm-cancel" onclick="closeCreate()">Cancel</button>
      <button class="cm-go" id="cmGo" onclick="startCreate()" disabled>Generate</button>
    </div>
  </div>
</div>

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
  if (st.running && st.kind === 'batch') {
    beginBatchPanel(st.label, st.plan, st.elapsed);
    pollGeneration();
  } else if (st.running) {
    selectDay(st.date);
    beginGenPanel(st.date, st.elapsed);
    pollGeneration();
  }
}

/* ── Nav ────────────────────────────────── */
function renderNav() {
  // While a batch runs, days it hasn't reached yet show as dashed "ghost"
  // pills and the day being generated pulses.
  const items = S.days.slice();
  const batch = G.running && G.kind === 'batch';
  if (batch) {
    const known = new Set(S.days.map(function(d) { return d.date; }));
    G.plan.forEach(function(p) { if (!known.has(p.date)) items.push({ date: p.date, ghost: true }); });
    items.sort(function(a, b) { return a.date < b.date ? -1 : a.date > b.date ? 1 : 0; });
  }
  document.getElementById('dayNav').innerHTML = items.map(d => {
    const cls = ['day-pill'];
    if (d.date === S.cur) cls.push('active');
    if (d.approved) cls.push('done');
    if (d.ghost) cls.push('ghost');
    if (batch && d.date === G.day) cls.push('genning');
    const label = shortDate(d.date);
    return '<button class="' + cls.join(' ') + '" onclick="pillClick(\'' + d.date + '\')">'
      + (d.approved ? '✓ ' : '') + label + '</button>';
  }).join('')
    + '<button class="add-pill" onclick="openCreate()" title="Create dailies" aria-label="Create dailies">+</button>';
}

// A pill click is the user navigating, so a running batch stops auto-following.
function pillClick(day) {
  G.follow = false;
  selectDay(day);
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
        + '<div class="meta">Approved · Theme: ' + esc(puzzle.theme || '—') + (puzzle.version ? ' · ' + esc(puzzle.version) : '') + '</div>';
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
      + '<div class="card-num">Option ' + n + (c.version ? '<span class="ver">' + esc(c.version) + '</span>' : '') + '</div>'
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
// Per-candidate progress lives in G.c[n] = {frac, retry, done}. The generator
// runs a day's candidates in parallel and prefixes every line "[n] ", so each
// line is attributed by that prefix; an unprefixed line (older, sequential
// output) falls back to G.cur, the last "Candidate n/8" header seen.
//
// A batch job (the + panel) runs several days one after another. For those,
// G.kind is 'batch', G.plan lists the days, G.dayState tracks each one, and
// G.c resets at every "Generating 8 candidates for <date>" header so the eight
// segments always show the day in progress.
const G = { timer: null, since: 0, cur: 0, stage: '', all: [], day: null, start: 0, c: {},
            kind: null, running: false, plan: [], dayState: {}, label: '',
            follow: false, newDay: null, savedTotal: 0 };
const TOTAL = 8;
function savedCount() {
  return Object.values(G.c).filter(function(x) { return x.done; }).length;
}

async function regenerate() {
  const day = S.cur;
  if (!day) return;
  const seeds = document.getElementById('seedInput').value.trim();
  const theme = document.getElementById('themeInput').value.trim();
  const version = document.getElementById('versionInput').value;
  const parts = ['version ' + version];
  if (theme) parts.push('theme "' + theme + '"');
  if (seeds) parts.push('seeds "' + seeds + '"');
  const label = parts.length ? ' with ' + parts.join(' and ') : '';
  if (!confirm('Regenerate all candidates for ' + longDate(day) + label + '?\n\nThis calls the Claude API. Progress shows live below.')) return;
  const res = await api('/api/regenerate', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({date: day, seeds: seeds, theme: theme, version: version})
  });
  if (!res.ok) { alert(res.message); return; }
  beginGenPanel(day, 0);
  pollGeneration();
}

function beginGenPanel(day, elapsedSec) {
  Object.assign(G, { since: 0, cur: 0, all: [], c: {},
                     day: day, stage: 'Starting generator…',
                     start: Date.now() - (elapsedSec || 0) * 1000,
                     kind: 'regen', running: true, plan: [], dayState: {}, label: day,
                     follow: false, newDay: null, savedTotal: 0 });
  document.getElementById('genDays').innerHTML = '';
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
  if (G.kind === 'batch') {
    if (G.newDay) {                        // the run moved on to a new day
      const d = G.newDay; G.newDay = null;
      renderNav();
      if (G.follow) showPendingDay(d);
    }
    if (landed) await onBatchLanded();
  } else if (landed) selectDay(G.day);     // cards appear as they land
  if (st.running) { setTimeout(pollGeneration, 600); return; }
  finishGen(st);
}

// A batch saved a candidate: refresh the nav (so the day's pill appears or
// updates) and redraw the grid only if the user is looking at that day.
async function onBatchLanded() {
  S.days = await api('/api/status');
  renderNav();
  renderProgress();
  if (S.cur === G.day) selectDay(G.day);
}

// Show a day the batch has started but that has no candidates on disk yet.
function showPendingDay(day) {
  S.cur = day;
  renderNav();
  const p = G.plan.find(function(x) { return x.date === day; });
  document.getElementById('dateHdr').innerHTML = '<h2>' + longDate(day) + '</h2>'
    + '<div class="meta">Generating' + (p && p.theme ? ' · Theme: ' + esc(p.theme) : '') + '</div>';
  document.getElementById('regen').style.display = 'none';
  document.getElementById('grid').innerHTML =
    '<div class="state">Candidates will appear here as each one passes the gate…</div>';
}

// Parse one stdout line from generate.py. Returns true when a candidate
// was saved (so the grid can refresh).
function ingestLine(raw0) {
  let raw = raw0, n = null, m;
  if ((m = raw0.match(/^\[(\d+)\] ?([\s\S]*)$/))) { n = +m[1]; raw = m[2]; }
  const t = raw.trim();
  if (!t) return false;

  // Day-level lines (unprefixed). A new day header resets the per-candidate
  // state so its eight segments start fresh; in a single-day regenerate it
  // arrives before any candidate, so the reset is a no-op there.
  if (n === null && (m = t.match(/^Generating \d+ candidates for (\d{4}-\d{2}-\d{2})/))) {
    if (G.kind === 'batch' && G.day && G.dayState[G.day] === 'active') G.dayState[G.day] = 'done';
    G.c = {}; G.cur = 0; G.day = m[1];
    if (G.kind === 'batch') {
      G.dayState[m[1]] = 'active'; G.newDay = m[1];
      const k = G.plan.findIndex(function(p) { return p.date === m[1]; }) + 1;
      G.stage = 'Day ' + k + ' of ' + G.plan.length + ' · ' + shortDate(m[1]) + ' · starting';
    }
    G.all.push({ text: t, cls: '' });
    return false;
  }
  if (n === null && (m = t.match(/^Skipping (\d{4}-\d{2}-\d{2})/))) {
    G.dayState[m[1]] = 'skipped';
    G.all.push({ text: t, cls: '' });
    return false;
  }
  if (n === null && (m = t.match(/^!! (\d{4}-\d{2}-\d{2}): only.*slots \[([\d, ]*)\]/))) {
    G.dayState[m[1]] = 'partial';           // falls through so the line is styled 'bad'
    m[2].split(',').forEach(function(s) {   // backstop for output that didn't tag the slot
      const k = +s.trim();
      if (k && m[1] === G.day) { G.c[k] = G.c[k] || { frac: 0, retry: false, done: false }; G.c[k].failed = true; }
    });
  }
  if (n === null && /^Done\./.test(t) && G.day && G.dayState[G.day] === 'active') {
    G.dayState[G.day] = 'done';
  }
  if (n === null && (m = t.match(/^Candidate (\d+)\/(\d+)/))) n = +m[1];   // unprefixed, sequential output
  if (n !== null) G.cur = n;
  const id = n !== null ? n : G.cur;
  const c = id ? (G.c[id] = G.c[id] || { frac: 0, retry: false, done: false }) : null;
  let cls = '', saved = false;
  const who = function() { return 'Candidate ' + id + ' · '; };
  if (!c) {
    /* belongs to no candidate: run header, version line, done, errors */
  } else if ((m = t.match(/^Candidate (\d+)\/(\d+)/))) {
    c.frac = 0.05; c.retry = false; c.done = false; G.stage = who() + 'picking an angle';
  } else if ((m = t.match(/^Word pool attempt (\d+)\/(\d+)/))) {
    c.frac = 0.1; c.retry = +m[1] > 1;
    G.stage = who() + 'drawing 12 words' + (+m[1] > 1 ? ' (fresh pool ' + m[1] + '/' + m[2] + ')' : '');
  } else if (/^Answers:/.test(t)) {
    c.frac = 0.18; G.stage = who() + 'writing the haiku';
  } else if (/^Pool \(12\)|^Decoys:/.test(t)) {
    /* informational */
  } else if (/^Haiku:/.test(t)) {
    c.frac = 0.45; c.retry = false; G.stage = who() + 'haiku written — checking';
  } else if (/^Structure rejected|^Craft probe failed|^Haiku leaked|^Haiku rejected/.test(t)) {
    cls = 'warn'; c.retry = true; c.frac = 0.3;
    const body = t.replace(/\s*\(try \d+\/\d+\):?\s*/, ' ').trim();
    let why;
    if ((m = body.match(/^Structure rejected\s*(.*)/))) why = 'structure: ' + m[1];
    else if ((m = body.match(/^Craft probe failed\s*(.*)/))) why = 'craft: ' + m[1];
    else if ((m = body.match(/^Haiku leaked\s*\[(.*?)\]/))) why = 'leaked ' + m[1].replace(/'/g, '');
    else if ((m = body.match(/Syllable count (\S+)/))) why = 'syllables ' + m[1] + ', need 5/7/5';
    else why = body.replace(/^Haiku rejected\s*/, '');
    G.stage = who() + 'rewriting — ' + why;
  } else if ((m = t.match(/^Gate check \((\d+)\/(\d+)\)/))) {
    c.frac = 0.55 + 0.09 * (+m[1]); c.retry = false;
    G.stage = who() + 'solver probes ' + m[1] + '/' + m[2];
  } else if (/^Gate failed/.test(t)) {
    cls = 'warn'; c.retry = true;
    G.stage = who() + 'gate: ' + t.replace(/^Gate failed\s*[—-]?\s*/, '') + ' — rewriting';
  } else if (/^Gate: fair but too obvious/.test(t)) {
    cls = 'warn'; c.retry = true; G.stage = who() + 'fair but too obvious — rewriting';
  } else if (/^Gate passed/.test(t)) {
    cls = 'good'; c.frac = 0.97; c.retry = false; G.stage = who() + 'passed ✓';
  } else if (/^Gate budget exhausted|^Could not produce|^All pools exhausted/.test(t)) {
    cls = 'warn'; c.retry = true; G.stage = who() + t.charAt(0).toLowerCase() + t.slice(1);
  } else if (/^!! candidate failed/.test(t)) {
    cls = 'bad'; c.failed = true; c.retry = false;
    G.stage = who() + 'failed — ' + t.replace(/^!! candidate failed:\s*/, '').slice(0, 80);
  } else if (/^First word .* collides/.test(t)) {
    if (c.done) G.savedTotal--;              // its save is being redone, don't count it twice
    cls = 'warn'; c.retry = true; c.done = false; c.frac = 0.05;    // sent back for a new opening
    G.stage = who() + t.charAt(0).toLowerCase() + t.slice(1).replace(/ — regenerating.*$/, '') + ' — regenerating';
  } else if (/^Saved →/.test(t)) {
    if (!c.done) G.savedTotal++;
    cls = 'good'; c.done = true; c.frac = 1; saved = true;
    G.stage = who() + 'saved ✓';
  }
  if (G.kind === 'batch' && c && G.day && !/^Day \d/.test(G.stage)) {
    const k = G.plan.findIndex(function(p) { return p.date === G.day; }) + 1;
    G.stage = 'Day ' + k + '/' + G.plan.length + ' · ' + shortDate(G.day) + ' · ' + G.stage;
  }
  if (/^Done\./.test(t)) cls = 'good';
  else if (/^Traceback|Error|^!!/.test(t)) cls = 'bad';
  else if (!cls && /^ {4}\S/.test(raw) && !/^\s*\[/.test(raw)) cls = 'verse';   // the three printed haiku lines
  G.all.push({ text: (n !== null ? '[' + n + '] ' : '') + t, cls: cls });
  return saved;
}

function renderGen() {
  const segs = document.getElementById('genSegs').children;
  for (let i = 0; i < segs.length; i++) {
    const n = i + 1, seg = segs[i], fill = seg.firstChild, c = G.c[n];
    let cls = 'seg', w = 0;
    if (c && c.done) { cls += ' saved'; w = 100; }
    else if (c && c.failed) { cls += ' failed'; w = 100; }
    else if (c) { cls += ' active' + (c.retry ? ' retry' : ''); w = Math.round(c.frac * 100); }
    seg.className = cls;
    fill.style.width = w + '%';
  }
  const countText = G.kind === 'batch'
    ? savedCount() + ' / ' + TOTAL + ' this day · ' + G.savedTotal + ' / ' + (G.plan.length * TOTAL) + ' total'
    : savedCount() + ' / ' + TOTAL + ' saved';
  document.getElementById('genStatus').innerHTML =
    '<span class="stage">' + esc(G.stage) + '</span>'
    + '<span class="count">' + countText + '</span>';
  if (G.kind === 'batch') {
    document.getElementById('genDays').innerHTML = G.plan.map(function(p) {
      const s = G.dayState[p.date] || 'queued';
      return '<span class="gday ' + s + '" title="' + esc(p.theme || '') + '">'
        + (s === 'done' ? '✓ ' : s === 'partial' ? '! ' : '') + shortDate(p.date) + '</span>';
    }).join('');
  }
  const gen = document.getElementById('gen');
  const n = gen.classList.contains('fail') ? 14 : 9;
  document.getElementById('genFeed').innerHTML = G.all.slice(-n).map(function(x) {
    return '<div class="ln' + (x.cls ? ' ' + x.cls : '') + '">' + esc(x.text) + '</div>';
  }).join('');
}

async function finishGen(st) {
  clearInterval(G.timer);
  G.running = false;
  if (!document.getElementById('cmOverlay').hidden) refreshBusy().then(function() { cmChanged(true); });
  const gen = document.getElementById('gen');
  gen.classList.add(st.ok ? 'done' : 'fail');
  document.getElementById('genStop').hidden = true;
  if (G.kind === 'batch') return finishBatch(st, gen);
  if (st.ok) { G.stage = 'Done — ' + savedCount() + ' candidates ready'; }
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

async function finishBatch(st, gen) {
  const short = G.plan.filter(function(p) { return G.dayState[p.date] === 'partial'; });
  const made = G.plan.filter(function(p) { return G.dayState[p.date] === 'done'; }).length + short.length;
  const partial = short.length;
  if (st.ok) {
    G.stage = 'Done — ' + made + ' day' + (made === 1 ? '' : 's') + ', ' + G.savedTotal + ' candidates ready'
      + (partial ? ' · short: ' + short.map(function(p) { return shortDate(p.date); }).join(', ') + ' (use Regenerate on that day)' : '');
  } else {
    G.stage = st.message + ' · ' + G.savedTotal + ' candidates were saved before it stopped';
  }
  renderGen();
  const btn = document.getElementById('regenBtn');
  btn.textContent = 'Regenerate';
  btn.disabled = false;
  S.days = await api('/api/status');
  renderNav();
  renderProgress();
  // If the user let it run, land on the first new day so review can start;
  // if they navigated away, leave them where they are.
  const first = G.plan.find(function(p) { return S.days.some(function(d) { return d.date === p.date && !d.approved && d.count; }); });
  if (G.follow && first) selectDay(first.date);
  else if (S.cur) selectDay(S.cur);
  if (st.ok) {
    toast(st.message);
    if (!partial) setTimeout(function() { gen.hidden = true; }, 6000);
  }
}

function beginBatchPanel(label, plan, elapsedSec) {
  Object.assign(G, { since: 0, cur: 0, all: [], c: {}, day: null,
                     stage: 'Starting generator…',
                     start: Date.now() - (elapsedSec || 0) * 1000,
                     kind: 'batch', running: true, plan: plan || [], dayState: {},
                     label: label, follow: true, newDay: null, savedTotal: 0 });
  const gen = document.getElementById('gen');
  gen.classList.remove('done', 'fail');
  gen.hidden = false;
  document.getElementById('genTitle').textContent = 'Generating ' + label;
  document.getElementById('genStop').hidden = false;
  document.getElementById('genSegs').innerHTML = '<div class="seg"><div class="fill"></div></div>'.repeat(TOTAL);
  document.getElementById('genFeed').innerHTML = '';
  const btn = document.getElementById('regenBtn');
  btn.textContent = 'Generating…';
  btn.disabled = true;
  if (G.plan.length) showPendingDay(G.plan[0].date);
  renderGen();
  clearInterval(G.timer);
  G.timer = setInterval(tickGen, 250);
  tickGen();
}

async function cancelGeneration() {
  const res = await api('/api/regenerate/cancel', { method: 'POST' });
  toast(res.message);
}

/* ── Create dailies (the + panel) ───────── */
// Every option generate.py has. The preview is computed by generate.py
// itself (--dry-run) so it shows exactly what Generate will do, and the run
// is pinned to the previewed dates.
const CM = { opts: null, mode: 'next', dates: [], recipe: 'POLISHED', plan: null,
             error: null, seq: 0, timer: null, busy: false };
const CM_KEY = 'dehaiku.create.v1';

async function openCreate() {
  if (!CM.opts) {
    CM.opts = await api('/api/generate/options');
    document.getElementById('cmWorkers').innerHTML = Array.from({ length: CM.opts.max_workers }, function(_, i) {
      const n = CM.opts.max_workers - i;
      return '<option value="' + n + '">' + n + '</option>';
    }).join('');
    document.getElementById('cmRotation').textContent =
      'Rotation (used when empty — each day gets the theme for its date): ' + CM.opts.rotation.join(' · ');
    document.getElementById('cmStart').min = CM.opts.today;
    document.getElementById('cmDateAdd').min = CM.opts.today;
    loadCreateForm();
  }
  document.getElementById('cmOverlay').hidden = false;
  await refreshBusy();
  cmChanged(true);
}

function closeCreate() {
  document.getElementById('cmOverlay').hidden = true;
}

document.addEventListener('keydown', function(e) {
  if (e.key === 'Escape' && !document.getElementById('cmOverlay').hidden) closeCreate();
});

async function refreshBusy() {
  const st = await api('/api/regenerate/status');
  CM.busy = !!st.running;
  const el = document.getElementById('cmBusy');
  el.hidden = !CM.busy;
  if (CM.busy) el.textContent = 'Already generating ' + (st.label || st.date) + '. You can set up the next run, but Generate unlocks when this one finishes.';
}

function setMode(mode) {
  CM.mode = mode;
  document.querySelectorAll('#cmModes button').forEach(function(b) { b.classList.toggle('on', b.dataset.mode === mode); });
  ['next', 'from', 'dates'].forEach(function(m) { document.getElementById('cmMode-' + m).hidden = m !== mode; });
  if (mode === 'from' && !document.getElementById('cmStart').value) document.getElementById('cmStart').value = CM.opts.today;
  cmChanged();
}

function setRecipe(v) {
  CM.recipe = v;
  document.querySelectorAll('#cmRecipes .recipe').forEach(function(b) { b.classList.toggle('on', b.dataset.v === v); });
  cmChanged();
}

function addDate() {
  const input = document.getElementById('cmDateAdd');
  const v = input.value;
  if (!v) return;
  if (CM.dates.indexOf(v) < 0) CM.dates.push(v);
  CM.dates.sort();
  input.value = '';
  renderChips();
  cmChanged();
}

function removeDate(v) {
  CM.dates = CM.dates.filter(function(d) { return d !== v; });
  renderChips();
  cmChanged();
}

function renderChips() {
  document.getElementById('cmChips').innerHTML = CM.dates.map(function(d) {
    return '<span class="chip">' + shortDate(d) + ' <button onclick="removeDate(\'' + d + '\')" aria-label="Remove">×</button></span>';
  }).join('');
}

function readCreateForm() {
  const themes = document.getElementById('cmThemes').value
    .split(/[\n,]/).map(function(t) { return t.trim(); }).filter(Boolean);
  const count = document.getElementById(CM.mode === 'from' ? 'cmCountFrom' : 'cmCountNext').value;
  return {
    mode: CM.mode,
    count: count,
    start: document.getElementById('cmStart').value,
    dates: CM.dates.slice(),
    themes: themes,
    force: document.getElementById('cmForce').checked,
    seeds: document.getElementById('cmSeeds').value.trim(),
    version: CM.recipe,
    workers: document.getElementById('cmWorkers').value,
  };
}

function saveCreateForm() {
  const f = readCreateForm();
  f.countNext = document.getElementById('cmCountNext').value;
  f.countFrom = document.getElementById('cmCountFrom').value;
  f.themesRaw = document.getElementById('cmThemes').value;
  try { localStorage.setItem(CM_KEY, JSON.stringify(f)); } catch (e) {}
}

function loadCreateForm() {
  let f = null;
  try { f = JSON.parse(localStorage.getItem(CM_KEY) || 'null'); } catch (e) {}
  f = f || {};
  document.getElementById('cmCountNext').value = f.countNext || CM.opts.lookahead;
  document.getElementById('cmCountFrom').value = f.countFrom || CM.opts.lookahead;
  if (f.start && f.start >= CM.opts.today) document.getElementById('cmStart').value = f.start;
  CM.dates = (f.dates || []).filter(function(d) { return d >= CM.opts.today; });
  renderChips();
  document.getElementById('cmThemes').value = f.themesRaw || '';
  document.getElementById('cmSeeds').value = f.seeds || '';
  document.getElementById('cmForce').checked = !!f.force;
  document.getElementById('cmWorkers').value = f.workers || CM.opts.max_workers;
  const v = CM.opts.versions.indexOf(f.version) >= 0 ? f.version : CM.opts.default_version;
  setRecipe(v);
  setMode(['next', 'from', 'dates'].indexOf(f.mode) >= 0 ? f.mode : 'next');
}

// Debounced: ask generate.py for the plan whenever an option changes.
function cmChanged(now) {
  if (!CM.opts) return;
  saveCreateForm();
  clearTimeout(CM.timer);
  CM.timer = setTimeout(refreshPreview, now ? 0 : 250);
}

async function refreshPreview() {
  const seq = ++CM.seq;
  const form = readCreateForm();
  let res;
  try {
    res = await api('/api/generate/plan', {
      method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(form),
    });
  } catch (e) {
    res = { ok: false, error: 'Could not reach the review server.' };
  }
  if (seq !== CM.seq) return;               // a newer edit superseded this one
  CM.plan = res.ok ? res.plan : null;
  CM.error = res.ok ? null : res.error;
  const seedCount = form.seeds ? form.seeds.split(',').filter(function(w) { return w.trim(); }).length : 0;
  if (!CM.error && seedCount > 11) CM.error = 'Seed words: at most 11 (the pool is 12).';
  renderPreview(form);
}

function renderPreview(form) {
  const box = document.getElementById('cmPreview');
  const go = document.getElementById('cmGo');
  const note = document.getElementById('cmFootNote');
  if (CM.error) {
    box.innerHTML = '<div class="cm-err">' + esc(CM.error) + '</div>';
    go.disabled = true; go.textContent = 'Generate'; note.textContent = '';
    return;
  }
  const plan = CM.plan || [];
  const gen = plan.filter(function(p) { return p.action === 'generate'; });
  const skipped = plan.length - gen.length;
  const replaced = gen.filter(function(p) { return p.existing > 0; }).length;
  const per = CM.opts.per_day;
  let sub = [];
  if (skipped) sub.push(skipped + ' approved day' + (skipped === 1 ? '' : 's') + ' skipped');
  if (replaced) sub.push(replaced + ' replace unreviewed candidates');
  const rows = plan.map(function(p) {
    let badge;
    if (p.action === 'skip') badge = '<span class="badge skip">approved · skipped</span>';
    else if (p.approved) badge = '<span class="badge redo">approved · new candidates</span>';
    else if (p.existing) badge = '<span class="badge repl">replaces ' + p.existing + ' unreviewed</span>';
    else badge = '<span class="badge new">new</span>';
    const d = new Date(p.date + 'T12:00:00');
    return '<div class="prow' + (p.action === 'skip' ? ' skip' : '') + '">'
      + '<span class="d"><b>' + shortDate(p.date) + '</b> ' + d.toLocaleDateString('en-US', { weekday: 'short' }) + '</span>'
      + '<span class="th">' + esc(p.theme || '—') + '</span>' + badge + '</div>';
  }).join('');
  box.innerHTML = '<div class="sum"><span>' + gen.length + ' day' + (gen.length === 1 ? '' : 's') + ' · '
    + (gen.length * per) + ' candidates</span><span class="sub">' + esc(sub.join(' · ')) + '</span></div>'
    + (plan.length ? '<div class="rows">' + rows + '</div>' : '<div class="cm-err">No days selected.</div>');
  note.textContent = gen.length && plan[0] && plan[0].date === CM.opts.today && form.mode === 'next'
    ? 'Starts today — it has no puzzle yet.' : '';
  go.disabled = CM.busy || !gen.length;
  go.textContent = gen.length
    ? 'Generate ' + gen.length + ' day' + (gen.length === 1 ? '' : 's') + ' (' + (gen.length * per) + ' candidates)'
    : 'Generate';
}

async function startCreate() {
  const form = readCreateForm();
  const gen = (CM.plan || []).filter(function(p) { return p.action === 'generate'; });
  if (!gen.length) return;
  const replaced = gen.filter(function(p) { return p.existing > 0 && !p.approved; });
  const redo = gen.filter(function(p) { return p.approved; });
  const warn = [];
  if (replaced.length) warn.push('This replaces unreviewed candidates on ' + replaced.map(function(p) { return shortDate(p.date); }).join(', ') + '.');
  if (redo.length) warn.push('Already-approved days get new candidates: ' + redo.map(function(p) { return shortDate(p.date); }).join(', ') + ' (their live puzzle is kept).');
  const msg = 'Generate ' + gen.length + ' day' + (gen.length === 1 ? '' : 's') + ' (' + (gen.length * CM.opts.per_day)
    + ' candidates) with ' + form.version + '?' + (warn.length ? '\n\n' + warn.join('\n') : '')
    + '\n\nThis calls the Claude API. Progress shows live below.';
  if (!confirm(msg)) return;
  const res = await api('/api/generate', {
    method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(form),
  });
  if (!res.ok) { alert(res.message); await refreshBusy(); cmChanged(true); return; }
  closeCreate();
  const st = await api('/api/regenerate/status');
  beginBatchPanel(res.label, st.plan && st.plan.length ? st.plan : gen.map(function(p) { return { date: p.date, theme: p.theme }; }), 0);
  pollGeneration();
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
