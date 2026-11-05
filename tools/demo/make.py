"""The README video in one go: the demo data (data.py), the demo instance (serve.py), each scene checked and
recorded (scenes.py), the video composed and encoded (video.py), and a sheet of stills to look over. STORYBOARD.md
says what it must show and how to fix a scene after Echolot changed.

    uv run --with playwright==1.64.1 --with pillow python tools/demo/make.py [options]

    --work DIR       where everything goes (default ~/.cache/echolot-demo)
    --audit DB       first check the demo against a real Echolot database (audit.py, read only)
    --only a,b       record only these scenes (the others' frames from the last run are reused)
    --publish        copy the WebP to docs/echolot.webp

Needs ffmpeg and Playwright's Chromium (once: uv run --with playwright==1.64.1 playwright install
chromium-headless-shell). The scenes change the demo (the upload imports, the review decides), so every run
builds it anew."""

import argparse
import shutil
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE))

import video  # noqa: E402
from capture import session  # noqa: E402
from scenes import HIDE, SCALE, SCENES, VIEW  # noqa: E402

ROOT = HERE.parent.parent
PORT = 8497
FPS, WINDOW, AT = 30, (1160, 724), (60, 38)  # the window in the 1280 x 800 backdrop
UPLOAD = [("Lady Gaga", "Poker Face", 237), ("Stromae", "Papaoutai", 232), ("Spice Girls", "Wannabe", 173)]  # (data.py)


def run(*cmd: str) -> None:
    subprocess.run([sys.executable, *cmd], check=True, cwd=ROOT)


def upload_files(work: Path) -> None:
    """The three files the upload scene drops: tagged FLACs of full-band noise (they pass the quality check)."""
    folder = work / "upload"
    folder.mkdir(parents=True, exist_ok=True)
    for artist, title, seconds in UPLOAD:
        out = folder / f"{artist} - {title}.flac"
        if out.exists():
            continue
        mix = "[0][1][2]amix=inputs=3:weights=1 0.6 0.25,volume=2,aformat=channel_layouts=stereo:sample_rates=44100"
        video.ffmpeg(
            "-f",
            "lavfi",
            "-i",
            f"anoisesrc=c=pink:a=0.12:d={seconds},highpass=f=40",
            "-f",
            "lavfi",
            "-i",
            f"sine=f=110:d={seconds}",
            "-f",
            "lavfi",
            "-i",
            f"sine=f=440:d={seconds}",
            "-filter_complex",
            mix,
            "-c:a",
            "flac",
            "-sample_fmt",
            "s16",
            "-metadata",
            f"artist={artist}",
            "-metadata",
            f"title={title}",
            str(out),
        )


def assets(work: Path) -> None:
    """The backdrop (bg.png: the sea, the window's shadow) and the window's shape (mask.png)."""
    from playwright.sync_api import sync_playwright

    sea = (
        "background:#030a14; background-image: radial-gradient(60% 70% at 18% 22%, rgba(31,107,207,.55), transparent 70%),"
        "radial-gradient(55% 60% at 85% 78%, rgba(15,143,154,.42), transparent 70%),"
        "radial-gradient(45% 50% at 78% 12%, rgba(91,63,196,.35), transparent 70%),"
        "linear-gradient(165deg, #0b2440 0%, #07182b 55%, #030a14 100%);"
    )
    w, h = WINDOW
    pages = {
        "bg.png": (
            1280,
            800,
            f'<body style="margin:0;width:1280px;height:800px;{sea}"><div style="position:absolute;'
            f"left:{AT[0]}px;top:{AT[1]}px;width:{w}px;height:{h}px;border-radius:16px;box-shadow:0 30px 80px -10px "
            'rgba(0,0,0,.65),0 0 0 1px rgba(106,169,242,.18)"></div></body>',
        ),
        "mask.png": (
            w,
            h,
            f'<body style="margin:0;background:#000"><div style="width:{w}px;height:{h}px;border-radius:16px;'
            'background:#fff"></div></body>',
        ),
    }
    with sync_playwright() as p:
        browser = p.chromium.launch()
        for name, (pw, ph, html) in pages.items():
            page = browser.new_page(viewport={"width": pw, "height": ph})
            page.set_content(html)
            page.screenshot(path=str(work / name))
        browser.close()


