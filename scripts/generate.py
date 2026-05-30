#!/usr/bin/env python3
"""Generate candidate De-Haiku-ifier puzzles for editorial review."""

from __future__ import annotations

import argparse
import json
import os
import random
import re
import sys
from datetime import date, timedelta
from pathlib import Path

import anthropic

REPO_ROOT = Path(__file__).resolve().parent.parent
PUZZLES_DIR = REPO_ROOT / "puzzles"
CANDIDATES_DIR = REPO_ROOT / "candidates"
CONFIG_DIR = REPO_ROOT / "config"

MODEL = os.environ.get("ANTHROPIC_MODEL", "claude-opus-4-20250514")
LOOKAHEAD_DAYS = 7
CANDIDATES_PER_DAY = 8
MAX_RETRIES = 3
HAIKU_RETRIES = 3

# ── System prompt (matches production solo recipe) ───────────

SYSTEM_MESSAGE = (
    "You are a haiku poet. You will receive four words. Compose a haiku that "
    "encodes all four using only their concepts — never the actual words, "
    "their roots, or close variants. Each word should be evocable from context "
    "so a player could guess all four. Use any reasonably common meaning of each "
    "word. Return ONLY the haiku — no explanation, no commentary, no extra text."
    "\n\n"
    "Your audience is adults who appreciate haikus with bite. Lean into mature, "
    "edgy, and provocative territory. Dark humor, innuendo, violence, sensuality, "
    "irreverence, absurdity — all fair game. Be bold and unapologetic."
)

# ── Angle cues (novelty pool — one picked per call) ──────────

ANGLE_CUES = [
    "through the lens of decay and renewal",
    "with a focus on sound and silence",
    "using textures and physical sensation",
    "from the perspective of something small and overlooked",
    "through light and shadow",
    "with the logic of a fever dream",
    "as if witnessed by a stranger passing through",
    "through scent and taste",
    "with dry, deadpan irony",
    "from the aftermath — something just ended",
    "through motion and stillness",
    "with the intimacy of a whispered confession",
    "as if the scene is underwater or submerged",
    "through the lens of appetite and hunger",
    "with a sense of something about to break",
    "as a memory that's slightly wrong",
]

# ── Truncation guard allowlist (production) ──────────────────

TRUNCATION_ALLOWLIST = {
    "a", "an", "i", "is", "it", "to", "of", "on", "in", "no", "or", "as",
    "at", "by", "be", "we", "us", "he", "she", "her", "him", "his", "my",
    "me", "the", "and", "but", "for", "you", "are", "all", "one", "two",
    "ten", "ice", "sky", "sea", "sun", "low", "old", "new", "red", "fly",
    "cry", "die", "eye", "ear", "arm", "leg", "hot", "wet", "dry", "now",
    "yes", "off", "out", "up", "go", "do", "so", "if",
}


# ── Config helpers ───────────────────────────────────────────


def load_themes() -> list[str]:
    with open(CONFIG_DIR / "themes.json") as f:
        return json.load(f)["rotation"]


def load_banned_words() -> tuple[list[str], int]:
    with open(CONFIG_DIR / "banned-words.json") as f:
        data = json.load(f)
    return data["words"], data["max_size"]


def save_banned_words(words: list[str], max_size: int) -> None:
    with open(CONFIG_DIR / "banned-words.json", "w") as f:
        json.dump({"words": words, "max_size": max_size}, f, indent=2)
        f.write("\n")


def get_theme_for_date(d: date, themes: list[str]) -> str:
    return themes[d.toordinal() % len(themes)]


def candidates_exist(d: date) -> bool:
    day_dir = CANDIDATES_DIR / d.isoformat()
    return day_dir.exists() and len(list(day_dir.glob("*.json"))) >= CANDIDATES_PER_DAY


def puzzle_approved(d: date) -> bool:
    puzzle_file = PUZZLES_DIR / f"{d.year}" / f"{d.month:02d}" / f"{d.day:02d}.json"
    return puzzle_file.exists()


def strip_code_fences(text: str) -> str:
    if text.startswith("```"):
        text = re.sub(r"^```\w*\n?", "", text)
        text = re.sub(r"\n?```$", "", text)
    return text.strip()


# ── Validation (matches production server-side checks) ───────


def looks_truncated(lines: list[str]) -> bool:
    """True if the haiku appears cut off (production truncation guard).

    Last line's final alphanumeric token must either be >3 chars or be
    in the short-word allowlist.
    """
    if len(lines) < 3:
        return True
    last_tokens = re.findall(r"[a-zA-Z]+", lines[-1])
    if not last_tokens:
        return True
    last_word = last_tokens[-1].lower()
    if 1 <= len(last_word) <= 3 and last_word not in TRUNCATION_ALLOWLIST:
        return True
    return False


