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

MODEL = os.environ.get("ANTHROPIC_MODEL", "claude-opus-5-5")
CASUAL_MODEL = os.environ.get("ANTHROPIC_CASUAL_MODEL", "claude-sonnet-5")

# Opus 5.5 thinks on every call and thinking cannot be turned off, so
# effort is the only dial for how hard it works. The model's own default
# is "medium" — set it explicitly so that default moving never silently
# changes the poetry. This is a batch job with nobody waiting on it, and
# a weak haiku costs far more than the tokens: it burns gate retries and
# fresh word pools. Never run this below "high".
# Tunable upward: "high" | "xhigh" | "max".
EFFORT = os.environ.get("ANTHROPIC_EFFORT", "high")

LOOKAHEAD_DAYS = 7
CANDIDATES_PER_DAY = 8
MAX_POOL_ATTEMPTS = 6
HAIKU_RETRIES = 5
GATE_BUDGET = 4

# ── API helpers ─────────────────────────────────────────────


def _response_text(response) -> str:
    """Return the first text block of a Messages response, or "".

    Newer models may emit a thinking block before the text block, so
    content[0] is not guaranteed to be text.
    """
    for block in response.content:
        if getattr(block, "type", None) == "text":
            return block.text
    return ""


def load_api_key() -> str | None:
    """Resolve the Anthropic API key: env var first, then REPO_ROOT/.env.

    The review server runs this script as a subprocess and inherits the
    environment of whatever shell started it, so a key exported in a
    different terminal tab is invisible to it. A .env file (gitignored)
    works from either entry point.
    """
    key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    if key:
        return key
    env_file = REPO_ROOT / ".env"
    if env_file.exists():
        for line in env_file.read_text().splitlines():
            line = line.strip()
            if line.startswith("ANTHROPIC_API_KEY="):
                val = line.split("=", 1)[1].strip().strip('"').strip("'")
                if val:
                    return val
    return None


# ── Prompt (v8 poem-first — matches production web/lib/prompts.ts) ──

POEM_FIRST_RULES = (
    "POEM FIRST. Write a real poem: concrete sensory imagery, economy, an "
    "unexpected turn between the lines. The four words must live INSIDE one "
    "scene, each implied by what is there — not assembled as a list of "
    "separate clues. A haiku that reads as a string of riddles has failed, "
    "even if all four words are present. Coherence is what makes it both "
    "beautiful AND solvable.\n\n"
    "ENCODE BY CONCEPT, NOT SYNONYM. Each word should be recoverable by a "
    "thoughtful, patient reader through its setting, consequence, function, "
    "or a less-obvious sense — but none should be a one-word synonym a reader "
    'grabs at a glance ("blade" for knife, "embers" for campfire). When a '
    "word would be too easy, let the image carry it more obliquely rather "
    "than naming its synonym.\n\n"
    "COMPLETENESS IS NON-NEGOTIABLE. All four words must genuinely live in "
    "the image — each one anchored by something concretely present, so a "
    "patient reader can recover it. A beautiful poem that leaves a word with "
    "no foothold has failed just as badly as one that names a word outright. "
    "If a word has nowhere to sit in the scene, rework the scene so it earns "
    "a place — never let it fall away."
)

