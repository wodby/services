"""Exercise batch publication against local bare Git repositories."""

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from service_apply_gitlab_batch import apply_batch
from service_update_report import GITLAB_REPOS
from test_gitlab_batch import FakeGenerator, batch_report


def git(path, *args):
    return subprocess.run(['git', *args], cwd=path, check=True, text=True, capture_output=True).stdout.strip()


class GitLabBatchGitTest(unittest.TestCase):
    def run_batch(self, unchanged_runner=False):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repos = root / 'checkouts'
            repos.mkdir()
            generator = FakeGenerator()
            if unchanged_runner:
                runner = generator.manifests[GITLAB_REPOS[1]]
                runner['helm']['version'] = '0.94.1'
                runner['options'][0]['tag'] = 'alpine-v19.5.1'
            report = batch_report(generator)
            origins = {}
            for repo in GITLAB_REPOS:
                origin = root / f'{repo}.git'
                git(root, 'init', '--bare', '--initial-branch=main', str(origin))
                path = repos / repo
                git(root, 'clone', str(origin), str(path))
                git(path, 'config', 'user.name', 'Test User')
                git(path, 'config', 'user.email', 'test@example.com')
                git(path, 'config', 'commit.gpgsign', 'false')
                (path / 'service.yml').write_text(generator.get_repo_file(repo, 'service.yml'))
                git(path, 'add', 'service.yml')
                git(path, 'commit', '-m', 'Initial fixture', '-m', 'Initialize a local Git test repository.')
                git(path, 'tag', '--no-sign', '-a', '0.1.0', '-m', 'Initial fixture release')
                git(path, 'push', 'origin', 'main', '--tags')
                origins[repo] = origin
            with mock.patch('service_apply_gitlab_batch.validate_planned_image_tags'):
                apply_batch(report, repos)
            for repo in GITLAB_REPOS:
                item = next(item for item in report['per_repo'] if item['repo'] == repo)
                if unchanged_runner and repo == GITLAB_REPOS[1]:
                    self.assertEqual(item['apply_result']['status'], 'skipped')
                    self.assertEqual(git(root, '--git-dir', str(origins[repo]), 'tag', '--list'), '0.1.0')
                else:
                    self.assertEqual(item['apply_result']['status'], 'applied')
                    self.assertEqual(git(root, '--git-dir', str(origins[repo]), 'cat-file', '-t', '0.1.1'), 'tag')
                    self.assertEqual(git(root, '--git-dir', str(origins[repo]), 'rev-parse', 'main'), item['apply_result']['commit'])
                actual = yaml.safe_load((repos / repo / 'service.yml').read_text())
                self.assertEqual(actual['options'][0]['tag'], 'alpine-v19.5.1' if repo == GITLAB_REPOS[1] else '19.5.2')
            # Replanning a completed/partially completed group uses current state.
            refreshed = FakeGenerator()
            for repo in GITLAB_REPOS:
                refreshed.manifests[repo] = yaml.safe_load((repos / repo / 'service.yml').read_text())
            followup = batch_report(refreshed)
            self.assertTrue(all(not item['planned_changes'] for item in followup['per_repo']))
            apply_batch(followup, repos)
            self.assertTrue(all(item['apply_result']['status'] == 'skipped' for item in followup['per_repo']))

    def test_publishes_both_prepared_releases_and_replans_cleanly(self):
        self.run_batch()

    def test_publishes_one_patch_when_partner_is_already_current(self):
        self.run_batch(unchanged_runner=True)


if __name__ == '__main__':
    unittest.main()
