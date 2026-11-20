import copy
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from service_report_email import build_body, event_counts, repo_items  # noqa: E402
from service_update_report import (  # noqa: E402
    UpdateReportGenerator,
    release_note_changes,
    render_release_description,
    render_markdown,
    strip_ssh_signature_blocks,
)
from update_repository_readmes import (  # noqa: E402
    build_boilerplates,
    preserved_custom_use_sections,
)


class FakeGenerator(UpdateReportGenerator):
    def __init__(self) -> None:
        self.owner = "wodby"
        self.repo_files: dict[tuple[str, str], str] = {}
        self.github_files: dict[tuple[str, str, str, str | None], str] = {}
        self.refs: set[tuple[str, str, str]] = set()
        self.tags: dict[tuple[str, str], set[str]] = {}
        self.tag_notes: dict[tuple[str, str, str], dict] = {}
        self.service_data_at_refs: dict[tuple[str, str, str], dict] = {}
        self.wodby_chart_metadata: dict[str, dict] = {}
        self.oci_tags: dict[str, list[str]] = {}
        self.oci_chart_metadata: dict[tuple[str, str], dict] = {}

    def get_repo_file(self, repo: str, path: str) -> str | None:
        return self.repo_files.get((repo, path))

    def get_github_file(self, owner: str, repo: str, path: str, ref: str | None = None) -> str | None:
        return self.github_files.get((owner, repo, path, ref))

    def github_ref_exists(self, owner: str, repo: str, ref_path: str) -> bool:
        return (owner, repo, ref_path) in self.refs

    def get_github_tags(self, owner: str, repo: str) -> set[str]:
        return self.tags.get((owner, repo), set())

    def get_github_tag_note(self, owner: str, repo: str, tag: str) -> dict | None:
        note = self.tag_notes.get((owner, repo, tag))
        return copy.deepcopy(note) if note is not None else None

    def get_service_data_at_ref(
        self,
        repo: str,
        ref: str,
        manifest_path: str = "service.yml",
    ) -> dict | None:
        data = self.service_data_at_refs.get((repo, ref, manifest_path))
        return copy.deepcopy(data) if data is not None else None

    def get_wodby_chart_metadata(self, chart_name: str) -> dict:
        return copy.deepcopy(self.wodby_chart_metadata[chart_name])

    def get_oci_tags(self, reference: str) -> list[str]:
        return list(self.oci_tags.get(reference) or [])

    def get_oci_chart_metadata(self, reference: str, version: str) -> dict:
        return copy.deepcopy(self.oci_chart_metadata[(reference, version)])


