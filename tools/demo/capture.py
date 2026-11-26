"""Recording a web app as video frames, for demos: a browser session (Playwright, Chromium), the browser's own
frames with their timestamps (CDP screencast; a page standing still sends none, so the stop time is kept too),
and a soft glowing pointer drawn into the page (headless Chromium shows none).

The pointer is moved by the page itself (requestAnimationFrame, eased curves), so it glides at the display's
rate. Along the way it dispatches pointer events (hover effects follow it); at each stop the real mouse is put
there (CSS :hover, clicks). Its position is kept across page loads (sessionStorage), and a click pulses an inner
dot, never the positioned element (an individual `scale` would scale its translate too: a jump)."""

import base64
import contextlib
import json
import math
import time
from collections.abc import Iterator
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # (Playwright only where it records: the scenes import without it)
    from playwright.sync_api import Page

POINTER_JS = """
addEventListener('DOMContentLoaded', () => {
  const p = document.createElement('div');
  p.id = 'demo-pointer';
  p.popover = 'manual';  // the top layer: above an open dialog too
  p.style.cssText = 'position:fixed;inset:auto;left:0;top:0;width:0;height:0;margin:0;padding:0;border:0;' +
    'overflow:visible;background:none;pointer-events:none;opacity:0;transition:opacity .35s;will-change:transform';
  const dot = document.createElement('div');  // the glow; a click pulses this, not the positioned element
  dot.style.cssText = 'position:absolute;left:-15px;top:-15px;width:30px;height:30px;border-radius:50%;' +
    'background:radial-gradient(circle, rgba(220,236,255,.98) 0 26%, rgba(106,169,242,.6) 44%, rgba(106,169,242,0) 72%);' +
    'box-shadow:0 0 24px 7px rgba(106,169,242,.38)';
  const chip = document.createElement('span');  // what the pointer carries (files being dragged)
  chip.style.cssText = 'position:absolute;left:19px;top:11px;white-space:nowrap;font:600 16px system-ui,sans-serif;' +
    'color:#dcecff;background:rgba(31,107,207,.92);border:1px solid rgba(160,200,255,.5);border-radius:8px;' +
    'padding:6px 12px;box-shadow:0 8px 24px rgba(0,0,0,.45);opacity:0;transition:opacity .25s';
  p.append(dot, chip);
  document.body.appendChild(p);
  p.showPopover();
  new MutationObserver(() => { if (p.matches(':popover-open')) p.hidePopover(); p.showPopover(); })
    .observe(document.body, {subtree: true, attributes: true, attributeFilter: ['open']});
  const at = {x: -100, y: -100};
  const put = (x, y) => {
    at.x = x; at.y = y;
    p.style.transform = `translate(${x}px,${y}px)`;
    try { sessionStorage.setItem('demo-pointer', JSON.stringify(at)); } catch (e) {}
    const el = document.elementFromPoint(x, y);
    if (el) el.dispatchEvent(new PointerEvent('pointermove', {clientX: x, clientY: y, bubbles: true, pointerType: 'mouse'}));
  };
  try {  // after a page load (a form sent): where it was, at once
    const was = JSON.parse(sessionStorage.getItem('demo-pointer') || 'null');
    if (was && was.x >= 0) { p.style.transition = 'none'; put(was.x, was.y); p.style.opacity = 1; }
  } catch (e) {}
  const ease = t => t < .5 ? 4 * t * t * t : 1 - Math.pow(-2 * t + 2, 3) / 2;
  window.__place = (x, y) => { put(x, y); p.style.opacity = 1; };
  window.__carry = text => { chip.textContent = text; chip.style.opacity = text ? 1 : 0; };
  window.__press = () => dot.animate([{transform: 'scale(1)'}, {transform: 'scale(.62)'}, {transform: 'scale(1)'}],
                                     {duration: 300, easing: 'ease-out'});
  // to (x, y) in ms on a gentle arc (bend: its sideways share; 0 = straight), eased in and out
  window.__glide = (x, y, ms, bend = .1, linear = false) => new Promise(done => {
    const x0 = at.x, y0 = at.y, t0 = performance.now();
    const cx = (x0 + x) / 2 - (y - y0) * bend, cy = (y0 + y) / 2 + (x - x0) * bend;
    p.style.opacity = 1;
    const step = now => {
      const t = Math.min(1, (now - t0) / ms), e = linear ? .5 - .5 * Math.cos(Math.PI * t) : ease(t), u = 1 - e;
      put(u * u * x0 + 2 * u * e * cx + e * e * x, u * u * y0 + 2 * u * e * cy + e * e * y);
      t < 1 ? requestAnimationFrame(step) : done();
    };
    requestAnimationFrame(step);
  });
});
"""


