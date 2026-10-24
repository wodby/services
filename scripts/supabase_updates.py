"""Keep Supabase's pinned workloads aligned with one published, curated chart bundle."""

import json
import re
from typing import Any, Callable

import yaml
from packaging.version import InvalidVersion, Version

CHART = "oci://registry-1.docker.io/wodby/supabase"
UPSTREAM_PREFIX = "self-hosted/v"
# Workload/container identities, chart value paths, and upstream Compose service names.
COMPONENTS = {
    ("gateway", "gateway"): ("gateway", "api-gw"),
    ("auth", "auth"): ("auth", "auth"),
    ("rest", "rest"): ("rest", "rest"),
    ("realtime", "realtime"): ("realtime", "realtime"),
    ("storage", "storage"): ("storage", "storage"),
    ("storage", "imgproxy"): ("storage.imgproxy", "imgproxy"),
    ("meta", "meta"): ("meta", "meta"),
    ("studio", "studio"): ("studio", "studio"),
}


def stable_version(value: Any) -> Version:
    """Parse a stable version without confusing an unrelated Supabase tag namespace."""
    try:
        version = Version(str(value))
    except InvalidVersion as exc:
        raise RuntimeError(f"invalid stable version {value!r}") from exc
    if version.is_prerelease or version.is_devrelease:
        raise RuntimeError(f"unstable version {value!r}")
    return version


def compatible_version(current: Version, target: Version) -> bool:
    """Keep automatic updates within a compatible release line, including zero majors."""
    depth = 1 if current.major else 2
    return target >= current and target.release[:depth] == current.release[:depth]


def containers(data: dict) -> dict[tuple[str, str], dict]:
    """Require every known component exactly once; unknown workloads need review."""
    result = {}
    for workload in data.get("workloads") or []:
        for container in workload.get("containers") or []:
            key = (workload.get("name"), container.get("name"))
            if key in result:
                raise RuntimeError(f"duplicate Supabase container {key}")
            result[key] = container
    if result.keys() != COMPONENTS.keys():
        raise RuntimeError("Supabase component identities changed; chart/service compatibility review required")
    return result


def chart_bundle(generator: Any, version: str) -> dict:
    """Load exact released chart values and verify their images against its upstream release."""
    metadata = generator.get_oci_chart_metadata(CHART, version)
    if str(metadata.get("version")) != version or metadata.get("name") != "supabase":
        raise RuntimeError("Supabase chart metadata does not match the requested release")
    ref = str(metadata.get("appVersion") or "")
    if not ref.startswith(UPSTREAM_PREFIX):
        raise RuntimeError("Supabase chart must identify an exact self-hosted upstream release")
    app = str(stable_version(ref.removeprefix(UPSTREAM_PREFIX)))
    values = generator.get_oci_chart_values(CHART, version)
    if set(values.get("components") or {}) != {key[0] for key in COMPONENTS}:
        raise RuntimeError("Supabase chart component identities changed; manifest review required")
    text = generator.get_github_file("supabase", "supabase", "docker/docker-compose.yml", ref)
    if text is None:
        raise RuntimeError(f"Supabase upstream composition {ref} could not be loaded")
    compose = yaml.safe_load(text) or {}
    services = compose.get("services") or {}
    images = {}
    for key, (value_path, upstream_name) in COMPONENTS.items():
        image_values = values.get("components") or {}
        for field in value_path.split("."):
            image_values = image_values.get(field) or {}
        image_values = image_values.get("image") or {}
        repository, tag = image_values.get("repository"), image_values.get("tag")
        if not repository or not tag or "@" in str(repository):
            raise RuntimeError(f"Supabase chart has no pinned image for {key}")
        image = f"{repository}:{tag}"
        if image != (services.get(upstream_name) or {}).get("image"):
            raise RuntimeError(f"Supabase chart image for {key} differs from upstream {ref}; review required")
        images[key] = image
    bootstrap = (values.get("bootstrap") or {}).get("image")
    if bootstrap != images[("studio", "studio")]:
        raise RuntimeError("Supabase bootstrap and Studio images must use the same release")
    db_image = str((services.get("db") or {}).get("image") or "")
    if not db_image.startswith("supabase/postgres:"):
        raise RuntimeError("Supabase upstream database image is not recognized")
    db_major = db_image.rsplit(":", 1)[-1].split(".", 1)[0]
    return {
        "chart_version": version,
        "app_version": app,
        "ref": ref,
        "images": images,
        "bootstrap": bootstrap,
        "db_major": db_major,
        "db_image": db_image,
    }


