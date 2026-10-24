import argparse
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import service_apply_gitlab_batch as batch_apply
from service_apply_updates import apply_updates
from service_update_report import (
    GITLAB_REPOS,
    GITLAB_RUNNER_IMAGE,
    UpdateReportGenerator,
    generate_report,
    load_service_repos,
    render_markdown,
)


class FakeGenerator(UpdateReportGenerator):
    def __init__(self):
        super().__init__("wodby")
        self.manifests = {
            repo: {
                "name": repo.removeprefix("service-"),
                "type": "service",
                "options": [{"version": "19", "tag": "alpine-v19.5.0" if repo == GITLAB_REPOS[1] else "19.5.0"}],
                "helm": {
                    "source": "https://charts.gitlab.io",
                    "chart": "gitlab/" + repo.removeprefix("service-"),
                    "version": "0.94.0" if repo == GITLAB_REPOS[1] else "10.5.0",
                },
            }
            for repo in GITLAB_REPOS
        }
        self.manifests[GITLAB_REPOS[1]]["workloads"] = [{"containers": [{"image": GITLAB_RUNNER_IMAGE}]}]
        self.entries = {
            "gitlab/gitlab": [
                {"version": "10.5.0", "appVersion": "19.5.0"},
                {"version": "10.5.2", "appVersion": "19.5.2"},
                {"version": "10.6.0", "appVersion": "19.6.0"},
            ],
            "gitlab/gitlab-runner": [
                {"version": "0.94.0", "appVersion": "19.5.0"},
                {"version": "0.94.1", "appVersion": "19.5.1"},
                {"version": "0.95.0", "appVersion": "19.6.0"},
            ],
        }
        self.image_tags = ["alpine-v19.5.0", "alpine-v19.5.1", "alpine-v19.6.0"]

    def get_repo_file(self, repo, path):
        data = self.manifests.get(repo)
        return yaml.safe_dump(data, sort_keys=False) if data and path == "service.yml" else None

    def get_helm_entries(self, source, chart):
        return self.entries[chart]

    def get_image_tags(self, image):
        return self.image_tags

    def get_helm_chart_change_notes(self, *args):
        return []

    def get_github_tags(self, *args):
        return {"0.1.0"}

    def get_helm_latest(self, *args):
        raise AssertionError("GitLab must not use independent generic chart selection")


def batch_report(generator):
    with tempfile.TemporaryDirectory() as directory:
        readme = Path(directory) / "README.md"
        readme.write_text("\n".join(f"[{repo}](https://github.com/wodby/{repo})" for repo in GITLAB_REPOS))
        args = argparse.Namespace(readme=str(readme), owner="wodby", repo_filter="gitlab$", build_boilerplate_mode="off")
        with mock.patch("service_update_report.UpdateReportGenerator", return_value=generator):
            return generate_report(args)


