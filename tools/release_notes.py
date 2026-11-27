"""A version's release notes: its section of CHANGELOG.md with each paragraph on one line (GitHub shows a
release's line breaks as they are), and a link to all its changes.

    python3 tools/release_notes.py <version> [<previous tag>]"""

import re
import sys
from pathlib import Path

CHANGELOG = Path(__file__).parent.parent / "CHANGELOG.md"
REPOSITORY = "https://github.com/Zweisteinium/echolot"


def notes(version: str, previous: str = "") -> str:
    section = re.search(rf"^## {re.escape(version)} \(.*?\)\n(.*?)(?=^## |\Z)", CHANGELOG.read_text(), re.M | re.S)
    if not section or not section[1].strip():
        raise SystemExit(f"CHANGELOG.md has no notes for {version}")
    body = re.sub(r"(?<=\S)\n(?!\n|- |#) *", " ", section[1].strip())  # wrapped lines join their paragraph
    if previous:
        body += f"\n\n**All changes:** [{previous}...v{version}]({REPOSITORY}/compare/{previous}...v{version})"
    return body + "\n"


if __name__ == "__main__":
    print(notes(*sys.argv[1:3]), end="")
