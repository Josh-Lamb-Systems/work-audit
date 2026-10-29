"""Image helpers: compress screenshots and detect whether the screen changed."""
from __future__ import annotations

from pathlib import Path
from typing import Optional, Tuple

from PIL import Image

SIG_SIZE = (32, 20)

try:  # Pillow >= 9.1
    LANCZOS = Image.Resampling.LANCZOS
    BILINEAR = Image.Resampling.BILINEAR
except AttributeError:  # pragma: no cover
    LANCZOS = Image.LANCZOS
    BILINEAR = Image.BILINEAR


def load(path: Path) -> Image.Image:
    img = Image.open(str(path))
    img.load()
    if img.mode != "RGB":
        img = img.convert("RGB")
    return img


def shrink(img: Image.Image, max_side: int) -> Image.Image:
    w, h = img.size
    longest = max(w, h)
    if longest <= max_side:
        return img
    scale = max_side / float(longest)
    return img.resize((max(1, int(round(w * scale))), max(1, int(round(h * scale)))), LANCZOS)


def signature(img: Image.Image) -> bytes:
    """Tiny grayscale thumbnail used to tell whether the screen changed."""
    return img.convert("L").resize(SIG_SIZE, BILINEAR).tobytes()


def diff(a: Optional[bytes], b: Optional[bytes]) -> float:
    """Mean absolute difference (0-255) between two signatures; 255 when not comparable."""
    if not a or not b or len(a) != len(b):
        return 255.0
    return sum(abs(x - y) for x, y in zip(a, b)) / float(len(a))


def save_jpeg(img: Image.Image, out: Path, quality: int) -> Tuple[int, int]:
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_suffix(".part")
    img.save(str(tmp), "JPEG", quality=int(quality), optimize=True, progressive=True)
    tmp.replace(out)
    return img.size
