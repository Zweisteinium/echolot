# The README video

`docs/echolot.webp`: Echolot in about 40 seconds, in a window on a dark sea, dark mode, real time, a soft pointer
doing what a person would. It is made again for every minor release (x.y.0), so it shows the Echolot that is
released. This file says what it must show; the code (`scenes.py`) is one way of showing it and may change.

```sh
uv run --with playwright==1.64.1 --with pillow python tools/demo/make.py --audit /opt/echolot/data/echolot.db
# look at ~/.cache/echolot-demo/sheet.png (a still every 2 s) and the WebP, then:
uv run --with playwright==1.64.1 --with pillow python tools/demo/make.py --publish   # into docs/
```

Once on a machine: `ffmpeg`, and `uv run --with playwright==1.64.1 playwright install chromium-headless-shell`.

## The scenes

Each scene makes one point; the pointer moves calmly and stops long enough to read what matters.

1. **Library** (`/library`): the library grows by itself. The pointer reads the song graph along (the values
   follow it), drags across a stretch to zoom in, then switches to Quality and reads the shares and counts over
   time. Needs two weeks of history that rises in bursts, and a FLAC share that rises.
2. **A list** (`/lists/spotify:playlist:demo1`, Trance Classics): every song of a list has its file and quality.
   Scrolled slowly; the first rows show every quality, a few lossy, two fake FLACs.
3. **Upload** (`/missing`): you can fill a gap yourself. Three files dropped on the dialog, checked one by one
   (what each is, its quality), imported. The three are missing songs on rick's lists (make.py writes them).
4. **Review** (`/review`): a doubtful download waits for a decision. Perfect match on the one in "Please
   confirm" (Fade), and what that did.
5. **Jobs** (`/jobs`): it all runs by itself. A FLAC upgrade counts on in its tile (serve.py's made-up run: the
   demo runs no jobs), then ⓘ tells what the job does.

Left out on purpose: Search (it asks Deezer and Apple live), the accounts (none in the demo; the connection
chips are hidden), anything slow to read.

## The data

Made up (`data.py`, `pools.py`): users rick (admin, the one recorded), sam and jo; a dozen lists on Spotify,
SoundCloud and YouTube; about 5,000 songs, 4,800 files; two weeks of activity, review decisions, searches.

- Real songs are welcome, also from real lists, but no demo list may mirror a real playlist (its songs or their
  order). `audit.py` checks that against a real database (read only) and prints only counts.
- As little as possible is faked, so the video shows Echolot's real behaviour (and its bugs): the instance is
  this checkout (`serve.py`), the history is replayed hour by hour and counted by Echolot's own
  `history.collect`, the upload and the review really import and decide. Faked: the rows themselves, and the
  running job in scene 5.
- The job messages (`JOBS` in data.py) use the jobs' own wording; the Jobs page reads counts from them.
- What the scenes rely on is checked at the end of data.py (the contract); `tests/test_demo.py` builds the data
  on every CI run and loads each scene's page.

## When Echolot changed

- **CI fails in test_demo**: the data no longer fits. A new required column: add it to that table's rows in
  data.py. A column gone: data.py says which; take it out there. The contract names a scene's missing piece.
- **make.py stops before recording** and names a scene's missing element: that page changed. Fix the selector
  or the steps in scenes.py so the scene still makes its point above; change the point here first if the page
  now works differently.
- **Something new worth showing**: write its scene here first (the point, the page, the data it needs), then
  code it, keeping the video near 40 seconds.
- **Look at every result** before publishing: the sheet, then the WebP once through. Numbers must agree between
  scenes (the upload's three songs leave Missing), nothing may flash an error or an empty page.
