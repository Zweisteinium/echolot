"""Building blocks for demo videos from recorded frames (capture.Recorder): a scene as a clip in real time, the
clips composed in a window on a backdrop with slides between them and a fade in and out, and the encodings:
H.264 MP4 and animated WebP. Everything in between is lossless (FFV1, RGB), so each encoding is the only lossy
step; the WebP goes through libwebp's own animation encoder (Pillow), which keeps whole-frame quality, unlike
ffmpeg's libwebp_anim (its lossy sub-frames pile up as dark blocks on gradients)."""

import json
import subprocess
from dataclasses import dataclass
from pathlib import Path


def ffmpeg(*args: str, out: int | None = None) -> subprocess.Popen | None:
    cmd = ["ffmpeg", "-v", "error", "-y", *args]
    if out is not None:
        return subprocess.Popen(cmd, stdout=out)
    subprocess.run(cmd, check=True)
    return None


@dataclass
class Clip:
    path: Path
    length: float  # s


def scene(frames: Path, work: Path, size: tuple[int, int], fps: int, cut_start: float = 0.0) -> Clip:
    """A recorded scene at a steady fps in real time (each frame as long as it was shown, the last one until
    the recording stopped), scaled to size; lossless."""
    rec = json.loads((frames / "times.json").read_text())
    t = [*rec["frames"], rec["end"]]
    t0 = t[0] + cut_start
    rows, span = ["ffconcat version 1.0"], 0.0
    for i in range(len(t) - 1):
        a, b = max(t[i], t0), t[i + 1]
        if b > t0:
            rows += [f"file '{frames / f'{i:05d}.jpg'}'", f"duration {b - a:.4f}"]
            span += b - a
    rows += [f"file '{frames / f'{len(t) - 2:05d}.jpg'}'"]
    work.mkdir(parents=True, exist_ok=True)
    lst, out = work / f"{frames.name}.ffconcat", work / f"{frames.name}.mkv"
    lst.write_text("\n".join(rows) + "\n")
    ffmpeg(
        "-safe",
        "0",
        "-f",
        "concat",
        "-i",
        str(lst),
        "-vf",
        f"fps={fps},scale={size[0]}:{size[1]}:flags=lanczos,trim=duration={span:.3f},setsar=1,format=gbrp",
        "-c:v",
        "ffv1",
        "-level",
        "3",
        str(out),
    )
    return Clip(out, span)


def compose(
    clips: list[Clip],
    out: Path,
    fps: int,
    backdrop: Path,
    mask: Path,
    window: tuple[int, int],
    at: tuple[int, int],
    slide: float = 0.7,
    fade_in: float = 0.5,
    fade_out: float = 0.8,
) -> Clip:
    """The clips one after another in a window (its size; mask: its shape; at: its place) on the backdrop, a slide from
    each to the next, a fade from and to black at the ends; lossless."""
    inputs, graph, last, offset = [], [], "[0:v]", 0.0
    for c in clips:
        inputs += ["-i", str(c.path)]
    for n, c in enumerate(clips[:-1]):
        offset += c.length - slide
        graph.append(f"{last}[{n + 1}:v]xfade=transition=slideleft:duration={slide}:offset={offset:.3f}[s{n}]")
        last = f"[s{n}]"
    total = sum(c.length for c in clips) - slide * (len(clips) - 1)
    n = len(clips)
    inputs += ["-i", str(backdrop), "-i", str(mask)]
    graph += [
        f"{last}format=rgba[win]",
        f"[{n + 1}:v]format=gray,scale={window[0]}:{window[1]}[m]",
        "[win][m]alphamerge[shaped]",
        f"[{n}:v]loop=-1:1,fps={fps},trim=duration={total:.3f},format=rgba[bg]",
        f"[bg][shaped]overlay={at[0]}:{at[1]}:shortest=1,format=gbrp,"
        f"fade=t=in:st=0:d={fade_in},fade=t=out:st={total - fade_out:.3f}:d={fade_out}[out]",
    ]
    ffmpeg(*inputs, "-filter_complex", ";".join(graph), "-map", "[out]", "-c:v", "ffv1", "-level", "3", str(out))
    return Clip(out, total)


def mp4(master: Clip, out: Path, crf: int = 16) -> Path:
    """H.264 for players and sharing: 4:2:0, tuned for flat UI pictures, starts playing while loading."""
    ffmpeg(
        "-i",
        str(master.path),
        "-c:v",
        "libx264",
        "-preset",
        "veryslow",
        "-tune",
        "animation",
        "-crf",
        str(crf),
        "-pix_fmt",
        "yuv420p",
        "-movflags",
        "+faststart",
        str(out),
    )
    return out


def webp(master: Clip, out: Path, width: int, fps: int, quality: int = 82, method: int = 6) -> Path:
    """An animated WebP (loops), frame by frame from the lossless master through libwebp's animation encoder;
    every frame lossy at the same quality, keyframes as libwebp decides."""
    from PIL import Image, _webp

    probe = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-select_streams",
            "v:0",
            "-show_entries",
            "stream=width,height",
            "-of",
            "json",
            str(master.path),
        ],
        capture_output=True,
        check=True,
        text=True,
    )
    s = json.loads(probe.stdout)["streams"][0]
    height = round(s["height"] * width / s["width"] / 2) * 2
    proc = ffmpeg(
        "-i",
        str(master.path),
        "-vf",
        f"fps={fps},scale={width}:{height}:flags=lanczos",
        "-f",
        "rawvideo",
        "-pix_fmt",
        "rgb24",
        "-",
        out=subprocess.PIPE,
    )
    enc = _webp.WebPAnimEncoder((width, height), 0, 0, False, 3, 5, False, 0)  # size, bg, loop, min, kmin, kmax, mixed
    step, n = 1000 / fps, 0
    size = width * height * 3
    while chunk := proc.stdout.read(size):
        if len(chunk) < size:
            break
        enc.add(Image.frombytes("RGB", (width, height), chunk).getim(), round(n * step), False, quality, 100, method)
        n += 1
    proc.wait()
    enc.add(None, round(n * step), False, quality, 100, 0)
    out.write_bytes(enc.assemble(b"", b"", b""))
    return out
