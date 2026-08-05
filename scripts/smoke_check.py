from __future__ import annotations

from io import BytesIO
from pathlib import Path
import sys
from tempfile import TemporaryDirectory

from PIL import Image

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from daum_market_guard.config import load_config
from daum_market_guard.db import Database
from daum_market_guard.detector import assess_post
from daum_market_guard.hashing import fingerprint_image, hamming_hex
from daum_market_guard.models import ImageRef, PostDetail, PostRef


def _png(color: tuple[int, int, int]) -> bytes:
    image = Image.new("RGB", (32, 32), color)
    output = BytesIO()
    image.save(output, format="PNG")
    return output.getvalue()


def main() -> None:
    config = load_config("config.example.toml")
    assert len(config.boards) == 4

    fingerprint = fingerprint_image(_png((255, 0, 0)))
    assert hamming_hex(fingerprint.ahash, fingerprint.ahash) == 0

    with TemporaryDirectory() as directory:
        db = Database(Path(directory) / "test.sqlite3")
        try:
            old_ref = PostRef(
                "C3Zg",
                "https://cafe.daum.net/730418/C3Zg/1",
                "old",
                "C3Zg:1",
            )
            new_ref = PostRef(
                "C3Zg",
                "https://cafe.daum.net/730418/C3Zg/2",
                "new",
                "C3Zg:2",
            )
            old_id = db.upsert_post(
                PostDetail(old_ref, "old", author_name="old", images=[ImageRef("old.jpg")])
            )
            db.add_image(old_id, "old.jpg", "", "same", "0" * 16, "0" * 16, 640, 480)
            new_id = db.upsert_post(
                PostDetail(new_ref, "new", author_name="new", images=[ImageRef("new.jpg")])
            )
            db.add_image(new_id, "new.jpg", "", "same", "0" * 16, "0" * 16, 640, 480)
            assessment = assess_post(db, new_id, 8)
            assert assessment.score < 70

            old_ref_2 = PostRef(
                "C3Zg",
                "https://cafe.daum.net/730418/C3Zg/3",
                "old 2",
                "C3Zg:3",
            )
            new_ref_2 = PostRef(
                "C3Zg",
                "https://cafe.daum.net/730418/C3Zg/4",
                "new 2",
                "C3Zg:4",
            )
            old_id_2 = db.upsert_post(
                PostDetail(
                    old_ref_2,
                    "old 2",
                    author_name="old 2",
                    images=[ImageRef("old2a.jpg"), ImageRef("old2b.jpg")],
                )
            )
            db.add_image(old_id_2, "old2a.jpg", "", "same-a", "1" * 16, "1" * 16, 640, 480)
            db.add_image(old_id_2, "old2b.jpg", "", "same-b", "2" * 16, "2" * 16, 640, 480)
            new_id_2 = db.upsert_post(
                PostDetail(
                    new_ref_2,
                    "new 2",
                    author_name="new 2",
                    images=[ImageRef("new2a.jpg"), ImageRef("new2b.jpg")],
                )
            )
            db.add_image(new_id_2, "new2a.jpg", "", "same-a", "1" * 16, "1" * 16, 640, 480)
            db.add_image(new_id_2, "new2b.jpg", "", "same-b", "2" * 16, "2" * 16, 640, 480)
            assessment = assess_post(db, new_id_2, 8)
            assert assessment.score >= 70
        finally:
            db.close()

    print("smoke check passed")


if __name__ == "__main__":
    main()
