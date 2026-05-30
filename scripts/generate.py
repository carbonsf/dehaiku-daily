#!/usr/bin/env python3
"""Generate candidate De-Haiku-ifier puzzles for editorial review."""

from __future__ import annotations

import argparse
import json
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

MODEL = "claude-opus-4-20250514"
LOOKAHEAD_DAYS = 7
CANDIDATES_PER_DAY = 8
MAX_RETRIES = 3

SYSTEM_MESSAGE = (
    "You are a haiku poet. You will receive four words. Compose a haiku that "
    "encodes all four using only their concepts — never the actual words, "
    "their roots, or close variants. Each word should be evocable from context "
    "so a player could guess all four. Use any reasonably common meaning of each "
    "word. Return ONLY the haiku — no explanation, no commentary, no extra text.\n\n"
    "Your audience is adults who appreciate haikus with bite. Lean into mature, "
    "edgy, and provocative territory. Dark humor, innuendo, violence, sensuality, "
    "irreverence, absurdity — all fair game. Be bold and unapologetic."
)

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


def validate_haiku(haiku: str, words: list[str]) -> None:
    """Raise ValueError if any answer word or close variant appears in the haiku.

    This is the core game rule: the haiku encodes concepts, never the words
    themselves, their roots, or obvious inflections.
    """
    haiku_tokens = set(re.findall(r"[a-z]+", haiku.lower()))
    for w in words:
        wl = w.lower()
        # Exact match
        if wl in haiku_tokens:
            raise ValueError(f"Answer word '{w}' appears verbatim in haiku")
        # Build common inflected / derived forms
        variants = {
            wl + "s", wl + "es", wl + "ed", wl + "er", wl + "est",
            wl + "ing", wl + "ly", wl + "ful", wl + "ness", wl + "ment",
            wl + "en", wl + "ish", wl + "y",
        }
        # Handle trailing-e words: bake→baked/baking/baker
        if wl.endswith("e"):
            stem = wl[:-1]
            variants |= {stem + "ed", stem + "ing", stem + "er", stem + "est"}
        # Handle consonant+y: carry→carried/carries
        if wl.endswith("y") and len(wl) > 2 and wl[-2] not in "aeiou":
            stem = wl[:-1]
            variants |= {stem + "ied", stem + "ies", stem + "ier", stem + "iest"}
        found = variants & haiku_tokens
        if found:
            raise ValueError(
                f"Variant '{found.pop()}' of answer word '{w}' found in haiku"
            )
        # Catch longer derivations: if answer is 4+ chars and a haiku token
        # starts with it (e.g. "blood" → "bloody", "blooded")
        if len(wl) >= 4:
            for token in haiku_tokens:
                if token != wl and token.startswith(wl) and len(token) <= len(wl) + 4:
                    raise ValueError(
                        f"Haiku word '{token}' derives from answer word '{w}'"
                    )


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
        # More seeds than slots — take 12 and shuffle
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
                    f'Pick {needed} concrete, evocative words loosely related to the theme "{theme}".\n'
                    f"Requirements:\n"
                    f"- Each word must be a single lowercase English word (no spaces, no hyphens)\n"
                    f"- Words should be concrete enough to clue in a haiku without using the word itself\n"
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
        raise ValueError(f"Expected {needed} words, got {len(generated)}: {generated}")
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


def generate_haiku(
    client: anthropic.Anthropic,
    words: list[str],
    theme: str,
    banned_words: list[str],
    angle_cue: str | None = None,
) -> str:
    if angle_cue is None:
        angle_cue = random.choice(ANGLE_CUES)
    seed = random.randint(1000, 9999)

    user_parts = [f"Words: {', '.join(words)}"]
    user_parts.append(f"Approach: {angle_cue}")
    user_parts.append(
        f'Theme: "{theme}" — weave it as mood/setting, don\'t overpower the word clues'
    )
    if banned_words:
        user_parts.append(
            f"FORBIDDEN — do NOT start the haiku with any of these words: "
            f"{', '.join(banned_words)}"
        )
    user_parts.append(
        f"Avoid cliché first-association imagery. Find a surprising angle. (seed:{seed})"
    )

    response = client.messages.create(
        model=MODEL,
        max_tokens=100,
        temperature=1.0,
        system=SYSTEM_MESSAGE,
        messages=[{"role": "user", "content": "\n".join(user_parts)}],
    )
    text = response.content[0].text.strip()
    lines = [line.strip() for line in text.split("\n") if line.strip()]
    if len(lines) != 3:
        raise ValueError(f"Expected 3 lines, got {len(lines)}: {text!r}")
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


