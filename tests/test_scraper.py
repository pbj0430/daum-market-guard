import unittest
from unittest.mock import patch

from daum_market_guard.scraper import find_system_chromium


class ScraperTests(unittest.TestCase):
    def test_find_system_chromium_returns_first_available_browser(self) -> None:
        def fake_which(command: str) -> str | None:
            return "/usr/bin/chromium" if command == "chromium" else None

        with patch("daum_market_guard.scraper.shutil.which", fake_which):
            self.assertEqual(find_system_chromium(), "/usr/bin/chromium")

    def test_find_system_chromium_returns_none_when_missing(self) -> None:
        with patch("daum_market_guard.scraper.shutil.which", return_value=None):
            self.assertIsNone(find_system_chromium())


if __name__ == "__main__":
    unittest.main()
