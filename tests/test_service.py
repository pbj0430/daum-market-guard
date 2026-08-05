import unittest

from daum_market_guard.config import AppConfig, BoardConfig
from daum_market_guard.service import _direct_read_url


class ServiceTests(unittest.TestCase):
    def test_direct_read_url_uses_board_id_and_post_number(self) -> None:
        config = AppConfig(cafe_grpid="1R9cj")
        board = BoardConfig("Market TyYz", "https://cafe.daum.net/730418/TyYz")

        self.assertEqual(
            _direct_read_url(config, board, 205, "730418"),
            "https://cafe.daum.net/_c21_/bbs_read?grpid=1R9cj&fldid=TyYz&datanum=205",
        )


if __name__ == "__main__":
    unittest.main()
