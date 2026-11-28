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


# 微信守护进程只原样保留「JPEG 且不超过 65,520 字节」的源图（实测 sourcePreserved=true、
# sha256 与源一致）；超过就重编码，并把最长边压到 1280、必要时继续降质量凑到预算内。
# 所以想让微信原样转发，就得自己先压进这个预算。
WECHAT_IMAGE_BUDGET_BYTES = 65200


def encode_jpeg(image: bytes, quality: int = 92) -> bytes | None:
    """把一张图整张编码成高质量 JPEG（不缩放、不降质迭代）。

    守护进程（≥3.0.0）在 ``imageQuality=original`` 下只会把「JPEG 源」逐字节原样
    转发；PNG 会被它转一次码（尺寸不变、字节变了）。所以微信原图模式先把长图编码
    成 q92 JPEG，发出的字节和微信里看到的完全一致。
    """
    try:
        quality = int(quality)
    except (TypeError, ValueError):
        quality = 92
    quality = max(40, min(quality, 100))
    from PIL import Image

    try:
        with Image.open(io.BytesIO(image)) as source:
            canvas = source.convert("RGB")
        buffer = io.BytesIO()
        canvas.save(buffer, format="JPEG", quality=quality, optimize=True, subsampling=0)
        return buffer.getvalue()
    except (OSError, ValueError):
        return None


def resize_image(image: bytes, width: int) -> bytes | None:
    """把整张长图等比缩到指定宽度，返回 PNG 字节；``width <= 0`` 或解码失败返回 None。

    微信聊天里先显示的是它自己生成的预览图，而预览的字节预算固定在 34KB 左右
    （实测 1080×2363 与 756×1654 的预览分别是 33,959 / 34,096 字节）。像素越少，
    同样的预算能分给每个字的码字就越多——渲染宽度从 1080 降到 900 后，预览从
    「JPEG q2」涨到「q5」，字缘明显干净。
    """
    try:
        width = int(width)
    except (TypeError, ValueError):
        return None
    if width <= 0:
        return None
    from PIL import Image

    try:
        with Image.open(io.BytesIO(image)) as source:
            canvas = source.convert("RGB")
            if canvas.width <= width:
                buffer = io.BytesIO()
                canvas.save(buffer, format="PNG")
                return buffer.getvalue()
            height = max(1, round(canvas.height * width / canvas.width))
            canvas = canvas.resize((width, height), Image.LANCZOS)
        buffer = io.BytesIO()
        canvas.save(buffer, format="PNG")
        return buffer.getvalue()
    except (OSError, ValueError):
        return None


def encode_jpeg_within_budget(image: bytes, max_bytes: int = WECHAT_IMAGE_BUDGET_BYTES,
                              *, min_quality: int = 45) -> bytes | None:
    """把一张图编码成不超过 max_bytes 的 JPEG；同尺寸压不进预算时等比缩小重试。

    宁可小幅缩图也不把质量压到 45 以下（低质量 JPEG 在中文细笔画上会出方块）；
    实在压不进就继续缩小，直到 0.4 倍仍不行则返回 None 交给调用方兜底。
    """
    from PIL import Image

    if max_bytes <= 0:
        return None
    try:
        with Image.open(io.BytesIO(image)) as source:
            canvas = source.convert("RGB")
    except (OSError, ValueError):
        return None
    scale = 1.0
    while True:
        target = canvas
        if scale < 1.0:
            size = (max(1, int(canvas.width * scale)), max(1, int(canvas.height * scale)))
            target = canvas.resize(size, Image.LANCZOS)
        quality = 95
        while True:
            buffer = io.BytesIO()
            target.save(buffer, format="JPEG", quality=quality, optimize=True)
            data = buffer.getvalue()
            if len(data) <= max_bytes:
                return data
            if quality <= min_quality:
                break
            quality = max(min_quality, quality - 5)
        if scale <= 0.4:
            return None
        scale *= 0.85


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
