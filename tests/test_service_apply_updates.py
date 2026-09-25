import argparse
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from service_apply_updates import apply_updates, create_annotated_tag, parse_args  # noqa: E402


class ServiceApplyUpdatesTest(unittest.TestCase):
    def test_release_tag_ignores_implicit_signing_configuration(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            repo_dir = Path(temp_dir)
            subprocess.run(["git", "init", "-q", str(repo_dir)], check=True)
            subprocess.run(["git", "config", "user.name", "Test User"], cwd=repo_dir, check=True)
            subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=repo_dir, check=True)
            subprocess.run(["git", "config", "tag.gpgSign", "true"], cwd=repo_dir, check=True)
            subprocess.run(["git", "config", "gpg.format", "ssh"], cwd=repo_dir, check=True)
            subprocess.run(
                ["git", "config", "user.signingKey", "/definitely/missing/signing-key"],
                cwd=repo_dir,
                check=True,
            )
            (repo_dir / "README.md").write_text("test\n")
            subprocess.run(["git", "add", "README.md"], cwd=repo_dir, check=True)
            subprocess.run(
                ["git", "-c", "commit.gpgSign=false", "commit", "-q", "-m", "Initial commit"],
                cwd=repo_dir,
                check=True,
            )

            create_annotated_tag(repo_dir, "1.0.0", "Release 1.0.0")

            tag_object = subprocess.run(
                ["git", "cat-file", "-p", "refs/tags/1.0.0"],
                cwd=repo_dir,
                check=True,
                capture_output=True,
                text=True,
            ).stdout
            self.assertIn("Release 1.0.0", tag_object)
            self.assertNotIn("BEGIN SSH SIGNATURE", tag_object)

    def test_disabled_release_is_recorded_without_a_service_repo(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            report_dir = Path(temp_dir)
            report = {
                "per_repo": [
                    {
                        "repo": "service-example",
                        "planned_changes": [{"path": "options[version=1].tag", "before": "1-r0", "after": "1-r1"}],
                        "planned_release": {"status": "planned", "tag": "1.0.1", "previous_tag": "1.0.0"},
                    }
                ]
            }
            (report_dir / "service-update-report.json").write_text(json.dumps(report))
            args = argparse.Namespace(
                report_dir=str(report_dir),
                repo="service-example",
                repo_dir=None,
                owner="wodby",
                releases_disabled=True,
            )

            updated, result = apply_updates(args)

            self.assertEqual(result["status"], "disabled")
            self.assertIn("tag 1.0.1 was not pushed", result["message"])
            self.assertEqual(updated["per_repo"][0]["apply_result"], result)

    def test_repo_dir_is_required_to_release(self) -> None:
        argv = ["service_apply_updates.py", "--report-dir", "reports", "--repo", "service-example"]
        with mock.patch.object(sys, "argv", argv), mock.patch("sys.stderr"):
            with self.assertRaises(SystemExit):
                parse_args()
        with mock.patch.object(sys, "argv", [*argv, "--releases-disabled"]):
            self.assertTrue(parse_args().releases_disabled)


if __name__ == "__main__":
    unittest.main()
