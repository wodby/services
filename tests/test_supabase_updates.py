import argparse
import copy
import io
import hashlib
import json
import sys
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from service_apply_updates import apply_manifest_changes, validate_planned_image_tags
from service_update_report import UpdateReportGenerator, generate_report
from supabase_updates import CHART, COMPONENTS, check_updates, chart_bundle


class FakeGenerator(UpdateReportGenerator):
    def __init__(self):
        super().__init__("wodby")
        repositories = [
            "envoyproxy/envoy",
            "supabase/gotrue",
            "postgrest/postgrest",
            "supabase/realtime",
            "supabase/storage-api",
            "darthsim/imgproxy",
            "supabase/postgres-meta",
            "supabase/studio",
        ]
        self.releases = {}
        self.files = {}
        self.tags = {}
        self.chart_tags = ["0.1.1", "0.1.2"]
        self.upstream_tags = {"0.8.2"}
        for version, app, suffix in [("0.1.1", "0.8.1", "1"), ("0.1.2", "0.8.2", "2")]:
            values = {"components": {}, "bootstrap": {}}
            services = {"db": {"image": "supabase/postgres:17.6.1.136"}}
            for ((workload, container), (path, upstream_name)), repository in zip(COMPONENTS.items(), repositories):
                tag = f"v1.2.{suffix}" if container != "studio" else f"2026.10.0{suffix}-sha-abcdef{suffix}"
                mapping = values["components"]
                for part in path.split("."):
                    mapping = mapping.setdefault(part, {})
                mapping["image"] = {"repository": repository, "tag": tag}
                services[upstream_name] = {"image": f"{repository}:{tag}"}
                self.tags.setdefault(repository, []).append(tag)
                if container == "studio":
                    values["bootstrap"]["image"] = f"{repository}:{tag}"
            self.releases[version] = (
                {"name": "supabase", "version": version, "appVersion": "self-hosted/v" + app},
                values,
            )
            self.files[("docker/docker-compose.yml", "self-hosted/v" + app)] = yaml.safe_dump({"services": services})
            self.files[("docker/upgrades.json", "self-hosted/v" + app)] = "{}"
        self.manifest = {
            "name": "supabase",
            "type": "service",
            "options": [{"version": "0.8.1", "default": True}],
            "helm": {"source": CHART, "chart": CHART, "version": "0.1.1", "values": []},
            "links": [{"name": "db", "selectors": [{"labels": ["supabase-postgres"], "option": "17"}]}],
            "workloads": [],
        }
        for workload in dict.fromkeys(key[0] for key in COMPONENTS):
            containers = []
            for key, (path, upstream) in COMPONENTS.items():
                if key[0] != workload:
                    continue
                image = yaml.safe_load(self.files[("docker/docker-compose.yml", "self-hosted/v0.8.1")])["services"][
                    upstream
                ]["image"]
                containers.append(
                    {
                        "name": key[1],
                        "image": image,
                        "helm": {
                            "image": {
                                "repository": f"components.{path}.image.repository",
                                "tag": f"components.{path}.image.tag",
                            }
                        },
                    }
                )
            self.manifest["workloads"].append({"name": workload, "containers": containers})

    def get_oci_chart_metadata(self, reference, version):
        return copy.deepcopy(self.releases[version][0])

    def get_oci_chart_values(self, reference, version):
        return copy.deepcopy(self.releases[version][1])

    def get_oci_tags(self, reference):
        return self.chart_tags

    def get_github_file(self, owner, repo, path, ref=None):
        return self.files.get((path, ref))

    def get_image_tags(self, image):
        return self.tags.get(image, [])

    def get_helm_chart_change_notes(self, *args):
        return []

    def get_github_tags(self, owner, repo):
        return {"1.0.0"}

    def get_repo_file(self, repo, path):
        return yaml.safe_dump(self.manifest, sort_keys=False) if path == "service.yml" else None

    def get_fallback_version_source_values(self, source):
        return self.upstream_tags

    def check_eol_options(self, *args):
        return {
            key: [] for key in ("updates", "current", "major_updates", "notifications", "warnings", "planned_changes")
        }

    def get_helm_latest(self, *args):
        raise AssertionError("Supabase must not use independent generic chart selection")