class BuildBoilerplateReportTest(unittest.TestCase):
    def test_ssh_signatures_are_removed_from_tag_messages(self) -> None:
        message = """\
Customer-facing change.
  -----BEGIN SSH SIGNATURE-----
  signature payload
  -----END SSH SIGNATURE-----

Additional detail.
"""

        self.assertEqual(
            strip_ssh_signature_blocks(message),
            "Customer-facing change.\n\nAdditional detail.",
        )

    def test_readme_generator_supports_canonical_and_legacy_fields(self) -> None:
        canonical = [{"name": "canonical"}]
        legacy = [{"name": "legacy"}]

        self.assertEqual(
            build_boilerplates({"build": {"boilerplates": canonical}}),
            canonical,
        )
        self.assertEqual(
            build_boilerplates({"build": {"templates": legacy}}),
            legacy,
        )
        with self.assertRaisesRegex(RuntimeError, "cannot define both"):
            build_boilerplates(
                {"build": {"boilerplates": [], "templates": []}}
            )

    def test_readme_generator_preserves_custom_use_sections(self) -> None:
        readme = """\
## Use this service

Generated usage guidance.

## Background jobs

Keep this repository-specific guidance.

## Maintain a custom version
"""

        self.assertEqual(
            preserved_custom_use_sections(readme),
            "## Background jobs\n\nKeep this repository-specific guidance.",
        )

    def test_nginx_proxy_uses_nginx_eol_data(self) -> None:
        generator = FakeGenerator()
        generator._eol_product_index_cache = {"nginx": "nginx"}

        self.assertEqual(
            generator.resolve_eol_product_name("service-nginx-proxy", "nginx-proxy"),
            "nginx",
        )

    def test_prebuilt_applications_use_their_own_eol_data(self) -> None:
        # Moodle and Matomo options are application versions, not PHP versions.
        generator = FakeGenerator()
        generator._eol_product_index_cache = {"moodle": "moodle", "matomo": "matomo", "php": "php"}

        self.assertEqual(generator.resolve_eol_product_name("service-moodle", "moodle"), "moodle")
        self.assertEqual(generator.resolve_eol_product_name("service-matomo", "matomo"), "matomo")

    def test_build_boilerplate_branch_and_dockerfile_are_current(self) -> None:
        generator = FakeGenerator()
        generator.repo_files[("service-node", "Dockerfile")] = "FROM node\n"
        generator.refs.add(("wodby", "expressjs-boilerplate", "heads/main"))

        result = generator.check_build_boilerplates(
            "service-node",
            "service.yml",
            {
                "build": {
                    "dockerfile": "Dockerfile",
                    "boilerplates": [
                        {
                            "name": "expressjs",
                            "repo": "https://github.com/wodby/expressjs-boilerplate",
                            "branch": "main",
                        }
                    ],
                }
            },
            "",
        )

        self.assertEqual(result["updates"], [])
        self.assertEqual(result["warnings"], [])
        self.assertIn("`build.dockerfile` file `Dockerfile` exists", result["current"])
        self.assertIn(
            "build boilerplate `expressjs` branch `main` exists in `https://github.com/wodby/expressjs-boilerplate`",
            result["current"],
        )

    def test_legacy_build_templates_are_still_checked(self) -> None:
        generator = FakeGenerator()
        generator.refs.add(("wodby", "expressjs-boilerplate", "heads/main"))

        result = generator.check_build_boilerplates(
            "service-node",
            "service.yml",
            {
                "build": {
                    "templates": [
                        {
                            "name": "expressjs",
                            "repo": "https://github.com/wodby/expressjs-boilerplate",
                            "branch": "main",
                        }
                    ],
                }
            },
            "",
        )

        self.assertEqual(result["warnings"], [])
        self.assertIn(
            "build boilerplate `expressjs` branch `main` exists in `https://github.com/wodby/expressjs-boilerplate`",
            result["current"],
        )

    def test_rejects_canonical_and_legacy_build_fields_together(self) -> None:
        generator = FakeGenerator()

        result = generator.check_build_boilerplates(
            "service-node",
            "service.yml",
            {"build": {"boilerplates": [], "templates": []}},
            "",
        )

        self.assertEqual(
            result["warnings"],
            [
                'service build cannot define both "boilerplates" '
                'and legacy "templates"'
            ],
        )

    def test_tag_constraint_reports_new_major_and_service_pipeline(self) -> None:
        generator = FakeGenerator()
        generator.repo_files[("service-laravel-php", "pipeline.yml")] = "version: 0.1\n"
        generator.tags[("laravel", "laravel")] = {"v11.0.0", "v11.6.1", "v13.8.0"}

        result = generator.check_build_boilerplates(
            "service-laravel-php",
            "service.yml",
            {
                "build": {
                    "boilerplates": [
                        {
                            "name": "boilerplate",
                            "repo": "https://github.com/laravel/laravel",
                            "tag": "^11",
                            "pipeline": "pipeline.yml",
                        }
                    ],
                }
            },
            "",
        )

        self.assertEqual(
            result["updates"],
            [
                "new major build boilerplate tag `v13.8.0` is available for `boilerplate` "
                "outside constraint `^11`; manual review required"
            ],
        )
        self.assertEqual(result["warnings"], [])
        self.assertIn(
            "build boilerplate `boilerplate` pipeline file `pipeline.yml` exists",
            result["current"],
        )
        self.assertIn("build boilerplate `boilerplate` tag constraint `^11` resolves to `v11.6.1`", result["current"])

    def test_missing_service_pipeline_does_not_make_tag_resolution_a_warning(self) -> None:
        generator = FakeGenerator()
        generator.tags[("laravel", "laravel")] = {"v11.6.1"}

        result = generator.check_build_boilerplates(
            "service-laravel-php",
            "service.yml",
            {
                "build": {
                    "boilerplates": [
                        {
                            "name": "boilerplate",
                            "repo": "https://github.com/laravel/laravel",
                            "tag": "^11",
                            "pipeline": "pipeline.yml",
                        }
                    ],
                }
            },
            "",
        )

        self.assertEqual(result["updates"], [])
        self.assertEqual(
            result["warnings"],
            ["build boilerplate `boilerplate` pipeline references missing file `pipeline.yml`"],
        )
        self.assertEqual(
            result["current"],
            ["build boilerplate `boilerplate` tag constraint `^11` resolves to `v11.6.1`"],
        )

    def test_markdown_includes_build_boilerplate_review_section(self) -> None:
        report = sample_build_boilerplate_report()

        markdown = render_markdown(report)

        self.assertIn("## Build Boilerplate Review", markdown)
        self.assertIn("new build boilerplate tag", markdown)
        self.assertNotIn("boilerplate file drift", markdown)

    def test_email_body_includes_build_boilerplate_review_section(self) -> None:
        reports = [sample_build_boilerplate_report()]
        items = repo_items(reports)
        counts = event_counts(reports, items, "success", "success")

        body = build_body(
            reports,
            items,
            counts,
            run_url="https://example.test/run",
            event="workflow_dispatch",
            sha="abcdef123456",
            workflow_result="success",
            artifact_result="success",
        )

        self.assertEqual(counts["build_boilerplate_review_items"], 1)
        self.assertIn("Build Boilerplate Review", body)
        self.assertIn("new build boilerplate tag", body)

    def test_base_service_changes_cover_every_release_in_the_update(self) -> None:
        generator = FakeGenerator()
        generator.tags[("wodby", "service-php-nginx")] = {"1.0.4", "1.0.5", "1.0.6", "1.0.7", "2.0.0"}
        notes = {
            "1.0.5": "Release 1.0.5\n\n(wodby/nginx image pinned by digest)",
            "1.0.6": """Release 1.0.6

Base service changes:
- nginx updates: 1.31.8 -> 1.31.9 (wodby/nginx r23)
  - Fixes a request smuggling bug.

(Base service `service-nginx` updated to `2.2.7`)""",
            "1.0.7": "Keep upload limits in sync with PHP\n\nSome background.\n\n- The nginx limit now follows PHP_POST_MAX_SIZE.",
            "2.0.0": "Release 2.0.0\n\nImage changes:\n- Outside the update",
        }
        for tag, message in notes.items():
            generator.tag_notes[("wodby", "service-php-nginx", tag)] = {"message": message}

        result = generator.check_parent_service_version("php-nginx", "^1.0.0", "1.0.4", "")

        change = result["planned_changes"][0]
        self.assertEqual(change["after"], "1.0.7")
        self.assertEqual(change["base_change_blocks"], [
            ["- nginx updates: 1.31.8 -> 1.31.9 (wodby/nginx r23)", "  - Fixes a request smuggling bug."],
            ["- Keep upload limits in sync with PHP", "  - The nginx limit now follows PHP_POST_MAX_SIZE."],
        ])
        self.assertIn("updating base service `service-php-nginx` from `1.0.4` to `1.0.7`", result["updates"][0])

    def test_older_release_notes_contribute_only_their_changes(self) -> None:
        # Notes written before this format nest each base release under "- Release X" with tag lists.
        message = """Release 2.2.5

Parent service changes:
- Release 2.2.5

  Parent service changes:
  - Release 2.2.6

    (wodby/nginx image pinned by digest for tags `1.31-r24`, `1.30-r24`)

  (Parent service `service-nginx` updated to `2.2.6`)
- Release 2.2.6

  Image changes:
  - nginx updates: 1.31.8 -> 1.31.9

(drupal11-nginx: Parent service `service-php-nginx` updated to `2.2.5`)"""
        self.assertEqual(release_note_changes(message), [["- nginx updates: 1.31.8 -> 1.31.9"]])

    def test_base_service_release_description(self) -> None:
        image_entry = {"image": "wodby/php", "revision": "r17", "title": "Update PHP to 8.4.28",
                       "bullets": [], "base_of": "wodby/drupal-php"}
        planned_changes = [
            {
                "change_type": "image_tag",
                "image": "wodby/drupal-php",
                "image_version": "8.4",
                "service_label": label,
                "before": "8.4-r22",
                "after": "8.4-r23",
                "image_option_tags": {"8.4": "8.4-r23"},
                "image_changelog": {"entries": [image_entry], "gaps": [], "fallback": None},
            }
            for label in ("drupal11-php", "drupal10-php")
        ] + [
            {
                "change_type": "parent_service_version",
                "parent_repo": "service-php",
                "service_label": label,
                "after": "3.4.5",
                "base_change_blocks": [
                    ["- Update PHP to 8.4.28 (wodby/php r17)"],
                    ["- Turn session upload progress off by default (wodby/php r18)"],
                ],
            }
            for label in ("drupal11-php", "drupal10-php")
        ]

        description = render_release_description("service-drupal-php", "1.8.4", "1.8.5", planned_changes)

        self.assertEqual(description, """\
Release 1.8.5

Image changes:
- Update PHP to 8.4.28 (wodby/php r17, base image of wodby/drupal-php)

Base service changes:
- Turn session upload progress off by default (wodby/php r18)

(drupal11-php and drupal10-php: wodby/drupal-php image revision updated to r23)
(drupal11-php and drupal10-php: Base service `service-php` updated to `3.4.5`)""")

    def test_service_release_leads_with_image_changes_and_deduplicates_tag_updates(self) -> None:
        entry = {
            "image": "wodby/node",
            "revision": "r18",
            "title": "Update Node.js to 26.5.1 and 24.18.1",
            "bullets": ["Node.js 26.5.0 → 26.5.1, 24.18.0 → 24.18.1"],
            "base_of": None,
        }
        option_tags = {"26": "26-r18", "24": "24-r18", "22": "22-r17"}
        planned_changes = []
        for version in ("26", "24"):
            planned_changes.append({
                "change_type": "image_tag",
                "image": "wodby/node",
                "image_version": version,
                "before": f"{version}-r17",
                "after": f"{version}-r18",
                "image_option_tags": option_tags,
                "image_changelog": {"entries": [copy.deepcopy(entry)], "gaps": [], "fallback": None},
            })
            planned_changes.append({
                "change_type": "image_tag",
                "image": "wodby/node",
                "image_version": version,
                "before": f"{version}-dev-r17",
                "after": f"{version}-dev-r18",
                "image_option_tags": option_tags,
                "development": True,
            })

        description = render_release_description(
            "service-node",
            "1.0.6",
            "1.0.7",
            planned_changes,
        )

        self.assertEqual(
            description,
            """\
Release 1.0.7

Image changes:
- Update Node.js to 26.5.1 and 24.18.1 (wodby/node r18)
  - Node.js 26.5.0 → 26.5.1, 24.18.0 → 24.18.1

(wodby/node image revision updated to r18 except 22)""",
        )

    def test_wodby_chart_changes_are_included_in_service_release_description(self) -> None:
        generator = FakeGenerator()
        reference = "oci://registry-1.docker.io/wodby/nginx"
        generator.oci_tags[reference] = ["0.2.2", "0.2.3", "0.2.4"]
        generator.oci_chart_metadata[(reference, "0.2.3")] = {
            "version": "0.2.3",
            "annotations": {
                "artifacthub.io/changes": """\
- kind: fixed
  description: Preserve the configured replica count during upgrades
""",
            },
        }
        generator.oci_chart_metadata[(reference, "0.2.4")] = {
            "version": "0.2.4",
            "annotations": {
                "artifacthub.io/changes": "- Update the default ingress timeout\n",
            },
        }

        notes = generator.get_helm_chart_change_notes(
            reference,
            reference,
            "0.2.2",
            "0.2.4",
        )
        description = render_release_description(
            "service-nginx",
            "1.0.0",
            "1.0.1",
            [
                {
                    "change_type": "helm_chart",
                    "helm_chart": "oci://registry-1.docker.io/wodby/nginx",
                    "before": "0.2.2",
                    "after": "0.2.4",
                    "chart_change_notes": notes,
                }
            ],
        )

        self.assertIn("Helm chart changes:", description)
        self.assertIn(
            "- Fixed: Preserve the configured replica count during upgrades",
            description,
        )
        self.assertIn("- Changed: Update the default ingress timeout", description)
        self.assertGreater(
            description.index("(Helm chart"),
            description.index("Helm chart changes:"),
        )
        self.assertIn(
            "(Helm chart `oci://registry-1.docker.io/wodby/nginx` updated to `0.2.4`)",
            description,
        )

    def test_wodby_chart_change_notes_require_the_requested_version(self) -> None:
        generator = FakeGenerator()
        reference = "oci://registry-1.docker.io/wodby/nginx"
        generator.oci_tags[reference] = ["0.2.3"]
        generator.oci_chart_metadata[(reference, "0.2.3")] = {
            "version": "0.2.4",
            "annotations": {
                "artifacthub.io/changes": "- kind: fixed\n  description: A fix\n",
            },
        }

        with self.assertRaisesRegex(RuntimeError, "expected `0.2.3`"):
            generator.get_helm_chart_change_notes(
                reference,
                reference,
                "0.2.2",
                "0.2.3",
            )


