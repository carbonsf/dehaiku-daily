#!/usr/bin/env python3
"""Generate daily De-Haiku-ifier puzzles and deposit them in queue/ for review."""

import json
import random
import re
import sys
from datetime import date, timedelta
from pathlib import Path

import anthropic

REPO_ROOT = Path(__file__).resolve().parent.parent
PUZZLES_DIR = REPO_ROOT / "puzzles"
QUEUE_DIR = REPO_ROOT / "queue"
CONFIG_DIR = REPO_ROOT / "config"

MODEL = "claude-opus-4-20250514"
LOOKAHEAD_DAYS = 7
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


def puzzle_exists(d: date) -> bool:
    queue_file = QUEUE_DIR / f"{d.isoformat()}.json"
    puzzle_file = PUZZLES_DIR / f"{d.year}" / f"{d.month:02d}" / f"{d.day:02d}.json"
    return queue_file.exists() or puzzle_file.exists()


def strip_code_fences(text: str) -> str:
    if text.startswith("```"):
        text = re.sub(r"^```\w*\n?", "", text)
        text = re.sub(r"\n?```$", "", text)
    return text.strip()


def generate_source_words(
    client: anthropic.Anthropic, theme: str, banned_words: list[str]
) -> list[str]:
    banned_csv = ", ".join(banned_words) if banned_words else "(none)"
    response = client.messages.create(
        model=MODEL,
        max_tokens=200,
        temperature=1.0,
        system=(
            "You generate word lists for a haiku puzzle game. "
            "Return ONLY a JSON array of exactly 4 single lowercase words. "
            "No explanation, no commentary."
        ),
        messages=[
            {
                "role": "user",
                "content": (
                    f'Pick 4 concrete, evocative words loosely related to the theme "{theme}".\n'
                    f"Requirements:\n"
                    f"- Each word must be a single lowercase English word (no spaces, no hyphens)\n"
                    f"- Words should be concrete enough to clue in a haiku without using the word itself\n"
                    f"- Avoid these recently used words: {banned_csv}\n"
                    f"- Surprise me — skip first-association clichés\n"
                    f"- The 4 words should allow a cohesive haiku when combined\n\n"
                    f'Return ONLY a JSON array: ["word1", "word2", "word3", "word4"]'
                ),
            }
        ],
    )
    text = strip_code_fences(response.content[0].text)
    words = json.loads(text)
    if len(words) != 4:
        raise ValueError(f"Expected 4 words, got {len(words)}: {words}")
    for w in words:
        if not isinstance(w, str) or not w.isalpha() or not w.islower():
            raise ValueError(f"Invalid word: {w!r}")
    return words


def generate_haiku(
    client: anthropic.Anthropic,
    words: list[str],
    theme: str,
    banned_words: list[str],
) -> str:
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


def generate_decoys(
    client: anthropic.Anthropic,
    theme: str,
    source_words: list[str],
    haiku: str,
) -> list[str]:
    response = client.messages.create(
        model=MODEL,
        max_tokens=200,
        temperature=1.0,
        system=(
            "You generate distractor words for a haiku puzzle game. "
            "Return ONLY a JSON array of exactly 8 single lowercase words. "
            "No explanation."
        ),
        messages=[
            {
                "role": "user",
                "content": (
                    f'Theme: "{theme}"\n'
                    f"Haiku:\n{haiku}\n\n"
                    f"The correct answer words are: {', '.join(source_words)}\n\n"
                    f"Generate 8 decoy words that:\n"
                    f"- Are thematically adjacent and tempting\n"
                    f"- Are NOT any of the correct words, their roots, or close variants\n"
                    f"- Are single lowercase English words (no spaces, no hyphens)\n"
                    f"- Would make a player second-guess themselves\n\n"
                    f'Return ONLY a JSON array: ["w1", "w2", "w3", "w4", "w5", "w6", "w7", "w8"]'
                ),
            }
        ],
    )
    text = strip_code_fences(response.content[0].text)
    decoys = json.loads(text)
    if len(decoys) != 8:
        raise ValueError(f"Expected 8 decoys, got {len(decoys)}: {decoys}")
    overlap = set(decoys) & set(source_words)
    if overlap:
        raise ValueError(f"Decoys overlap with source words: {overlap}")
    for w in decoys:
        if not isinstance(w, str) or not w.isalpha() or not w.islower():
            raise ValueError(f"Invalid decoy: {w!r}")
    return decoys


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


def generate_puzzle(
    client: anthropic.Anthropic,
    d: date,
    theme: str,
    banned_words: list[str],
    max_size: int,
) -> tuple[dict, list[str]]:
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            print(f"  Generating source words (attempt {attempt})...")
            words = generate_source_words(client, theme, banned_words)
            print(f"  Words: {words}")

            print(f"  Generating haiku...")
            haiku = generate_haiku(client, words, theme, banned_words)
            print(f"  Haiku:\n    " + haiku.replace("\n", "\n    "))

            print(f"  Generating decoys...")
            decoys = generate_decoys(client, theme, words, haiku)
            print(f"  Decoys: {decoys}")

            banned_words = update_banned_words(haiku, list(banned_words), max_size)

            puzzle = {
                "date": d.isoformat(),
                "haiku": haiku,
                "words": words,
                "decoys": decoys,
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


def main() -> None:
    client = anthropic.Anthropic()
    themes = load_themes()
    banned_words, max_size = load_banned_words()

    today = date.today()
    generated = 0

    for i in range(1, LOOKAHEAD_DAYS + 1):
        d = today + timedelta(days=i)
        if puzzle_exists(d):
            print(f"Skipping {d} — already exists")
            continue

        theme = get_theme_for_date(d, themes)
        print(f"\nGenerating puzzle for {d} (theme: {theme})...")

        puzzle, banned_words = generate_puzzle(client, d, theme, banned_words, max_size)

        QUEUE_DIR.mkdir(parents=True, exist_ok=True)
        out_path = QUEUE_DIR / f"{d.isoformat()}.json"
        with open(out_path, "w") as f:
            json.dump(puzzle, f, indent=2)
            f.write("\n")
        print(f"  Written to {out_path}")
        generated += 1

    save_banned_words(banned_words, max_size)
    print(f"\nDone. Generated {generated} puzzle(s).")


if __name__ == "__main__":
    main()
