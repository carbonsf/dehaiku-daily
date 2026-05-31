# De-Haiku-ifier Daily Pipeline

Static puzzle pipeline for the De-Haiku-ifier iOS game. Puzzles are generated via Claude API, reviewed editorially through a local web UI, and served as static JSON from this repo.

## How it works

1. **Generate** — `scripts/generate.py` creates 8 candidate puzzles per day using the Claude API
2. **Review** — `scripts/review.py` launches a local web UI to browse candidates and pick winners
3. **Push** — Approved puzzles are committed and pushed to GitHub from the review UI
4. **Serve** — iOS app fetches puzzles from `https://raw.githubusercontent.com/carbonsf/dehaiku-daily/main/puzzles/{YYYY}/{MM}/{DD}.json`

## Quick start

```bash
# One-time setup
pip install anthropic
export ANTHROPIC_API_KEY="sk-ant-..."   # add to ~/.zshrc for persistence

# Generate candidates for the next 7 days
python3 /Users/carbon/Documents/daily-haiku/scripts/generate.py

# Open the review interface
python3 /Users/carbon/Documents/daily-haiku/scripts/review.py
```

## Repo structure

```
puzzles/              <- approved, live puzzles (app reads from here)
  2026/05/31.json
candidates/           <- generated options awaiting review (gitignored)
  2026-05-31/
    1.json ... 8.json
config/
  themes.json         <- theme rotation schedule
  banned-words.json   <- FIFO list to prevent haiku first-word repetition
scripts/
  generate.py         <- puzzle generation (Claude API)
  review.py           <- local web review server
  purge.py            <- unapprove/delete future puzzles
.github/workflows/
  generate.yml        <- weekly auto-generation via GitHub Actions
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

# Custom themes (one per day, N themes = N days)
python3 scripts/generate.py --themes "winter wonderland,cozy cabin,holiday feast"

# Specific start date
python3 scripts/generate.py --day 2026-06-15

# Seed words mixed into the 12-word pool (may land as answers or decoys)
python3 scripts/generate.py --seeds "tree,gift,snow"

# Regenerate even if candidates already exist
python3 scripts/generate.py --day 2026-06-01 --force

# Combine flags
python3 scripts/generate.py --day 2026-12-25 --themes "christmas" --seeds "tree,gift,snow" --force
```

**How generation works:**
1. Build a 12-word pool (seed words + API-generated words for the theme)
2. Randomly draw 4 as answer words; the remaining 8 become decoys
3. Generate a haiku encoding the 4 answer words (production prompt with CONSTRAINTS block)
4. Leak check: if any answer word or its stem appears in the haiku, retry with feedback
5. Truncation guard: verify the haiku isn't cut off
6. Repeat 8 times with distinct creative "angle cues" for diversity

**Environment variables:**
- `ANTHROPIC_API_KEY` — required
- `ANTHROPIC_MODEL` — override model (default: `claude-opus-4-20250514`)

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

## CI / GitHub Actions

The workflow (`.github/workflows/generate.yml`) runs every Monday at 6:00 UTC and generates candidates for the week. You can also trigger it manually from the Actions tab.

**Required secret:** Add `ANTHROPIC_API_KEY` to repo Settings → Secrets and variables → Actions.

## Theme rotation

Themes cycle through `config/themes.json`:
```json
["nature", "urban life", "seasons", "emotions", "food & drink", "travel", "nostalgia"]
```

Each day maps to a theme by `(day_of_year % len(themes))`. Override with `--themes` on the CLI or the theme field in the review UI's regenerate bar.

## Banned words

`config/banned-words.json` tracks a FIFO list of haiku first-words (max 50) to prevent repetitive openings. Updated automatically when you approve a puzzle.