def serve(work: Path) -> subprocess.Popen:
    proc = subprocess.Popen([sys.executable, str(HERE / "serve.py"), str(work), str(PORT)], cwd=ROOT)
    for _ in range(120):
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{PORT}/healthz", timeout=1)
            return proc
        except OSError:
            time.sleep(0.5)
    proc.kill()
    raise SystemExit("the demo instance did not start")


def record(work: Path, only: set[str]) -> None:
    base = f"http://127.0.0.1:{PORT}"
    cookie = ("echolot_session", (work / "cookie.txt").read_text().strip())
    with session(base, cookie, VIEW, SCALE, HIDE) as (page, cdp, ptr):
        missing = []
        for s in SCENES:  # every scene's page has what the scene needs, before anything is recorded
            page.goto(base + s.path)
            missing += [f"{s.name} ({s.path}): {sel}" for sel in s.needs if not page.locator(sel).count()]
        if missing:
            raise SystemExit("the scenes miss (see STORYBOARD.md, 'When Echolot changed'):\n  " + "\n  ".join(missing))
        for s in SCENES:
            if not only or s.name in only:
                s.play(page, cdp, ptr, base, work)


def sheet(master: Path, out: Path) -> None:
    """A still every 2 s, in a grid: the whole video at a glance."""
    stills = out.parent / "stills"
    shutil.rmtree(stills, ignore_errors=True)
    stills.mkdir()
    video.ffmpeg("-i", str(master), "-vf", "fps=1/2,scale=480:-2", str(stills / "%03d.png"))
    video.ffmpeg(
        "-pattern_type",
        "glob",
        "-i",
        str(stills / "*.png"),
        "-vf",
        "tile=4x6:padding=6:color=black",
        "-frames:v",
        "1",
        str(out),
    )


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--work", type=Path, default=Path.home() / ".cache" / "echolot-demo")
    ap.add_argument("--audit", type=Path)
    ap.add_argument("--only", default="")
    ap.add_argument("--publish", action="store_true")
    args = ap.parse_args()
    work = args.work.resolve()
    work.mkdir(parents=True, exist_ok=True)
    for old in ("data", "music"):
        shutil.rmtree(work / old, ignore_errors=True)
    run(str(HERE / "data.py"), str(work))
    if args.audit:
        run(str(HERE / "audit.py"), str(work / "data" / "echolot.db"), str(args.audit))
    upload_files(work)
    if not (work / "bg.png").exists():
        assets(work)
    proc = serve(work)
    try:
        record(work, set(filter(None, args.only.split(","))))
    finally:
        proc.terminate()
    clips = [video.scene(work / "frames" / s.name, work / "clips", WINDOW, FPS, s.cut) for s in SCENES]
    for s, c in zip(SCENES, clips, strict=True):
        print(f"{s.name}: {c.length:.1f} s")
    master = video.compose(clips, work / "clips" / "master.mkv", FPS, work / "bg.png", work / "mask.png", WINDOW, AT)
    webp = video.webp(master, work / "echolot.webp", width=1280, fps=25)
    video.mp4(master, work / "echolot.mp4")
    sheet(master.path, work / "sheet.png")
    print(f"{master.length:.1f} s, {webp.stat().st_size / 1e6:.1f} MB: {webp}; stills: {work / 'sheet.png'}")
    if args.publish:
        shutil.copy(webp, ROOT / "docs" / "echolot.webp")
        print("docs/echolot.webp replaced")


if __name__ == "__main__":
    main()