def plan(generator):
    from service_update_report import make_planned_change

    return check_updates(generator, generator.manifest, "service.yml", "", make_planned_change)


class SupabaseBundleTest(unittest.TestCase):
    def setUp(self):
        self.generator = FakeGenerator()

    def apply(self, changes, expect_error=None):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "service.yml"
            original = yaml.safe_dump(self.generator.manifest, sort_keys=False)
            path.write_text(original)
            if expect_error:
                with self.assertRaisesRegex(RuntimeError, expect_error):
                    apply_manifest_changes(Path(directory), changes, self.generator)
                self.assertEqual(path.read_text(), original)
            else:
                apply_manifest_changes(Path(directory), changes, self.generator)
                return yaml.safe_load(path.read_text())

    def test_updates_all_eight_components_chart_and_application_as_one_plan(self):
        result = plan(self.generator)
        self.assertEqual(result["warnings"], [])
        self.assertEqual(len([c for c in result["planned_changes"] if c["change_type"] == "container_image"]), 8)
        updated = self.apply(result["planned_changes"])
        self.assertEqual(updated["helm"]["version"], "0.1.2")
        self.assertEqual(updated["options"][0]["version"], "0.8.2")
        self.generator.manifest = updated
        self.assertEqual(plan(self.generator)["planned_changes"], [])

    def test_chart_only_or_incomplete_image_set_is_refused_without_writing(self):
        changes = plan(self.generator)["planned_changes"]
        self.apply([c for c in changes if c["path"] == "helm.version"], "option version differs")
        self.apply(changes[1:], "pinned image")

    def test_missing_component_image_blocks_the_whole_plan(self):
        self.generator.tags["supabase/gotrue"].remove("v1.2.2")
        result = plan(self.generator)
        self.assertEqual(result["planned_changes"], [])
        self.assertIn("not published", result["warnings"][0])

    def test_missing_image_at_apply_time_blocks_every_write(self):
        changes = plan(self.generator)["planned_changes"]
        self.generator.tags["supabase/gotrue"].remove("v1.2.2")
        self.apply(changes, "not published")

    def test_chart_and_upstream_composition_must_agree(self):
        self.generator.releases["0.1.2"][1]["components"]["auth"]["image"]["tag"] = "different"
        self.assertIn("differs from upstream", plan(self.generator)["warnings"][0])

    def test_bootstrap_and_studio_must_agree(self):
        self.generator.releases["0.1.2"][1]["bootstrap"]["image"] = "supabase/studio:old"
        self.assertIn("bootstrap and Studio", plan(self.generator)["warnings"][0])

    def test_explicit_bootstrap_override_advances_with_studio(self):
        old = self.generator.releases["0.1.1"][1]["bootstrap"]["image"]
        self.generator.manifest["helm"]["values"] = [{"name": "bootstrap.image", "value": old}]
        result = plan(self.generator)
        self.assertTrue(any(c["change_type"] == "bootstrap_image" for c in result["planned_changes"]))
        updated = self.apply(result["planned_changes"])
        self.assertEqual(
            updated["helm"]["values"][0]["value"], self.generator.releases["0.1.2"][1]["bootstrap"]["image"]
        )

    def test_custom_pins_duplicate_components_and_changed_image_mappings_require_review(self):
        for mutation in ("pin", "duplicate", "mapping"):
            with self.subTest(mutation=mutation):
                self.generator = FakeGenerator()
                container = self.generator.manifest["workloads"][0]["containers"][0]
                if mutation == "pin":
                    container["image"] = "example/custom:1"
                elif mutation == "duplicate":
                    self.generator.manifest["workloads"][0]["containers"].append(copy.deepcopy(container))
                else:
                    container["helm"]["image"]["tag"] = "wrong.path"
                self.assertEqual(plan(self.generator)["planned_changes"], [])
                self.assertTrue(plan(self.generator)["warnings"])

    def test_nonbreaking_release_with_manual_steps_is_dry_run_only(self):
        self.generator.files[("docker/upgrades.json", "self-hosted/v0.8.2")] = json.dumps(
            {
                "0.8.2": {
                    "breaking": False,
                    "requires": ["Storage migration cannot be rolled back; back up the database."],
                }
            }
        )
        result = plan(self.generator)
        self.assertEqual(result["planned_changes"], [])
        self.assertTrue(result["dry_run_changes"])
        self.assertIn("cannot be rolled back", " ".join(result["notifications"]))
        self.apply(result["dry_run_changes"], "requires manual review")

    def test_older_manual_steps_do_not_block_later_compatible_patches(self):
        self.generator.files[("docker/upgrades.json", "self-hosted/v0.8.2")] = json.dumps(
            {"0.8.0": {"breaking": True, "requires": ["old migration"]}}
        )
        self.assertTrue(plan(self.generator)["planned_changes"])

    def test_new_upstream_release_waits_for_published_chart_and_preserves_actions(self):
        self.generator.chart_tags = ["0.1.1"]
        self.generator.files[("docker/upgrades.json", "self-hosted/v0.8.2")] = json.dumps(
            {"0.8.2": {"requires": ["Back up the database."]}}
        )
        result = plan(self.generator)
        self.assertEqual(result["planned_changes"], [])
        self.assertEqual(result["warnings"], [])
        self.assertIn("awaits a compatible published chart", " ".join(result["notifications"]))
        self.assertIn("Back up the database", " ".join(result["notifications"]))

    def test_missing_upgrade_manifest_blocks_all_changes(self):
        del self.generator.files[("docker/upgrades.json", "self-hosted/v0.8.2")]
        result = plan(self.generator)
        self.assertEqual(result["planned_changes"], [])
        self.assertIn("could not be loaded", result["warnings"][0])

    def test_live_report_path_skips_single_option_image_comparisons(self):
        with tempfile.TemporaryDirectory() as directory:
            readme = Path(directory) / "README.md"
            readme.write_text("https://github.com/wodby/service-supabase")
            args = argparse.Namespace(readme=str(readme), owner="wodby", repo_filter="", build_boilerplate_mode="off")
            with mock.patch("service_update_report.UpdateReportGenerator", return_value=self.generator):
                report = generate_report(args)
        item = report["per_repo"][0]
        self.assertEqual(item["warnings"], [])
        self.assertEqual(item["planned_release"]["status"], "planned")
        self.assertEqual(len(item["planned_changes"]), 10)

    def test_chart_bundle_changes_between_planning_and_apply_are_rejected(self):
        changes = plan(self.generator)["planned_changes"]
        values = self.generator.releases["0.1.2"][1]
        values["components"]["auth"]["image"]["tag"] = "v1.2.3"
        compose = yaml.safe_load(self.generator.files[("docker/docker-compose.yml", "self-hosted/v0.8.2")])
        compose["services"]["auth"]["image"] = "supabase/gotrue:v1.2.3"
        self.generator.files[("docker/docker-compose.yml", "self-hosted/v0.8.2")] = yaml.safe_dump(compose)
        self.apply(changes, "pinned image")

    def test_bundle_that_rolls_back_a_component_is_manual_review_only(self):
        values = self.generator.releases["0.1.2"][1]
        values["components"]["auth"]["image"]["tag"] = "v1.2.0"
        compose = yaml.safe_load(self.generator.files[("docker/docker-compose.yml", "self-hosted/v0.8.2")])
        compose["services"]["auth"]["image"] = "supabase/gotrue:v1.2.0"
        self.generator.files[("docker/docker-compose.yml", "self-hosted/v0.8.2")] = yaml.safe_dump(compose)
        self.generator.tags["supabase/gotrue"].append("v1.2.0")
        result = plan(self.generator)
        self.assertEqual(result["planned_changes"], [])
        self.assertIn("rollback", " ".join(result["notifications"]))
        self.apply(result["dry_run_changes"], "requires manual review")

    def test_changed_database_major_is_not_automatically_applied(self):
        compose = yaml.safe_load(self.generator.files[("docker/docker-compose.yml", "self-hosted/v0.8.2")])
        compose["services"]["db"]["image"] = "supabase/postgres:18.1.0.001"
        self.generator.files[("docker/docker-compose.yml", "self-hosted/v0.8.2")] = yaml.safe_dump(compose)
        result = plan(self.generator)
        self.assertEqual(result["planned_changes"], [])
        self.assertTrue(result["dry_run_changes"])
        self.assertIn("manual database upgrade", " ".join(result["notifications"]))
        self.apply(result["dry_run_changes"], "manual database upgrade")

    def test_changed_upstream_database_image_within_the_same_major_requires_review(self):
        compose = yaml.safe_load(self.generator.files[("docker/docker-compose.yml", "self-hosted/v0.8.2")])
        compose["services"]["db"]["image"] = "supabase/postgres:17.7.1.001"
        self.generator.files[("docker/docker-compose.yml", "self-hosted/v0.8.2")] = yaml.safe_dump(compose)
        result = plan(self.generator)
        self.assertEqual(result["planned_changes"], [])
        self.assertTrue(result["dry_run_changes"])
        self.assertIn("verify the linked Supabase PostgreSQL service", " ".join(result["notifications"]))
        self.apply(result["dry_run_changes"], "requires manual review")

    def test_new_chart_component_requires_a_matching_service_manifest_review(self):
        self.generator.releases["0.1.2"][1]["components"]["additional-service"] = {}
        result = plan(self.generator)
        self.assertEqual(result["planned_changes"], [])
        self.assertIn("component identities changed", result["warnings"][0])

    def test_container_reference_and_validation_metadata_cannot_disagree(self):
        changes = plan(self.generator)["planned_changes"]
        changes[0]["after"] = "example/unvalidated:1"
        with self.assertRaisesRegex(RuntimeError, "differs from its validated"):
            validate_planned_image_tags("wodby", changes, self.generator)

    def test_container_pins_are_revalidated_by_generic_image_validation(self):
        changes = plan(self.generator)["planned_changes"]
        validate_planned_image_tags("wodby", changes, self.generator)
        self.generator.tags["supabase/gotrue"] = []
        with self.assertRaisesRegex(RuntimeError, "not found"):
            validate_planned_image_tags("wodby", changes, self.generator)


