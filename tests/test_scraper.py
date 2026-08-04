import unittest
from unittest.mock import patch

from daum_market_guard.config import AppConfig, BoardConfig
from daum_market_guard.scraper import find_system_chromium
from daum_market_guard.scraper import DaumCafeScraper


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

    def test_board_page_urls_include_mobile_fallback(self) -> None:
        scraper = DaumCafeScraper(AppConfig())
        board = BoardConfig("Market C3Zg", "https://cafe.daum.net/730418/C3Zg")
        urls = scraper._board_page_urls(board, 1)
        self.assertIn("https://cafe.daum.net/730418/C3Zg", urls)
        self.assertIn("https://m.cafe.daum.net/730418/C3Zg", urls)


if __name__ == "__main__":
    unittest.main()
