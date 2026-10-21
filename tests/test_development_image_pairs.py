import argparse
import copy
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from service_apply_updates import apply_manifest_changes
from service_update_report import UpdateReportGenerator, development_image_tag, generate_report


class DevelopmentImagePairsTest(unittest.TestCase):
    def report(self, options, tags, image="wodby/php", path="service.yml", parent=None, git_tags=None):
        manifest = {
            "name": "php",
            "type": "service",
            "options": copy.deepcopy(options),
            "workloads": [{"containers": [{"image": image}]}],
        }
        if parent:
            manifest["from"] = parent
        generator = UpdateReportGenerator("wodby")
        generator.get_repo_manifest_paths = lambda _: [path]
        generator.get_service_data = lambda *_: manifest
        generator.get_image_tags = lambda _: tags
        generator.get_github_tags = lambda _, repo: {"3.2.1"} if repo == "service-php" else set(tags if git_tags is None else git_tags)
        generator.get_image_change_notes = lambda *_: []
        empty = {key: [] for key in (
            "updates", "current", "warnings", "notifications", "major_updates", "planned_changes",
        )}
        generator.check_eol_options = lambda *_: copy.deepcopy(empty)
        generator.check_parent_service_version = lambda *_: {**copy.deepcopy(empty), "comparable": True}
        generator.resolve_fallback_version_source = lambda *_: None
        with tempfile.TemporaryDirectory() as directory:
            readme = Path(directory) / "README.md"
            readme.write_text("[PHP](https://github.com/wodby/service-php)")
            args = argparse.Namespace(readme=str(readme), owner="wodby", repo_filter="", build_boilerplate_mode="off")
            with mock.patch("service_update_report.UpdateReportGenerator", return_value=generator):
                report = generate_report(args)
        return manifest, report["per_repo"][0]

    def option(self, revision=8, dev_revision=None):
        return {"version": "8.5", "tag": f"8.5-r{revision}", "devTag": f"8.5-dev-r{revision if dev_revision is None else dev_revision}"}

    def test_report_and_apply_advance_both_tags(self):
        manifest, report = self.report([self.option()], ["8.5-r8", "8.5-dev-r8", "8.5-r9", "8.5-dev-r9"])
        changes = report["planned_changes"]
        self.assertEqual([(change["key"], change["after"]) for change in changes], [("tag", "8.5-r9"), ("devTag", "8.5-dev-r9")])
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "service.yml"
            path.write_text(yaml.safe_dump(manifest))
            apply_manifest_changes(Path(directory), changes)
            self.assertEqual(yaml.safe_load(path.read_text())["options"], [self.option(9)])

    def test_repairs_dev_tag_when_production_is_already_current(self):
        _, report = self.report([self.option(9, 8)], ["8.5-r9", "8.5-dev-r9"])
        self.assertEqual([(c["key"], c["after"]) for c in report["planned_changes"]], [("devTag", "8.5-dev-r9")])
        self.assertEqual(report["planned_release"]["status"], "planned")

    def test_defers_new_revision_until_both_images_are_published(self):
        _, report = self.report([self.option()], ["8.5-r8", "8.5-dev-r8", "8.5-r9"])
        self.assertEqual(report["planned_changes"], [])

    def test_partial_listing_does_not_roll_back_to_an_older_pair(self):
        _, report = self.report([self.option(9)], ["8.5-r8", "8.5-dev-r8", "8.5-r9"])
        self.assertEqual(report["planned_changes"], [])
        self.assertTrue(any("leaving both tags unchanged" in warning for warning in report["warnings"]))

    def test_development_image_alone_is_not_a_pair(self):
        _, report = self.report([self.option()], ["8.5-dev-r9"])
        self.assertEqual(report["planned_changes"], [])

    def test_paired_revision_still_requires_production_git_alias(self):
        _, report = self.report([self.option()], ["8.5-r9", "8.5-dev-r9"], git_tags=[])
        self.assertEqual(report["planned_changes"], [])

    def test_multiple_options_and_indexed_child_manifest(self):
        options = [self.option(), {"version": "8.4", "tag": "8.4-r8", "devTag": "8.4-dev-r8"}]
        tags = ["8.5-r9", "8.5-dev-r9", "8.4-r9", "8.4-dev-r9"]
        _, report = self.report(options, tags, path="11/service.yml", parent="php")
        self.assertEqual(len(report["planned_changes"]), 4)
        self.assertTrue(all(change["file"] == "11/service.yml" for change in report["planned_changes"]))

    def test_services_without_dev_tags_keep_production_only_updates(self):
        _, report = self.report([{"version": "8.5", "tag": "8.5-r8"}], ["8.5-r9"])
        self.assertEqual([change["key"] for change in report["planned_changes"]], ["tag"])

    def test_external_images_keep_their_declared_development_tag(self):
        _, report = self.report([{"version": "8.5", "tag": "8.5.0", "devTag": "custom-dev"}], ["8.5.1"], image="example/php")
        self.assertEqual([change["key"] for change in report["planned_changes"]], ["tag"])

    def test_legacy_pairs_migrate_to_revision_pairs(self):
        _, report = self.report([{"version": "8.5", "tag": "8.5-4.71.5", "devTag": "8.5-dev-4.71.5"}], ["8.5-r9", "8.5-dev-r9"])
        self.assertEqual([change["after"] for change in report["planned_changes"]], ["8.5-r9", "8.5-dev-r9"])

    def test_preserves_full_version_and_variant_revision_counters(self):
        generator = UpdateReportGenerator("wodby")
        tags = ["18-postgis-r9", "18.6-postgis-r0", "18.6-postgis-dev-r0"]
        self.assertEqual(
            generator.latest_wodby_tag("18", tags, set(tags), "18-postgis-r8", require_development=True),
            "18.6-postgis-r0",
        )
        self.assertEqual(development_image_tag("18.6-postgis-r0"), "18.6-postgis-dev-r0")

    def test_published_pair_is_already_current(self):
        _, report = self.report([self.option(9)], ["8.5-r9", "8.5-dev-r9"])
        self.assertEqual(report["planned_changes"], [])
        self.assertEqual(report["warnings"], ["no local `helm` section was found for this non-external service"])

    def test_duplicate_versions_cannot_produce_partial_pair_updates(self):
        _, report = self.report([self.option(), self.option()], ["8.5-r9", "8.5-dev-r9"])
        self.assertEqual(report["planned_changes"], [])

    def test_apply_rejects_a_production_only_update_before_writing(self):
        manifest, report = self.report([self.option()], ["8.5-r9", "8.5-dev-r9"])
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "service.yml"
            original = yaml.safe_dump(manifest)
            path.write_text(original)
            with self.assertRaisesRegex(RuntimeError, "must pair 8.5-r9 with 8.5-dev-r9"):
                apply_manifest_changes(Path(directory), report["planned_changes"][:1])
            self.assertEqual(path.read_text(), original)

    def test_fixed_wodby_sidecars_do_not_impose_tag_conventions_on_external_images(self):
        manifest = {
            "options": [{"version": "8.5", "tag": "8.5-r8", "devTag": "custom-dev"}],
            "workloads": [{"containers": [{"image": "example/php"}, {"image": "wodby/nginx:1.28-r0"}]}],
        }
        change = {"file": "service.yml", "path": "options[version=8.5].tag", "key": "tag", "before": "8.5-r8", "after": "8.5-r9"}
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "service.yml"
            path.write_text(yaml.safe_dump(manifest))
            apply_manifest_changes(Path(directory), [change])
            self.assertEqual(yaml.safe_load(path.read_text())["options"][0]["devTag"], "custom-dev")


if __name__ == "__main__":
    unittest.main()
