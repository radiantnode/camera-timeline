"""Taking pictures: from a camera's stream, or from its snapshot.

A battery camera's snapshot is only as fresh as its last wake, so it's
captured from its stream, which wakes it; ffmpeg reads the stream until it has
the frames. Any other camera answers a snapshot with a current picture, which
is cheaper than opening its stream.
"""

import asyncio
import contextlib
import logging
import os
import time

from homeassistant.components.camera import async_get_image, async_get_stream_source
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError

_LOGGER = logging.getLogger(__name__)

SOI, EOI = b"\xff\xd8", b"\xff\xd9"


def split_jpegs(buffer: bytes) -> tuple[list[bytes], bytes]:
    """Whole JPEGs at the front of an MJPEG pipe, and what's left. ffmpeg's
    baseline JPEGs carry no thumbnail, and their entropy data byte-stuffs every
    0xFF, so the first EOI after an SOI ends the picture."""
    out = []
    while True:
        start = buffer.find(SOI)
        if start < 0:
            return out, b""
        end = buffer.find(EOI, start + 2)
        if end < 0:
            return out, buffer[start:]
        out.append(buffer[start:end + 2])
        buffer = buffer[end + 2:]


#: Frames a second a stream reader decodes: enough for a burst's half-second
#: spacing, without decoding every frame of a 1080p stream.
READER_FPS = 4
#: How long a stream reader stays open after its last use, so captures close
#: together share one stream. Reopening a stream that has only just closed is
#: what an eufy-sdk bridge refuses, and then refuses every open for 30s.
LINGER_S = 15


class StreamReader:
    """One camera's stream, read while something wants pictures from it and
    for LINGER_S after: decoded to JPEG at READER_FPS, the newest kept."""

    def __init__(self, hass: HomeAssistant, ffmpeg: str, entity_id: str,
                 starting: asyncio.Lock | None = None) -> None:
        self._hass = hass
        self._ffmpeg = ffmpeg
        self._entity_id = entity_id
        #: Held from opening until the first frame, shared by the cameras of
        #: one hub: two Eufy cameras waking on one HomeBase at once each took
        #: 20-47s, and one gave nothing in 40s; one at a time they take 5-18s.
        self._starting = starting
        self._process: asyncio.subprocess.Process | None = None
        self._reader: asyncio.Task | None = None
        self._frames: list[tuple[float, bytes]] = []
        self._arrived = asyncio.Condition()
        self._last_use = 0.0
        self._closer: asyncio.TimerHandle | None = None
        #: Held while opening, so callers at once share one stream.
        self._opening = asyncio.Lock()
        #: Callers waiting on frames right now; it isn't closed while any are.
        self._users = 0

    @property
    def running(self) -> bool:
        return self._process is not None and self._process.returncode is None

    async def _open(self) -> None:
        source = await async_get_stream_source(self._hass, self._entity_id)
        if not source:
            raise HomeAssistantError(f"{self._entity_id} has no stream to take a picture from")
        args = [self._ffmpeg, "-hide_banner", "-loglevel", "error"]
        if source.startswith("rtsp"):
            args += ["-rtsp_transport", "tcp"]
        args += ["-i", source, "-vf", f"fps={READER_FPS}", "-q:v", "3", "-f", "image2pipe", "-c:v", "mjpeg", "pipe:1"]
        self._frames = []
        self._process = await asyncio.create_subprocess_exec(
            *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL
        )
        self._reader = self._hass.async_create_background_task(
            self._read(self._process), f"camera_timeline reader {self._entity_id}"
        )

    async def _read(self, process: asyncio.subprocess.Process) -> None:
        buffer = b""
        assert process.stdout
        while chunk := await process.stdout.read(65536):
            pictures, buffer = split_jpegs(buffer + chunk)
            if pictures:
                now = time.monotonic()
                async with self._arrived:
                    self._frames.extend((now, p) for p in pictures)
                    del self._frames[:-READER_FPS * 5]
                    self._arrived.notify_all()
        await process.wait()
        async with self._arrived:
            self._arrived.notify_all()

    async def start(self, timeout: float) -> None:
        """Open the stream unless it's open, and wait up to `timeout` for its
        first frame: the camera is awake once this returns."""
        self._users += 1
        try:
            async with self._opening:
                if self.running:
                    return
                async with self._starting or contextlib.nullcontext():
                    await self._open()
                    await self._first_frame(timeout)
                if not self._frames:
                    await self.close()
                    raise HomeAssistantError(f"{self._entity_id} didn't send a picture in {timeout:.0f}s")
        finally:
            self._users -= 1
            self._used()

    async def frames(self, count: int, gap: float, timeout: float) -> list[tuple[float, bytes]]:
        """`count` frames that arrived after this was asked, `gap` apart."""
        asked = time.monotonic()
        self._users += 1
        try:
            await self.start(timeout)
            taken = await self._take(asked, count, gap, timeout)
        finally:
            self._users -= 1
            self._used()
        if not taken:
            if not self.running:
                await self.close()
            raise HomeAssistantError(f"{self._entity_id} didn't send a picture in {timeout:.0f}s")
        first = taken[0][0]
        return [(round(t - first, 2), data) for t, data in taken]

    async def _take(self, asked: float, count: int, gap: float, timeout: float) -> list[tuple[float, bytes]]:
        taken: list[tuple[float, bytes]] = []
        try:
            async with asyncio.timeout(timeout):
                async with self._arrived:
                    while len(taken) < count:
                        due = asked if not taken else taken[0][0] + len(taken) * gap
                        # Never the frame already taken: one that arrived late
                        # can also be at or past the next one's due time.
                        after = taken[-1][0] if taken else float("-inf")
                        fresh = [f for f in self._frames if f[0] >= due and f[0] > after]
                        if fresh:
                            taken.append(fresh[0])
                            continue
                        if not self.running:
                            break
                        await self._arrived.wait()
        except TimeoutError:
            _LOGGER.debug("%s: %s of %s frames in %ss", self._entity_id, len(taken), count, timeout)
        return taken

    async def _first_frame(self, timeout: float) -> None:
        """Until the stream just opened gives its first frame, or ends."""
        with contextlib.suppress(TimeoutError):
            async with asyncio.timeout(timeout):
                async with self._arrived:
                    while not self._frames and self.running:
                        await self._arrived.wait()

    def _used(self) -> None:
        """Keep the stream open for LINGER_S from now."""
        self._last_use = time.monotonic()
        if self._closer:
            self._closer.cancel()
        self._closer = self._hass.loop.call_later(LINGER_S, self._close_if_idle)

    def _close_if_idle(self) -> None:
        if not self._users and time.monotonic() - self._last_use >= LINGER_S - 0.5:
            self._hass.async_create_background_task(self.close(), f"camera_timeline close {self._entity_id}")

    async def close(self) -> None:
        if self._closer:
            self._closer.cancel()
            self._closer = None
        process, self._process = self._process, None
        if process is not None and process.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                process.kill()
            await process.wait()
        if self._reader is not None:
            await asyncio.gather(self._reader, return_exceptions=True)
            self._reader = None


