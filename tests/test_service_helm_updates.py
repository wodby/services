import sys
import tempfile
import unittest
from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from service_apply_updates import apply_manifest_changes  # noqa: E402
from service_update_report import UpdateReportGenerator  # noqa: E402


class FakeGenerator(UpdateReportGenerator):
    def __init__(self, tags: dict[str, list[str]]) -> None:
        super().__init__("wodby")
        self.tags = tags

    def get_oci_tags(self, reference: str) -> list[str]:
        return self.tags.get(reference, [])

    def get_helm_chart_change_notes(
        self,
        source: str,
        chart: str,
        current_version: str,
        target_version: str,
    ) -> list[dict]:
        return []


def crd_chart(version: str = "v1.8.3") -> dict:
    return {
        "name": "envoy-gateway-crds",
        "source": "oci://docker.io/envoyproxy/gateway-crds-helm",
        "chart": "oci://docker.io/envoyproxy/gateway-crds-helm",
        "version": version,
    }


def planned_change(path: str, before: str, after: str, change_type: str) -> dict:
    return {
        "file": "service.yml",
        "path": path,
        "key": "version",
        "before": before,
        "after": after,
        "change_type": change_type,
    }


class ServiceHelmUpdatesTest(unittest.TestCase):
    def test_crd_chart_update_uses_nested_helm_path(self) -> None:
        generator = FakeGenerator(
            {"oci://docker.io/envoyproxy/gateway-crds-helm": ["v1.8.3", "v1.8.4"]}
        )

        result = generator.check_crd_chart_updates(
            [crd_chart()],
            [crd_chart()],
            "v1.8.4",
            "",
            "service.yml",
            "",
        )

        self.assertTrue(result["ready"])
        self.assertEqual(
            result["planned_changes"][0]["path"],
            "helm.crdCharts[name=envoy-gateway-crds].version",
        )

    def test_crd_chart_update_is_not_ready_until_target_is_published(self) -> None:
        generator = FakeGenerator(
            {"oci://docker.io/envoyproxy/gateway-crds-helm": ["v1.8.3"]}
        )

        result = generator.check_crd_chart_updates(
            [crd_chart()],
            [crd_chart()],
            "v1.8.4",
            "",
            "service.yml",
            "",
        )

        self.assertFalse(result["ready"])
        self.assertEqual(result["planned_changes"], [])
        self.assertIn("does not publish version `v1.8.4`", result["warnings"][0])

    def test_apply_updates_main_and_crd_chart_to_same_version(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            repo_dir = Path(temp_dir)
            manifest = {
                "name": "envoy-gateway",
                "helm": {
                    "version": "v1.8.3",
                    "crdCharts": [crd_chart()],
                },
            }
            (repo_dir / "service.yml").write_text(yaml.safe_dump(manifest, sort_keys=False))
            changes = [
                planned_change("helm.version", "v1.8.3", "v1.8.4", "helm_chart"),
                planned_change(
                    "helm.crdCharts[name=envoy-gateway-crds].version",
                    "v1.8.3",
                    "v1.8.4",
                    "crd_helm_chart",
                ),
            ]

            apply_manifest_changes(repo_dir, changes)

            updated = yaml.safe_load((repo_dir / "service.yml").read_text())
            self.assertEqual(updated["helm"]["version"], "v1.8.4")
            self.assertEqual(updated["helm"]["crdCharts"][0]["version"], "v1.8.4")

    def test_apply_rejects_partial_main_chart_update(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            repo_dir = Path(temp_dir)
            manifest = {
                "name": "envoy-gateway",
                "helm": {
                    "version": "v1.8.3",
                    "crdCharts": [crd_chart()],
                },
            }
            manifest_path = repo_dir / "service.yml"
            manifest_path.write_text(yaml.safe_dump(manifest, sort_keys=False))

            with self.assertRaisesRegex(RuntimeError, "without updating CRD chart envoy-gateway-crds"):
                apply_manifest_changes(
                    repo_dir,
                    [planned_change("helm.version", "v1.8.3", "v1.8.4", "helm_chart")],
                )

            unchanged = yaml.safe_load(manifest_path.read_text())
            self.assertEqual(unchanged["helm"]["version"], "v1.8.3")
            self.assertEqual(unchanged["helm"]["crdCharts"][0]["version"], "v1.8.3")


if __name__ == "__main__":
    unittest.main()
