# De-Haiku-ifier Daily Pipeline

Static puzzle pipeline for the De-Haiku-ifier iOS game. Puzzles are generated locally via Claude API, reviewed editorially through a local web UI, and served as static JSON from this repo.

## iOS App Integration

```
GET https://raw.githubusercontent.com/carbonsf/dehaiku-daily/main/puzzles/{YYYY}/{MM}/{DD}.json

Example: /puzzles/2026/05/31.json
404 = no puzzle for that date (not yet generated or doesn't exist)
```

Response shape:
```json
{
  "date": "2026-05-31",
  "haiku": "line one\nline two\nline three",
  "words": ["answer1", "answer2", "answer3", "answer4"],
  "decoys": ["d1", "d2", "d3", "d4", "d5", "d6", "d7", "d8"],
  "theme": "zen garden"
}
```

App logic:
- Request today's date only — never future dates
- `words` (4) + `decoys` (8) = 12 total — shuffle before display
- Player guesses which 4 words the haiku encodes
- Haiku never contains the answer words (enforced at generation)
- `theme` is metadata only (display optional)
- `\n` separates haiku lines (always exactly 3)

## How it works

1. **Generate** — `scripts/generate.py` creates 8 candidate puzzles per day using the Claude API
2. **Review** — `scripts/review.py` launches a local web UI to browse candidates and pick winners
3. **Push** — Approved puzzles are committed and pushed to GitHub from the review UI
4. **Serve** — iOS app fetches from the URL above

## Quick start

```bash
# One-time setup
pip install anthropic
export ANTHROPIC_API_KEY="sk-ant-..."   # add to ~/.zshrc for persistence

# Generate candidates for the next 7 days
python3 scripts/generate.py

# Open the review interface
python3 scripts/review.py
```

## Repo structure

```
puzzles/              <- approved, live puzzles (app reads from here)
  2026/05/31.json
candidates/           <- generated options awaiting review (gitignored)
  2026-05-31/
    1.json ... 8.json
config/
  words.json          <- dictionary (5k+ words, same as iOS app ships)
  themes.json         <- theme rotation schedule
  banned-words.json   <- FIFO list to prevent haiku first-word repetition
scripts/
  generate.py         <- puzzle generation (Claude API)
  review.py           <- local web review server
  purge.py            <- unapprove/delete future puzzles
```

## Puzzle JSON format

```json
{
  "date": "2026-05-31",
  "haiku": "Bamboo shadows bend\nacross the stone where we sat\nrain fills both our cups",
  "words": ["bamboo", "stone", "sat", "rain"],
  "decoys": ["wind", "moon", "silk", "drift", "temple", "cloud", "pine", "ash"],
  "theme": "zen garden"
}
```

Players see all 12 words (4 answers + 8 decoys) and the haiku, then guess which 4 words the haiku encodes. The haiku must **never** contain the answer words or their stems.

---

## Scripts

### `generate.py` — Create candidates

```bash
# Default: generate 8 candidates/day for the next 7 days
python3 scripts/generate.py

# Override themes (cycles across days — all 8 candidates per day share one theme)
python3 scripts/generate.py --themes "winter wonderland,cozy cabin,holiday feast"

# Specific start date (single day)
python3 scripts/generate.py --day 2026-06-15

# Seed words mixed into the 12-word pool (may land as answers or decoys)
python3 scripts/generate.py --seeds "tree,gift,snow"

# Regenerate even if candidates already exist
python3 scripts/generate.py --day 2026-06-01 --force

# Combine flags
python3 scripts/generate.py --day 2026-12-25 --themes "christmas" --seeds "tree,gift,snow" --force
```

**How generation works:**
1. Draw 12 random words from `config/words.json` (+ any user seeds) — words are NOT themed
2. Randomly split: 4 answer words, 8 decoys
3. Generate a haiku encoding the 4 answers — theme applied HERE (sets mood/setting only)
4. Leak check: if any answer word or its stem appears in the haiku, retry with feedback
5. Truncation guard: verify the haiku isn't cut off
6. **Line structure** (two layers, both feed specific feedback into the retry — the base prompt is never touched):
   - *Layer 1 — deterministic, free:* reject commas, any mid-line break in the last line, more than one cut in the poem, or a line ending on a function word (`the`, `of`, `like`…)
   - *Layer 2 — Sonnet craft probe:* judges line integrity only (no knowledge of the hidden words) — catches noun phrases split across a line break and last lines stitched from fragments
   - Runs before the gate so a structurally dead haiku never costs an Opus gate call
7. **Gate** (matches production): two solver probes check the puzzle against the full 12-word pool:
   - *Obvious probe* (sonnet, casual skim) — if it gets 4/4, puzzle is too easy → regenerate
   - *Trace probe* (opus, careful solve) — any answer it can't find is unfair → regenerate
   - Up to 4 gate retries with craft-preserving feedback per word pool, 6 pools max (24 total tries)
   - Zero-trace (unfindable word) = hard fail, always rejected
   - Too-obvious = soft fail — tracks the best fair candidate; ships immediately on a full pass, falls back to fair-but-obvious if all 24 tries are obvious
8. Repeat 8 times with distinct angle cues for diversity
9. Each candidate's first word is banned for the next, preventing repetitive openings

**Environment variables:**
- `ANTHROPIC_API_KEY` — required
- `ANTHROPIC_MODEL` — override model (default: `claude-opus-4-8`)

### `review.py` — Pick winners

```bash
python3 scripts/review.py
# Opens http://localhost:8000
```

The review UI provides:
- **Day pills** — navigate between dates; green = approved
- **8 candidate cards** per day with haiku, answers, and decoys
- **Pick button** — approve a candidate (auto-advances to next day)
- **Unapprove button** — undo an approval to re-pick
- **Regenerate bar** — re-roll all candidates with optional theme override and seed words
- **Commit & Push** — stage and push all approved puzzles to GitHub

### `purge.py` — Unapprove / delete

```bash
# List all approved future dates
python3 scripts/purge.py

# Unapprove one date (keeps candidates so you can re-pick)
python3 scripts/purge.py 2026-06-05

# Unapprove everything from a date forward
python3 scripts/purge.py 2026-06-05 --all-after

# Full nuke — also delete the candidates
python3 scripts/purge.py 2026-06-05 --purge

# Nuke everything from a date forward
python3 scripts/purge.py 2026-06-05 --all-after --purge
```

---

## Typical weekly workflow

```bash
# 1. Generate next week's candidates
python3 scripts/generate.py

# 2. Open review UI, pick the best candidate for each day
python3 scripts/review.py

# 3. Click "Commit & Push" in the UI when done
#    (or manually: git add puzzles/ config/ && git commit && git push)
```

## Theme rotation

Themes cycle through `config/themes.json`:
```json
["nature", "urban life", "seasons", "emotions", "food & drink", "travel", "nostalgia"]
```

Default: each day maps to a theme by `(day_of_year % len(themes))`. Override with `--themes` on the CLI or the theme field in the review UI's regenerate bar.

## Banned words

`config/banned-words.json` tracks a FIFO list of haiku first-words (max 50) to prevent repetitive openings. Updated automatically when you approve a puzzle. During generation, first words are also tracked per-batch so all 8 candidates start differently.
