from __future__ import annotations

import unittest

from game_stack_planner.requirements import derive_requirements


class AmbiguityTests(unittest.TestCase):
    def test_meta_quest_does_not_imply_narrative_quest_system(self):
        keys = {
            item.key
            for item in derive_requirements("Quest VR puzzle", platform="quest")
        }
        self.assertIn("xr", keys)
        self.assertNotIn("dialogue_quest", keys)

    def test_explicit_quest_system_still_maps_to_narrative(self):
        keys = {
            item.key
            for item in derive_requirements("RPG quest system and dialogue")
        }
        self.assertIn("dialogue_quest", keys)


if __name__ == "__main__":
    unittest.main()
