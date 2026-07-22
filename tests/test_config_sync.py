import hashlib
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from service_config_sync import (  # noqa: E402
    CONFIG_CHANGE_TYPE,
    ConfigSyncError,
    DockerConfigExtractor,
    ExtractedConfig,
    apply_snapshot_changes,
    augment_report,
    plan_repository_configs,
    sha256_text,
    validate_repo_path,
    validate_source_path,
)
from service_apply_updates import apply_manifest_changes  # noqa: E402
from service_update_report import render_planned_diffs  # noqa: E402


class FakeGenerator:
    def __init__(self) -> None:
        self.service_data: dict[tuple[str, str], dict] = {}
        self.repo_files: dict[tuple[str, str], str] = {}
        self.tags: set[str] = {"1.0.0"}

    def get_service_data(self, repo: str, manifest_path: str = "service.yml") -> dict | None:
        return self.service_data.get((repo, manifest_path))

    def get_repo_file(self, repo: str, path: str) -> str | None:
        return self.repo_files.get((repo, path))

    def get_github_tags(self, owner: str, repo: str) -> set[str]:
        return self.tags


class FakeExtractor:
    def __init__(self, contents: dict[tuple[str, str], str]) -> None:
        self.contents = contents
        self.calls: list[tuple[str, str, str]] = []

    def extract(self, image: str, source_path: str, platform: str) -> ExtractedConfig:
        self.calls.append((image, source_path, platform))
        try:
            content = self.contents[(image, source_path)]
        except KeyError as exc:
            raise ConfigSyncError(f"no fake content for {image}:{source_path}") from exc
        digest = "sha256:" + hashlib.sha256(image.encode()).hexdigest()
        repository = image.split("@", 1)[0].rsplit(":", 1)[0]
        image_ref = image if "@" in image else f"{repository}@{digest}"
        if "@" in image:
            digest = image.rsplit("@", 1)[1]
        return ExtractedConfig(
            content=content,
            content_sha256=sha256_text(content),
            image=image,
            image_digest=digest,
            image_ref=image_ref,
            source_path=source_path,
            platform=platform,
        )


def inventory(*config_names: str) -> dict:
    return {
        "services": {
            "service-demo": {
                "manifests": {
                    "service.yml": {
                        "configs": [
                            {"name": name, "workload": "main", "container": "demo"}
                            for name in config_names
                        ]
                    }
                }
            }
        }
    }


def service_data(configs: list[dict], options: list[dict] | None = None) -> dict:
    return {
        "name": "demo",
        "options": options or [{"version": "1", "tag": "1-old"}],
        "configs": configs,
        "workloads": [
            {
                "name": "main",
                "containers": [{"name": "demo", "image": "wodby/demo"}],
            }
        ],
    }


def snapshot_config(name: str = "main", version: str | None = "1") -> dict:
    config = {
        "name": name,
        "filepath": f"/etc/{name}.conf",
        "config": f"{name}.conf",
    }
    if version is not None:
        config["version"] = version
    return config


def sample_report(planned_changes: list[dict] | None = None) -> dict:
    changes = planned_changes or []
    return {
        "generated_at": "2026-07-22T00:00:00+00:00",
        "totals": {
            "repos_in_readme": 1,
            "repos_reported": 1,
            "external_excluded": 0,
            "updates": 1 if changes else 0,
            "no_changes": 0,
            "special": 0,
            "notifications": 0,
            "major_version_notifications": 0,
            "helm_major_version_notifications": 0,
            "missing_version_source_notifications": 0,
            "missing_eol_notifications": 0,
            "planned_releases": 1 if changes else 0,
            "dry_run_updates": 0,
            "release_blockers": 0,
            "build_template_review_items": 0,
            "build_template_warnings": 0,
        },
        "per_repo": [
            {
                "repo": "service-demo",
                "planned_changes": changes,
                "planned_diffs": [],
                "planned_release": {
                    "status": "planned",
                    "previous_tag": "1.0.0",
                    "tag": "1.0.1",
                }
                if changes
                else None,
                "dry_run_changes": [],
                "dry_run_diffs": [],
                "apply_result": None,
                "updates": ["image update"] if changes else [],
                "updates_without_local_diff": [],
                "notification_groups": {},
                "notifications": [],
                "warnings": [],
                "current": [],
                "expects_image": True,
                "expects_helm": True,
                "expects_options": True,
                "has_image": True,
                "has_helm": True,
                "has_options": True,
                "comparable": True,
                "build_template_review_items": [],
                "build_template_warnings": [],
            }
        ],
        "no_changes": {},
        "missing_service_yml": [],
        "special": {},
        "category_lists": {"no_image": [], "no_helm": [], "no_options": []},
    }


