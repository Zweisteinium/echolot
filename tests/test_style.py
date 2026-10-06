"""Code style the formatter cannot enforce."""

from pathlib import Path

ROOT = Path(__file__).parent.parent
MARKER = "# fmt: " + "skip"  # (split, so this file does not count itself)
OLD_SKIPS = 87  # hand-compacted code from before ruff laid out the code; may only go down


def test_no_new_fmt_skip() -> None:
    """ruff lays out new code (CLAUDE.md, Formatting): a statement too long for one line gets named parts,
    never a hand layout kept by the marker."""
    found = sum(p.read_text().count(MARKER) for d in ("src", "tests") for p in (ROOT / d).rglob("*.py"))
    assert found <= OLD_SKIPS, f"{found - OLD_SKIPS} new '{MARKER}': remove it and let ruff format the code"