def validate_manifest_bundle(data: dict, bundle: dict) -> None:
    """Reject partial updates, custom pins, and mismatched image value mappings."""
    if str((data.get("helm") or {}).get("version")) != bundle["chart_version"]:
        raise RuntimeError("Supabase chart version differs from its validated bundle")
    if (data.get("helm") or {}).get("source") != CHART or (data.get("helm") or {}).get("chart") != CHART:
        raise RuntimeError("Supabase must use its curated OCI chart")
    options = data.get("options") or []
    if len(options) != 1 or str(options[0].get("version")) != bundle["app_version"]:
        raise RuntimeError("Supabase option version differs from its chart application release")
    if options[0].get("tag") or options[0].get("devTag"):
        raise RuntimeError("Supabase components cannot share a single option image tag")
    for key, container in containers(data).items():
        if container.get("image") != bundle["images"][key]:
            raise RuntimeError(f"Supabase pinned image for {key} differs from the published chart bundle")
        value_path = f"components.{COMPONENTS[key][0]}.image"
        mappings = (container.get("helm") or {}).get("image") or {}
        if mappings.get("registry"):
            raise RuntimeError(f"Supabase registry mapping for {key} changed; review required")
        if mappings.get("repository") != f"{value_path}.repository" or mappings.get("tag") != f"{value_path}.tag":
            raise RuntimeError(f"Supabase image mapping for {key} changed; review required")
    overrides = [
        value.get("value")
        for value in (data.get("helm") or {}).get("values") or []
        if value.get("name") == "bootstrap.image"
    ]
    if len(overrides) > 1 or (overrides and overrides[0] != bundle["bootstrap"]):
        raise RuntimeError("Supabase bootstrap override differs from the published Studio image")
    db_links = [link for link in data.get("links") or [] if link.get("name") == "db"]
    if len(db_links) != 1 or not any(
        "supabase-postgres" in (selector.get("labels") or []) and str(selector.get("option")) == bundle["db_major"]
        for selector in db_links[0].get("selectors") or []
    ):
        raise RuntimeError("Supabase database major differs from its upstream bundle; manual database upgrade required")


def validate_bundle_images(generator: Any, bundle: dict) -> None:
    """Require the entire component set to be published before allowing any update."""
    for image in set(bundle["images"].values()):
        repository, tag = image.rsplit(":", 1)
        if tag not in set(generator.get_image_tags(repository)):
            raise RuntimeError(f"Supabase bundle image {image} is not published")


def upgrade_actions(generator: Any, current: str, target: str) -> list[str]:
    """Surface upstream migration/backup requirements even when breaking is false."""
    if stable_version(target) <= stable_version(current):
        return []
    ref = UPSTREAM_PREFIX + target
    raw = generator.get_github_file("supabase", "supabase", "docker/upgrades.json", ref)
    if raw is None:
        raise RuntimeError(f"Supabase upgrade requirements for {ref} could not be loaded")
    entries = json.loads(raw)
    if not isinstance(entries, dict):
        raise RuntimeError("Supabase upgrade manifest must be a mapping")
    actions = []
    for version, entry in entries.items():
        if version.startswith("_"):
            continue
        parsed = stable_version(version)
        if not stable_version(current) < parsed <= stable_version(target):
            continue
        if not isinstance(entry, dict):
            raise RuntimeError("Supabase upgrade entry must be a mapping")
        if entry.get("breaking") or entry.get("gate") or entry.get("requires"):
            actions.append(
                f"Supabase {version} requires upgrade review: "
                + "; ".join(
                    str(action) for action in entry.get("requires") or [entry.get("gate") or "breaking release"]
                )
            )
    return actions


def upgrade_review_reasons(generator: Any, current: dict, target: dict) -> list[str]:
    """Share every automatic-upgrade gate between report generation and application."""
    actions = upgrade_actions(generator, current["app_version"], target["app_version"])
    if not compatible_version(stable_version(current["chart_version"]), stable_version(target["chart_version"])):
        actions.append("Supabase chart release-line changes require compatibility review")
    if not compatible_version(stable_version(current["app_version"]), stable_version(target["app_version"])):
        actions.append("Supabase application release-line changes require compatibility review")
    if target["db_major"] != current["db_major"]:
        actions.append("Supabase database major changes require a manual database upgrade")
    elif target["db_image"] != current["db_image"]:
        actions.append(
            "Supabase upstream database image changed; verify the linked Supabase PostgreSQL service before upgrading"
        )
    for key, image in target["images"].items():
        previous = current["images"][key]
        if previous == image:
            continue
        before_repo, before_tag = previous.rsplit(":", 1)
        after_repo, after_tag = image.rsplit(":", 1)
        if before_repo != after_repo:
            actions.append(f"Supabase component repository change for {key} requires compatibility review")
            continue
        before = re.fullmatch(r"v?(\d+(?:\.\d+)*)(?:-sha-[0-9a-f]+)?", before_tag)
        after = re.fullmatch(r"v?(\d+(?:\.\d+)*)(?:-sha-[0-9a-f]+)?", after_tag)
        if not before or not after or stable_version(after.group(1)) < stable_version(before.group(1)):
            actions.append(f"Supabase component image rollback or unfamiliar tag format for {key} requires review")
    return actions