class ConfigSyncPlanningTest(unittest.TestCase):
    def test_snapshot_diff_uses_unified_context(self) -> None:
        diffs = render_planned_diffs(
            [
                {
                    "change_type": CONFIG_CHANGE_TYPE,
                    "file": "main.conf",
                    "diff_before_lines": ["one", "same", "three"],
                    "diff_after_lines": ["one", "same", "four"],
                }
            ]
        )

        self.assertIn(" one", diffs[0])
        self.assertIn("-three", diffs[0])
        self.assertIn("+four", diffs[0])

    def test_uses_planned_target_image_tag(self) -> None:
        generator = FakeGenerator()
        generator.service_data[("service-demo", "service.yml")] = service_data([snapshot_config()])
        generator.repo_files[("service-demo", "main.conf")] = "old\n"
        extractor = FakeExtractor({("wodby/demo:1-new", "/etc/main.conf"): "new\n"})
        report_item = {
            "planned_changes": [
                {
                    "change_type": "image_tag",
                    "file": "service.yml",
                    "image": "wodby/demo",
                    "image_version": "1",
                    "after": "1-new",
                }
            ]
        }

        plan = plan_repository_configs(
            "service-demo", report_item, inventory("main"), generator, extractor
        )

        self.assertIsNotNone(plan)
        assert plan is not None
        self.assertEqual(len(plan.changes), 1)
        self.assertEqual(plan.changes[0]["after_sha256"], sha256_text("new\n"))
        self.assertEqual(plan.changes[0]["sources"][0]["image"], "wodby/demo:1-new")

    def test_unversioned_config_must_match_every_option(self) -> None:
        generator = FakeGenerator()
        generator.service_data[("service-demo", "service.yml")] = service_data(
            [snapshot_config(version=None)],
            [{"version": "1", "tag": "1-a"}, {"version": "2", "tag": "2-a"}],
        )
        extractor = FakeExtractor(
            {
                ("wodby/demo:1-a", "/etc/main.conf"): "shared\n",
                ("wodby/demo:2-a", "/etc/main.conf"): "shared\n",
            }
        )

        plan = plan_repository_configs(
            "service-demo", {"planned_changes": []}, inventory("main"), generator, extractor
        )

        assert plan is not None
        self.assertEqual(len(plan.changes), 1)
        self.assertEqual(len(plan.changes[0]["sources"]), 2)

    def test_unversioned_config_divergence_is_rejected(self) -> None:
        generator = FakeGenerator()
        generator.service_data[("service-demo", "service.yml")] = service_data(
            [snapshot_config(version=None)],
            [{"version": "1", "tag": "1-a"}, {"version": "2", "tag": "2-a"}],
        )
        extractor = FakeExtractor(
            {
                ("wodby/demo:1-a", "/etc/main.conf"): "one\n",
                ("wodby/demo:2-a", "/etc/main.conf"): "two\n",
            }
        )

        plan = plan_repository_configs(
            "service-demo", {"planned_changes": []}, inventory("main"), generator, extractor
        )

        assert plan is not None
        self.assertEqual(plan.changes, [])
        self.assertIn("split it into version-specific", plan.blockers[0])

    def test_current_snapshot_does_not_create_change(self) -> None:
        generator = FakeGenerator()
        generator.service_data[("service-demo", "service.yml")] = service_data([snapshot_config()])
        generator.repo_files[("service-demo", "main.conf")] = "same\n"
        extractor = FakeExtractor({("wodby/demo:1-old", "/etc/main.conf"): "same\n"})

        plan = plan_repository_configs(
            "service-demo", {"planned_changes": []}, inventory("main"), generator, extractor
        )

        assert plan is not None
        self.assertEqual(plan.changes, [])
        self.assertIn("matches its image source", plan.current[0])

    def test_paths_reject_traversal(self) -> None:
        with self.assertRaises(ConfigSyncError):
            validate_repo_path("../secret")
        with self.assertRaises(ConfigSyncError):
            validate_source_path("etc/config")


