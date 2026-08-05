import unittest
from unittest.mock import patch

from daum_market_guard.config import AppConfig, BoardConfig
from daum_market_guard.scraper import find_system_chromium
from daum_market_guard.scraper import DaumCafeScraper
from daum_market_guard.scraper import _author_from_article_sections
from daum_market_guard.scraper import _is_notice_title
from daum_market_guard.scraper import _posted_at_from_article_sections
from daum_market_guard.scraper import _title_from_article_sections


class ScraperTests(unittest.TestCase):
    def test_find_system_chromium_returns_first_available_browser(self) -> None:
        def fake_which(command: str) -> str | None:
            return "/usr/bin/chromium" if command == "chromium" else None

        with patch("daum_market_guard.scraper.shutil.which", fake_which):
            self.assertEqual(find_system_chromium(), "/usr/bin/chromium")

    def test_find_system_chromium_returns_none_when_missing(self) -> None:
        with patch("daum_market_guard.scraper.shutil.which", return_value=None):
            self.assertIsNone(find_system_chromium())

    def test_rejects_links_from_other_boards(self) -> None:
        scraper = DaumCafeScraper(AppConfig())
        board = BoardConfig("Market C3Zg", "https://cafe.daum.net/730418/C3Zg")
        ref = scraper._link_to_post_ref(
            board,
            {
                "text": "[공식]Daum카페 라운지",
                "href": "https://cafe.daum.net/_c21_/bbs_read?fldid=notice&dataid=1",
            },
        )
        self.assertIsNone(ref)

    def test_accepts_current_board_legacy_article_links(self) -> None:
        scraper = DaumCafeScraper(AppConfig())
        board = BoardConfig("Market C3Zg", "https://cafe.daum.net/730418/C3Zg")
        ref = scraper._link_to_post_ref(
            board,
            {
                "text": "판매글",
                "href": "https://cafe.daum.net/_c21_/bbs_read?fldid=C3Zg&dataid=123",
            },
        )
        self.assertIsNotNone(ref)
        self.assertEqual(ref.post_key, "C3Zg:123")

    def test_accepts_current_board_legacy_datanum_links(self) -> None:
        scraper = DaumCafeScraper(AppConfig())
        board = BoardConfig("Market C3Zg", "https://cafe.daum.net/730418/C3Zg")
        ref = scraper._link_to_post_ref(
            board,
            {
                "text": "?먮ℓ湲",
                "href": (
                    "https://cafe.daum.net/_c21_/bbs_read?"
                    "grpid=1R9cj&fldid=C3Zg&datanum=789"
                ),
            },
        )
        self.assertIsNotNone(ref)
        self.assertEqual(ref.post_key, "C3Zg:789")

    def test_rejects_notice_titles(self) -> None:
        scraper = DaumCafeScraper(AppConfig())
        board = BoardConfig("Market C3Zg", "https://cafe.daum.net/730418/C3Zg")
        ref = scraper._link_to_post_ref(
            board,
            {
                "text": "공지 인터넷 익스플로러 안내",
                "href": "https://cafe.daum.net/_c21_/bbs_read?fldid=C3Zg&dataid=1",
            },
        )
        self.assertIsNone(ref)
        self.assertTrue(_is_notice_title("怨듭??명꽣???ш린"))

    def test_accepts_javascript_article_links(self) -> None:
        scraper = DaumCafeScraper(AppConfig())
        board = BoardConfig("Market C3Zg", "https://cafe.daum.net/730418/C3Zg")
        ref = scraper._link_to_post_ref(
            board,
            {
                "text": "판매글",
                "href": "javascript:;",
                "onclick": "goArticle('C3Zg', '456')",
            },
        )
        self.assertIsNotNone(ref)
        self.assertEqual(ref.post_key, "C3Zg:456")

    def test_board_page_urls_use_desktop_only_by_default(self) -> None:
        scraper = DaumCafeScraper(AppConfig())
        board = BoardConfig("Market C3Zg", "https://cafe.daum.net/730418/C3Zg")
        urls = scraper._board_page_urls(board, 1)
        self.assertEqual(
            urls,
            ["https://cafe.daum.net/_c21_/bbs_list?grpid=1R9cj&fldid=C3Zg"],
        )

    def test_board_page_urls_include_mobile_fallback_when_enabled(self) -> None:
        scraper = DaumCafeScraper(AppConfig(allow_mobile_fallback=True))
        board = BoardConfig("Market C3Zg", "https://cafe.daum.net/730418/C3Zg")
        urls = scraper._board_page_urls(board, 1)
        self.assertEqual(
            urls,
            [
                "https://cafe.daum.net/_c21_/bbs_list?grpid=1R9cj&fldid=C3Zg",
                "https://m.cafe.daum.net/730418/C3Zg",
            ],
        )

    def test_mobile_board_url_is_converted_to_desktop(self) -> None:
        scraper = DaumCafeScraper(AppConfig())
        board = BoardConfig("Market C3Zg", "https://m.cafe.daum.net/730418/C3Zg")
        urls = scraper._board_page_urls(board, 1)
        self.assertEqual(
            urls,
            ["https://cafe.daum.net/_c21_/bbs_list?grpid=1R9cj&fldid=C3Zg"],
        )

    def test_legacy_board_url_parses_board_id_from_query(self) -> None:
        board = BoardConfig(
            "Market C3Zg",
            "https://cafe.daum.net/_c21_/bbs_list?grpid=1R9cj&fldid=C3Zg",
        )
        self.assertEqual(board.cafe_id, "1R9cj")
        self.assertEqual(board.board_id, "C3Zg")

    def test_rejects_profile_like_images(self) -> None:
        scraper = DaumCafeScraper(AppConfig())
        self.assertFalse(
            scraper._is_content_image(
                "https://example.com/member/avatar.png",
                640,
                480,
                "thumb_profile",
                "",
            )
        )
        self.assertTrue(
            scraper._is_content_image(
                "https://t1.daumcdn.net/cafeattach/1R9cj/post-image",
                640,
                480,
                "txc-image",
                "",
            )
        )

    def test_extracts_title_author_and_date_from_article_header(self) -> None:
        sections = [
            (
                [
                    "패러관련 중고 삽니다",
                    "포톤 MS 상태 좋은 장비 구합니다",
                    "카우보이 추천 0 조회 137 26.05.25 20:16 댓글 0",
                    "포톤 상태 좋은 MS (85-95) 구합니다",
                    "01053075041",
                ],
                [
                    "포톤 상태 좋은 MS (85-95) 구합니다",
                    "01053075041",
                ],
            )
        ]

        self.assertEqual(_title_from_article_sections(sections), "포톤 MS 상태 좋은 장비 구합니다")
        self.assertEqual(_author_from_article_sections(sections), "카우보이")
        self.assertEqual(_posted_at_from_article_sections(sections), "26.05.25 20:16")

    def test_extracts_header_metadata_for_each_article_independently(self) -> None:
        first_sections = [
            (
                [
                    "패러관련 중고 팝니다",
                    "naviter oudie N 팝니다",
                    "김정구 추천 0 조회 219 26.07.27 14:57 댓글 0",
                    "4회 사용했습니다",
                ],
                ["4회 사용했습니다"],
            )
        ]
        second_sections = [
            (
                [
                    "패러관련 중고 팝니다",
                    "독일 살리 비테스 헬멧 판매",
                    "최홍삼 추천 0 조회 90 26.07.28 10:12 댓글 0",
                    "미사용으로 판매합니다",
                ],
                ["미사용으로 판매합니다"],
            )
        ]

        self.assertEqual(_title_from_article_sections(first_sections), "naviter oudie N 팝니다")
        self.assertEqual(_author_from_article_sections(first_sections), "김정구")
        self.assertEqual(_title_from_article_sections(second_sections), "독일 살리 비테스 헬멧 판매")
        self.assertEqual(_author_from_article_sections(second_sections), "최홍삼")


if __name__ == "__main__":
    unittest.main()