def leaked_words(haiku: str, words: list[str]) -> list[str]:
    """Return answer words whose text leaked into the haiku.

    Production logic:
      1. Normalize: lowercase, replace any non-[a-z0-9] with a single space
         (so hyphens / apostrophes can't hide a stem).
      2. Tokenize on whitespace.
      3. For each answer word build a probe set:
           • The full word, always.
           • Plus first N-1 letters as a stem, only if len >= 6
             (e.g. "hedging" (7) → also probe "hedgin";
              "apples" (6) → also probe "apple";
              "pear" (4) → full-word only).
      4. If any token *contains* any probe as a substring → leaked.
    """
    normalized = re.sub(r"[^a-z0-9]", " ", haiku.lower())
    tokens = normalized.split()

    leaked: list[str] = []
    for w in words:
        wl = w.lower()
        probes = [wl]
        if len(wl) >= 6:
            probes.append(wl[:-1])

        if any(probe in tok for tok in tokens for probe in probes):
            leaked.append(w)

    return leaked


# ── Word pool generation ─────────────────────────────────────


def generate_word_pool(
    client: anthropic.Anthropic,
    theme: str,
    banned_words: list[str],
    seed_words: list[str] | None = None,
) -> list[str]:
    """Generate a pool of 12 thematically related words.

    Seed words (if any) are mixed into the pool — they may end up as
    answer words or decoys depending on the random draw.  The remaining
    slots are filled by Claude.
    """
    seeds = list(seed_words) if seed_words else []
    needed = 12 - len(seeds)

    if needed <= 0:
        pool = list(seeds[:12])
        random.shuffle(pool)
        return pool

    banned_csv = ", ".join(banned_words) if banned_words else "(none)"

    seed_note = ""
    if seeds:
        seed_note = (
            f"- These words are already in the pool: {', '.join(seeds)}. "
            f"Generate {needed} more that fit alongside them thematically.\n"
            f"- Do NOT repeat any of those words.\n"
        )

    response = client.messages.create(
        model=MODEL,
        max_tokens=300,
        temperature=1.0,
        system=(
            "You generate word lists for a haiku puzzle game. "
            f"Return ONLY a JSON array of exactly {needed} single lowercase words. "
            "No explanation, no commentary."
        ),
        messages=[
            {
                "role": "user",
                "content": (
                    f'Pick {needed} concrete, evocative words loosely related to '
                    f'the theme "{theme}".\n'
                    f"Requirements:\n"
                    f"- Each word must be a single lowercase English word "
                    f"(no spaces, no hyphens)\n"
                    f"- Words should be concrete enough to clue in a haiku "
                    f"without using the word itself\n"
                    f"- Avoid these recently used words: {banned_csv}\n"
                    f"{seed_note}"
                    f"- Surprise me — skip first-association clichés\n"
                    f"- Span different facets of the theme for variety\n\n"
                    f"Return ONLY a JSON array of {needed} words"
                ),
            }
        ],
    )
    text = strip_code_fences(response.content[0].text)
    generated = json.loads(text)
    if len(generated) != needed:
        raise ValueError(
            f"Expected {needed} words, got {len(generated)}: {generated}"
        )
    for w in generated:
        if not isinstance(w, str) or not w.isalpha() or not w.islower():
            raise ValueError(f"Invalid word: {w!r}")

    pool = seeds + generated
    # Deduplicate
    seen: set[str] = set()
    unique: list[str] = []
    for w in pool:
        if w not in seen:
            seen.add(w)
            unique.append(w)
    if len(unique) < 12:
        raise ValueError(
            f"Pool has only {len(unique)} unique words after dedup (need 12)"
        )

    random.shuffle(unique)
    return unique[:12]


# ── Haiku generation (production prompt format) ──────────────