class DockerConfigExtractorTest(unittest.TestCase):
    def test_uses_create_and_copy_without_running_image(self) -> None:
        calls: list[list[str]] = []

        def runner(args: list[str]) -> subprocess.CompletedProcess[str]:
            calls.append(args)
            if args[:2] == ["docker", "pull"]:
                return subprocess.CompletedProcess(args, 0, "", "")
            if args[:3] == ["docker", "image", "inspect"]:
                return subprocess.CompletedProcess(args, 0, '["wodby/demo@sha256:abc"]\n', "")
            if args[:2] == ["docker", "create"]:
                return subprocess.CompletedProcess(args, 0, "container-id\n", "")
            if args[:2] == ["docker", "cp"]:
                Path(args[-1]).write_text("value\n")
                return subprocess.CompletedProcess(args, 0, "", "")
            if args[:2] == ["docker", "rm"]:
                return subprocess.CompletedProcess(args, 0, "", "")
            raise AssertionError(args)

        extractor = DockerConfigExtractor(command_runner=runner)
        first = extractor.extract("wodby/demo:1", "/etc/one.conf")
        second = extractor.extract("wodby/demo:1", "/etc/two.conf")

        self.assertEqual(first.content, "value\n")
        self.assertEqual(second.image_digest, "sha256:abc")
        self.assertEqual(sum(1 for call in calls if call[:2] == ["docker", "pull"]), 1)
        self.assertEqual(sum(1 for call in calls if call[:2] == ["docker", "create"]), 2)
        self.assertEqual(sum(1 for call in calls if call[:2] == ["docker", "rm"]), 2)
        self.assertFalse(any(call[:2] == ["docker", "run"] for call in calls))

    def test_container_is_removed_when_copy_fails(self) -> None:
        calls: list[list[str]] = []

        def runner(args: list[str]) -> subprocess.CompletedProcess[str]:
            calls.append(args)
            if args[:2] == ["docker", "pull"]:
                return subprocess.CompletedProcess(args, 0, "", "")
            if args[:3] == ["docker", "image", "inspect"]:
                return subprocess.CompletedProcess(args, 0, '["wodby/demo@sha256:abc"]\n', "")
            if args[:2] == ["docker", "create"]:
                return subprocess.CompletedProcess(args, 0, "container-id\n", "")
            if args[:2] == ["docker", "cp"]:
                raise ConfigSyncError("copy failed")
            if args[:2] == ["docker", "rm"]:
                return subprocess.CompletedProcess(args, 0, "", "")
            raise AssertionError(args)

        with self.assertRaisesRegex(ConfigSyncError, "copy failed"):
            DockerConfigExtractor(command_runner=runner).extract("wodby/demo:1", "/etc/config")
        self.assertTrue(any(call[:2] == ["docker", "rm"] for call in calls))


