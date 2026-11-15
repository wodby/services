import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from service_open_major_prs import branch_name, propose_major_update  # noqa: E402
from service_update_report import UpdateReportGenerator, is_newer_version  # noqa: E402


class FakeGitHub:
    def __init__(self, pulls=None):
        self.pulls = pulls or []
        self.created = []

    def pull_requests(self, owner, repo, branch):
        return [pull for pull in self.pulls if pull["head"] == branch]

    def create_pull_request(self, owner, repo, payload):
        self.created.append(payload)
        return {"html_url": f"https://github.com/{owner}/{repo}/pull/1"}


def git(cwd, *args):
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


class ServiceRepo:
    """A service repository clone with its own bare origin, on branch master."""

    def __init__(self, helm_version="7.4.0"):
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name)
        self.origin = base / "origin.git"
        self.clone = base / "clone"
        git(base, "init", "--bare", "-b", "master", str(self.origin))
        git(base, "clone", str(self.origin), str(self.clone))
        for key, value in {"user.name": "wodbot", "user.email": "bot@example.com", "commit.gpgsign": "false"}.items():
            git(self.clone, "config", key, value)
        (self.clone / "service.yml").write_text(
            "name: kube-state-metrics\n"
            "helm:\n"
            "  chart: prometheus-community/kube-state-metrics\n"
            f"  version: {helm_version}\n"
        )
        git(self.clone, "add", "service.yml")
        git(self.clone, "commit", "-m", "init")
        git(self.clone, "push", "origin", "HEAD:refs/heads/master")
        git(self.clone, "remote", "set-head", "origin", "master")
        git(self.clone, "fetch", "origin")

    def close(self):
        self.tmp.cleanup()


UPDATE = {
    "manifest_path": "service.yml",
    "helm_source": "https://prometheus-community.github.io/helm-charts",
    "helm_chart": "prometheus-community/kube-state-metrics",
    "current": "7.4.0",
    "target": "8.4.0",
    "service_label": "",
    "message": "new major Helm chart version `8.4.0` is available",
}


class BranchNameTest(unittest.TestCase):
    def test_names_the_chart_and_target_major(self):
        self.assertEqual("chore/helm-chart-kube-state-metrics-8", branch_name(UPDATE))
        self.assertEqual(
            "chore/helm-chart-alloy-logging-2",
            branch_name({"helm_chart": "grafana/alloy", "target": "2.0.1", "service_label": "logging"}),
        )


class LatestInMajorTest(unittest.TestCase):
    def test_finds_the_newest_stable_chart_of_a_major(self):
        generator = UpdateReportGenerator("wodby")
        source = "https://charts.example.com"
        generator._helm_index_cache[source] = {
            "entries": {
                "kube-state-metrics": [
                    {"version": "7.2.0"},
                    {"version": "7.9.1"},
                    {"version": "7.10.0-rc.1"},
                    {"version": "8.4.0"},
                ]
            }
        }
        self.assertEqual("7.9.1", generator.get_helm_latest_in_major(source, "prometheus/kube-state-metrics", 7))
        self.assertEqual("8.4.0", generator.get_helm_latest_in_major(source, "prometheus/kube-state-metrics", 8))
        self.assertIsNone(generator.get_helm_latest_in_major(source, "prometheus/kube-state-metrics", 6))

    def test_compares_versions(self):
        self.assertTrue(is_newer_version("7.9.1", "7.4.0"))
        self.assertFalse(is_newer_version("7.4.0", "7.4.0"))
        self.assertFalse(is_newer_version("not-a-version", "7.4.0"))


class ProposeMajorUpdateTest(unittest.TestCase):
    def setUp(self):
        self.repo = ServiceRepo()
        self.addCleanup(self.repo.close)

    def test_opens_a_pull_request_with_only_the_chart_version_changed(self):
        github = FakeGitHub()

        result = propose_major_update(github, self.repo.clone, "wodby", "service-kube-state-metrics", "master", UPDATE)

        self.assertEqual("opened", result["status"])
        branch = "chore/helm-chart-kube-state-metrics-8"
        self.assertTrue(git(self.repo.origin, "branch", "--list", branch))
        manifest = git(self.repo.origin, "show", f"{branch}:service.yml")
        self.assertIn("version: 8.4.0", manifest)
        self.assertIn("chart: prometheus-community/kube-state-metrics", manifest)
        self.assertEqual("master", git(self.repo.clone, "rev-parse", "--abbrev-ref", "HEAD"))
        self.assertIn("version: 7.4.0", (self.repo.clone / "service.yml").read_text())
        payload = github.created[0]
        self.assertEqual(branch, payload["head"])
        self.assertEqual("master", payload["base"])
        self.assertIn("Tag a release", payload["body"])
        self.assertIn("`7.4.0`", payload["body"])

    def test_never_duplicates_or_reopens_a_pull_request(self):
        branch = "chore/helm-chart-kube-state-metrics-8"
        for state, status in (("open", "already_open"), ("closed", "closed")):
            github = FakeGitHub([{"head": branch, "state": state, "html_url": "https://example.com/pull/7"}])
            result = propose_major_update(github, self.repo.clone, "wodby", "service-kube-state-metrics", "master", UPDATE)
            self.assertEqual(status, result["status"])
            self.assertEqual([], github.created)

    def test_leaves_a_branch_someone_else_pushed(self):
        git(self.repo.clone, "push", "origin", "HEAD:refs/heads/chore/helm-chart-kube-state-metrics-8")
        github = FakeGitHub()

        result = propose_major_update(github, self.repo.clone, "wodby", "service-kube-state-metrics", "master", UPDATE)

        self.assertEqual("branch_exists", result["status"])
        self.assertEqual([], github.created)

    def test_does_nothing_once_the_service_uses_the_major(self):
        self.repo = ServiceRepo(helm_version="8.0.0")
        self.addCleanup(self.repo.close)
        github = FakeGitHub()

        result = propose_major_update(github, self.repo.clone, "wodby", "service-kube-state-metrics", "master", UPDATE)

        self.assertEqual("current", result["status"])
        self.assertEqual([], github.created)


if __name__ == "__main__":
    unittest.main()