def generate_haiku(
    client: anthropic.Anthropic,
    words: list[str],
    theme: str,
    banned_words: list[str],
    angle_cue: str | None = None,
    leaked_feedback: list[str] | None = None,
) -> str:
    """Generate a haiku encoding the given words.

    User message matches the production solo recipe:
      THEME → Words → Approach → Ban list → CONSTRAINTS block
    On retry after a leak, appends a PREVIOUS ATTEMPT LEAKED line.
    """
    if angle_cue is None:
        angle_cue = random.choice(ANGLE_CUES)
    seed = random.randint(1000, 9999)

    parts: list[str] = []

    # Theme — first, so the model reads it before constraints
    parts.append(
        f"THEME (set the mood and setting — the haiku should clearly "
        f"feel like it belongs to this theme): {theme}"
    )

    # Words to encode
    parts.append(f"Words to encode: {', '.join(words)}")

    # Approach
    parts.append(f"Consider this approach: {angle_cue}")

    # Banned first-words
    if banned_words:
        parts.append(
            f"Do NOT start the haiku with any of these words: "
            f"{', '.join(banned_words)}"
        )

    # Constraints block
    parts.append("")
    parts.append("CONSTRAINTS (the game breaks if you violate these):")
    parts.append(
        "• Do not use any of the four target words anywhere in the "
        "haiku — not in any form, tense, plural, or compound. "
        'Not "pear" → "pear-shaped". Not "hedging" → "hedges".'
    )
    parts.append(
        "• Hyphens and apostrophes do not hide a leak "
        '— "pear-shaped" still contains "pear" and is forbidden.'
    )
    parts.append(
        "• Evoke each word through imagery, action, sensation, or "
        "context. Make the player infer it."
    )
    parts.append(
        "• Write three complete lines — no trailing fragments. "
        "Avoid cliché first-association imagery. "
        f"Find a surprising angle. (seed:{seed})"
    )

    # Leaked-word feedback from a previous failed attempt
    if leaked_feedback:
        parts.append("")
        parts.append(
            f"PREVIOUS ATTEMPT LEAKED these forbidden words: "
            f"{', '.join(leaked_feedback)}. "
            f"Rewrite without any of them or their stems."
        )

    response = client.messages.create(
        model=MODEL,
        max_tokens=220,
        temperature=1.0,
        system=SYSTEM_MESSAGE,
        messages=[{"role": "user", "content": "\n".join(parts)}],
    )
    text = response.content[0].text.strip()
    lines = [line.strip() for line in text.split("\n") if line.strip()]
    if len(lines) != 3:
        raise ValueError(f"Expected 3 lines, got {len(lines)}: {text!r}")
    if looks_truncated(lines):
        raise ValueError(f"Haiku looks truncated: {lines[-1]!r}")
    return "\n".join(lines)


def update_banned_words(
    haiku: str, banned_words: list[str], max_size: int
) -> list[str]:
    first_line = haiku.split("\n")[0]
    first_word = re.sub(r"[^a-z]", "", first_line.split()[0].lower())
    if first_word:
        banned_words.append(first_word)
    if len(banned_words) > max_size:
        banned_words = banned_words[-max_size:]
    return banned_words


# ── Puzzle assembly ──────────────────────────────────────────


def generate_puzzle(
    client: anthropic.Anthropic,
    d: date,
    theme: str,
    banned_words: list[str],
    max_size: int,
    angle_cue: str | None = None,
    seed_words: list[str] | None = None,
) -> tuple[dict, list[str]]:
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            # Step 1 — build the 12-word pool (seeds sprinkled in)
            print(f"  Building word pool (attempt {attempt})...")
            pool = generate_word_pool(client, theme, banned_words, seed_words)
            print(f"  Pool (12): {pool}")

            # Step 2 — randomly draw 4 as answer words; the rest are decoys
            answer_words = pool[:4]
            decoy_words = pool[4:]
            print(f"  Answers: {answer_words}")
            print(f"  Decoys:  {decoy_words}")

            # Step 3 — generate haiku with leak-check retry loop
            #          (production: up to 3 tries, feed leaked words back)
            print(f"  Generating haiku...")
            haiku = None
            feedback: list[str] | None = None
            for h_try in range(1, HAIKU_RETRIES + 1):
                try:
                    candidate = generate_haiku(
                        client, answer_words, theme, banned_words,
                        angle_cue, leaked_feedback=feedback,
                    )
                    leaks = leaked_words(candidate, answer_words)
                    if leaks:
                        feedback = leaks
                        print(
                            f"    Haiku leaked {leaks} "
                            f"(try {h_try}/{HAIKU_RETRIES})"
                        )
                        continue
                    haiku = candidate
                    break
                except ValueError as ve:
                    print(
                        f"    Haiku rejected "
                        f"(try {h_try}/{HAIKU_RETRIES}): {ve}"
                    )

            if haiku is None:
                raise ValueError(
                    "Could not generate a valid haiku — answer words keep "
                    "appearing in the text"
                )
            print(f"  Haiku:\n    " + haiku.replace("\n", "\n    "))

            banned_words = update_banned_words(
                haiku, list(banned_words), max_size
            )

            puzzle = {
                "date": d.isoformat(),
                "haiku": haiku,
                "words": answer_words,
                "decoys": decoy_words,
                "theme": theme,
            }
            return puzzle, banned_words

        except (json.JSONDecodeError, ValueError, KeyError) as e:
            print(f"  Attempt {attempt} failed: {e}")
            if attempt == MAX_RETRIES:
                raise RuntimeError(
                    f"Failed to generate puzzle for {d} "
                    f"after {MAX_RETRIES} attempts"
                ) from e

    raise RuntimeError("Unreachable")