def check_updates(generator: Any, data: dict, manifest_path: str, prefix: str, make_change: Callable) -> dict:
    """Plan all component pins, application version, bootstrap override, and chart together."""
    result = {
        key: [] for key in ("updates", "current", "notifications", "warnings", "planned_changes", "dry_run_changes")
    }
    try:
        current_chart = str((data.get("helm") or {}).get("version") or "")
        current = chart_bundle(generator, current_chart)
        validate_manifest_bundle(data, current)
        published = []
        for raw in generator.get_oci_tags(CHART):
            try:
                parsed = stable_version(raw)
            except RuntimeError:
                continue
            if parsed >= stable_version(current_chart):
                published.append((parsed, raw))
        if not published or current_chart not in {raw for _, raw in published}:
            raise RuntimeError("Supabase current chart is not among published chart releases")
        compatible = [
            (parsed, raw) for parsed, raw in published if compatible_version(stable_version(current_chart), parsed)
        ]
        _, target_chart = max(compatible)
        if max(published)[0] > stable_version(target_chart):
            result["notifications"].append(
                f"{prefix}new Supabase chart {max(published)[1]} requires compatibility review"
            )
        target = chart_bundle(generator, target_chart)
        validate_bundle_images(generator, target)
        actions = upgrade_review_reasons(generator, current, target)
        changes = []
        for (workload, container), image in target["images"].items():
            before = current["images"][(workload, container)]
            if before == image:
                continue
            repository, tag = image.rsplit(":", 1)
            changes.append(
                make_change(
                    manifest_path,
                    f"workloads[name={workload}].containers[name={container}].image",
                    "image",
                    before,
                    image,
                    f"{prefix}update Supabase {container} to the image from {target['ref']}: {image}",
                    {"change_type": "container_image", "image": repository, "image_tag": tag, "bundle": "supabase"},
                )
            )
        overrides = [value for value in data["helm"].get("values") or [] if value.get("name") == "bootstrap.image"]
        if overrides and overrides[0]["value"] != target["bootstrap"]:
            changes.append(
                make_change(
                    manifest_path,
                    "helm.values[name=bootstrap.image].value",
                    "value",
                    overrides[0]["value"],
                    target["bootstrap"],
                    "Update Supabase bootstrap image with Studio",
                    {"change_type": "bootstrap_image", "bundle": "supabase"},
                )
            )
        if target_chart != current_chart:
            notes = generator.get_helm_chart_change_notes(CHART, CHART, current_chart, target_chart)
            changes.append(
                make_change(
                    manifest_path,
                    "helm.version",
                    "version",
                    current_chart,
                    target_chart,
                    f"Update Supabase chart to {target_chart}",
                    {
                        "change_type": "helm_chart",
                        "helm_chart": CHART,
                        "chart_change_notes": notes,
                        "bundle": "supabase",
                    },
                )
            )
        if target["app_version"] != current["app_version"]:
            changes.append(
                make_change(
                    manifest_path,
                    f"options[version={current['app_version']}].version",
                    "version",
                    current["app_version"],
                    target["app_version"],
                    f"Update Supabase application bundle to {target['app_version']}",
                    {"change_type": "supabase_bundle", "bundle": "supabase"},
                )
            )
        result["notifications"].extend(prefix + action for action in actions)
        if actions:
            result["dry_run_changes"] = changes
        else:
            result["planned_changes"] = changes
            result["updates"] = [change["description"] for change in changes]
        result["current"].append(
            f"{prefix}all eight Supabase components validated against published chart {target_chart} ({target['ref']})"
        )
        latest = generator.get_fallback_version_source_values(
            generator.resolve_fallback_version_source("service-supabase", "supabase")
        )
        latest_version = max(stable_version(value) for value in latest if _stable(value))
        if latest_version > stable_version(target["app_version"]):
            result["notifications"].append(
                f"{prefix}upstream Supabase {latest_version} awaits a compatible published chart bundle; do not update individual component images. Release notes: https://github.com/supabase/supabase/releases/tag/{UPSTREAM_PREFIX}{latest_version}"
            )
            result["notifications"].extend(
                prefix + action for action in upgrade_actions(generator, target["app_version"], str(latest_version))
            )
    except Exception as exc:
        result["warnings"].append(f"{prefix}Supabase bundle update blocked: {exc}")
        result["planned_changes"] = []
        result["updates"] = []
    return result


def _stable(value: Any) -> bool:
    try:
        stable_version(value)
        return True
    except RuntimeError:
        return False
