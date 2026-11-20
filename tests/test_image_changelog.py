"""Release notes for Wodby image updates: following image release notes and base images."""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from image_changelog import WodbyImageChangelog, render_changelog_entry  # noqa: E402
from image_digests import parse_apk  # noqa: E402
from service_update_report import render_image_change_notes, summarize_image_tag_changes  # noqa: E402

SIGNATURE = "-----BEGIN SSH SIGNATURE-----\nabc\n-----END SSH SIGNATURE-----\n"

NOTES = {
    ("php", "r17"): """Update PHP to 8.5.13 and 8.4.28

These releases fix 18 vulnerabilities.

- PHP 8.5.12 → 8.5.13, 8.4.27 → 8.4.28.
- Changelog: https://www.php.net/ChangeLog-8.php
""" + SIGNATURE,
    ("php", "r18"): "Base image wodby/alpine: 3.23-r17 -> 3.23-r18\n",
    ("alpine", "r18"): """Alpine package security updates

- Alpine 3.24 (amd64, arm64): libssl3 3.5.9-r0 -> 3.5.9-r1
- Alpine 3.23 (amd64, arm64): zlib 1.3.2-r1 -> 1.4-r0
""",
    ("nginx", "r24"): "Base image wodby/alpine: 3.23-r17 -> 3.23-r18\n",
    ("nginx", "r25"): "Base image wodby/alpine: 3.22-r18 -> 3.22-r19\n",
    ("alpine", "r19"): """Alpine package security updates

- Alpine 3.24 (amd64, arm64): curl 8.17.0-r0 -> 8.17.0-r1
""",
    ("drupal-php", "r23"): "Base image wodby/php: 8.5-r17 -> 8.5-r18\n",
    ("mariadb", "r20"): """Republish the MariaDB 12.3.4 and 10.11.20 images

- MariaDB 12.3.4 and 10.11.20 on both architectures.
""",
    ("mariadb", "11.4.12-r5"): "wodby/mariadb:11.4.12-r5 from image release r36",
    ("mariadb", "11.4.13-r0"): "wodby/mariadb:11.4.13-r0 from image release r37",
    ("mariadb", "r37"): "MariaDB updates: 11.4.12 -> 11.4.13",
    ("redis", "r5"): "Alpine Linux updates: 3.21.10 -> 3.22.8 (except wodby/redis:8.6, wodby/redis:8.4)",
    ("loop", "r2"): "Base image wodby/loop: 1-r1 -> 1-r2\n",
}
TAGS = {("php", "8.4-r18"), ("php", "8.4-r17")}


def changelog() -> WodbyImageChangelog:
    return WodbyImageChangelog(lambda repo, tag: NOTES.get((repo, tag)), lambda repo, tag: (repo, tag) in TAGS)


class WodbyImageChangelogTest(unittest.TestCase):
    def test_collects_every_revision_and_follows_base_images(self):
        result = changelog().collect("php", "8.4-r16", "8.4-dev-r18")
        self.assertEqual(
            [(e.image, e.revision, e.base_of) for e in result.entries],
            [("wodby/php", "r17", None), ("wodby/alpine", "r18", "wodby/php")],
        )
        php, alpine = result.entries
        # Prose paragraphs and signatures are dropped; bullets stay.
        self.assertEqual(php.bullets, ["PHP 8.5.12 → 8.5.13, 8.4.27 → 8.4.28.", "Changelog: https://www.php.net/ChangeLog-8.php"])
        # Only the bullet for the Alpine line in use survives.
        self.assertEqual(alpine.bullets, ["Alpine 3.23 (amd64, arm64): zlib 1.3.2-r1 -> 1.4-r0"])

    def test_skips_base_notes_for_other_version_lines(self):
        result = changelog().collect("nginx", "1.31-r24", "1.31-r25")
        self.assertEqual(result.entries, [])
        self.assertEqual(result.gaps, [])

    def test_follows_the_base_on_the_images_own_line(self):
        TAGS_BEFORE = set(TAGS)
        result = changelog().collect("drupal-php", "8.4-r22", "8.4-r23")
        self.assertEqual(TAGS, TAGS_BEFORE)
        # The note names php 8.5, but drupal-php 8.4 is built on php 8.4-r18, which follows Alpine 3.23.
        self.assertEqual([(e.image, e.revision) for e in result.entries], [("wodby/alpine", "r18")])
        self.assertEqual(result.entries[0].base_of, "wodby/drupal-php")

    def test_drops_titles_that_name_other_versions_or_exclude_the_image(self):
        self.assertEqual(changelog().collect("mariadb", "11.4-r19", "11.4-r20").entries, [])
        self.assertEqual(len(changelog().collect("mariadb", "12.3-r19", "12.3-r20").entries), 1)
        self.assertEqual(changelog().collect("redis", "8.6-r4", "8.6-r5").entries, [])
        self.assertEqual(len(changelog().collect("redis", "7.4-r4", "7.4-r5").entries), 1)

    def test_maps_full_version_aliases_to_repository_releases(self):
        result = changelog().collect("mariadb", "11.4.12-r5", "11.4.13-r0")
        self.assertEqual([(e.revision, e.title) for e in result.entries], [("r37", "MariaDB updates: 11.4.12 -> 11.4.13")])

    def test_reports_tags_it_cannot_map(self):
        result = changelog().collect("mariadb", "11.4.12-r4", "11.4.13-r0")
        self.assertEqual(result.entries, [])
        self.assertIn("can't be mapped", result.gaps[0])

    def test_stops_on_base_image_cycles(self):
        result = changelog().collect("loop", "1-r1", "1-r2")
        self.assertEqual(result.entries, [])
        self.assertTrue(result.gaps)

    def test_renders_entry_with_its_source(self):
        entry = changelog().collect("nginx", "1.31-r23", "1.31-r24").entries[0]
        self.assertEqual(
            render_changelog_entry(entry),
            [
                "- Alpine package security updates (wodby/alpine r18, base image of wodby/nginx)",
                "  - Alpine 3.23 (amd64, arm64): zlib 1.3.2-r1 -> 1.4-r0",
            ],
        )


