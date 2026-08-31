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

    def test_fafnir_brand_assets_are_packaged_and_header_uses_recommended_logo(self):
        for name in (
            "fafnir-mark.svg",
            "fafnir-logo-rune.svg",
            "fafnir-logo-flat.svg",
            "fafnir-logo-graffiti.svg",
            "fafnir-logo-cartoon.svg",
        ):
            with self.subTest(name=name):
                self.assertTrue((self.root / name).is_file())
        brand = (self.root / "brand.html").read_text(encoding="utf-8")
        self.assertIn("Rune Vault", brand)
        self.assertIn("Flat Geometry", brand)
        self.assertIn("Graffiti Slash", brand)
        self.assertIn("Cartoon Hoarder", brand)
        self.assertIn('src="/fafnir-logo-rune.svg"', self.html)
        self.assertIn("Fafnir — Asset Stack Manager", self.html)
        self.assertNotIn("STACKFORGE /", self.html)

    def test_gui_exposes_plans_catalog_and_manual_capture(self):
        self.assertIn("ゲーム要件", self.html)
        self.assertIn("選定候補", self.html)
        self.assertIn("アセット", self.html)
        self.assertIn("商品候補", self.html)
        self.assertIn('id="pin-categories"', self.html)
        self.assertIn("/api/recommend", self.javascript)
        self.assertIn("/api/catalog/manual", self.javascript)
        self.assertIn("search.requirement", self.javascript)
        self.assertNotIn("categories: []", self.javascript)

    def test_gui_copy_uses_compact_workbench_language(self):
        self.assertIn("構成を生成", self.html)
        self.assertIn("要件未入力", self.html)
        self.assertNotIn("どんなゲームを作る？", self.html)
        self.assertNotIn("ブリーフから始めましょう", self.html)
        self.assertNotIn("構成案をつくる", self.html + self.javascript)

    def test_untrusted_api_text_is_not_inserted_as_html(self):
        self.assertNotIn("innerHTML", self.javascript)
        self.assertIn("textContent", self.javascript)


if __name__ == "__main__":
    unittest.main()
