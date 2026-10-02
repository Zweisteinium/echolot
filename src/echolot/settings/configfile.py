"""echolot.yml: Echolot's whole configuration as one YAML file, for backups, moving to another
install and editing in bulk. It holds each user's lists (by user name), the schedule and the settings
sections; never secrets, nor the users themselves (they come from Navidrome).

An import is checked completely before anything changes, then applied in one transaction. Parts left
out keep their current values: a missing `schedule` job or settings field stays as it is; a user under
`sources` gets exactly the lists given there, users left out keep theirs. A file of format 1 (one set of
lists, from before users had lists) gives its lists to the oldest admin.
"""

import difflib
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

import yaml

from echolot.jobs import schedule
from echolot.settings import options, sources
from echolot.settings.sources import ConfigError

FORMAT = 2
TOP_LEVEL = {"version", "sources", "schedule", "settings"}
# sections that are part of `sources` in the file (the likes user, removed playlists)
IN_SOURCES = {options.SourceOptions.SECTION}


def _names(con: sqlite3.Connection) -> dict[int, str]:
    return {r["id"]: r["name"] for r in con.execute("SELECT id, name FROM users")}


def export_data(con: sqlite3.Connection) -> dict[str, Any]:
    names = _names(con)  # (lists nobody owns yet are adopted at the start: none to export)
    lists = {names[uid]: sources.as_config(con, uid) for uid in sources.owners(con) if uid in names}
    return {
        "version": FORMAT,
        "sources": lists,
        "schedule": schedule.file_form(schedule.rules(con)),
        "settings": {
            s.SECTION: options.get(con, s).model_dump() for s in options.SECTIONS if s.SECTION not in IN_SOURCES
        },
    }


def dump(data: dict[str, Any]) -> str:
    return sources.dump(data)


def export_text(con: sqlite3.Connection) -> str:
    return (
        f"# Echolot configuration, exported {datetime.now().isoformat(timespec='seconds')}.\n"
        "# No secrets, no users. Import: Settings page, or `echolot config import <file>`.\n" + dump(export_data(con))
    )


@dataclass
class Parsed:
    lists: dict[int | None, dict[str, Any]] | None = None  # user id -> sources.yml structure; None: keep all
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
    version = data.get("version", FORMAT)
    if version not in (1, FORMAT):
        raise ConfigError(f"This is format version {version}; Echolot reads 1 and {FORMAT}.")
    parsed = Parsed()
    if "sources" in data:
        parsed.lists = _lists(con, data["sources"] or {}, version)
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


def _lists(con: sqlite3.Connection, given: Any, version: int) -> dict[int | None, dict[str, Any]]:
    """The users' lists of a file: format 1 has one set (the oldest admin's), format 2 one per user name."""
    if version == 1:
        sql = "SELECT id FROM users WHERE (admin OR navidrome_admin) AND NOT disabled ORDER BY id LIMIT 1"
        owner = con.execute(sql).fetchone()
        given = {owner["id"] if owner else None: given}
    elif not isinstance(given, dict):
        raise ConfigError("sources: a mapping of user names to their lists.")
    else:
        ids = {name.casefold(): uid for uid, name in _names(con).items()}
        unknown = sorted(n for n in given if str(n).casefold() not in ids)
        if unknown:
            raise ConfigError(f"sources: no user {', '.join(map(str, unknown))} here (they log in once first).")
        given = {ids[str(n).casefold()]: v for n, v in given.items()}
    for value in given.values():
        try:
            sources.check(value)
        except ConfigError as err:
            raise ConfigError(f"sources: {err}") from err
    return {uid: value or {} for uid, value in given.items()}


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
        difflib.unified_diff(before.splitlines(keepends=True), after.splitlines(keepends=True), "current", "import")
    )


def _apply(con: sqlite3.Connection, parsed: Parsed) -> None:
    for uid, value in (parsed.lists or {}).items():
        sources.replace_rows(con, value, uid)
    if parsed.rules:
        schedule.store(con, parsed.rules)
    for section in parsed.sections:
        options.put(con, section)


def apply(con: sqlite3.Connection, text: str) -> None:
    """Import an echolot.yml (checked first, then all of it in one transaction)."""
    from echolot.jobs import lists

    parsed = parse(con, text)
    with con:
        _apply(con, parsed)
    lists.sync_table(con)
