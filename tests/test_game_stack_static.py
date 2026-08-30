from __future__ import annotations

import unittest
from pathlib import Path

import game_stack_planner


class StaticGuiTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(game_stack_planner.__file__).with_name("static")
        self.html = (self.root / "index.html").read_text(encoding="utf-8")
        self.javascript = (self.root / "app.js").read_text(encoding="utf-8")

    def test_packaged_gui_assets_exist_and_are_linked(self):
        self.assertTrue((self.root / "styles.css").is_file())
        self.assertTrue((self.root / "app.js").is_file())
        self.assertIn('href="/styles.css"', self.html)
        self.assertIn('src="/app.js"', self.html)
        self.assertNotIn("<script>", self.html)

    def test_gui_exposes_plans_catalog_and_manual_capture(self):
        self.assertIn("機能ブループリント", self.html)
        self.assertIn("実装スタック", self.html)
        self.assertIn("保存カタログ", self.html)
        self.assertIn("Asset Store商品を保存", self.html)
        self.assertIn('id="pin-categories"', self.html)
        self.assertIn("/api/recommend", self.javascript)
        self.assertIn("/api/catalog/manual", self.javascript)
        self.assertIn("search.requirement", self.javascript)
        self.assertNotIn("categories: []", self.javascript)

    def test_untrusted_api_text_is_not_inserted_as_html(self):
        self.assertNotIn("innerHTML", self.javascript)
        self.assertIn("textContent", self.javascript)


if __name__ == "__main__":
    unittest.main()
