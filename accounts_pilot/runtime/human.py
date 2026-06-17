"""Human-like interaction layer.

Anti-bot systems on OTA signup pages (DataDome / PerimeterX / reCAPTCHA's risk
score) flag *behaviour*, not just fingerprints. Teleporting the cursor to a
selector and calling click(), or setting a field value instantly, is an obvious
bot tell — CloakBrowser hides the fingerprint but does NOT humanise behaviour.

This module makes every interaction look human:
  - mouse moves along a curved (cubic-Bézier) path with eased, variable speed
    and small jitter, aiming at a random point inside the target (not dead centre)
  - a short dwell before pressing; press/release have their own micro-delay
  - text is typed character-by-character with randomised inter-key delays and the
    occasional longer "think" pause
  - configurable think() pauses between actions

All randomness uses Python's `random` (fine in normal code). Timing ranges are
tunable from config so you can slow things down for stubborn targets.
"""
from __future__ import annotations

import math
import random
import time
from typing import Tuple

Point = Tuple[float, float]


def cubic_bezier(p0: Point, p1: Point, p2: Point, p3: Point, t: float) -> Point:
    """Cubic Bézier interpolation. t=0 -> p0, t=1 -> p3."""
    mt = 1.0 - t
    x = (mt**3) * p0[0] + 3 * (mt**2) * t * p1[0] + 3 * mt * (t**2) * p2[0] + (t**3) * p3[0]
    y = (mt**3) * p0[1] + 3 * (mt**2) * t * p1[1] + 3 * mt * (t**2) * p2[1] + (t**3) * p3[1]
    return x, y


def _ease(t: float) -> float:
    """Smoothstep ease-in-out — slow start, fast middle, slow approach."""
    return t * t * (3.0 - 2.0 * t)


class HumanActor:
    def __init__(
        self,
        page,
        *,
        enabled: bool = True,
        key_delay: Tuple[float, float] = (0.04, 0.17),
        think_range: Tuple[float, float] = (0.3, 1.1),
    ):
        self.page = page
        self.enabled = enabled
        self.key_delay = key_delay
        self.think_range = think_range
        # virtual cursor position (where we last "are")
        self._x = random.uniform(0, 300)
        self._y = random.uniform(0, 300)

    # ---- timing -----------------------------------------------------------
    def think(self, lo: float | None = None, hi: float | None = None) -> None:
        if not self.enabled:
            return
        lo = self.think_range[0] if lo is None else lo
        hi = self.think_range[1] if hi is None else hi
        time.sleep(random.uniform(lo, hi))

    # ---- mouse ------------------------------------------------------------
    def move_to(self, x: float, y: float) -> None:
        if not self.enabled:
            self.page.mouse.move(x, y)
            self._x, self._y = x, y
            return

        start = (self._x, self._y)
        # aim a little off dead-centre so coordinates aren't suspiciously exact
        end = (x + random.uniform(-2.5, 2.5), y + random.uniform(-2.5, 2.5))
        dist = math.hypot(end[0] - start[0], end[1] - start[1])

        # two random control points → a natural arc, not a straight line
        def ctrl(frac: float) -> Point:
            jitter = max(8.0, dist * 0.22)
            return (
                start[0] + (end[0] - start[0]) * frac + random.uniform(-jitter, jitter),
                start[1] + (end[1] - start[1]) * frac + random.uniform(-jitter, jitter),
            )

        c1, c2 = ctrl(0.33), ctrl(0.66)
        steps = max(14, min(60, int(dist / 4) + random.randint(-3, 3)))

        for i in range(1, steps + 1):
            t = _ease(i / steps)
            px, py = cubic_bezier(start, c1, c2, end, t)
            self.page.mouse.move(px, py)
            time.sleep(random.uniform(0.004, 0.018))

        self._x, self._y = end

    def _target_point(self, selector: str, timeout_ms: int = 15000):
        el = self.page.wait_for_selector(selector, timeout=timeout_ms)
        el.scroll_into_view_if_needed()
        box = el.bounding_box()
        if not box:
            # element not visible/measurable — fall back to native
            return el, None, None
        cx = box["x"] + box["width"] * random.uniform(0.35, 0.65)
        cy = box["y"] + box["height"] * random.uniform(0.35, 0.65)
        return el, cx, cy

    def click(self, selector: str) -> None:
        el, cx, cy = self._target_point(selector)
        if cx is None:
            el.click()
            return
        self.move_to(cx, cy)
        self.think(0.08, 0.28)
        self.page.mouse.down()
        time.sleep(random.uniform(0.04, 0.12))
        self.page.mouse.up()

    def check(self, selector: str) -> None:
        # route through a human click (checkboxes/radios are click targets)
        el = self.page.wait_for_selector(selector, timeout=15000)
        if not el.is_checked():
            self.click(selector)

    # ---- keyboard ---------------------------------------------------------
    def type(self, selector: str, text: str, *, clear: bool = True) -> None:
        self.click(selector)
        self.think(0.1, 0.3)
        if clear:
            self.page.keyboard.press("Control+A")
            time.sleep(random.uniform(0.03, 0.08))
            self.page.keyboard.press("Delete")
            time.sleep(random.uniform(0.05, 0.12))
        if not self.enabled:
            self.page.keyboard.type(text)
            return
        for ch in text:
            self.page.keyboard.type(ch)
            time.sleep(random.uniform(*self.key_delay))
            if random.random() < 0.04:                    # occasional think mid-word
                time.sleep(random.uniform(0.25, 0.7))

    # ---- native dropdown --------------------------------------------------
    def select(self, selector: str, value: str) -> None:
        el, cx, cy = self._target_point(selector)
        if cx is not None:
            self.move_to(cx, cy)
            self.think(0.1, 0.3)
        el.select_option(value)

    # ---- scrolling --------------------------------------------------------
    def scroll_to(self, selector: str) -> None:
        el = self.page.wait_for_selector(selector, timeout=15000)
        if not self.enabled:
            el.scroll_into_view_if_needed()
            return
        # a few wheel nudges read more human than an instant jump
        box = el.bounding_box()
        if box:
            for _ in range(random.randint(2, 4)):
                self.page.mouse.wheel(0, random.uniform(120, 280))
                time.sleep(random.uniform(0.08, 0.22))
        el.scroll_into_view_if_needed()
