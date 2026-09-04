import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from service_apply_updates import create_annotated_tag  # noqa: E402


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


if __name__ == "__main__":
    unittest.main()
