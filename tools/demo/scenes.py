"""The scenes of the README video (STORYBOARD.md says what each must show), recorded in real time from the demo
instance (serve.py) by make.py. A scene is a function of (page, cdp, pointer, base, work); SCENES lists them in
the video's order with the elements each needs (checked on the live pages before anything is recorded, so a
changed page fails at once, naming what is missing) and how much to cut from its start."""

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from capture import Recorder, smooth_scroll

VIEW, SCALE = (1440, 900), 4 / 3  # the browser window, and the frames' scale (1920 x 1200)
FRAME = (int(VIEW[0] * SCALE), int(VIEW[1] * SCALE))
HIDE = (
    ".connections { display: none !important; } html { scrollbar-width: none; } ::-webkit-scrollbar { display: none; }"
)


@dataclass
class Scene:
    name: str
    path: str  # the page it starts on
    needs: list[str]  # selectors that must be on that page
    play: Callable
    cut: float = 0.0  # s cut from its start (the page drawing in)


def library(page, cdp, ptr, base: str, work: Path) -> None:
    """The library's growth read along, a stretch dragged to zoom in, then the quality over time."""
    page.goto("about:blank")
    with Recorder(page, cdp, work / "frames" / "library", FRAME):
        page.goto(base + "/library")
        ptr.place(1010, 830)
        page.wait_for_timeout(1300)  # the graph draws in
        plot = page.locator("[data-panel=songs] .canvas").bounding_box()
        y = plot["y"] + plot["height"] * 0.8  # below the curve: the tooltip above it stays clear
        ptr.glide(plot["x"] + plot["width"] * 0.08, y, 800)
        ptr.glide(plot["x"] + plot["width"] * 0.6, y, 1600, bend=0, linear=True)  # the values follow
        page.wait_for_timeout(300)
        ptr.glide(plot["x"] + plot["width"] * 0.55, y, 400)  # a stretch to zoom into: drag across it
        page.mouse.down()
        ptr.glide(plot["x"] + plot["width"] * 0.9, y, 900, bend=0)
        page.mouse.up()
        page.wait_for_timeout(1400)  # the zoomed graph
        ptr.to("label:has(input[value=quality])", 800)
        ptr.click()
        page.wait_for_timeout(900)
        plot = page.locator("[data-panel=quality] .canvas").bounding_box()
        y = plot["y"] + plot["height"] * 0.85
        ptr.glide(plot["x"] + plot["width"] * 0.3, y, 800)
        ptr.glide(plot["x"] + plot["width"] * 0.85, y, 1500, bend=0, linear=True)  # each quality's share and count
        page.wait_for_timeout(900)


def songs_list(page, cdp, ptr, base: str, work: Path) -> None:
    """A list with every song's file and quality, scrolled through."""
    page.goto(base + "/lists/spotify:playlist:demo1")
    page.wait_for_timeout(600)
    with Recorder(page, cdp, work / "frames" / "list", FRAME):
        ptr.place(1180, 560)
        page.wait_for_timeout(900)
        smooth_scroll(page, 820, 4.2)
        page.wait_for_timeout(1200)


def upload(page, cdp, ptr, base: str, work: Path) -> None:
    """Three files dropped in, checked one by one, imported."""
    page.goto(base + "/missing")
    page.wait_for_timeout(600)
    with Recorder(page, cdp, work / "frames" / "upload", FRAME):
        ptr.place(1130, 430)
        page.wait_for_timeout(600)
        ptr.to("button:has-text('Upload files')", 1000)
        page.wait_for_timeout(200)
        ptr.click()
        page.wait_for_selector("#upload [data-files]", state="attached")
        page.wait_for_timeout(800)  # the dialog
        drop = page.locator("#upload .drop").first
        d = drop.bounding_box()
        ptr.glide(d["x"] + d["width"] + 150, d["y"] + d["height"] + 120, 750)  # (where the files come from)
        ptr.carry("3 files")
        page.wait_for_timeout(300)
        ptr.glide(d["x"] + d["width"] * 0.55, d["y"] + d["height"] * 0.55, 1100)
        drop.evaluate("e => e.classList.add('drag')")
        page.wait_for_timeout(500)
        ptr.carry("")
        drop.evaluate("e => e.classList.remove('drag')")
        page.set_input_files("#upload [data-files]", sorted(str(f) for f in (work / "upload").glob("*.flac")))
        page.wait_for_function(
            "document.querySelectorAll('#upload tr.upload-row.pending').length === 0 && "
            "document.querySelectorAll('#upload tr.upload-row').length === 3",
            timeout=120000,
        )
        page.wait_for_timeout(2700)  # time to read what each file is
        ptr.to("#upload [data-import]", 1000)
        page.wait_for_timeout(300)
        ptr.click()
        page.wait_for_selector("#upload [data-imported]", timeout=120000)
        page.wait_for_timeout(2500)  # what was imported


def review(page, cdp, ptr, base: str, work: Path) -> None:
    """A download to confirm: Perfect match, and what that does."""
    page.goto(base + "/review")
    page.wait_for_timeout(600)
    with Recorder(page, cdp, work / "frames" / "review", FRAME):
        ptr.place(1180, 640)
        page.wait_for_timeout(900)
        ptr.to("button:has-text('Perfect match')", 1200)
        page.wait_for_timeout(700)
        ptr.click()
        page.wait_for_load_state("load")
        page.wait_for_timeout(1700)  # what it did


def jobs(page, cdp, ptr, base: str, work: Path) -> None:
    """The jobs at work: a FLAC upgrade counting on (serve.py's made-up run), what it does behind ⓘ."""
    page.goto(base + "/demo/run")
    page.goto(base + "/jobs")
    page.wait_for_timeout(800)
    with Recorder(page, cdp, work / "frames" / "jobs", FRAME):
        ptr.place(1100, 700)
        page.wait_for_timeout(700)
        ptr.to(".job.ok .step.live", 1100, fx=0.3, fy=0.35)
        page.wait_for_timeout(3200)  # the counts go on (the page reloads its jobs every few seconds)
        ptr.to(".job.ok .info-toggle", 900)
        ptr.click()
        page.wait_for_timeout(2600)  # what the job does


SCENES = [
    Scene("library", "/library", ["[data-panel=songs] .canvas", "label:has(input[value=quality])"], library, 0.35),
    Scene("list", "/lists/spotify:playlist:demo1", ["table.list-songs"], songs_list),
    Scene("upload", "/missing", ["button:has-text('Upload files')", "#upload"], upload),
    Scene("review", "/review", ["button:has-text('Perfect match')"], review),
    Scene("jobs", "/jobs", [".job", ".info-toggle"], jobs),
]
