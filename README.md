# Echolot

Self-hosted music library sync: playlists and likes in, only the right songs in the best available quality out.

## Run

```sh
docker compose up -d --build
```

## Develop

```sh
uv sync
uv run pytest
uv run echolot serve
```
