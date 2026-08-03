from io import BytesIO
import unittest

from PIL import Image

from daum_market_guard.hashing import fingerprint_image, hamming_hex


def _png(color: tuple[int, int, int]) -> bytes:
    image = Image.new("RGB", (32, 32), color)
    out = BytesIO()
    image.save(out, format="PNG")
    return out.getvalue()


class HashingTests(unittest.TestCase):
    def test_fingerprint_is_stable_for_same_bytes(self) -> None:
        data = _png((255, 0, 0))
        left = fingerprint_image(data)
        right = fingerprint_image(data)
        self.assertEqual(left.sha256, right.sha256)
        self.assertEqual(hamming_hex(left.ahash, right.ahash), 0)
        self.assertEqual(hamming_hex(left.dhash, right.dhash), 0)

    def test_hamming_hex_counts_bits(self) -> None:
        self.assertEqual(hamming_hex("0", "f"), 4)
        self.assertEqual(hamming_hex("00", "ff"), 8)


if __name__ == "__main__":
    unittest.main()