class ImageTagSummaryTest(unittest.TestCase):
    def change(self, version, after, option_tags, development=False):
        return {"change_type": "image_tag", "image": "wodby/php", "image_version": version, "after": after,
                "image_option_tags": option_tags, "development": development}

    def test_single_revision_names_only_options_left_behind(self):
        tags = {"8.5": "8.5-r18", "8.4": "8.4-r18", "8.3": "8.3-r18", "8.2": "8.2-r16"}
        changes = [self.change(v, f"{v}-r18", tags) for v in ("8.5", "8.4", "8.3")]
        changes += [self.change(v, f"{v}-dev-r18", tags, True) for v in ("8.5", "8.4", "8.3")]
        self.assertEqual(summarize_image_tag_changes("wodby/php", changes), "wodby/php image revision updated to r18 except 8.2")

    def test_single_revision_for_every_option(self):
        tags = {"8.5": "8.5-r18", "8.4": "8.4-r18"}
        changes = [self.change(v, f"{v}-r18", tags) for v in tags]
        self.assertEqual(summarize_image_tag_changes("wodby/php", changes), "wodby/php image revision updated to r18")

    def test_names_updated_options_when_most_stay_behind(self):
        tags = {"8.5": "8.5-r18", "8.4": "8.4-r16", "8.3": "8.3-r16"}
        changes = [self.change("8.5", "8.5-r18", tags)]
        self.assertEqual(summarize_image_tag_changes("wodby/php", changes), "wodby/php image revision updated to r18 for 8.5")

    def test_several_revisions(self):
        tags = {"8.5": "8.5-r18", "8.2": "8.2-r17"}
        changes = [self.change("8.5", "8.5-r18", tags), self.change("8.2", "8.2-r17", tags)]
        self.assertEqual(summarize_image_tag_changes("wodby/php", changes),
                         "wodby/php image revisions updated: r18 for 8.5; r17 for 8.2")

    def test_third_party_tags(self):
        changes = [{"change_type": "image_tag", "image": "postgres", "image_version": v, "after": t}
                   for v, t in (("17", "17.7"), ("16", "16.11"))]
        self.assertEqual(summarize_image_tag_changes("postgres", changes), "postgres image updated to `17.7` and `16.11`")


class ImageChangeNotesTest(unittest.TestCase):
    def test_comparisons_render_after_release_notes_and_merge_unchanged_tags(self):
        def change(version, fallback, entries=()):
            return {"change_type": "image_tag", "image": "wodby/mariadb", "image_version": version,
                    "after": f"{version}-r21", "image_changelog": {"entries": list(entries), "gaps": [], "fallback": fallback}}

        entry = {"image": "wodby/mariadb", "revision": "r20", "title": "Republish the MariaDB 12.3.4 images",
                 "bullets": [], "base_of": None}
        lines = render_image_change_notes([
            change("12.3", None, [entry]),
            change("11.8", {"lines": ["Updates to 1 Alpine package: zlib"], "error": None}),
            change("11.4", {"lines": [], "error": None}),
            change("10.6", {"lines": [], "error": None}),
        ])
        self.assertEqual(lines, [
            "- Republish the MariaDB 12.3.4 images (wodby/mariadb r20)",
            "- `wodby/mariadb:11.8-r21`:",
            "  - Updates to 1 Alpine package: zlib",
            "- `wodby/mariadb` tags `11.4-r21` and `10.6-r21`: rebuilt with no version or OS package changes",
        ])


class ApkVirtualPackagesTest(unittest.TestCase):
    def test_virtual_packages_are_ignored(self):
        data = b"P:.nginx-rundeps\nV:20261119.181455\n\nP:zlib\nV:1.4-r0\no:zlib\n\n"
        self.assertEqual(list(parse_apk(data)), ["zlib"])


if __name__ == "__main__":
    unittest.main()
