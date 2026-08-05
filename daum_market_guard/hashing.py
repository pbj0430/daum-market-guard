from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
from io import BytesIO

from PIL import Image, ImageOps


@dataclass(frozen=True)
class ImageFingerprint:
    sha256: str
    ahash: str
    dhash: str
    width: int
    height: int


def _bits_to_hex(bits: list[bool]) -> str:
    value = 0
    for bit in bits:
        value = (value << 1) | int(bit)
    width = max(1, (len(bits) + 3) // 4)
    return f"{value:0{width}x}"


def _load_image(image_bytes: bytes) -> tuple[Image.Image, tuple[int, int]]:
    image = Image.open(BytesIO(image_bytes))
    original_size = image.size
    try:
        image.draft("L", (1024, 1024))
    except (AttributeError, OSError):
        pass
    image.load()
    image = ImageOps.exif_transpose(image).convert("L")
    image.thumbnail((1024, 1024), Image.Resampling.LANCZOS)
    return image, original_size


def average_hash(image: Image.Image, size: int = 8) -> str:
    small = image.resize((size, size), Image.Resampling.LANCZOS)
    pixels = _pixels(small)
    avg = sum(pixels) / len(pixels)
    return _bits_to_hex([pixel >= avg for pixel in pixels])


def difference_hash(image: Image.Image, width: int = 9, height: int = 8) -> str:
    small = image.resize((width, height), Image.Resampling.LANCZOS)
    pixels = _pixels(small)
    bits: list[bool] = []
    for row in range(height):
        offset = row * width
        for col in range(width - 1):
            bits.append(pixels[offset + col] > pixels[offset + col + 1])
    return _bits_to_hex(bits)


def _pixels(image: Image.Image) -> list[int]:
    if hasattr(image, "get_flattened_data"):
        return list(image.get_flattened_data())
    return list(image.getdata())


def hamming_hex(left: str, right: str) -> int:
    if not left or not right:
        return 999
    max_len = max(len(left), len(right))
    left_int = int(left.zfill(max_len), 16)
    right_int = int(right.zfill(max_len), 16)
    return (left_int ^ right_int).bit_count()


def fingerprint_image(image_bytes: bytes) -> ImageFingerprint:
    digest = sha256(image_bytes).hexdigest()
    image, original_size = _load_image(image_bytes)
    width, height = original_size
    return ImageFingerprint(
        sha256=digest,
        ahash=average_hash(image),
        dhash=difference_hash(image),
        width=width,
        height=height,
    )
