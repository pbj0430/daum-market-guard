import tempfile
import unittest
from pathlib import Path

from daum_market_guard.config import AppConfig, BoardConfig, load_config
from daum_market_guard.service import _collect_post_refs, _direct_read_url


class FakeDb:
    def __init__(self, max_number: int = 0) -> None:
        self.max_number = max_number

    def max_post_number(self, board_id: str) -> int:
        return self.max_number


class FakeScraper:
    def __init__(self) -> None:
        self.collect_board_posts_calls = 0

    def collect_board_posts(self, board: BoardConfig, progress=None):
        self.collect_board_posts_calls += 1
        return []


class ServiceTests(unittest.TestCase):
    def test_direct_read_url_uses_board_id_and_post_number(self) -> None:
        config = AppConfig(cafe_grpid="1R9cj")
        board = BoardConfig("Market TyYz", "https://cafe.daum.net/730418/TyYz")

        self.assertEqual(
            _direct_read_url(config, board, 205, "730418"),
            "https://cafe.daum.net/_c21_/bbs_read?grpid=1R9cj&fldid=TyYz&datanum=205",
        )

    def test_direct_scan_uses_configured_start_when_list_is_unavailable(self) -> None:
        config = AppConfig(
            cafe_grpid="1R9cj",
            direct_scan_start_post_ids={"C3Zi": 2061},
            direct_scan_limit_per_board=3,
        )
        board = BoardConfig("Market C3Zi", "https://cafe.daum.net/730418/C3Zi")

        scraper = FakeScraper()

        refs = _collect_post_refs(config, FakeDb(), scraper, board, None)

        self.assertEqual(
            [ref.post_key for ref in refs],
            ["C3Zi:2061", "C3Zi:2060", "C3Zi:2059"],
        )
        self.assertEqual(scraper.collect_board_posts_calls, 0)
        self.assertEqual(
            refs[0].url,
            "https://cafe.daum.net/_c21_/bbs_read?grpid=1R9cj&fldid=C3Zi&datanum=2061",
        )

    def test_direct_scan_probes_above_saved_max_when_list_is_unavailable(self) -> None:
        config = AppConfig(
            cafe_grpid="1R9cj",
            direct_scan_start_post_ids={"C3Zi": 2061},
            direct_scan_probe_ahead=3,
            direct_scan_limit_per_board=5,
        )
        board = BoardConfig("Market C3Zi", "https://cafe.daum.net/730418/C3Zi")

        refs = _collect_post_refs(config, FakeDb(max_number=2061), FakeScraper(), board, None)

        self.assertEqual(
            [ref.post_key for ref in refs],
            ["C3Zi:2062", "C3Zi:2063", "C3Zi:2064", "C3Zi:2061", "C3Zi:2060"],
        )
        self.assertEqual(
            [ref.cache_missing for ref in refs],
            [False, False, False, True, True],
        )

    def test_config_loads_direct_scan_start_post_ids(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / "config.toml"
            config_path.write_text(
                """
direct_scan_probe_ahead = 7

[direct_scan_start_post_ids]
C3Zi = 2061
TyYz = 205
""".strip(),
                encoding="utf-8",
            )

            config = load_config(config_path)

        self.assertEqual(config.direct_scan_start_post_ids, {"C3Zi": 2061, "TyYz": 205})
        self.assertEqual(config.direct_scan_probe_ahead, 7)


if __name__ == "__main__":
    unittest.main()
