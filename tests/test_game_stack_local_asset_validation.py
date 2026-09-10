from __future__ import annotations

import unittest
import tempfile
from pathlib import Path
from subprocess import CompletedProcess
from unittest.mock import patch

from game_stack_planner.local_asset_validation import (
    _compiler_diagnostics,
    _has_successful_batchmode_exit,
    run_project_compile_validation,
    run_staging_compile_validation,
)
from game_stack_planner.models import ProjectSnapshot


class UnityCompileLogTests(unittest.TestCase):
    def test_detects_compiler_error(self) -> None:
        log = "Assets/Legacy.cs(1,1): error CS0619: old API\nScripts have compiler errors."
        self.assertEqual(2, len(_compiler_diagnostics(log)))
        self.assertFalse(_has_successful_batchmode_exit(log))

    def test_recognizes_success_after_api_updater_diagnostics(self) -> None:
        log = (
            "Assets/Legacy.cs(1,1): error CS0619: old API\n"
            "*** Tundra build success (2.41 seconds)\n"
            "Exiting batchmode successfully now!\n"
            "Application will terminate with return code 0\n"
        )
        self.assertTrue(_compiler_diagnostics(log))
        self.assertTrue(_has_successful_batchmode_exit(log))

    @patch("game_stack_planner.local_asset_validation.find_unity_editor")
    @patch("game_stack_planner.local_asset_validation._run_unity")
    def test_api_updater_diagnostics_do_not_override_clean_exit(
        self,
        run_unity,
        find_editor,
    ) -> None:
        find_editor.return_value = Path("C:/Unity/Editor/Unity.exe")
        run_unity.side_effect = [
            CompletedProcess([], 0, "Exiting batchmode successfully now!"),
            CompletedProcess(
                [],
                0,
                "Assets/Legacy.cs(1,1): error CS0619: old API\n"
                "*** Tundra build success (2.41 seconds)\n"
                "Exiting batchmode successfully now!",
            ),
        ]
        project = ProjectSnapshot(
            path="C:/missing-project",
            name="Test",
            unity_version="6000.3.15f1",
            render_pipeline="urp",
            input_backend="both",
            packages=(),
        )

        result = run_staging_compile_validation(Path("C:/cache/asset.unitypackage"), project)

        self.assertEqual("passed", result["state"])
        self.assertEqual([], result["diagnostics"])
        self.assertEqual(1, len(result["resolved_diagnostics"]))

    @patch("game_stack_planner.local_asset_validation.find_unity_editor")
    @patch("game_stack_planner.local_asset_validation._run_unity")
    def test_installed_project_uses_real_project_compile(
        self,
        run_unity,
        find_editor,
    ) -> None:
        find_editor.return_value = Path("C:/Unity/Editor/Unity.exe")
        run_unity.return_value = CompletedProcess(
            [], 0, "Exiting batchmode successfully now!"
        )
        with tempfile.TemporaryDirectory() as directory:
            project_root = Path(directory)
            (project_root / "Assets").mkdir()
            project = ProjectSnapshot(
                path=str(project_root),
                name="Installed",
                unity_version="6000.3.15f1",
                render_pipeline="urp",
                input_backend="both",
                packages=(),
            )

            result = run_project_compile_validation(project)

        self.assertEqual("passed", result["state"])
        self.assertEqual("installed_project_compile", result["phase"])
        command = run_unity.call_args.args[0]
        self.assertIn(str(project_root.resolve()), command)


if __name__ == "__main__":
    unittest.main()
