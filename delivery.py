"""Text fallback, throttled progress and original PNG file management."""
import asyncio
import io
import time
from pathlib import Path
from uuid import uuid4


def split_message(text: str, limit: int = 1200) -> list[str]:
    """Split even oversized lines; concatenating chunks reproduces every byte."""
    chunks = []
    while len(text) > limit:
        boundary = text.rfind("\n", 0, limit)
        end = boundary + 1 if boundary >= 0 else limit
        chunks.append(text[:end])
        text = text[end:]
    if text:
        chunks.append(text)
    return chunks or [""]


class QueryProgress:
    def __init__(self, event, enabled=True, interval=8.0, clock=time.monotonic,
                 mode="等待时提示一次", wait_seconds=15.0):
        self.event, self.enabled, self.interval, self.clock = event, enabled, interval, clock
        self.last = None
        self.mode, self.wait_seconds = mode, wait_seconds
        self.started = False
        self.closed = False
        self.task = None

    def start(self):
        if self.started or self.closed or not self.enabled:
            return
        self.started = True
        if self.mode != "详细进度":
            self.task = asyncio.create_task(self._delayed_notice())

    async def _delayed_notice(self):
        await asyncio.sleep(self.wait_seconds)
        if self.closed or not self.enabled:
            return
        try:
            await self.event.send(self.event.plain_result("查询仍在进行，请稍候；完成后会发送结果。"))
        except Exception:
            self.enabled = False

    async def close(self):
        self.closed = True
        if self.task is not None:
            if not self.task.done():
                self.task.cancel()
            try:
                await self.task
            except asyncio.CancelledError:
                pass

    async def __call__(self, message, *, force=False):
        self.start()
        if self.mode != "详细进度" or self.closed:
            return False
        now = self.clock()
        # Stage changes must respect the same interval as detail updates.
        if not self.enabled or (self.last is not None and now - self.last < self.interval):
            return False
        try:
            await self.event.send(self.event.plain_result(message))
        except Exception:
            self.enabled = False
            return False
        self.last = now
        return True


def slice_poster(image: bytes, max_height: int, *, min_height_ratio: float = 0.7) -> list[bytes]:
    """把过长的战报切成几张，每张不超过 max_height，优先切在空白行上。

    微信等平台会把内嵌图片压得很小，一张两千多像素高的长图压完文字全糊；
    切成几张后每张都能拿到一份压缩预算，字就清楚得多。切点落在空白行，
    不会把某一行文字切成两半。
    """
    from PIL import Image

    if max_height <= 0:
        return [image]
    with Image.open(io.BytesIO(image)) as source:
        canvas = source.convert("RGB")
        width, height = canvas.size
        if height <= max_height:
            return [image]
        paper = bytes(canvas.getpixel((0, 0)))
        blank_row = paper * width
        pages, top = [], 0
        while height - top > max_height:
            limit = top + max_height
            floor = min(limit, top + int(max_height * min_height_ratio))
            cut = None
            for y in range(limit, floor, -1):
                if canvas.crop((0, y, width, y + 1)).tobytes() == blank_row:
                    cut = y
                    break
            if cut is None:
                cut = limit
            pages.append(canvas.crop((0, top, width, cut)))
            top = cut
        pages.append(canvas.crop((0, top, width, height)))
        out = []
        for page in pages:
            buffer = io.BytesIO()
            page.save(buffer, format="PNG")
            out.append(buffer.getvalue())
        return out


def save_originals(directory, images, *, now=None):
    """Keep sent originals for callback downloads; expire only our own old PNGs."""
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    stamp = time.time() if now is None else now
    for path in directory.glob("naraka-original-*.png"):
        if path.is_file() and not path.is_symlink():
            try:
                if stamp - path.stat().st_mtime > 86400:
                    path.unlink()
            except OSError:
                pass
    paths = []
    for index, image in enumerate(images, 1):
        path = directory / f"naraka-original-{int(stamp)}-{uuid4().hex}-{index}.png"
        path.write_bytes(image)
        paths.append(path)
    return paths
