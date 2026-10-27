"""Watching a camera for movement, frame by frame.

Each frame is shrunk to a grid of cells, each the average of its patch, and
compared with the frame before. What a picture does as a whole (a cloud
over the sun, the camera switching to night vision) isn't movement: each
frame's average brightness is taken off before comparing, and a change over
most of the picture is ignored. What keeps moving (a tree in the wind, a
fountain) isn't either: a cell that has changed often lately is left out
until it settles. What's left, in enough cells for two frames running, is.
"""

import io

#: The grid a frame is compared on: about 40 pixels a cell at 1280 wide.
COLS, ROWS = 32, 18
#: How far a cell must change, out of 255, after the frame's own brightness
#: is taken off: above a compressed stream's flicker.
CELL_CHANGE = 14
#: The fewest moving cells that are movement, and the share of the picture
#: past which a change is the whole picture's doing, not something in it.
MIN_CELLS = 2
WHOLE_PICTURE = 0.6
#: How fast a cell's recent activity follows it, and the activity past which
#: it's background that keeps moving.
BUSY_RATE = 0.1
BUSY = 0.3
#: Frames that only learn the scene before anything counts: what sways is
#: found before it can be called movement.
WARM_UP = 8


def cells(jpeg: bytes) -> list[int]:
    """A frame as its grid of cells, gray, row by row."""
    from PIL import Image  # noqa: PLC0415 — Home Assistant ships Pillow

    image = Image.open(io.BytesIO(jpeg))
    image.draft("L", (COLS * 8, ROWS * 8))
    return list(image.convert("L").resize((COLS, ROWS), Image.BOX).getdata())


class Movement:
    """One camera's frames, fed in order; says when something moved."""

    def __init__(self) -> None:
        self._last: list[float] | None = None
        self._busy = [0.0] * (COLS * ROWS)
        self._seen = 0
        self._moving_before = False

    def feed(self, grid: list[int]) -> float | None:
        """The share of the picture that moved, when this frame and the one
        before both moved; None otherwise."""
        mean = sum(grid) / len(grid)
        flat = [v - mean for v in grid]
        last, self._last = self._last, flat
        self._seen += 1
        if last is None:
            return None
        changed = [abs(a - b) > CELL_CHANGE for a, b in zip(flat, last, strict=True)]
        moving = sum(1 for c, busy in zip(changed, self._busy, strict=True) if c and busy < BUSY)
        for i, c in enumerate(changed):
            self._busy[i] += BUSY_RATE * (float(c) - self._busy[i])
        whole = sum(changed) / len(changed) > WHOLE_PICTURE
        now = self._seen > WARM_UP and not whole and moving >= MIN_CELLS
        both, self._moving_before = now and self._moving_before, now
        return round(moving / len(changed), 3) if both else None
