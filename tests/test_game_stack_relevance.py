from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from game_stack_planner.models import Candidate
from game_stack_planner.repository import StackRepository
from game_stack_planner.service import GameStackPlanner


class RelevanceGateTests(unittest.TestCase):
    def test_installed_bonus_never_fills_an_unrelated_requirement(self):
        with tempfile.TemporaryDirectory() as directory:
            repository = StackRepository(Path(directory) / "catalog.db")
            repository.upsert_candidates((Candidate(
                id="local:unrelated",
                source="local",
                external_id="unrelated",
                title="Unrelated Installed Utility",
                url="",
                ownership="installed",
                installed=True,
                categories=("enemy_ai",),
            ),))
            planner = GameStackPlanner(repository)
            result = planner.recommend(
                prompt="アイテム収集",
                remote=False,
                budget="owned_first",
            )
            self.assertEqual(result["recommendations"]["inventory"], [])
            self.assertIn("inventory", result["plans"][0]["missing"])
            repository.close()


if __name__ == "__main__":
    unittest.main()
