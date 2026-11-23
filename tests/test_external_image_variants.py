import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from service_update_report import UpdateReportGenerator


class ExternalImageVariantsTest(unittest.TestCase):
    def setUp(self):
        self.generator = UpdateReportGenerator('wodby')

    def test_mongodb_preserves_distribution(self):
        self.assertEqual(self.generator.latest_external_tag('8.0', ['8.0.34-noble', '8.0.35-noble', '8.0.36', '8.0.37-jammy'], '8.0.34-noble'), '8.0.35-noble')

    def test_nextcloud_preserves_web_server(self):
        self.assertEqual(self.generator.latest_external_tag('34', ['34.0.3-apache', '34.0.5-apache', '34.0.6-fpm', '34.0.7'], '34.0.3-apache'), '34.0.5-apache')

    def test_missing_variant_never_falls_back_or_rolls_back(self):
        self.assertIsNone(self.generator.latest_external_tag('34', ['34.0.2-apache', '34.0.5'], '34.0.3-apache'))

    def test_numeric_tags_keep_prefix_and_ignore_variants_and_prereleases(self):
        self.assertEqual(self.generator.latest_external_tag('1', ['v1.2.0', '1.2.0', 'v1.3.0-rc1', 'v1.4.0-alpine'], 'v1.1.0'), 'v1.2.0')

    def test_unknown_tag_formats_do_not_select_an_unrelated_variant(self):
        self.assertIsNone(self.generator.latest_external_tag('1', ['1.2.0'], 'alpine-v1.1.0'))

    def test_release_timestamp_tags_advance_within_their_variant(self):
        tags = [
            'RELEASE.2026-11-12T00-00-00Z',
            'RELEASE.2026-12-01T00-00-00Z',
            'RELEASE.2027-01-05T00-00-00Z-distroless',
            'RELEASE.2026-12-20T00-00-00Z-arm64',
            'latest',
        ]
        self.assertEqual(
            self.generator.latest_external_tag('2026', tags, 'RELEASE.2026-11-12T00-00-00Z'),
            'RELEASE.2026-12-01T00-00-00Z',
        )
        self.assertEqual(
            self.generator.latest_external_tag('2026', tags, 'RELEASE.2026-11-12T00-00-00Z-distroless'),
            'RELEASE.2027-01-05T00-00-00Z-distroless',
        )

    def test_release_timestamp_tags_never_roll_back(self):
        self.assertEqual(
            self.generator.latest_external_tag(
                '2026', ['RELEASE.2026-10-01T00-00-00Z'], 'RELEASE.2026-11-12T00-00-00Z'
            ),
            'RELEASE.2026-11-12T00-00-00Z',
        )


    def test_stable_build_tags_advance_by_build_then_rebuild(self):
        tags = ['stable', 'stable-11248-3', 'stable-11509', 'stable-11509-1', 'stable-11509-2', 'jvb-2.3-297-1', 'unstable']
        self.assertEqual(self.generator.latest_external_tag('2', tags, 'stable-11509-1'), 'stable-11509-2')
        self.assertEqual(self.generator.latest_external_tag('2', tags + ['stable-11600'], 'stable-11509-1'), 'stable-11600')

    def test_stable_build_tags_never_roll_back(self):
        self.assertEqual(
            self.generator.latest_external_tag('2', ['stable-11248-3', 'stable-11509'], 'stable-11509-1'),
            'stable-11509-1',
        )

if __name__ == '__main__':
    unittest.main()
