# Echolot

Self-hosted music library sync. Echolot reads your playlists and likes (Spotify, SoundCloud),
searches several sources in order of quality, checks that each file really is the wanted song,
and files it into a library for Navidrome or any other music server.

The name is German for *sonar*: send a ping to every source, keep only what echoes back clearly.

> **Status:** early development. So far: project setup and an empty dashboard.

## Principles

- **Only the right song.** A wrong file is worse than a missing one.
- **Never destroy.** Existing files are never overwritten or deleted without a way back.
- **Best available quality.** Lossless first, verified (no upscaled "fake" FLACs).
- **Bring your own keys.** No API keys, tokens or media ship with Echolot.

## Development

Requires [uv](https://docs.astral.sh/uv/) (Python 3.13 is installed automatically if missing).

```sh
uv sync                  # create .venv with all dependencies
uv run pytest            # tests
uv run ruff check        # lint
uv run ruff format       # format
uv run echolot serve     # dashboard on http://127.0.0.1:8490
```

With Docker:

```sh
docker compose up -d --build   # dev instance on 127.0.0.1:8490
docker compose logs -f
docker compose down
```

## Configuration

| Variable              | Default     | Meaning                                              |
|-----------------------|-------------|------------------------------------------------------|
| `ECHOLOT_DATA_DIR`    | `data`      | Database, state and logs (`/data` in the container)  |
| `ECHOLOT_LIBRARY_DIR` | unset       | Music library, read-only for now                     |
| `ECHOLOT_HOST`        | `127.0.0.1` | Listen address (`0.0.0.0` in the container)          |
| `ECHOLOT_PORT`        | `8490`      | Listen port                                          |
