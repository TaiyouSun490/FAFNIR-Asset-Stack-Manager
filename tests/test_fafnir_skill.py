from pathlib import Path
import unittest


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
SKILL_ROOT = REPOSITORY_ROOT / "skills" / "fafnir-unity-assets"


class FafnirSkillTests(unittest.TestCase):
    def test_skill_declares_fafnir_workflow_and_acquisition_boundaries(self) -> None:
        skill = (SKILL_ROOT / "SKILL.md").read_text(encoding="utf-8")

        self.assertIn("name: fafnir-unity-assets", skill)
        for tool_name in (
            "fafnir_status",
            "retrieve_game_stack_evidence",
            "search_owned_asset_rag",
            "prepare_candidate_install",
            "apply_reviewed_install",
            "validate_cached_asset_for_project",
            "prepare_owned_asset_download",
            "start_reviewed_asset_download",
            "get_asset_store_download_status",
            "review_asset_store_candidate_visuals",
        ):
            self.assertIn(f"`{tool_name}`", skill)

        self.assertIn("only after the user approves that exact plan", skill)
        self.assertIn("Do not take control of a browser", skill)
        self.assertIn("must not extract browser data", skill)
        self.assertIn("If the bridge reports offline", skill)
        self.assertIn("downloads to Unity's global cache and does not import", skill)
        self.assertIn("Do not download an item already present", skill)
        self.assertIn("`visual_review` preference", skill)
        self.assertIn("`adopt`, `hold for detail`, or `reject`", skill)

    def test_agent_metadata_points_to_fafnir_mcp(self) -> None:
        metadata = (SKILL_ROOT / "agents" / "openai.yaml").read_text(encoding="utf-8")

        self.assertIn('display_name: "Fafnir Unity Assets"', metadata)
        self.assertIn('value: "fafnir"', metadata)
        self.assertIn("$fafnir-unity-assets", metadata)

if __name__ == "__main__":
    unittest.main()
