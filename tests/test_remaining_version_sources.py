import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from service_update_report import (
    UpdateReportGenerator,
    caret_upper_bound,
    latest_matching_caret_tag,
    parse_caret_constraint,
    parse_source_version,
)
from test_build_boilerplate_report import FakeGenerator


class RemainingVersionSourcesTest(unittest.TestCase):
    def test_all_audited_missing_sources_detect_new_release_families(self):
        cases = {
            "adminer": ("6", "v7.0.0"),
            "chatwoot": ("4.17", "v4.18.0"),
            "distribution": ("3.1", "v3.2.0"),
            "frps": ("0.69", "v0.70.0"),
            "ghost": ("6", "v7.0.0"),
            "mongoku": ("2.11", "v2.12.0"),
            "supabase": ("0.8.1", "0.9.0"),
            "supabase-postgres": ("17", "18.1.0.001"),
            "victoria-metrics": ("1.150", "v1.151.0"),
            "vllm": ("0.29", "v0.30.0"),
        }
        for name, (current, latest) in cases.items():
            with self.subTest(name=name):
                generator = UpdateReportGenerator("wodby")
                source = generator.resolve_fallback_version_source("service-" + name, name)
                self.assertIsNotNone(source)
                generator.get_fallback_version_source_values = mock.Mock(return_value={latest})
                result = generator.check_fallback_version_source(source, [{"version": current}], "")
                self.assertEqual(result["warnings"], [])
                self.assertTrue(result["notification_groups"].get("major_version"))
                self.assertIn("manual review required", result["notifications"][0])

    def test_supabase_filters_unrelated_tags_and_unstable_self_hosted_releases(self):
        generator = UpdateReportGenerator("wodby")
        generator.get_github_tags = mock.Mock(return_value={"v99.0.0", "self-hosted/v0.8.3", "self-hosted/v0.9.0-rc1"})
        source = generator.resolve_fallback_version_source("service-supabase", "supabase")
        values = generator.get_fallback_version_source_values(source)
        self.assertEqual(values, {"0.8.3", "0.9.0-rc1"})
        result = generator.check_fallback_version_source(source, [{"version": "0.8.1"}], "")
        self.assertEqual(result["notifications"], [])
        self.assertIn("0.8.3", result["current"][0])

    def test_supabase_postgres_four_part_versions_are_valid_stable_sources(self):
        self.assertEqual(str(parse_source_version("17.11.0.007")), "17.11.0.7")
        self.assertIsNone(parse_source_version("17.11.0.007-rc1"))

    def test_failed_source_lookup_is_visible_and_does_not_invent_eol_dates(self):
        generator = UpdateReportGenerator("wodby")
        generator.resolve_eol_product_name = mock.Mock(return_value=None)
        result = generator.check_eol_options(
            "service-vllm", "vllm", "service", [{"version": "0.29"}], [], "", "service.yml"
        )
        self.assertFalse(result["notification_groups"].get("missing_version_source"))
        self.assertTrue(result["notification_groups"].get("missing_eol"))
        self.assertEqual(result["planned_changes"], [])


class PartialCaretConstraintsTest(unittest.TestCase):
    def test_calendar_and_zero_major_caret_bounds(self):
        cases = {"^2026.8": "2027.0.0", "^0.0": "0.1.0", "^0.2": "0.3.0", "^0.0.3": "0.0.4", "^1.2.3": "2.0.0"}
        for constraint, expected in cases.items():
            with self.subTest(constraint=constraint):
                self.assertEqual(str(caret_upper_bound(parse_caret_constraint(constraint))), expected)
        self.assertIsNone(parse_caret_constraint("^2026.8.0-rc1"))

    def test_discourse_month_constraints_resolve_patches_and_ignore_next_year(self):
        tags = {"v2026.8.0", "v2026.10.2", "v2027.1.0", "v2026.11.0-rc1"}
        self.assertEqual(latest_matching_caret_tag(tags, parse_caret_constraint("^2026.8"))[0], "v2026.10.2")

    def test_actual_boilerplate_checker_resolves_both_discourse_channels(self):
        generator = FakeGenerator()
        generator.tags[("discourse", "discourse")] = {"v2026.7.0", "v2026.8.0", "v2026.10.2"}
        generator.repo_files[("service-discourse", "pipeline.yml")] = "version: 0.1\n"
        for name, constraint in [("stable", "^2026.8"), ("esr", "^2026.7")]:
            with self.subTest(name=name):
                result = generator.check_build_boilerplate_entry(
                    "service-discourse",
                    {
                        "name": name,
                        "repo": "https://github.com/discourse/discourse",
                        "tag": constraint,
                        "pipeline": "pipeline.yml",
                    },
                    "service.yml",
                    "",
                )
                self.assertEqual(result["status"], "current")
                self.assertEqual(result["resolved_tag"], "v2026.10.2")
                self.assertEqual(result["warning_messages"], [])


if __name__ == "__main__":
    unittest.main()
