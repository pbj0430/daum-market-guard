import unittest

from daum_market_guard.cli import _normalize_global_options


class CliTests(unittest.TestCase):
    def test_config_can_follow_subcommand(self) -> None:
        self.assertEqual(
            _normalize_global_options(["login", "--config", "config.toml"]),
            ["--config", "config.toml", "login"],
        )

    def test_config_equals_can_follow_subcommand(self) -> None:
        self.assertEqual(
            _normalize_global_options(["scan", "--config=config.toml"]),
            ["--config=config.toml", "scan"],
        )

    def test_config_can_follow_nested_subcommand(self) -> None:
        self.assertEqual(
            _normalize_global_options(
                ["blacklist", "add", "--author-name", "a", "--reason", "r", "--config", "x.toml"]
            ),
            ["--config", "x.toml", "blacklist", "add", "--author-name", "a", "--reason", "r"],
        )


if __name__ == "__main__":
    unittest.main()
