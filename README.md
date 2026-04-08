# dehaiku-daily

Static puzzle pipeline for the De-Haiku-ifier daily mode. Puzzles are generated via Claude API, reviewed editorially, and served as static JSON from this repo.

## How it works

1. **GitHub Actions** runs `scripts/generate.py` weekly (or on manual dispatch), generating puzzles for the next 7 days into `queue/`
2. **Editorial review** — approve puzzles by moving them from `queue/` to `puzzles/YYYY/MM/DD.json`
3. **iOS app** fetches puzzles from `https://raw.githubusercontent.com/carbonsf/dehaiku-daily/main/puzzles/{YYYY}/{MM}/{DD}.json`

## Repo structure

```
puzzles/          ← approved, live puzzles (app reads from here)
queue/            ← generated, awaiting review
rejected/         ← reviewed and rejected
config/
  themes.json     ← theme rotation schedule
  banned-words.json ← FIFO list to prevent repetition
scripts/
  generate.py     ← puzzle generation script
```

## Puzzle JSON format

```json
{
  "date": "2026-04-08",
  "haiku": "Bamboo shadows bend\nacross the stone where we sat\nrain fills both our cups",
  "words": ["bamboo", "stone", "sat", "rain"],
  "decoys": ["wind", "moon", "silk", "drift", "temple", "cloud", "pine", "ash"],
  "theme": "zen garden"
}
```

## Editorial workflow

```bash
# Approve a single puzzle
mkdir -p puzzles/2026/04
mv queue/2026-04-10.json puzzles/2026/04/10.json

# Batch approve
for f in queue/2026-04-*.json; do
  date=$(basename "$f" .json)
  y=${date:0:4}; m=${date:5:2}; d=${date:8:2}
  mkdir -p "puzzles/$y/$m"
  mv "$f" "puzzles/$y/$m/$d.json"
done

git add puzzles/ queue/
git commit -m "Approve dailies through 2026-04-16"
git push
```

## Local generation

```bash
export ANTHROPIC_API_KEY=sk-...
pip install anthropic
python scripts/generate.py
```

## Setup

Add `ANTHROPIC_API_KEY` to repo Settings → Secrets and variables → Actions.
