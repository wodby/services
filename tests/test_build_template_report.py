import copy
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from service_report_email import build_body, event_counts, repo_items  # noqa: E402
from service_update_report import (  # noqa: E402
    UpdateReportGenerator,
    render_markdown,
    render_tag_note_details,
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


class BuildTemplateReportTest(unittest.TestCase):
    def test_nginx_proxy_uses_nginx_eol_data(self) -> None:
        generator = FakeGenerator()
        generator._eol_product_index_cache = {"nginx": "nginx"}

        self.assertEqual(
            generator.resolve_eol_product_name("service-nginx-proxy", "nginx-proxy"),
            "nginx",
        )

    def test_build_template_branch_and_dockerfile_are_current(self) -> None:
        generator = FakeGenerator()
        generator.repo_files[("service-node", "Dockerfile")] = "FROM node\n"
        generator.refs.add(("wodby", "expressjs-boilerplate", "heads/main"))

        result = generator.check_build_templates(
            "service-node",
            "service.yml",
            {
                "build": {
                    "dockerfile": "Dockerfile",
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

        self.assertEqual(result["updates"], [])
        self.assertEqual(result["warnings"], [])
        self.assertIn("`build.dockerfile` file `Dockerfile` exists", result["current"])
        self.assertIn(
            "build template `expressjs` branch `main` exists in `https://github.com/wodby/expressjs-boilerplate`",
            result["current"],
        )

    def test_tag_constraint_reports_new_major_and_missing_pipeline(self) -> None:
        generator = FakeGenerator()
        generator.tags[("laravel", "laravel")] = {"v11.0.0", "v11.6.1", "v13.8.0"}

        result = generator.check_build_templates(
            "service-laravel-php",
            "service.yml",
            {
                "build": {
                    "templates": [
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
                "new major build template tag `v13.8.0` is available for `boilerplate` "
                "outside constraint `^11`; manual review required"
            ],
        )
        self.assertEqual(
            result["warnings"],
            ["build template `boilerplate` pipeline `pipeline.yml` was not found at `v11.6.1`"],
        )
        self.assertIn("build template `boilerplate` tag constraint `^11` resolves to `v11.6.1`", result["current"])

    def test_markdown_includes_build_template_review_section(self) -> None:
        report = sample_build_template_report()

        markdown = render_markdown(report)

        self.assertIn("## Build Template Review", markdown)
        self.assertIn("new build template tag", markdown)
        self.assertNotIn("boilerplate file drift", markdown)

    def test_email_body_includes_build_template_review_section(self) -> None:
        reports = [sample_build_template_report()]
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

        self.assertEqual(counts["build_template_review_items"], 1)
        self.assertIn("Build Template Review", body)
        self.assertIn("new build template tag", body)

    def test_parent_service_change_notes_include_ancestor_services(self) -> None:
        generator = FakeGenerator()
        generator.tags[("wodby", "service-laravel-nginx")] = {"1.0.5", "1.0.6"}
        for repo, tag, message in (
            ("service-laravel-nginx", "1.0.6", "Laravel Nginx parent changes."),
            ("service-php-nginx", "1.0.5", "PHP Nginx parent changes."),
            ("service-nginx", "1.0.4", "Nginx image changes."),
        ):
            generator.tag_notes[("wodby", repo, tag)] = {
                "repo": f"wodby/{repo}",
                "tag": tag,
                "message": message,
                "url": f"https://github.com/wodby/{repo}/releases/tag/{tag}",
                "base_changes": [],
            }
        generator.service_data_at_refs[("service-laravel-nginx", "1.0.6", "service.yml")] = {
            "from": "php-nginx",
            "fromVersion": "1.0.5",
        }
        generator.service_data_at_refs[("service-php-nginx", "1.0.5", "service.yml")] = {
            "from": "nginx",
            "fromVersion": "1.0.4",
        }
        generator.service_data_at_refs[("service-nginx", "1.0.4", "service.yml")] = {
            "name": "nginx",
        }

        result = generator.check_parent_service_version(
            "laravel-nginx",
            "^1.0.0",
            "1.0.5",
            "",
        )

        note = result["planned_changes"][0]["parent_change_notes"][0]
        self.assertEqual(note["repo"], "wodby/service-laravel-nginx")
        parent_note = note["parent_changes"][0]
        self.assertEqual(parent_note["repo"], "wodby/service-php-nginx")
        self.assertEqual(parent_note["parent_changes"][0]["repo"], "wodby/service-nginx")

        description = result["planned_changes"][0]["parent_change_notes"][0]
        rendered = "\n".join(render_tag_note_details(description))
        self.assertIn("Laravel Nginx parent changes.", rendered)
        self.assertIn("PHP Nginx parent changes.", rendered)
        self.assertIn("Nginx image changes.", rendered)

    def test_parent_service_change_note_traversal_stops_on_cycle(self) -> None:
        generator = FakeGenerator()
        for repo in ("service-a", "service-b"):
            generator.tag_notes[("wodby", repo, "1.0.0")] = {
                "repo": f"wodby/{repo}",
                "tag": "1.0.0",
                "message": f"{repo} changes.",
                "url": f"https://github.com/wodby/{repo}/releases/tag/1.0.0",
                "base_changes": [],
            }
        generator.service_data_at_refs[("service-a", "1.0.0", "service.yml")] = {
            "from": "b",
            "fromVersion": "1.0.0",
        }
        generator.service_data_at_refs[("service-b", "1.0.0", "service.yml")] = {
            "from": "a",
            "fromVersion": "1.0.0",
        }

        note = generator.build_service_tag_note_tree("service-a", "1.0.0")

        cycle_note = note["parent_changes"][0]["parent_changes"][0]
        self.assertIn("inheritance cycle", cycle_note["message"])


def sample_build_template_report() -> dict:
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
            "build_template_review_items": 1,
            "build_template_warnings": 0,
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
                "build_template_review_items": ["new build template tag `v2.0.0` is available"],
                "build_template_warnings": [],
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
                "build_template_review_items": ["new build template tag `v2.0.0` is available"],
                "build_template_warnings": [],
                "current": [],
                "warnings": [],
            }
        },
        "category_lists": {"no_image": [], "no_helm": [], "no_options": []},
    }


if __name__ == "__main__":
    unittest.main()