class PublishedChartValuesTest(unittest.TestCase):
    def setUp(self):
        self.generator = UpdateReportGenerator("wodby")
        self.metadata = {"name": "supabase", "version": "0.1.1", "appVersion": "self-hosted/v0.8.1"}
        self.generator.get_oci_chart_metadata = mock.Mock(return_value=self.metadata)
        self.generator.fetch = mock.Mock()

    def archive(self, *, mismatch=False, duplicate=False):
        output = io.BytesIO()
        with tarfile.open(fileobj=output, mode="w:gz") as tar:
            for name, data in [
                ("Chart.yaml", {**self.metadata, "version": "0.1.2"} if mismatch else self.metadata),
                ("values.yaml", {"bootstrap": {"image": "example:1"}}),
            ]:
                content = yaml.safe_dump(data).encode()
                for _ in range(2 if duplicate and name == "values.yaml" else 1):
                    info = tarfile.TarInfo("supabase/" + name)
                    info.size = len(content)
                    tar.addfile(info, io.BytesIO(content))
        raw = output.getvalue()
        digest = "sha256:" + hashlib.sha256(raw).hexdigest()
        self.generator._oci_manifest_cache[(CHART, "0.1.1")] = {
            "layers": [{"digest": digest, "mediaType": "application/vnd.cncf.helm.chart.content.v1.tar+gzip"}]
        }
        self.generator.fetch.return_value = mock.Mock(content=raw)

    def test_reads_values_from_exact_published_layer_and_caches_them(self):
        self.archive()
        self.assertEqual(self.generator.get_oci_chart_values(CHART, "0.1.1")["bootstrap"]["image"], "example:1")
        self.generator.get_oci_chart_values(CHART, "0.1.1")
        self.generator.fetch.assert_called_once()

    def test_rejects_wrong_digest_archive_metadata_and_duplicate_values(self):
        self.archive()
        self.generator.fetch.return_value.content = b"different"
        with self.assertRaisesRegex(RuntimeError, "digest verification"):
            self.generator.get_oci_chart_values(CHART, "0.1.1")
        self.archive(mismatch=True)
        with self.assertRaisesRegex(RuntimeError, "disagree on version"):
            self.generator.get_oci_chart_values(CHART, "0.1.1")
        self.archive(duplicate=True)
        with self.assertRaisesRegex(RuntimeError, "exactly one regular"):
            self.generator.get_oci_chart_values(CHART, "0.1.1")


if __name__ == "__main__":
    unittest.main()
