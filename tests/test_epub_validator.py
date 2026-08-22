import tempfile
import unittest
import zipfile
from pathlib import Path

from hangul_novel_translator.epub_validator import validate_epub_resources


class EpubValidatorTest(unittest.TestCase):
    def _write(self, root: Path, files: dict[str, bytes]) -> Path:
        path = root / "check.epub"
        with zipfile.ZipFile(path, "w") as archive:
            for name, content in files.items():
                archive.writestr(name, content)
        return path

    def test_valid_links_and_used_selector(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = self._write(Path(tmp), {
                "Text/ch.xhtml": b"<html><head><link rel='stylesheet' href='../Styles/main.css'></head><body><p class='used'>x</p></body></html>",
                "Styles/main.css": b".used { color: red; }",
            })
            result = validate_epub_resources(path)
        self.assertTrue(result.ok)
        self.assertEqual(result.unused_selectors, [])

    def test_reports_missing_resources_and_unused_selector(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = self._write(Path(tmp), {
                "Text/ch.xhtml": b"<html><head><link rel='stylesheet' href='../Styles/main.css'></head><body></body></html>",
                "Styles/main.css": b".unused { color: red; } body { background: url('../Images/missing.png'); }",
            })
            result = validate_epub_resources(path)
        self.assertFalse(result.ok)
        self.assertTrue(any("missing.png" in item for item in result.errors))
        self.assertIn("Styles/main.css: .unused", result.unused_selectors)


if __name__ == "__main__":
    unittest.main()
