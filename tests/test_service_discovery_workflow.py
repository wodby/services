"""Run the workflow's discovery script with fake public repository metadata."""

import contextlib
import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts import service_update_report as report_module


class DiscoveryWorkflowTest(unittest.TestCase):
    def discover(self, repo_filter, extra_public_repo=None):
        workflow = yaml.safe_load((ROOT / '.github/workflows/service-update-report.yml').read_text())
        step = next(step for step in workflow['jobs']['discover-repos']['steps'] if step.get('id') == 'discover')
        script = step['run'].split("python - <<'PY'\n", 1)[1].rsplit('\nPY', 1)[0]
        public = set(report_module.load_service_repos(ROOT / 'README.md', 'wodby', ''))
        if extra_public_repo:
            public.add(extra_public_repo)
        generator = mock.Mock()
        generator.get_public_service_repos.return_value = public
        generator.get_repo_manifest_paths.return_value = ['service.yml']
        generator.get_service_data.return_value = {}
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / 'output'
            with mock.patch.dict(os.environ, {'OWNER': 'wodby', 'REPO_FILTER': repo_filter, 'GITHUB_OUTPUT': str(output)}):
                with mock.patch.object(report_module, 'UpdateReportGenerator', return_value=generator):
                    with contextlib.redirect_stdout(io.StringIO()):
                        exec(compile(script, '<workflow discovery>', 'exec'), {})
            return dict(line.split('=', 1) for line in output.read_text().splitlines())

    def test_gitlab_filter_schedules_one_complete_batch_without_individual_jobs(self):
        output = self.discover('gitlab$')
        self.assertEqual(json.loads(output['repos']), list(report_module.GITLAB_REPOS))
        self.assertEqual(output['gitlab_batch_count'], '1')
        self.assertEqual(json.loads(output['non_child_repos']), [])
        self.assertEqual(json.loads(output['child_repos']), [])

    def test_other_filter_does_not_schedule_gitlab_batch(self):
        output = self.discover('^service-nextcloud$')
        self.assertEqual(output['gitlab_batch_count'], '0')
        self.assertEqual(json.loads(output['non_child_repos']), ['service-nextcloud'])

    def test_new_public_repository_missing_from_catalog_fails_discovery(self):
        with self.assertRaisesRegex(SystemExit, 'missing from the update catalog: service-new'):
            self.discover('', extra_public_repo='service-new')


if __name__ == '__main__':
    unittest.main()
