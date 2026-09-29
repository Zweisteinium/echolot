"""echolot.yml: Echolot's whole configuration as one YAML file, for backups, moving to another
install and editing in bulk. It holds the lists, the pipeline's schedule and the settings sections;
never secrets or users.

An import is checked completely before anything changes, then applied in one transaction. Parts left
out keep their current values: a missing `schedule` job or settings field stays as it is; `sources`,
when present, replaces every list.
"""

import difflib
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

import yaml

from echolot import options, schedule, sources
from echolot.sources import ConfigError

FORMAT = 1
TOP_LEVEL = {"version", "sources", "schedule", "settings"}
# sections that are part of `sources` in the file (the likes user, removed playlists)
IN_SOURCES = {options.SourceOptions.SECTION}


def export_data(con: sqlite3.Connection) -> dict[str, Any]:
    return {
        "version": FORMAT,
        "sources": sources.as_config(con),
        "schedule": schedule.file_form(schedule.rules(con)),
        "settings": {
            s.SECTION: options.get(con, s).model_dump()
            for s in options.SECTIONS
            if s.SECTION not in IN_SOURCES
        },
    }


def dump(data: dict[str, Any]) -> str:
    return sources.dump(data)


def export_text(con: sqlite3.Connection) -> str:
    return (
        f"# Echolot configuration, exported {datetime.now().isoformat(timespec='seconds')}.\n"
        "# No secrets, no users. Import: Settings page, or `echolot config import <file>`.\n"
        + dump(export_data(con))
    )


@dataclass
class Parsed:
    lists: dict[str, Any] | None = None  # sources.yml structure; None: keep the lists
    rules: dict[str, schedule.Rule] = field(default_factory=dict)
    sections: list[options.Section] = field(default_factory=list)


def parse(con: sqlite3.Connection, text: str) -> Parsed:
    """Check an echolot.yml completely; ConfigError describes the first problem found."""
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as err:
        raise ConfigError(f"Not valid YAML: {err}") from err
    if not isinstance(data, dict):
        raise ConfigError("An echolot.yml is a mapping (version, sources, schedule, settings).")
    unknown = sorted(set(data) - TOP_LEVEL)
    if unknown:
        raise ConfigError(f"Unknown part(s): {', '.join(unknown)}.")
    if data.get("version", FORMAT) != FORMAT:
        raise ConfigError(f"This is format version {data['version']}; Echolot reads {FORMAT}.")
    parsed = Parsed()
    if "sources" in data:
        try:
            sources.check(data["sources"])
        except ConfigError as err:
            raise ConfigError(f"sources: {err}") from err
        parsed.lists = data["sources"] or {}
    parsed.rules = schedule.parse_rules(data.get("schedule"))
    given = data.get("settings") or {}
    if not isinstance(given, dict):
        raise ConfigError("settings must be a mapping of sections.")
    for name, value in given.items():
        model = options.BY_NAME.get(name)
        if model is None or name in IN_SOURCES:
            raise ConfigError(f"settings: unknown section '{name}'.")
        if not isinstance(value, dict):
            raise ConfigError(f"settings.{name} must be a mapping.")
        current = options.get(con, model).model_dump()
        try:
            parsed.sections.append(options.validate(model, {**current, **value}))
        except options.OptionsError as err:
            raise ConfigError(str(err)) from err
    return parsed


def preview(con: sqlite3.Connection, text: str) -> str:
    """What an import would change, as a unified diff of the exported configuration ('' = nothing)."""
    parsed = parse(con, text)
    before = dump(export_data(con))
    with con:  # applied and rolled back, so the result is exactly what an import would store
        con.execute("SAVEPOINT preview")
        try:
            _apply(con, parsed)
            after = dump(export_data(con))
        finally:
            con.execute("ROLLBACK TO preview")
            con.execute("RELEASE preview")
    return "".join(
        difflib.unified_diff(
            before.splitlines(keepends=True), after.splitlines(keepends=True), "current", "import"
        )
    )


def _apply(con: sqlite3.Connection, parsed: Parsed) -> None:
    if parsed.lists is not None:
        sources.replace_rows(con, parsed.lists)
    if parsed.rules:
        schedule.store(con, parsed.rules, "imported echolot.yml")
    for section in parsed.sections:
        options.put(con, section)


def apply(con: sqlite3.Connection, text: str) -> None:
    """Import an echolot.yml (checked first, then all of it in one transaction)."""
    parsed = parse(con, text)
    before = sources.render(con)
    with con:
        _apply(con, parsed)
        if sources.render(con) != before:
            con.execute(
                "INSERT INTO config_versions (name, ts, text, note) VALUES (?, ?, ?, ?)",
                (sources.FILE, datetime.now().isoformat(timespec="seconds"), before,
                 "imported echolot.yml"),
            )  # fmt: skip
