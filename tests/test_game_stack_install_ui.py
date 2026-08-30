from __future__ import annotations

import unittest
from pathlib import Path

import game_stack_planner


class InstallGuiContractTests(unittest.TestCase):
    def setUp(self) -> None:
        root = Path(game_stack_planner.__file__).with_name("static")
        self.html = (root / "index.html").read_text(encoding="utf-8")
        self.javascript = (root / "app.js").read_text(encoding="utf-8")

    def test_gui_requires_a_reviewed_plan_before_install(self) -> None:
        self.assertIn('id="install-dialog"', self.html)
        self.assertIn('id="install-manifest-diff"', self.html)
        self.assertIn("確認事項・リスク", self.html)
        self.assertIn("ロールバック制限", self.html)
        self.assertIn("承認して導入", self.html)
        self.assertIn('confirm.disabled = status !== "ready" || !activeInstallNonce', self.javascript)
        self.assertIn('/api/install/prepare', self.javascript)
        self.assertIn('/api/install/execute', self.javascript)

    def test_source_actions_do_not_pretend_every_result_is_installable(self) -> None:
        for label in (
            "導入内容を確認",
            "導入可否を調べる",
            "手動導入",
            "導入済み",
        ):
            with self.subTest(label=label):
                self.assertIn(label, self.javascript)
        self.assertNotIn("innerHTML", self.javascript)


if __name__ == "__main__":
    unittest.main()
