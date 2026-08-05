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


def _detail_with_images(board: str, number: int, author: str, count: int) -> PostDetail:
    ref = PostRef(
        board_id=board,
        url=f"https://cafe.daum.net/730418/{board}/{number}",
        title=f"post {number}",
        post_key=f"{board}:{number}",
    )
    return PostDetail(
        ref=ref,
        title=ref.title,
        author_name=author,
        images=[ImageRef(f"x{index}") for index in range(count)],
    )


class DetectorTests(unittest.TestCase):
    def test_single_different_author_duplicate_stays_below_suspect_threshold(self) -> None:
        with TemporaryDirectory() as directory:
            db = Database(Path(directory) / "test.sqlite3")
            try:
                old_id = db.upsert_post(_detail("C3Zg", 1, "old"))
                self.assertTrue(db.post_key_exists("C3Zg:1"))
                self.assertFalse(db.post_key_exists("C3Zg:404"))
                self.assertEqual(db.max_post_number("C3Zg"), 1)
                self.assertFalse(db.missing_post_exists("C3Zg", 404))
                db.mark_missing_post("C3Zg", 404)
                self.assertTrue(db.missing_post_exists("C3Zg", 404))
                db.add_image(old_id, "old.jpg", "", "same", "0" * 16, "0" * 16, 640, 480)
                new_id = db.upsert_post(_detail("C3Zg", 2, "new"))
                db.add_image(new_id, "new.jpg", "", "same", "0" * 16, "0" * 16, 640, 480)
                assessment = assess_post(db, new_id, 8)
                self.assertEqual(assessment.score, 45)
                self.assertLess(assessment.score, 70)
                self.assertEqual(assessment.duplicate_image_count, 1)
                self.assertEqual(assessment.different_author_duplicate_count, 1)
                self.assertIn("content_text", db.get_post(new_id).keys())
            finally:
                db.close()

    def test_single_similar_only_different_author_duplicate_scores_low(self) -> None:
        with TemporaryDirectory() as directory:
            db = Database(Path(directory) / "test.sqlite3")
            try:
                old_id = db.upsert_post(_detail("C3Zg", 1, "old"))
                db.add_image(old_id, "old.jpg", "", "old-bytes", "0" * 16, "0" * 16, 640, 480)
                new_id = db.upsert_post(_detail("C3Zg", 2, "new"))
                db.add_image(new_id, "new.jpg", "", "new-bytes", "0" * 15 + "1", "0" * 15 + "1", 640, 480)

                assessment = assess_post(db, new_id, 8)

                self.assertEqual(assessment.score, 25)
                self.assertEqual(assessment.duplicate_image_count, 1)
                self.assertEqual(assessment.different_author_duplicate_count, 1)
            finally:
                db.close()

    def test_one_matching_hash_is_not_enough_for_similarity(self) -> None:
        with TemporaryDirectory() as directory:
            db = Database(Path(directory) / "test.sqlite3")
            try:
                old_id = db.upsert_post(_detail("C3Zg", 1, "old"))
                db.add_image(old_id, "old.jpg", "", "old-bytes", "0" * 16, "0" * 16, 640, 480)
                new_id = db.upsert_post(_detail("C3Zg", 2, "new"))
                db.add_image(new_id, "new.jpg", "", "new-bytes", "f" * 16, "0" * 16, 640, 480)

                assessment = assess_post(db, new_id, 4)

                self.assertEqual(assessment.score, 0)
                self.assertEqual(assessment.duplicate_image_count, 0)
                self.assertEqual(assessment.different_author_duplicate_count, 0)
            finally:
                db.close()

    def test_loose_config_threshold_is_capped(self) -> None:
        with TemporaryDirectory() as directory:
            db = Database(Path(directory) / "test.sqlite3")
            try:
                old_id = db.upsert_post(_detail("C3Zg", 1, "old"))
                db.add_image(old_id, "old.jpg", "", "old-bytes", "0" * 16, "0" * 16, 640, 480)
                new_id = db.upsert_post(_detail("C3Zg", 2, "new"))
                db.add_image(new_id, "new.jpg", "", "new-bytes", "0" * 14 + "3f", "0" * 14 + "3f", 640, 480)

                assessment = assess_post(db, new_id, 8)

                self.assertEqual(assessment.score, 0)
                self.assertEqual(assessment.duplicate_image_count, 0)
            finally:
                db.close()

    def test_multiple_different_author_duplicates_score_high(self) -> None:
        with TemporaryDirectory() as directory:
            db = Database(Path(directory) / "test.sqlite3")
            try:
                old_id = db.upsert_post(_detail_with_images("C3Zg", 1, "old", 2))
                db.add_image(old_id, "old1.jpg", "", "same1", "0" * 16, "0" * 16, 640, 480)
                db.add_image(old_id, "old2.jpg", "", "same2", "1" * 16, "1" * 16, 640, 480)
                new_id = db.upsert_post(_detail_with_images("C3Zg", 2, "new", 2))
                db.add_image(new_id, "new1.jpg", "", "same1", "0" * 16, "0" * 16, 640, 480)
                db.add_image(new_id, "new2.jpg", "", "same2", "1" * 16, "1" * 16, 640, 480)
                assessment = assess_post(db, new_id, 8)
                self.assertGreaterEqual(assessment.score, 70)
                self.assertEqual(assessment.duplicate_image_count, 2)
                self.assertEqual(assessment.different_author_duplicate_count, 1)
            finally:
                db.close()

    def test_unknown_authors_are_not_counted_as_different_authors(self) -> None:
        with TemporaryDirectory() as directory:
            db = Database(Path(directory) / "test.sqlite3")
            try:
                old_id = db.upsert_post(_detail("C3Zg", 1, ""))
                db.add_image(old_id, "old.jpg", "", "same", "0" * 16, "0" * 16, 640, 480)
                new_id = db.upsert_post(_detail("C3Zg", 2, ""))
                db.add_image(new_id, "new.jpg", "", "same", "0" * 16, "0" * 16, 640, 480)

                assessment = assess_post(db, new_id, 8)

                self.assertEqual(assessment.duplicate_image_count, 1)
                self.assertEqual(assessment.same_author_duplicate_count, 0)
                self.assertEqual(assessment.different_author_duplicate_count, 0)
            finally:
                db.close()

    def test_same_author_image_reuse_does_not_raise_score(self) -> None:
        with TemporaryDirectory() as directory:
            db = Database(Path(directory) / "test.sqlite3")
            try:
                old_id = db.upsert_post(_detail("C3Zg", 1, "same-author"))
                db.add_image(old_id, "old.jpg", "", "same", "0" * 16, "0" * 16, 640, 480)
                new_id = db.upsert_post(_detail("C3Zi", 2, "same-author"))
                db.add_image(new_id, "new.jpg", "", "same", "0" * 16, "0" * 16, 640, 480)

                assessment = assess_post(db, new_id, 4)

                self.assertEqual(assessment.score, 0)
                self.assertEqual(assessment.duplicate_image_count, 1)
                self.assertEqual(assessment.same_author_duplicate_count, 1)
                self.assertEqual(assessment.different_author_duplicate_count, 0)
            finally:
                db.close()


if __name__ == "__main__":
    unittest.main()
