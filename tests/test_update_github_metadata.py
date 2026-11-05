import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from update_github_metadata import catalog_page_url  # noqa: E402


class CatalogPageUrlTest(unittest.TestCase):
    def test_links_the_first_indexed_manifest(self) -> None:
        manifests = [{"name": "drupal11-php"}, {"name": "drupal10-php"}]

        self.assertEqual(
            catalog_page_url("services", manifests),
            "https://wodby.com/services/drupal11-php",
        )

    def test_falls_back_to_the_catalog_index(self) -> None:
        self.assertEqual(catalog_page_url("services", []), "https://wodby.com/services")


if __name__ == "__main__":
    unittest.main()
