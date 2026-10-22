"""Exercise the release workflow with real Git tags and a local GitHub CLI double."""

import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

import yaml


WORKFLOW = Path(__file__).resolve().parents[1] / ".github/workflows/tag-release.yml"
STEPS = yaml.safe_load(WORKFLOW.read_text())["jobs"]["release"]["steps"]


class TagReleaseWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.env = dict(os.environ, RELEASE_TAG="1.2.3", GITHUB_REPOSITORY="example/service")
        self.env["PATH"] = str(self.root) + os.pathsep + self.env["PATH"]
        self.env["RELEASE_CAPTURE"] = str(self.root / "release.json")
        self.env["API_ERROR"] = "404"
        self.git("init", "-q")
        self.git("config", "user.name", "Release test")
        self.git("config", "user.email", "release@example.com")
        self.git("-c", "commit.gpgsign=false", "commit", "-qm", "Initial commit", "--allow-empty")
        cli = self.root / "gh"
        cli.write_text("""#!/usr/bin/env python3
import json, os, pathlib, sys
args = sys.argv[1:]
capture = pathlib.Path(os.environ['RELEASE_CAPTURE'])
if args[0] == 'api':
    if os.environ['API_ERROR'] == 'exists':
        print('{}')
        sys.exit(0)
    print('gh: Request failed (HTTP ' + os.environ['API_ERROR'] + ')', file=sys.stderr)
    sys.exit(1)
if args[:2] == ['release', 'create']:
    if os.environ.get('CREATE_ERROR'):
        sys.exit(1)
    notes = pathlib.Path(args[args.index('--notes-file') + 1]).read_text()
    capture.write_text(json.dumps({'args': args, 'body': notes,
        'tagName': args[2], 'name': args[args.index('--title') + 1]}))
elif args[:2] == ['release', 'view']:
    result = json.loads(capture.read_text())
    if os.environ.get('WRONG_BODY'):
        result['body'] = 'Wrong notes'
    print(json.dumps(result))
else:
    sys.exit('Unexpected gh call: ' + repr(args))
""")
        cli.chmod(0o755)

    def git(self, *args):
        """Use only a temporary local repository for tag fixtures."""
        return subprocess.run(["git", *args], cwd=self.root, env=self.env,
                              check=True, capture_output=True, text=True)

    def tag(self, description="Customer-visible change", name=None):
        self.git("-c", "tag.gpgsign=false", "tag", "-a", name or self.env["RELEASE_TAG"],
                 "-m", description, "--cleanup=verbatim")

    def run_step(self, name):
        """Execute the actual embedded workflow code without contacting GitHub."""
        source = next(step["run"] for step in STEPS if step["name"] == name)
        return subprocess.run(["bash", "-e", "-c", source], cwd=self.root, env=self.env,
                              capture_output=True, text=True)

    def publish(self):
        return self.run_step("Publish tag description")

    def captured(self):
        return json.loads(Path(self.env["RELEASE_CAPTURE"]).read_text())

    def test_preserves_multiline_description_and_exact_title(self):
        description = "Add runtime support\n\n- Keep existing defaults.\n- Support `$HOME` and **Markdown**."
        self.tag(description)
        result = self.publish()
        self.assertEqual(0, result.returncode, result.stderr)
        release = self.captured()
        self.assertEqual(description, release["body"])
        self.assertEqual("1.2.3", release["name"])
        self.assertIn("--verify-tag", release["args"])

    def test_rejects_lightweight_and_empty_tags(self):
        self.git("-c", "tag.gpgsign=false", "tag", "1.2.3")
        self.assertNotEqual(0, self.publish().returncode)
        self.git("tag", "-d", "1.2.3")
        self.tag("")
        self.assertNotEqual(0, self.publish().returncode)
        self.assertFalse(Path(self.env["RELEASE_CAPTURE"]).exists())

    def test_removes_signature_from_release_notes(self):
        self.tag("Useful description\n\n-----BEGIN PGP SIGNATURE-----\nexample\n-----END PGP SIGNATURE-----")
        result = self.publish()
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual("Useful description", self.captured()["body"])

    def test_existing_release_is_preserved(self):
        self.tag()
        self.env["API_ERROR"] = "exists"
        self.assertEqual(0, self.publish().returncode)
        self.assertFalse(Path(self.env["RELEASE_CAPTURE"]).exists())

    def test_api_and_creation_errors_fail(self):
        self.tag()
        for status in ["401", "403", "500"]:
            with self.subTest(status=status):
                self.env["API_ERROR"] = status
                self.assertNotEqual(0, self.publish().returncode)
        self.env["API_ERROR"] = "404"
        self.env["CREATE_ERROR"] = "1"
        self.assertNotEqual(0, self.publish().returncode)
        self.assertFalse(Path(self.env["RELEASE_CAPTURE"]).exists())

    def test_detects_incorrect_published_notes(self):
        self.tag()
        self.env["WRONG_BODY"] = "1"
        self.assertNotEqual(0, self.publish().returncode)

    def test_prerelease_and_build_metadata(self):
        for name, expected in [("v2.0.0-rc.1+build", True), ("2.0.0+build-test", False)]:
            with self.subTest(tag=name):
                self.env["RELEASE_TAG"] = name
                self.tag(name=name)
                result = self.publish()
                self.assertEqual(0, result.returncode, result.stderr)
                self.assertEqual(expected, "--prerelease" in self.captured()["args"])
                self.assertEqual(expected, "--latest=false" in self.captured()["args"])

    def test_semantic_version_validation(self):
        for name in ["1.2.3", "v1.2.3", "2.0.0-rc.1", "1.2.3+build-test"]:
            with self.subTest(tag=name):
                self.env["RELEASE_TAG"] = name
                self.assertEqual(0, self.run_step("Validate version tag").returncode)
        for name in ["main", "1.2", "01.2.3", "1.2.3-01", "1.2.3/other", "1.2.3; false"]:
            with self.subTest(tag=name):
                self.env["RELEASE_TAG"] = name
                self.assertNotEqual(0, self.run_step("Validate version tag").returncode)


if __name__ == "__main__":
    unittest.main()