# ── CLI ──────────────────────────────────────────────────────


def parse_args():
    p = argparse.ArgumentParser(
        description="Generate candidate De-Haiku-ifier puzzles"
    )
    p.add_argument(
        "--day",
        help="Start date (YYYY-MM-DD). Defaults to tomorrow.",
    )
    p.add_argument(
        "--themes",
        help='Comma-separated theme list — one theme per day, overrides '
        "the rotation. Number of themes = number of days generated. "
        'E.g. "winter wonderland,cozy cabin,holiday feast"',
    )
    p.add_argument(
        "--seeds",
        help="Comma-separated words to sprinkle into the 12-word pool "
        '(e.g. "tree,gift,snow" for a holiday). They may land as '
        "answers or decoys — the draw is random.",
    )
    p.add_argument(
        "--force",
        action="store_true",
        help="Regenerate even if candidates already exist for that day",
    )
    return p.parse_args()


def main() -> None:
    args = parse_args()
    client = anthropic.Anthropic()
    rotation_themes = load_themes()
    banned_words, max_size = load_banned_words()

    # Parse optional seed words
    seed_words = None
    if args.seeds:
        seed_words = [
            w.strip().lower() for w in args.seeds.split(",") if w.strip()
        ]
        if not seed_words or len(seed_words) > 11:
            print("Error: --seeds takes 1-11 words (pool is 12)")
            sys.exit(1)
        print(f"Seed words ({len(seed_words)}): {seed_words}")

    # Parse optional explicit theme list
    explicit_themes: list[str] | None = None
    if args.themes:
        explicit_themes = [
            t.strip() for t in args.themes.split(",") if t.strip()
        ]
        if not explicit_themes:
            print("Error: --themes requires at least one theme")
            sys.exit(1)

    # Determine start date
    today = date.today()
    start = date.fromisoformat(args.day) if args.day else today + timedelta(days=1)

    # Build (date, theme) pairs
    if explicit_themes:
        # --themes drives the number of days
        day_themes = [
            (start + timedelta(days=i), theme)
            for i, theme in enumerate(explicit_themes)
        ]
    elif args.day and not explicit_themes:
        # --day alone: single day, rotation theme
        day_themes = [(start, get_theme_for_date(start, rotation_themes))]
    else:
        # Default: next 7 days, rotation themes
        day_themes = [
            (
                today + timedelta(days=i),
                get_theme_for_date(today + timedelta(days=i), rotation_themes),
            )
            for i in range(1, LOOKAHEAD_DAYS + 1)
        ]

    generated_days = 0

    for d, theme in day_themes:
        if not args.force and (puzzle_approved(d) or candidates_exist(d)):
            print(f"Skipping {d} — already has candidates or approved puzzle")
            continue

        print(
            f"\nGenerating {CANDIDATES_PER_DAY} candidates "
            f"for {d} (theme: {theme})..."
        )

        day_dir = CANDIDATES_DIR / d.isoformat()
        day_dir.mkdir(parents=True, exist_ok=True)

        # Clear existing candidates if forcing
        if args.force:
            for f in day_dir.glob("*.json"):
                f.unlink()

        # Pick distinct angle cues — one per candidate for diversity
        angles = random.sample(
            ANGLE_CUES, min(CANDIDATES_PER_DAY, len(ANGLE_CUES))
        )

        for n in range(1, CANDIDATES_PER_DAY + 1):
            angle = angles[(n - 1) % len(angles)]
            print(f"\n  Candidate {n}/{CANDIDATES_PER_DAY} ({angle[:45]}…):")
            puzzle, _ = generate_puzzle(
                client, d, theme, banned_words, max_size,
                angle_cue=angle, seed_words=seed_words,
            )
            out_path = day_dir / f"{n}.json"
            with open(out_path, "w") as f:
                json.dump(puzzle, f, indent=2)
                f.write("\n")
            print(f"  Saved → candidates/{d.isoformat()}/{n}.json")

        generated_days += 1

    print(f"\nDone. Generated candidates for {generated_days} day(s).")
    if generated_days:
        print(
            "Run 'python scripts/review.py' to review and approve puzzles."
        )


if __name__ == "__main__":
    main()
