from __future__ import annotations

import unittest

from game_stack_planner.models import GameRequirement
from game_stack_planner.sources import OpenUpmSource


class _Fetcher:
    def __init__(self):
        self.url = ""

    def get(self, url, *, headers=None):
        self.url = url
        return {"objects": [], "total": 0}, {}


class OpenUpmQueryTests(unittest.TestCase):
    def test_registry_uses_short_requirement_specific_query(self):
        fetcher = _Fetcher()
        requirement = GameRequirement(
            key="lobby_matchmaking",
            title="Lobby",
            priority="high",
            query="unity lobby matchmaking relay multiplayer",
            rationale="",
        )
        OpenUpmSource(fetcher=fetcher).search(requirement)
        self.assertIn("text=lobby", fetcher.url)
        self.assertNotIn("relay", fetcher.url)


if __name__ == "__main__":
    unittest.main()
