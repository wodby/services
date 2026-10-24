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


if __name__ == '__main__':
    unittest.main()