SYSTEM_MESSAGE = (
    "You are a haiku poet. You will receive four words. Compose ONE genuine "
    "haiku — a single vivid, coherent image with a turn — that quietly "
    "encodes all four words by concept, never by name.\n\n"
    f"{POEM_FIRST_RULES}\n\n"
    "Your audience is adults who appreciate haiku with bite — dark humor, "
    "innuendo, sensuality, irreverence are all welcome — but it must still "
    "read as a poem, not a puzzle. Return ONLY the haiku — no explanation, "
    "no commentary, no extra text."
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


def puzzle_approved(d: date) -> bool:
    puzzle_file = PUZZLES_DIR / f"{d.year}" / f"{d.month:02d}" / f"{d.day:02d}.json"
    return puzzle_file.exists()


def select_days(
    today: date,
    explicit_day: str | None = None,
    n_themes: int = 0,
    force: bool = False,
    approved=puzzle_approved,
) -> list[date]:
    """Choose which days to generate for.

    Today is included when it has no approved puzzle — otherwise the app
    has nothing to serve today and you would need --force to fix it.
    Once today is approved the window starts tomorrow, so a routine run
    never re-rolls the puzzle that is already live.

    --day always wins and names a single day. With --themes, scan forward
    skipping approved days so every theme lands on a day that needs one.
    Otherwise take a LOOKAHEAD_DAYS window; the caller skips approved
    days inside it.
    """
    if explicit_day:
        return [date.fromisoformat(explicit_day)]

    start = today if (force or not approved(today)) else today + timedelta(days=1)

    if n_themes:
        days: list[date] = []
        d = start
        while len(days) < n_themes:
            if force or not approved(d):
                days.append(d)
            d += timedelta(days=1)
        return days

    return [start + timedelta(days=i) for i in range(LOOKAHEAD_DAYS)]


# ── Syllable counting (5/7/5 validation) ───────────────────

def count_syllables(word: str) -> int:
    """Heuristic syllable count for English words."""
    w = word.lower().strip()
    if not w:
        return 0
    # Remove trailing silent e
    if len(w) > 2 and w.endswith("e") and w[-2] not in "aeiou":
        w = w[:-1]
    # Count vowel groups
    count = len(re.findall(r"[aeiouy]+", w))
    return max(1, count)


def line_syllables(line: str) -> int:
    words = re.findall(r"[a-zA-Z']+", line)
    return sum(count_syllables(w) for w in words)


def check_syllables(lines: list[str]) -> tuple[bool, list[int]]:
    """Check if haiku is 5/7/5. Returns (passed, [s1, s2, s3])."""
    counts = [line_syllables(line) for line in lines]
    # Allow ±1 tolerance since the counter is heuristic
    passed = (
        abs(counts[0] - 5) <= 1
        and abs(counts[1] - 7) <= 1
        and abs(counts[2] - 5) <= 1
    )
    return passed, counts


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


# ── Line structure (Layer 1 — deterministic, free) ────────────
# One specific defect, and nothing else: a phrase that spills over a line
# break and stops a word or two into the next line, leaving the rest of
# that line as a bolted-on tail —
#     silence in the tobacco / sack; we lose, he sighs
#     one filtered / organ gone—he roams.
#
# Punctuation itself is NOT the defect. The poems that work are punctuated
# sentences — a dash closing line one, a comma closing line two, a period
# to land — and an earlier version of this check that banned commas and
# capped dashes rejected 87% of the puzzles that had been approved by hand,
# flattening the output into three unpunctuated noun phrases. Measured
# against those same approved puzzles, the two rules below reject ~5% and
# still catch every example of the defect. Keep it this narrow;
# craft_probe() (Layer 2) covers what a regex can't see.

# Words that can only end a line if the phrase continues on the next one.
# Verb particles (in, on, by, up, through…) are deliberately absent:
# "the tide comes in" and "a ghost drifts by" are complete lines.
LINE_END_FUNCTION_WORDS = {
    "the", "a", "an", "of", "to", "and", "or", "nor", "with", "from",
    "into", "than", "like", "as", "for", "at", "my", "your", "their",
    "our", "its",
}

_LINE_END_PUNCT = re.compile(r"(—|–|--|[,;:.!?])\s*$")
_BREAK = re.compile(r"—|–|--|[,;:]|[.!?](?=\s*\S)")
_TRAILING = ".,;:—–!?- "
RUNOVER_MAX_WORDS = 2


def _words_before_first_break(line: str) -> int | None:
    """Words in the line before its first internal break, or None if the
    line has no internal break (trailing punctuation doesn't count)."""
    core = line.strip().rstrip(_TRAILING)
    m = _BREAK.search(core)
    if not m:
        return None
    return len(re.findall(r"[A-Za-z']+", core[: m.start()]))


class StructureError(ValueError):
    """Haiku line structure is fragmented. The message is retry feedback."""


def check_structure(lines: list[str]) -> str | None:
    """Return a reason the line structure is broken, or None if clean."""
    for i in range(len(lines) - 1):
        if _LINE_END_PUNCT.search(lines[i]):
            continue  # the line closes cleanly; whatever follows starts fresh
        words = re.findall(r"[A-Za-z']+", lines[i])
        if words and words[-1].lower() in LINE_END_FUNCTION_WORDS:
            return (
                f'line {i + 1} ends on "{words[-1]}", so its phrase is cut '
                f"in half by the line break"
            )
        spill = _words_before_first_break(lines[i + 1])
        if spill is not None and 1 <= spill <= RUNOVER_MAX_WORDS:
            stub = " ".join(re.findall(r"[A-Za-z'-]+", lines[i + 1])[:spill])
            return (
                f"line {i + 1} spills over the break and stops at "
                f'"{stub}" in line {i + 2}, leaving the rest of that line '
                f"as a tacked-on tail"
            )
    return None


def strip_line_end_commas(haiku: str) -> str:
    """Remove a comma that closes a line. Post-processing only.

    The model is allowed — encouraged — to write a punctuated sentence,
    because that is what gives the poem its grammar and its turn. But a
    haiku on the page doesn't end its lines with commas: the line break
    is already the pause. So the comma is dropped from the published text.

    Only line-END commas go. Mid-line commas stay (removing those makes a
    run-on: "the sea exhales a coin sinks"), and so do dashes, periods and
    question marks, which carry the cut and the landing.

    Must run AFTER check_structure() and craft_probe(): both read line-end
    punctuation to tell a cleanly closed line from one that spills over.
    """
    return "\n".join(re.sub(r"\s*,+\s*$", "", line) for line in haiku.split("\n"))


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


def load_dictionary() -> list[str]:
    """Load the word dictionary (same words.json the iOS app ships)."""
    with open(CONFIG_DIR / "words.json") as f:
        all_words = json.load(f)
    return [w for w in all_words if len(w) >= 3 and w == w.lower()]


def generate_word_pool(
    dictionary: list[str],
    banned_words: list[str],
    seed_words: list[str] | None = None,
) -> list[str]:
    """Build a pool of 12 words by random draw from the local dictionary.

    No API call. No theme. Just random words from words.json.
    Seed words (user-provided) are mixed in — the rest are filled
    randomly from the dictionary excluding seeds and banned words.
    """
    seeds = list(seed_words) if seed_words else []
    needed = 12 - len(seeds)

    if needed <= 0:
        pool = list(seeds[:12])
        random.shuffle(pool)
        return pool

    exclude = set(seeds) | set(banned_words)
    available = [w for w in dictionary if w not in exclude]
    drawn = random.sample(available, needed)

    pool = seeds + drawn
    random.shuffle(pool)
    return pool[:12]


# ── Haiku generation (production prompt format) ──────────────


def generate_haiku(
    client: anthropic.Anthropic,
    words: list[str],
    theme: str,
    banned_words: list[str],
    angle_cue: str | None = None,
    leaked_feedback: list[str] | None = None,
    gate_feedback: str | None = None,
    structure_feedback: str | None = None,
) -> str:
    """Generate a haiku encoding the given words.

    User message matches the production v8 solo recipe:
      THEME → Words → Approach → Ban list → CONSTRAINTS block
    On retry, appends leak feedback and/or gate feedback.
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

    # Constraints block (v8 — matches production buildSoloPrompt)
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
        "• Write ONE coherent poem — a single image with a turn — "
        "with all four words implied inside it; do not write four "
        "separate clues."
    )
    parts.append(
        "• Let each line be one whole phrase and punctuate it like a "
        "sentence: a dash or comma belongs at the END of a line, where "
        "the poem turns or carries on. Don't let a phrase spill over a "
        "line break and stop a word into the next line, and don't bolt "
        "a leftover clue onto the last line as a tail."
    )
    parts.append(
        "• Make the player infer each word from the scene; avoid "
        "one-word synonym giveaways."
    )
    parts.append(
        "• Every one of the four must genuinely be findable — "
        "anchored by something concrete in the image. A lovely poem "
        "that strands a word fails; if a word has no foothold, "
        "rework the scene so it earns one."
    )
    parts.append(
        "• Strictly 5/7/5 syllable count — five syllables in line one, "
        "seven in line two, five in line three. No exceptions."
    )
    parts.append(
        "• Write three complete lines — no trailing fragments. "
        "Avoid cliché first-association imagery. "
        f"(seed:{seed})"
    )

    # Leaked-word feedback from a previous failed attempt
    if leaked_feedback:
        parts.append("")
        parts.append(
            f"PREVIOUS ATTEMPT LEAKED these forbidden words: "
            f"{', '.join(leaked_feedback)}. "
            f"Rewrite without any of them or their stems."
        )

    # Gate feedback from a previous failed gate check
    if gate_feedback:
        parts.append("")
        parts.append(gate_feedback)

    # Line-structure feedback from check_structure() or craft_probe()
    if structure_feedback:
        parts.append("")
        parts.append(
            f"PREVIOUS ATTEMPT broke a line badly: {structure_feedback}. "
            f"Keep the voice, the wit and the punctuation — only make "
            f"each phrase end where its line ends, so the last line lands "
            f"as one image."
        )

    # No temperature: Opus 5.5 rejects sampling parameters with a 400.
    # Variety comes from the angle cue and the per-call seed in the prompt.
    # max_tokens must cover the model's thinking as well as the three
    # lines, or the haiku gets cut off mid-poem.
    response = client.messages.create(
        model=MODEL,
        max_tokens=4000,
        output_config={"effort": EFFORT},
        system=SYSTEM_MESSAGE,
        messages=[{"role": "user", "content": "\n".join(parts)}],
    )
    text = _response_text(response).strip()
    if not text:
        raise ValueError("Empty API response (likely content filter)")
    lines = [line.strip() for line in text.split("\n") if line.strip()]
    if len(lines) != 3:
        raise ValueError(f"Expected 3 lines, got {len(lines)}: {text!r}")
    if looks_truncated(lines):
        raise ValueError(f"Haiku looks truncated: {lines[-1]!r}")
    structure_issue = check_structure(lines)
    if structure_issue:
        raise StructureError(structure_issue)
    syl_ok, syl_counts = check_syllables(lines)
    if not syl_ok:
        raise ValueError(
            f"Syllable count {syl_counts[0]}/{syl_counts[1]}/{syl_counts[2]}"
            f" (need 5/7/5)"
        )
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


# ── Post-generation gate (ported from web/lib/gate.ts) ──────
# Two probes check the puzzle against the full 12-word pool:
#   1. obviousProbe (sonnet, casual) — if it gets 4/4, too easy
#   2. traceProbe (opus, careful) — any answer it misses is unfair
# On failure, gate_feedback_text() produces craft-preserving
# guidance that gets appended to the prompt on regeneration.

CAREFUL_SYSTEM = (
    "You are an expert word-puzzle solver with unlimited patience.\n\n"
    "The puzzle: a haiku secretly encodes exactly 4 of the candidate "
    "words shown to you. The haiku never contains an encoded word "
    "literally — each one is represented through its concept: imagery, "
    "function, setting, consequence, idiom, or an alternate dictionary "
    "sense. The other candidates are decoys with no intended connection.\n\n"
    "Work methodically. For EVERY candidate, scan the haiku for any "
    "conceptual trace — direct or oblique, any sense of the word. Rank "
    "them by strength of connection, then choose the 4 best-supported. "
    "Consider alternate meanings of each candidate.\n\n"
    'Respond with ONLY a JSON object:\n{"picks": ["w1","w2","w3","w4"]}'
)

OBVIOUS_SYSTEM = (
    "You are skimming a word game on your phone in a hurry.\n\n"
    "A short poem hides 4 of the candidate words listed (by idea or "
    "image, never the literal word). The others are red herrings.\n\n"
    "Glance once — maybe ten seconds — and grab the 4 words that leap "
    "out immediately. Trust the first hit. Don't analyze, don't weigh "
    "alternates, don't reread.\n\n"
    'Reply with ONLY JSON: {"picks":["w","w","w","w"]}'
)


def _pool_user(haiku: str, theme: str | None, pool: list[str]) -> str:
    return (
        f'Theme of the haiku: "{theme or ""}"\n\n'
        f"Haiku:\n{haiku}\n\n"
        f"The candidate words:\n{', '.join(pool)}\n\n"
        f"Pick exactly 4."
    )


def _extract_json(text: str) -> dict | None:
    """Parse the first JSON object in a response, tolerating code fences."""
    fence = re.search(r"```(?:json)?\s*([\s\S]*?)```", text)
    candidate = fence.group(1) if fence else text
    start = candidate.find("{")
    end = candidate.find("}", start) if start != -1 else -1
    if start == -1 or end == -1:
        return None
    try:
        obj = json.loads(candidate[start : end + 1])
    except json.JSONDecodeError:
        return None
    return obj if isinstance(obj, dict) else None


def _extract_picks(text: str, pool: list[str]) -> list[str]:
    """Parse picks from a probe response."""
    obj = _extract_json(text)
    if not obj:
        return []
    valid = {w.lower() for w in pool}
    raw = obj.get("picks", [])
    if not isinstance(raw, list):
        return []
    return [p.lower().strip() for p in raw if p.lower().strip() in valid][:len(pool)]


# ── Craft probe (Layer 2 — Sonnet line-integrity check, pre-gate) ──
# Runs after the leak check and BEFORE the expensive gate probes, so a
# structurally dead haiku never costs an Opus gate call. Judges line
# integrity only — it knows nothing about the hidden words, so it can't
# weaken the concept encoding.

CRAFT_SYSTEM = (
    "You are a strict haiku editor judging LINE INTEGRITY only — not "
    "meaning, theme, or imagery quality.\n\n"
    "A haiku passes if each of its three lines reads as one complete "
    "phrase and the final line lands as a single unbroken image. One cut "
    "(a dash at the end of line one or two) is traditional and fine.\n\n"
    "It FAILS if any of these are true:\n"
    "• A noun phrase or verb phrase is split across a line break "
    '("the tobacco / sack", "one filtered / organ").\n'
    "• The last line is stitched from fragments or has a clause bolted on "
    'after punctuation ("sack; we lose, he sighs").\n'
    "• Any line is a broken fragment rather than a readable unit.\n\n"
    'Respond with ONLY JSON: {"ok": true} or '
    '{"ok": false, "issue": "one short sentence naming the defect"}'
)


def craft_probe(client: anthropic.Anthropic, haiku: str) -> tuple[bool, str]:
    """Returns (ok, issue). A parse failure passes — never block on noise."""
    response = client.messages.create(
        model=CASUAL_MODEL, max_tokens=120,
        system=CRAFT_SYSTEM,
        messages=[{"role": "user", "content": haiku}],
    )
    obj = _extract_json(_response_text(response))
    if not obj or obj.get("ok", True):
        return True, ""
    return False, str(obj.get("issue", "a line is fragmented"))


def gate_probe(
    client: anthropic.Anthropic,
    haiku: str,
    theme: str | None,
    pool: list[str],
    answers: list[str],
) -> tuple[bool, bool, list[str]]:
    """Run both gate probes. Returns (passed, too_obvious, zero_trace_words)."""
    answer_set = {w.lower() for w in answers}
    user_msg = _pool_user(haiku, theme, pool)

    obv_response = client.messages.create(
        model=CASUAL_MODEL, max_tokens=200,
        system=OBVIOUS_SYSTEM,
        messages=[{"role": "user", "content": user_msg}],
    )
    # Opus probe: max_tokens has to leave room for thinking, not just the
    # small picks JSON, or the response is truncated before the JSON closes.
    trace_response = client.messages.create(
        model=MODEL, max_tokens=4000,
        output_config={"effort": EFFORT},
        system=CAREFUL_SYSTEM,
        messages=[{"role": "user", "content": user_msg}],
    )

    too_obvious = False
    try:
        obv_picks = _extract_picks(_response_text(obv_response), pool)
        too_obvious = (
            len(obv_picks) == 4
            and all(p in answer_set for p in obv_picks)
        )
    except Exception:
        pass

    zero_trace: list[str] = []
    try:
        trace_picks = _extract_picks(_response_text(trace_response), pool)
        zero_trace = [a for a in answers if a.lower() not in trace_picks]
    except Exception:
        pass

    passed = not too_obvious and len(zero_trace) == 0
    return passed, too_obvious, zero_trace


def gate_severity(too_obvious: bool, zero_trace: list[str]) -> int:
    return len(zero_trace) * 2 + (1 if too_obvious else 0)


def gate_feedback_text(too_obvious: bool, zero_trace: list[str]) -> str:
    """Craft-preserving guidance appended to the prompt on gate failure."""
    parts: list[str] = []
    if zero_trace:
        parts.append(
            f"A careful solver could find NO trace of these answer words: "
            f"{', '.join(zero_trace)}. Weave each one into the scene with "
            f"a real handle — a consequence, setting, function, or vivid "
            f"alternate sense — so it is recoverable."
        )
    if too_obvious:
        parts.append(
            "A reader skimming once instantly guessed all four answers — "
            "one or two clues are too on-the-nose. Let the image carry "
            "those one or two words more obliquely (no near-synonyms), "
            "so a quick pass can't grab all four."
        )
    parts.append(
        "Keep it ONE vivid, coherent poem with a turn — not a list of "
        "clues. The rewrite must read as well as a real haiku, not worse."
    )
    return " ".join(parts)


# ── Puzzle assembly ──────────────────────────────────────────


def generate_puzzle(
    client: anthropic.Anthropic,
    dictionary: list[str],
    d: date,
    theme: str,
    banned_words: list[str],
    max_size: int,
    angle_cue: str | None = None,
    seed_words: list[str] | None = None,
) -> tuple[dict, list[str]]:
    # Track the best FAIR candidate across all attempts.
    # Zero-trace (unfindable word) = hard fail, never ship.
    # Too-obvious = soft fail — prefer not-obvious, but a solvable-easy
    # puzzle beats no puzzle. Ship immediately on a full pass.
    best: dict | None = None  # full puzzle dict
    best_banned: list[str] = []
    best_obvious = True  # True = obvious, False = not; prefer False

    for pool_attempt in range(1, MAX_POOL_ATTEMPTS + 1):
        print(f"  Word pool attempt {pool_attempt}/{MAX_POOL_ATTEMPTS}...")
        pool = generate_word_pool(dictionary, banned_words, seed_words)
        print(f"  Pool (12): {pool}")

        answer_words = pool[:4]
        decoy_words = pool[4:]
        print(f"  Answers: {answer_words}")
        print(f"  Decoys:  {decoy_words}")

        g_feedback: str | None = None

        for g_try in range(1, GATE_BUDGET + 1):
            haiku = None
            leak_fb: list[str] | None = None
            struct_fb: str | None = None

            for h_try in range(1, HAIKU_RETRIES + 1):
                try:
                    candidate = generate_haiku(
                        client, answer_words, theme, banned_words,
                        angle_cue,
                        leaked_feedback=leak_fb,
                        gate_feedback=g_feedback,
                        structure_feedback=struct_fb,
                    )
                    # Layer 1 (check_structure) passed — clear stale feedback
                    struct_fb = None
                    leaks = leaked_words(candidate, answer_words)
                    if leaks:
                        leak_fb = leaks
                        print(
                            f"    Haiku leaked {leaks} "
                            f"(try {h_try}/{HAIKU_RETRIES})"
                        )
                        continue
                    # Layer 2 — Sonnet line-integrity check
                    craft_ok, craft_issue = craft_probe(client, candidate)
                    if not craft_ok:
                        struct_fb = craft_issue
                        print(
                            f"    Craft probe failed "
                            f"(try {h_try}/{HAIKU_RETRIES}): {craft_issue}"
                        )
                        print(f"      [{candidate.replace(chr(10), ' / ')}]")
                        continue
                    # Passed every line check — now tidy for the page.
                    haiku = strip_line_end_commas(candidate)
                    break
                except StructureError as se:
                    struct_fb = str(se)
                    print(
                        f"    Structure rejected "
                        f"(try {h_try}/{HAIKU_RETRIES}): {se}"
                    )
                except ValueError as ve:
                    print(
                        f"    Haiku rejected "
                        f"(try {h_try}/{HAIKU_RETRIES}): {ve}"
                    )

            if haiku is None:
                print(f"    Could not produce a clean haiku — new pool")
                break

            print(f"  Haiku:\n    " + haiku.replace("\n", "\n    "))

            print(f"    Gate check ({g_try}/{GATE_BUDGET})...")
            passed, too_obvious, zero_trace = gate_probe(
                client, haiku, theme, pool, answer_words,
            )

            if zero_trace:
                print(
                    f"    Gate failed — zero-trace: "
                    f"{', '.join(zero_trace)}"
                )
                g_feedback = gate_feedback_text(too_obvious, zero_trace)
                continue

            # Fair puzzle (no zero-trace). Track it.
            new_banned = update_banned_words(
                haiku, list(banned_words), max_size
            )
            puzzle = {
                "date": d.isoformat(),
                "haiku": haiku,
                "words": answer_words,
                "decoys": decoy_words,
                "theme": theme,
            }

            if not too_obvious:
                print(f"    Gate passed ✓")
                return puzzle, new_banned

            # Fair but obvious — stash if it's our first fair candidate
            print(f"    Gate: fair but too obvious — continuing")
            if best is None or best_obvious:
                best = puzzle
                best_banned = new_banned
                best_obvious = too_obvious
            g_feedback = gate_feedback_text(too_obvious, zero_trace)
        else:
            print(f"    Gate budget exhausted — drawing new words")

    # Exhausted all pools. Ship the best fair-but-obvious if we have one.
    if best is not None:
        label = "obvious" if best_obvious else "clean"
        print(f"    All pools exhausted — using best fair attempt ({label})")
        return best, best_banned

    raise RuntimeError(
        f"Failed to generate a fair puzzle for {d} "
        f"after {MAX_POOL_ATTEMPTS} word pools × {GATE_BUDGET} gate tries"
    )


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
        help='Comma-separated themes — cycles through them across days, '
        "overriding the rotation. "
        'E.g. "zen garden" (all days) or "winter,spring" (alternates)',
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
    api_key = load_api_key()
    if not api_key:
        print(
            "Error: ANTHROPIC_API_KEY not set. Export it in this shell, or "
            "put ANTHROPIC_API_KEY=sk-ant-... in a .env file at the repo root."
        )
        sys.exit(1)
    client = anthropic.Anthropic(api_key=api_key)
    dictionary = load_dictionary()
    rotation_themes = load_themes()
    banned_words, max_size = load_banned_words()
    print(f"Dictionary loaded: {len(dictionary)} words")

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

    today = date.today()
    days = select_days(
        today=today,
        explicit_day=args.day,
        n_themes=len(explicit_themes) if explicit_themes else 0,
        force=args.force,
    )
    if not args.day and days and days[0] == today:
        print("Today has no approved puzzle — starting from today.")

    themes_pool = explicit_themes if explicit_themes else rotation_themes
    day_themes = [
        (d, themes_pool[i % len(themes_pool)] if explicit_themes
         else get_theme_for_date(d, rotation_themes))
        for i, d in enumerate(days)
    ]

    generated_days = 0

    for d, theme in day_themes:
        if not args.force and puzzle_approved(d):
            print(f"Skipping {d} — already approved")
            continue

        print(
            f"\nGenerating {CANDIDATES_PER_DAY} candidates "
            f"for {d} (theme: {theme})..."
        )

        day_dir = CANDIDATES_DIR / d.isoformat()
        day_dir.mkdir(parents=True, exist_ok=True)

        # Clear any existing candidates
        for f in day_dir.glob("*.json"):
            f.unlink()

        # Pick distinct angle cues — one per candidate for diversity
        angles = random.sample(
            ANGLE_CUES, min(CANDIDATES_PER_DAY, len(ANGLE_CUES))
        )

        # Track first words within this day's batch so each candidate
        # is forced to start differently (same mechanism as the app's
        # FIFO ban list, but scoped to this generation run)
        day_banned = list(banned_words)

        for n in range(1, CANDIDATES_PER_DAY + 1):
            angle = angles[(n - 1) % len(angles)]
            print(f"\n  Candidate {n}/{CANDIDATES_PER_DAY} ({angle[:45]}…):")
            puzzle, day_banned = generate_puzzle(
                client, dictionary, d, theme, day_banned, max_size,
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
