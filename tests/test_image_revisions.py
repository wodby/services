import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from service_update_report import UpdateReportGenerator


class ImageRevisionTest(unittest.TestCase):
    def setUp(self):
        self.generator = UpdateReportGenerator("wodby")

    def pick(self, tags, git_tags, wanted="11.4", configured="11.4-3.36.4"):
        return self.generator.latest_wodby_tag(wanted, tags, set(git_tags), configured)

    def test_revision_migration_and_numeric_order(self):
        tags = ["11.4-99.0.0", "11.4-r0", "11.4-r9", "11.4-r10"]
        self.assertEqual(self.pick(tags, tags + ["99.0.0"]), "11.4-r10")

    def test_revision_requires_matching_git_alias(self):
        self.assertEqual(self.pick(["11.4-r0", "11.4-3.36.4"], ["r0", "3.36.4"]), "11.4-3.36.4")

    def test_revision_never_falls_back_to_legacy(self):
        self.assertIsNone(self.pick(["11.4-99.0.0"], ["99.0.0"], configured="11.4-r1"))

    def test_partial_registry_listing_does_not_roll_back_revision(self):
        self.assertIsNone(self.pick(["11.4-r9"], ["11.4-r9"], configured="11.4-r10"))

    def test_exact_selector_precedes_full_version(self):
        tags = ["11.4-r5", "11.4.13-r0"]
        self.assertEqual(self.pick(tags, tags), "11.4-r5")

    def test_full_version_revision_precedes_legacy_exact_selector(self):
        tags = ["11.4-3.36.4", "11.4.12-r9", "11.4.13-r0"]
        self.assertEqual(self.pick(tags, tags + ["3.36.4"]), "11.4.13-r0")

    def test_preserves_postgis_variant(self):
        tags = ["18-r9", "18-postgis-r0", "18.6-postgis-r0"]
        self.assertEqual(self.pick(tags, tags, "18", "18-postgis-1.43.1"), "18-postgis-r0")
        self.assertIsNone(self.pick(["18-r9"], tags, "18", "18-postgis-r0"))

    def test_does_not_select_development_images(self):
        tags = ["8.5-dev-macos-r9", "8.5-dev-r9", "8.5-r0"]
        self.assertEqual(self.pick(tags, tags, "8.5", "8.5-4.71.5"), "8.5-r0")
        self.assertEqual(self.pick(tags, tags, "8.5", "8.5-dev-macos-4.71.5"), "8.5-dev-macos-r9")

    def test_legacy_fallback_requires_repository_release(self):
        self.assertEqual(self.pick(["11.4-3.36.4"], ["3.36.4"]), "11.4-3.36.4")
        self.assertIsNone(self.pick(["11.4-3.36.4"], []))

    def test_changelog_maps_full_version_aliases_through_their_annotation(self):
        messages = {
            ("mariadb", "11.4.12-r5"): "wodby/mariadb:11.4.12-r5 from image release r36",
            ("mariadb", "11.4.13-r0"): "wodby/mariadb:11.4.13-r0 from image release r37",
            ("mariadb", "r37"): "MariaDB updates: 11.4.12 -> 11.4.13",
        }
        self.generator.wodby_release_message = lambda repo, tag: messages.get((repo, tag))
        self.generator.get_github_tag_note = lambda _owner, repo, tag: {"message": messages[(repo, tag)]} if (repo, tag) in messages else None
        changelog = self.generator.get_image_changelog("wodby/mariadb", "11.4.12-r5", "11.4.13-r0")
        self.assertEqual([(e["revision"], e["title"]) for e in changelog["entries"]], [("r37", "MariaDB updates: 11.4.12 -> 11.4.13")])
        self.assertIsNone(changelog["fallback"])

    def test_changelog_without_previous_tag_is_empty(self):
        changelog = self.generator.get_image_changelog("wodby/mariadb", None, "11.4.13-r0")
        self.assertEqual(changelog, {"entries": [], "gaps": [], "fallback": None})

    def test_chart_fallback_extracts_upstream_version(self):
        self.generator.get_wodby_chart_image_tag = lambda _: "0.69-r12"
        result = self.generator.configured_fallback_source_versions(
            {"current_field": "wodby_chart_image_tag"}, [], None, "frpc", None
        )
        self.assertEqual(str(result[0][0]), "0.69")
        self.assertEqual(result[0][1], "0.69-r12")


if __name__ == "__main__":
    unittest.main()
