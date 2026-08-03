from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from daum_market_guard.db import Database
from daum_market_guard.detector import assess_post
from daum_market_guard.models import ImageRef, PostDetail, PostRef


def _detail(board: str, number: int, author: str) -> PostDetail:
    ref = PostRef(
        board_id=board,
        url=f"https://cafe.daum.net/730418/{board}/{number}",
        title=f"post {number}",
        post_key=f"{board}:{number}",
    )
    return PostDetail(ref=ref, title=ref.title, author_name=author, images=[ImageRef("x")])


class DetectorTests(unittest.TestCase):
    def test_different_author_duplicate_scores_high(self) -> None:
        with TemporaryDirectory() as directory:
            db = Database(Path(directory) / "test.sqlite3")
            try:
                old_id = db.upsert_post(_detail("C3Zg", 1, "old"))
                db.add_image(old_id, "old.jpg", "", "same", "0" * 16, "0" * 16, 640, 480)
                new_id = db.upsert_post(_detail("C3Zg", 2, "new"))
                db.add_image(new_id, "new.jpg", "", "same", "0" * 16, "0" * 16, 640, 480)
                assessment = assess_post(db, new_id, 8)
                self.assertGreaterEqual(assessment.score, 70)
                self.assertEqual(assessment.duplicate_image_count, 1)
                self.assertEqual(assessment.different_author_duplicate_count, 1)
            finally:
                db.close()


if __name__ == "__main__":
    unittest.main()
