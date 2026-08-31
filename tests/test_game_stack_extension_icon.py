from __future__ import annotations

import json
import unittest
from pathlib import Path

from PIL import Image


class StackforgeExtensionIconTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = (
            Path(__file__).parents[1]
            / "browser_extension"
            / "unity_asset_store"
        )
        self.manifest = json.loads(
            (self.root / "manifest.json").read_text(encoding="utf-8")
        )

    def test_manifest_registers_every_chrome_icon_size(self) -> None:
        expected = {
            "16": "icons/icon16.png",
            "32": "icons/icon32.png",
            "48": "icons/icon48.png",
            "128": "icons/icon128.png",
        }
        self.assertEqual(expected, self.manifest["icons"])
        self.assertEqual(expected, self.manifest["action"]["default_icon"])
        self.assertEqual("0.2.0", self.manifest["version"])
        self.assertEqual("Fafnir Asset Store Capture & RAG", self.manifest["name"])
        self.assertNotIn("host_permissions", self.manifest)
        self.assertEqual(
            {"activeTab", "nativeMessaging"},
            set(self.manifest["permissions"]),
        )

    def test_png_files_have_exact_dimensions_and_brand_palette(self) -> None:
        for size in (16, 32, 48, 128):
            with self.subTest(size=size):
                path = self.root / "icons" / f"icon{size}.png"
                with Image.open(path) as image:
                    self.assertEqual("PNG", image.format)
                    self.assertEqual((size, size), image.size)
                    self.assertIn(image.mode, {"RGB", "RGBA"})
        with Image.open(self.root / "icons" / "icon128.png") as image:
            colors = set(image.convert("RGB").get_flattened_data())
        self.assertIn((22, 25, 31), colors)
        self.assertIn((36, 91, 215), colors)
        self.assertIn((239, 104, 73), colors)

    def test_popup_uses_the_generated_icon(self) -> None:
        popup = (self.root / "popup.html").read_text(encoding="utf-8")
        self.assertIn('class="mark" src="icons/icon48.png"', popup)
        self.assertNotIn('<span class="mark">SF</span>', popup)


if __name__ == "__main__":
    unittest.main()
