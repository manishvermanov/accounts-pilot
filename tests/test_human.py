"""Tests for the human-interaction layer (math only — no real browser)."""
import math

from accounts_pilot.runtime.human import cubic_bezier, _ease, HumanActor


def test_bezier_endpoints():
    p0, p1, p2, p3 = (0, 0), (10, 50), (90, 50), (100, 0)
    assert cubic_bezier(p0, p1, p2, p3, 0.0) == p0      # t=0 -> start
    assert cubic_bezier(p0, p1, p2, p3, 1.0) == p3      # t=1 -> end


def test_bezier_midpoint_is_between():
    p0, p1, p2, p3 = (0.0, 0.0), (0.0, 100.0), (100.0, 100.0), (100.0, 0.0)
    mx, my = cubic_bezier(p0, p1, p2, p3, 0.5)
    assert 0.0 < mx < 100.0
    assert my > 0.0                                     # arcs upward, not a straight line


def test_ease_bounds_and_monotonic():
    assert _ease(0.0) == 0.0
    assert _ease(1.0) == 1.0
    # smoothstep is monotonic increasing
    vals = [_ease(i / 10) for i in range(11)]
    assert all(b >= a for a, b in zip(vals, vals[1:]))


class _FakePage:
    """Minimal stand-in to construct a HumanActor without Playwright."""
    class _Mouse:
        def move(self, *a, **k): ...
        def down(self): ...
        def up(self): ...
        def wheel(self, *a, **k): ...
    def __init__(self):
        self.mouse = self._Mouse()


def test_humanactor_disabled_is_instant():
    actor = HumanActor(_FakePage(), enabled=False)
    # disabled actor should not raise and should update virtual cursor
    actor.move_to(123, 456)
    assert (actor._x, actor._y) == (123, 456)