#: A loop is this long, and this wide: 720p H.264, which every browser plays
#: and which stays above the 360 lines below which Chrome decodes in software.
LOOP_S = 3
LOOP_WIDTH = 1280
#: Smaller than this, a loop has no picture in it: ffmpeg ends well, with just
#: the file's header (299 bytes), when a stream gives it nothing. A real loop
#: is 40 KB or more.
LOOP_EMPTY_BYTES = 4096


async def record_loop(hass: HomeAssistant, ffmpeg: str, entity_id: str, path: str, timeout: float) -> bool:
    """A few seconds of the camera's stream as an MP4 at `path`. Encoded
    rather than copied, so it starts on a whole picture wherever the stream
    was, and is small: about 250 KB. Whether it was written, with a picture
    in it."""
    source = await async_get_stream_source(hass, entity_id)
    if not source:
        return False
    args = [ffmpeg, "-hide_banner", "-loglevel", "error"]
    if source.startswith("rtsp"):
        args += ["-rtsp_transport", "tcp"]
    # Timed from its first picture, not from opening the stream: joining a
    # stream another viewer already holds starts between whole pictures, up
    # to 5s from the next, and `-t 3` then ran out first and wrote nothing
    # (3 of 6 tries on the kitchen camera, 2026-10-26).
    trim = f"setpts=PTS-STARTPTS,trim=duration={LOOP_S},scale={LOOP_WIDTH}:-2"
    args += ["-i", source, "-an", "-vf", trim, "-c:v", "libx264",
             "-preset", "veryfast", "-crf", "26", "-pix_fmt", "yuv420p", "-movflags", "+faststart", "-y", path]
    process = await asyncio.create_subprocess_exec(
        *args, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL
    )
    try:
        async with asyncio.timeout(timeout):
            if await process.wait() != 0:
                return False
        return await hass.async_add_executor_job(os.path.getsize, path) > LOOP_EMPTY_BYTES
    except TimeoutError:
        _LOGGER.debug("%s: loop timed out", entity_id)
        return False
    finally:
        if process.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                process.kill()
            await process.wait()


async def from_snapshot(hass: HomeAssistant, entity_id: str, frames: int, gap: float) -> list[tuple[float, bytes]]:
    """`frames` snapshots about `gap` seconds apart."""
    taken: list[tuple[float, bytes]] = []
    first = None
    for i in range(frames):
        if i:
            await asyncio.sleep(max(0.0, first + i * gap - time.monotonic()))
        image = await async_get_image(hass, entity_id)
        now = time.monotonic()
        first = now if first is None else first
        taken.append((round(now - first, 2), image.content))
    return taken