HAIKU_RETRIES = 3  # inner retries for haiku validation


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
            answer_words = pool[:4]     # pool is already shuffled
            decoy_words = pool[4:]
            print(f"  Answers: {answer_words}")
            print(f"  Decoys:  {decoy_words}")

            # Step 3 — generate haiku encoding the 4 answer words
            #          (retry the haiku if answer words leak into the text)
            print(f"  Generating haiku...")
            haiku = None
            for h_try in range(1, HAIKU_RETRIES + 1):
                candidate = generate_haiku(
                    client, answer_words, theme, banned_words, angle_cue
                )
                try:
                    validate_haiku(candidate, answer_words)
                    haiku = candidate
                    break
                except ValueError as ve:
                    print(f"    Haiku rejected (try {h_try}/{HAIKU_RETRIES}): {ve}")
            if haiku is None:
                raise ValueError(
                    "Could not generate a valid haiku — answer words keep "
                    "appearing in the text"
                )
            print(f"  Haiku:\n    " + haiku.replace("\n", "\n    "))

            banned_words = update_banned_words(haiku, list(banned_words), max_size)

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
                    f"Failed to generate puzzle for {d} after {MAX_RETRIES} attempts"
                ) from e

    raise RuntimeError("Unreachable")


def parse_args():
    p = argparse.ArgumentParser(description="Generate candidate De-Haiku-ifier puzzles")
    p.add_argument(
        "--day",
        help="Generate for a specific date (YYYY-MM-DD) instead of the next 7 days",
    )
    p.add_argument(
        "--seeds",
        help="Comma-separated words to sprinkle into the 12-word pool "
        '(e.g. "tree,gift,snow" for a holiday). They may land as answers '
        "or decoys — the draw is random.",
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
    themes = load_themes()
    banned_words, max_size = load_banned_words()

    # Parse optional seed words
    seed_words = None
    if args.seeds:
        seed_words = [w.strip().lower() for w in args.seeds.split(",") if w.strip()]
        if not seed_words or len(seed_words) > 11:
            print("Error: --seeds takes 1-11 comma-separated words (pool is 12)")
            sys.exit(1)
        print(f"Seed words ({len(seed_words)}): {seed_words}")

    # Determine which days to generate
    today = date.today()
    if args.day:
        days_to_generate = [date.fromisoformat(args.day)]
    else:
        days_to_generate = [today + timedelta(days=i) for i in range(1, LOOKAHEAD_DAYS + 1)]

    generated_days = 0

    for d in days_to_generate:
        if not args.force and (puzzle_approved(d) or candidates_exist(d)):
            print(f"Skipping {d} — already has candidates or approved puzzle")
            continue

        theme = get_theme_for_date(d, themes)
        print(f"\nGenerating {CANDIDATES_PER_DAY} candidates for {d} (theme: {theme})...")

        day_dir = CANDIDATES_DIR / d.isoformat()
        day_dir.mkdir(parents=True, exist_ok=True)

        # Clear existing candidates if forcing
        if args.force:
            for f in day_dir.glob("*.json"):
                f.unlink()

        # Pick distinct angle cues — one per candidate for diversity
        angles = random.sample(ANGLE_CUES, min(CANDIDATES_PER_DAY, len(ANGLE_CUES)))

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
        print("Run 'python scripts/review.py' to review and approve puzzles.")


if __name__ == "__main__":
    main()