class ConfigSyncApplyTest(unittest.TestCase):
    def test_manifest_apply_ignores_snapshot_change_type(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            repo_dir = Path(tmp_dir)
            manifest = repo_dir / "service.yml"
            manifest.write_text("name: demo\n")

            changed = apply_manifest_changes(
                repo_dir,
                [{"change_type": CONFIG_CHANGE_TYPE, "file": "main.conf"}],
            )

            self.assertEqual(changed, [])
            self.assertEqual(manifest.read_text(), "name: demo\n")

    def test_applies_snapshot_after_hash_and_digest_verification(self) -> None:
        old = "old\n"
        new = "new\n"
        digest = "sha256:" + hashlib.sha256("source".encode()).hexdigest()
        immutable = f"wodby/demo@{digest}"
        extractor = FakeExtractor({(immutable, "/etc/main.conf"): new})
        change = {
            "change_type": CONFIG_CHANGE_TYPE,
            "file": "configs/main.conf",
            "before_sha256": sha256_text(old),
            "after_sha256": sha256_text(new),
            "sources": [
                {
                    "image_ref": immutable,
                    "image_digest": digest,
                    "source_path": "/etc/main.conf",
                    "platform": "linux/amd64",
                }
            ],
        }
        with tempfile.TemporaryDirectory() as tmp_dir:
            repo_dir = Path(tmp_dir)
            output = repo_dir / "configs/main.conf"
            output.parent.mkdir()
            output.write_text(old)

            changed = apply_snapshot_changes(repo_dir, [change], extractor)

            self.assertEqual(changed, ["configs/main.conf"])
            self.assertEqual(output.read_text(), new)

    def test_rejects_stale_checked_out_snapshot(self) -> None:
        change = {
            "change_type": CONFIG_CHANGE_TYPE,
            "file": "main.conf",
            "before_sha256": sha256_text("expected\n"),
            "after_sha256": sha256_text("new\n"),
            "sources": [{}],
        }
        with tempfile.TemporaryDirectory() as tmp_dir:
            repo_dir = Path(tmp_dir)
            (repo_dir / "main.conf").write_text("changed\n")
            with self.assertRaisesRegex(ConfigSyncError, "changed since report generation"):
                apply_snapshot_changes(repo_dir, [change], FakeExtractor({}))


class ConfigSyncReportTest(unittest.TestCase):
    def setUp(self) -> None:
        self.generator = FakeGenerator()
        self.generator.service_data[("service-demo", "service.yml")] = service_data([snapshot_config()])
        self.generator.repo_files[("service-demo", "main.conf")] = "old\n"
        self.extractor = FakeExtractor({("wodby/demo:1-new", "/etc/main.conf"): "new\n"})
        self.image_change = {
            "change_type": "image_tag",
            "file": "service.yml",
            "path": "options[version=1].tag",
            "key": "tag",
            "before": "1-old",
            "after": "1-new",
            "image": "wodby/demo",
            "image_version": "1",
        }

    def write_inputs(self, root: Path, report: dict) -> tuple[Path, Path]:
        report_dir = root / "report"
        report_dir.mkdir()
        (report_dir / "service-update-report.json").write_text(json.dumps(report))
        (report_dir / "service-update-report.md").write_text("")
        inventory_path = root / "config-sync.yml"
        inventory_path.write_text(yaml_inventory())
        return report_dir, inventory_path

    def test_report_mode_blocks_image_release_when_snapshot_drift_exists(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            report_dir, inventory_path = self.write_inputs(
                Path(tmp_dir), sample_report([self.image_change])
            )

            report = augment_report(
                report_dir,
                "service-demo",
                inventory_path,
                "wodby",
                "report",
                self.generator,
                self.extractor,
            )

            item = report["per_repo"][0]
            self.assertEqual(item["config_sync"]["status"], "drift")
            self.assertEqual(len(item["dry_run_changes"]), 1)
            self.assertEqual(item["planned_release"]["status"], "blocked")

    def test_apply_mode_adds_snapshot_to_patch_release(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            report_dir, inventory_path = self.write_inputs(Path(tmp_dir), sample_report())

            report = augment_report(
                report_dir,
                "service-demo",
                inventory_path,
                "wodby",
                "apply",
                self.generator,
                FakeExtractor({("wodby/demo:1-old", "/etc/main.conf"): "new\n"}),
            )

            item = report["per_repo"][0]
            self.assertEqual(item["planned_changes"][0]["change_type"], CONFIG_CHANGE_TYPE)
            self.assertEqual(item["planned_release"]["status"], "planned")
            self.assertEqual(item["planned_release"]["tag"], "1.0.1")

    def test_divergence_blocks_existing_image_release(self) -> None:
        self.generator.service_data[("service-demo", "service.yml")] = service_data(
            [snapshot_config(version=None)],
            [{"version": "1", "tag": "1-old"}, {"version": "2", "tag": "2-old"}],
        )
        extractor = FakeExtractor(
            {
                ("wodby/demo:1-new", "/etc/main.conf"): "one\n",
                ("wodby/demo:2-old", "/etc/main.conf"): "two\n",
            }
        )
        with tempfile.TemporaryDirectory() as tmp_dir:
            report_dir, inventory_path = self.write_inputs(
                Path(tmp_dir), sample_report([self.image_change])
            )

            report = augment_report(
                report_dir,
                "service-demo",
                inventory_path,
                "wodby",
                "apply",
                self.generator,
                extractor,
            )

            item = report["per_repo"][0]
            self.assertEqual(item["config_sync"]["status"], "blocked")
            self.assertEqual(item["planned_release"]["status"], "blocked")
            self.assertEqual(report["totals"]["release_blockers"], 1)


def yaml_inventory() -> str:
    return """services:
  service-demo:
    manifests:
      service.yml:
        configs:
        - name: main
          workload: main
          container: demo
"""


if __name__ == "__main__":
    unittest.main()