def sample_build_boilerplate_report() -> dict:
    return {
        "generated_at": "2026-07-07T00:00:00+00:00",
        "totals": {
            "repos_in_readme": 1,
            "repos_reported": 1,
            "external_excluded": 0,
            "updates": 0,
            "no_changes": 0,
            "special": 1,
            "notifications": 0,
            "major_version_notifications": 0,
            "helm_major_version_notifications": 0,
            "missing_version_source_notifications": 0,
            "missing_eol_notifications": 0,
            "planned_releases": 0,
            "dry_run_updates": 0,
            "release_blockers": 0,
            "build_boilerplate_review_items": 1,
            "build_boilerplate_warnings": 0,
        },
        "per_repo": [
            {
                "repo": "service-demo",
                "planned_diffs": [],
                "planned_release": None,
                "dry_run_diffs": [],
                "apply_result": None,
                "updates_without_local_diff": [],
                "notification_groups": {},
                "notifications": [],
                "warnings": [],
                "current": [],
                "expects_image": False,
                "expects_helm": False,
                "expects_options": False,
                "has_image": True,
                "has_helm": True,
                "has_options": True,
                "build_boilerplate_review_items": ["new build boilerplate tag `v2.0.0` is available"],
                "build_boilerplate_warnings": [],
            }
        ],
        "no_changes": {},
        "missing_service_yml": [],
        "special": {
            "service-demo": {
                "expects_image": False,
                "expects_helm": False,
                "expects_options": False,
                "has_image": True,
                "has_helm": True,
                "has_options": True,
                "build_boilerplate_review_items": ["new build boilerplate tag `v2.0.0` is available"],
                "build_boilerplate_warnings": [],
                "current": [],
                "warnings": [],
            }
        },
        "category_lists": {"no_image": [], "no_helm": [], "no_options": []},
    }


if __name__ == "__main__":
    unittest.main()
