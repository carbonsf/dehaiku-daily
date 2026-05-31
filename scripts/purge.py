#!/usr/bin/env python3
"""
Unapprove and/or purge future daily puzzles.

Usage:
    # List all approved future dates
    python scripts/purge.py

    # Unapprove a single date (keeps candidates so you can re-pick)
    python scripts/purge.py 2025-06-05

    # Unapprove everything from a date forward
    python scripts/purge.py 2025-06-05 --all-after

    # Also delete the candidates (full purge)
    python scripts/purge.py 2025-06-05 --purge
    python scripts/purge.py 2025-06-05 --all-after --purge
"""

import argparse
import re
import shutil
import sys
from datetime import date
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
PUZZLES_DIR = REPO_ROOT / "puzzles"
CANDIDATES_DIR = REPO_ROOT / "candidates"


def find_approved_dates(from_date=None):
    """Return sorted list of (date_str, puzzle_path) for approved future dates."""
    today = date.today()
    cutoff = from_date or today
    results = []

    if not PUZZLES_DIR.exists():
        return results

    for year_dir in sorted(PUZZLES_DIR.iterdir()):
        if not year_dir.is_dir() or not re.match(r"\d{4}$", year_dir.name):
            continue
        for month_dir in sorted(year_dir.iterdir()):
            if not month_dir.is_dir() or not re.match(r"\d{2}$", month_dir.name):
                continue
            for puzzle_file in sorted(month_dir.glob("*.json")):
                day_str = puzzle_file.stem
                try:
                    d = date.fromisoformat(
                        f"{year_dir.name}-{month_dir.name}-{day_str}"
                    )
                except ValueError:
                    continue
                if d >= cutoff:
                    results.append((d.isoformat(), puzzle_file))

    return results


def unapprove(date_str, puzzle_path, purge_candidates=False):
    """Remove the approved puzzle file. Optionally purge candidates too."""
    puzzle_path.unlink()
    print(f"  ✗ Unapproved {date_str} — removed {puzzle_path.relative_to(REPO_ROOT)}")

    # Clean up empty parent dirs
    for parent in [puzzle_path.parent, puzzle_path.parent.parent]:
        if parent != PUZZLES_DIR and parent.is_dir() and not any(parent.iterdir()):
            parent.rmdir()

    if purge_candidates:
        cand_dir = CANDIDATES_DIR / date_str
        if cand_dir.exists():
            shutil.rmtree(cand_dir)
            print(f"    Purged candidates/{date_str}/")


def main():
    p = argparse.ArgumentParser(
        description="Unapprove and/or purge future daily puzzles"
    )
    p.add_argument(
        "date",
        nargs="?",
        help="Date to unapprove (YYYY-MM-DD). Omit to list approved future dates.",
    )
    p.add_argument(
        "--all-after",
        action="store_true",
        help="Unapprove this date AND all approved dates after it",
    )
    p.add_argument(
        "--purge",
        action="store_true",
        help="Also delete the candidates (not just the approved puzzle)",
    )
    args = p.parse_args()

    if args.date is None:
        # List mode
        approved = find_approved_dates()
        if not approved:
            print("No approved future puzzles.")
            return
        print(f"Approved future puzzles ({len(approved)}):\n")
        for date_str, path in approved:
            cand_dir = CANDIDATES_DIR / date_str
            cand_count = len(list(cand_dir.glob("*.json"))) if cand_dir.exists() else 0
            print(f"  {date_str}  ({cand_count} candidates)")
        print(f"\nTo unapprove:  python scripts/purge.py YYYY-MM-DD")
        print(f"Full purge:    python scripts/purge.py YYYY-MM-DD --purge")
        print(f"All from date: python scripts/purge.py YYYY-MM-DD --all-after")
        return

    target = date.fromisoformat(args.date)

    if args.all_after:
        approved = find_approved_dates(from_date=target)
    else:
        # Single date
        y, m, d = args.date.split("-")
        puzzle_path = PUZZLES_DIR / y / m / f"{d}.json"
        if not puzzle_path.exists():
            print(f"No approved puzzle for {args.date}.")
            if args.purge:
                cand_dir = CANDIDATES_DIR / args.date
                if cand_dir.exists():
                    shutil.rmtree(cand_dir)
                    print(f"Purged candidates/{args.date}/")
            return
        approved = [(args.date, puzzle_path)]

    if not approved:
        print(f"No approved puzzles found from {args.date} onward.")
        return

    print(f"Unapproving {len(approved)} date(s){'  + purging candidates' if args.purge else ''}:\n")
    for date_str, path in approved:
        unapprove(date_str, path, purge_candidates=args.purge)

    print(f"\nDone. Run 'python scripts/review.py' to re-pick.")


if __name__ == "__main__":
    main()