class Recorder:
    """`with Recorder(...)`: the frames shown meanwhile, as <folder>/NNNNN.png (lossless: JPEG halves the colour detail) and times.json
    ({"frames": [timestamp, ...], "end": stop time}, seconds)."""

    def __init__(self, page: "Page", cdp, folder: Path, size: tuple[int, int]) -> None:
        self.page, self.cdp, self.folder, self.size, self.frames = page, cdp, folder, size, []
        folder.mkdir(parents=True, exist_ok=True)
        for f in folder.glob("[0-9]*.*"):
            f.unlink()

    def frame(self, ev: dict) -> None:
        (self.folder / f"{len(self.frames):05d}.png").write_bytes(base64.b64decode(ev["data"]))
        self.frames.append(ev["metadata"]["timestamp"])
        self.cdp.send("Page.screencastFrameAck", {"sessionId": ev["sessionId"]})

    def __enter__(self) -> "Recorder":
        self.cdp.on("Page.screencastFrame", self.frame)
        self.cdp.send(
            "Page.startScreencast",
            {"format": "png", "maxWidth": self.size[0], "maxHeight": self.size[1], "everyNthFrame": 1},
        )
        return self

    def __exit__(self, *exc: object) -> None:
        self.page.wait_for_timeout(150)
        end = time.time()
        self.cdp.send("Page.stopScreencast")
        self.cdp.remove_listener("Page.screencastFrame", self.frame)
        (self.folder / "times.json").write_text(json.dumps({"frames": self.frames, "end": end}))
        span = end - self.frames[0]
        print(f"{self.folder.name}: {len(self.frames)} frames, {span:.1f} s")


class Pointer:
    def __init__(self, page: "Page") -> None:
        self.page, self.x, self.y = page, -100.0, -100.0

    def place(self, x: float, y: float) -> None:
        self.page.evaluate("([x, y]) => __place(x, y)", [x, y])
        self.page.mouse.move(x, y)
        self.x, self.y = x, y

    def glide(self, x: float, y: float, ms: int | None = None, bend: float = 0.1, linear: bool = False) -> None:
        """Glide there (the time by the distance unless given), then the real mouse there too (hover)."""
        dist = math.hypot(x - self.x, y - self.y)
        ms = ms or int(min(1400, max(650, 500 + 0.7 * dist)))
        self.page.evaluate("([x, y, ms, b, l]) => __glide(x, y, ms, b, l)", [x, y, ms, bend, linear])
        self.page.mouse.move(x, y)
        self.x, self.y = x, y

    def to(self, selector: str, ms: int | None = None, fx: float = 0.5, fy: float = 0.5) -> None:
        """Glide to a point of the first element the selector finds (fx, fy: its share of the box)."""
        b = self.page.locator(selector).first.bounding_box()
        self.glide(b["x"] + b["width"] * fx, b["y"] + b["height"] * fy, ms)

    def click(self, before: int = 140) -> None:
        self.page.evaluate("__press()")
        self.page.wait_for_timeout(before)
        self.page.mouse.down()
        self.page.mouse.up()

    def carry(self, text: str) -> None:
        self.page.evaluate("t => __carry(t)", text)


def smooth_scroll(page: "Page", dy: float, seconds: float) -> None:
    page.evaluate(
        """([dy, ms]) => new Promise(done => {
        const y0 = scrollY, t0 = performance.now();
        const step = now => { const t = Math.min(1, (now - t0) / ms); const e = 0.5 - 0.5 * Math.cos(Math.PI * t);
          scrollTo(0, y0 + dy * e); t < 1 ? requestAnimationFrame(step) : done(); };
        requestAnimationFrame(step); })""",
        [dy, int(seconds * 1000)],
    )


@contextlib.contextmanager
def session(base: str, cookie: tuple[str, str], viewport=(1440, 900), scale=4 / 3, css: str = "") -> Iterator:
    """(page, cdp, pointer) of a dark-mode browser logged in with the cookie, the pointer and the CSS in
    every page; the recorder's frame size is viewport * scale."""
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        browser = p.chromium.launch()
        ctx = browser.new_context(
            viewport={"width": viewport[0], "height": viewport[1]}, color_scheme="dark", device_scale_factor=scale
        )
        ctx.add_cookies([{"name": cookie[0], "value": cookie[1], "url": base}])
        ctx.add_init_script(POINTER_JS)
        if css:
            ctx.add_init_script(
                "addEventListener('DOMContentLoaded', () => { const s = document.createElement('style'); "
                f"s.textContent = {json.dumps(css)}; document.head.appendChild(s); }});"
            )
        page = ctx.new_page()
        try:
            yield page, ctx.new_cdp_session(page), Pointer(page)
        finally:
            browser.close()
