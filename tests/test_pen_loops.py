"""A pen stroke drawn around something means what is inside it."""
import math

from retouch import geometry


def _loop(cx, cy, r, n=40, close=True):
    pts = [(cx + r * math.cos(2 * math.pi * i / n), cy + r * math.sin(2 * math.pi * i / n)) for i in range(n)]
    return pts if close else pts[: n * 3 // 4]


def test_a_closed_pen_loop_fills_what_it_circles():
    mask = geometry.build_mask("brush", {"points": _loop(0.5, 0.5, 0.2), "width": 0.02}, (400, 400))
    assert mask.getpixel((200, 200)) == 255  # the centre
    assert mask.getpixel((20, 20)) == 0  # outside the loop


def test_a_loop_whose_ends_nearly_meet_still_fills():
    pts = _loop(0.5, 0.5, 0.2, n=40)[:38]  # ends 2 segments apart, like a real hand-drawn circle
    mask = geometry.build_mask("brush", {"points": pts, "width": 0.02}, (400, 400))
    assert mask.getpixel((200, 200)) == 255


def test_an_open_pen_stroke_marks_only_the_stroke():
    pts = _loop(0.5, 0.5, 0.2, close=False)  # three quarters of a circle: an arc, not a circle
    mask = geometry.build_mask("brush", {"points": pts, "width": 0.02}, (400, 400))
    assert mask.getpixel((200, 200)) == 0


def test_an_open_zigzag_is_not_filled_in():
    # a "Z" drawn across an object, 12 points, ends far apart
    corners = [(0.2, 0.2), (0.8, 0.2), (0.2, 0.8), (0.8, 0.8)]
    pts = []
    for (ax, ay), (bx, by) in zip(corners, corners[1:]):
        pts += [(ax + (bx - ax) * t / 4, ay + (by - ay) * t / 4) for t in range(4)]
    pts.append(corners[-1])
    mask = geometry.build_mask("brush", {"points": pts, "width": 0.02}, (400, 400))
    assert mask.getpixel((200, 80)) == 255  # on the top stroke
    assert mask.getpixel((120, 200)) == 0  # inside the Z's bounding box, away from every stroke