class GitLabPlanningTest(unittest.TestCase):
    def setUp(self):
        self.generator = FakeGenerator()

    def test_patch_versions_can_differ_but_both_chart_and_application_tag_advance(self):
        report = batch_report(self.generator)
        batch = report["update_batches"]["gitlab"]
        self.assertEqual(batch["status"], "ready")
        self.assertEqual(batch["application_versions"], {GITLAB_REPOS[0]: "19.5.2", GITLAB_REPOS[1]: "19.5.1"})
        self.assertEqual(len(report["per_repo"]), 2)
        for item in report["per_repo"]:
            self.assertEqual(item["warnings"], [])
            self.assertEqual(len(item["planned_changes"]), 2)
            self.assertEqual(item["planned_release"]["status"], "planned")
        self.assertIn("GitLab application updated to `19.5.2`", report["per_repo"][0]["planned_release"]["description"])
        self.assertIn("Update Batch: gitlab", render_markdown(report))
        self.assertEqual(len(batch["review"]), 2)

    def test_missing_new_runner_image_keeps_runner_on_published_patch(self):
        self.generator.image_tags.remove("alpine-v19.5.1")
        batch = self.generator.check_gitlab_batch()
        self.assertEqual(batch["status"], "ready")
        self.assertEqual(batch["application_versions"][GITLAB_REPOS[1]], "19.5.0")
        self.assertEqual(batch["per_repo"][GITLAB_REPOS[1]], [])

    def test_no_published_runner_image_blocks_both_releases(self):
        self.generator.image_tags = []
        batch = self.generator.check_gitlab_batch()
        self.assertEqual(batch["status"], "blocked")
        self.assertEqual(batch["per_repo"], {})
        self.assertIn("published Alpine Runner image", batch["reason"])

    def test_minor_and_major_upgrades_are_reported_without_crossing_release_line(self):
        self.generator.entries["gitlab/gitlab"].append({"version": "11.0.0", "appVersion": "20.0.0"})
        batch = self.generator.check_gitlab_batch()
        self.assertIn("20.0.0", " ".join(batch["review"]))
        self.assertEqual(batch["application_versions"][GITLAB_REPOS[0]], "19.5.2")

    def test_prerelease_application_and_charts_are_ignored(self):
        self.generator.entries["gitlab/gitlab"] += [
            {"version": "10.5.3-rc1", "appVersion": "19.5.3"},
            {"version": "10.5.3", "appVersion": "19.5.3-rc1"},
        ]
        self.assertEqual(self.generator.check_gitlab_batch()["application_versions"][GITLAB_REPOS[0]], "19.5.2")

    def test_misaligned_current_release_lines_block_both(self):
        self.generator.entries["gitlab/gitlab-runner"][0]["appVersion"] = "19.4.0"
        self.assertIn("different major/minor", self.generator.check_gitlab_batch()["reason"])

    def test_never_rolls_back_configured_application_tag(self):
        self.generator.manifests[GITLAB_REPOS[1]]["options"][0]["tag"] = "alpine-v19.5.4"
        self.assertEqual(self.generator.check_gitlab_batch()["status"], "blocked")

    def test_missing_chart_application_metadata_blocks_both(self):
        del self.generator.entries["gitlab/gitlab"][0]["appVersion"]
        self.assertIn("missing or unstable", self.generator.check_gitlab_batch()["reason"])

    def test_unsupported_multiple_options_or_runner_image_block_both(self):
        self.generator.manifests[GITLAB_REPOS[0]]["options"].append({"version": "18", "tag": "18.11.0"})
        self.assertEqual(self.generator.check_gitlab_batch()["status"], "blocked")
        self.generator = FakeGenerator()
        self.generator.manifests[GITLAB_REPOS[1]]["workloads"][0]["containers"][0]["image"] = "example/runner"
        self.assertIn("expected GitLab Runner image", self.generator.check_gitlab_batch()["reason"])

    def test_server_image_overrides_and_changed_runner_variant_block_updates(self):
        self.generator.manifests[GITLAB_REPOS[0]]["workloads"] = [{"containers": [{"image": "example/server"}]}]
        self.assertIn("managed by the official chart", self.generator.check_gitlab_batch()["reason"])
        self.generator = FakeGenerator()
        self.generator.manifests[GITLAB_REPOS[1]]["options"][0]["tag"] = "19.5.0"
        self.assertIn("Alpine Runner image variant", self.generator.check_gitlab_batch()["reason"])

    def test_source_failure_becomes_visible_blocked_batch_with_no_changes(self):
        self.generator.get_image_tags = mock.Mock(side_effect=RuntimeError("registry unavailable"))
        report = batch_report(self.generator)
        self.assertEqual(report["update_batches"]["gitlab"]["status"], "blocked")
        self.assertTrue(all(not item["planned_changes"] and item["warnings"] for item in report["per_repo"]))

    def test_filter_for_runner_also_includes_server(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "README.md"
            path.write_text("\n".join(f"https://github.com/wodby/{repo}" for repo in GITLAB_REPOS))
            self.assertEqual(load_service_repos(path, "wodby", "runner$"), list(GITLAB_REPOS))
            path.write_text("https://github.com/wodby/service-gitlab")
            with self.assertRaisesRegex(RuntimeError, "requires both"):
                load_service_repos(path, "wodby", "")


class GitLabApplyTest(unittest.TestCase):
    def setUp(self):
        self.generator = FakeGenerator()
        self.report = batch_report(self.generator)
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        for repo in GITLAB_REPOS:
            path = self.root / repo
            path.mkdir()
            (path / "service.yml").write_text(self.generator.get_repo_file(repo, "service.yml"))
        self.events = []
        self.patches = []
        for name in ("prepare_repo_for_apply", "validate_planned_release", "validate_planned_image_tags", "apply_manifest_changes", "commit_push_and_tag", "run_git"):
            patch = mock.patch.object(batch_apply, name)
            self.patches.append(patch.start())
            self.addCleanup(patch.stop)
        self.prepare, self.release, self.images, self.apply, self.commit, self.git = self.patches
        self.prepare.return_value = "main"
        self.apply.side_effect = lambda path, changes: self.record("apply", path.name, ["service.yml"])
        self.commit.side_effect = lambda path, repo, branch, release, changed, **kwargs: self.record("prepare", repo, {
            "repo": repo, "status": "prepared", "commit": f"new-{repo}", "tag": release["tag"], "branch": branch,
        })
        self.pushed = set()
        self.git.side_effect = self.run_git

    def record(self, action, repo, value):
        self.events.append((action, repo))
        return value

    def run_git(self, path, *args):
        if args[0] == "push" and "--dry-run" in args:
            self.events.append(("preflight", path.name))
            return mock.Mock(stdout="")
        if args[0] == "push":
            self.events.append(("push", path.name))
            self.pushed.add(path.name)
        if args[0] == "ls-remote":
            value = f"{'new' if path.name in self.pushed else 'old'}-{path.name} refs/heads/main"
        else:
            value = f"old-{path.name}"
        return mock.Mock(stdout=value)

    def test_all_local_changes_and_releases_prepared_before_first_push(self):
        batch_apply.apply_batch(self.report, self.root)
        self.assertEqual([e[0] for e in self.events], ["apply", "apply", "prepare", "prepare", "preflight", "preflight", "push", "push"])
        self.assertTrue(all(i["apply_result"]["status"] == "applied" for i in self.report["per_repo"]))
        self.assertTrue(all(call.kwargs == {"prepare_only": True} for call in self.commit.call_args_list))

    def test_stale_partner_blocks_everything_before_any_files_change(self):
        (self.root / GITLAB_REPOS[1] / "service.yml").write_text("changed")
        with self.assertRaisesRegex(RuntimeError, "changed since"):
            batch_apply.apply_batch(self.report, self.root)
        self.apply.assert_not_called()
        self.commit.assert_not_called()

    def test_invalid_partner_release_blocks_everything(self):
        self.report["per_repo"][1]["planned_release"]["status"] = "blocked"
        with self.assertRaisesRegex(RuntimeError, "no valid release"):
            batch_apply.apply_batch(self.report, self.root)
        self.prepare.assert_not_called()

    def test_image_validation_failure_prevents_file_changes(self):
        self.images.side_effect = RuntimeError("image no longer published")
        with self.assertRaisesRegex(RuntimeError, "no longer published"):
            batch_apply.apply_batch(self.report, self.root)
        self.apply.assert_not_called()

    def test_second_prepare_failure_prevents_both_pushes(self):
        self.commit.side_effect = [dict(status="prepared"), RuntimeError("signing failed")]
        with self.assertRaisesRegex(RuntimeError, "signing failed"):
            batch_apply.apply_batch(self.report, self.root)
        self.git.assert_not_called()

    def test_failed_push_preflight_prevents_publication_of_either_member(self):
        def fail_preflight(path, *args):
            if args[0] == "push" and "--dry-run" in args and path.name == GITLAB_REPOS[1]:
                raise RuntimeError("push denied")
            return self.run_git(path, *args)
        self.git.side_effect = fail_preflight
        with self.assertRaisesRegex(RuntimeError, "push denied"):
            batch_apply.apply_batch(self.report, self.root)
        self.assertFalse(self.pushed)

    def test_disabled_batch_does_not_touch_repositories(self):
        batch_apply.apply_batch(self.report, self.root / "nonexistent", releases_disabled=True)
        self.prepare.assert_not_called()
        self.assertTrue(all(i["apply_result"]["status"] == "disabled" for i in self.report["per_repo"]))

    def test_single_repository_release_is_rejected(self):
        (self.root / "service-update-report.json").write_text(json.dumps(self.report))
        args = argparse.Namespace(report_dir=str(self.root), repo=GITLAB_REPOS[0], repo_dir=str(self.root), owner="wodby", releases_disabled=False)
        with self.assertRaisesRegex(RuntimeError, "must be released together"):
            apply_updates(args)

    def test_unchanged_partner_is_still_checked_for_stale_manifest(self):
        self.report["per_repo"][1]["planned_changes"] = []
        (self.root / GITLAB_REPOS[1] / "service.yml").write_text("changed")
        with self.assertRaisesRegex(RuntimeError, "changed since"):
            batch_apply.apply_batch(self.report, self.root)
        self.apply.assert_not_called()

    def test_remote_race_blocks_both_pushes(self):
        self.git.return_value = mock.Mock(stdout="unexpected refs/heads/main")
        self.git.side_effect = None
        with self.assertRaisesRegex(RuntimeError, "remote branch changed"):
            batch_apply.apply_batch(self.report, self.root)
        self.assertFalse(self.pushed)

    def test_failure_on_second_push_preserves_first_success(self):
        def fail_second(path, *args):
            if args[0] == "push" and "--dry-run" not in args and path.name == GITLAB_REPOS[1]:
                raise RuntimeError("push failed")
            return self.run_git(path, *args)
        self.git.side_effect = fail_second
        with self.assertRaisesRegex(RuntimeError, "push failed"):
            batch_apply.apply_batch(self.report, self.root)
        self.assertEqual(self.report["per_repo"][0]["apply_result"]["status"], "applied")
        self.assertEqual(self.report["per_repo"][1]["apply_result"]["status"], "prepared")


class CatalogDiscoveryTest(unittest.TestCase):
    def test_paginates_and_excludes_archived_private_and_non_service_repositories(self):
        generator = UpdateReportGenerator("example")
        page = [{"name": f"service-{i}", "visibility": "public"} for i in range(100)]
        response = mock.Mock()
        response.json.side_effect = [page, [
            {"name": "service-last", "visibility": "public"},
            {"name": "service-archived", "visibility": "public", "archived": True},
            {"name": "service-private", "visibility": "private"},
            {"name": "service", "visibility": "public"},
        ]]
        generator.fetch = mock.Mock(return_value=response)
        repos = generator.get_public_service_repos()
        self.assertEqual(len(repos), 101)
        self.assertIn("service-last", repos)
        self.assertIn("page=2", generator.fetch.call_args.args[0])

    def test_newly_covered_services_are_all_in_catalog(self):
        repos = load_service_repos(ROOT / "README.md", "wodby", "")
        self.assertTrue(set(GITLAB_REPOS) | {"service-mongodb", "service-nextcloud"} <= set(repos))


if __name__ == "__main__":
    unittest.main()
