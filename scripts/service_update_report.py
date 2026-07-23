#!/usr/bin/env python3

import argparse
import base64
import copy
import difflib
import json
import os
import re
import subprocess
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote, urljoin, urlparse

import requests
import yaml
from packaging.version import InvalidVersion, Version


README_SERVICE_REPO_RE_TEMPLATE = r"https://github\.com/{owner}/(?P<repo>service-[A-Za-z0-9._-]+)(?:/)?(?=[)\s|]|$)"
README_MANAGED_SERVICES_HEADING = "## Managed services"
EXCLUDED_README_REPOS = {"service"}
DOCKER_TOKEN_URL = "https://auth.docker.io/token?service=registry.docker.io&scope=repository:{repo}:pull"
GHCR_TOKEN_URL = "https://ghcr.io/token?scope=repository:{repo}:pull"
REGISTRY_TAGS_URL = "https://{registry}/v2/{repo}/tags/list?n=10000"
GITHUB_CONTENTS_URL = "https://api.github.com/repos/{owner}/{repo}/contents/{path}"
GITHUB_REF_URL = "https://api.github.com/repos/{owner}/{repo}/git/ref/{ref_path}"
GITHUB_TAGS_URL = "https://api.github.com/repos/{owner}/{repo}/tags?per_page=100&page={page}"
WODBY_CHART_URL = "https://raw.githubusercontent.com/{owner}/charts/main/{chart}/Chart.yaml"
ENDOFLIFE_PRODUCTS_URL = "https://endoflife.date/api/v1/products"
ENDOFLIFE_PRODUCT_URL = "https://endoflife.date/api/v1/products/{product}/"
TAILSCALE_STABLE_URL = "https://pkgs.tailscale.com/stable/"

EOL_PRODUCT_ALIASES = {
    "cloud-mariadb": "mariadb",
    "cloud-mysql": "mysql",
    "cloud-postgres": "postgresql",
    "httpd": "apache-http-server",
    "matomo": "php",
    "nextjs": "nodejs",
    "nginx-proxy": "nginx",
    "node": "nodejs",
    "php-httpd": "apache-http-server",
    "postgis": "postgresql",
    "postgres": "postgresql",
    "varnish": "vinyl-cache",
    "vinyl": "vinyl-cache",
}

FALLBACK_VERSION_SOURCES: dict[str, dict[str, Any]] = {
    "3xui": {
        "label": "3X UI",
        "source_label": "MHSanaei/3x-ui GitHub tags",
        "report_only": True,
        "kind": "github_tags",
        "owner": "MHSanaei",
        "repo": "3x-ui",
        "current_field": "tag",
        "comparison": "major",
    },
    "dagster": {
        "label": "Dagster",
        "source_label": "dagster/dagster-celery-k8s image tags",
        "report_only": True,
        "kind": "image_tags",
        "image": "dagster/dagster-celery-k8s",
        "current_field": "tag",
        "comparison": "major",
    },
    "gotenberg": {
        "label": "Gotenberg",
        "source_label": "gotenberg/gotenberg GitHub tags",
        "report_only": True,
        "kind": "github_tags",
        "owner": "gotenberg",
        "repo": "gotenberg",
        "current_field": "tag",
        "comparison": "major",
    },
    "mailpit": {
        "label": "Mailpit",
        "source_label": "axllent/mailpit GitHub tags",
        "report_only": True,
        "kind": "github_tags",
        "owner": "axllent",
        "repo": "mailpit",
        "current_field": "tag",
        "comparison": "major",
    },
    "nfs-provisioner": {
        "label": "NFS-Ganesha",
        "source_label": "nfs-ganesha/nfs-ganesha GitHub tags",
        "report_only": True,
        "kind": "github_tags",
        "owner": "nfs-ganesha",
        "repo": "nfs-ganesha",
        "current_field": "tag",
        "comparison": "major",
    },
    "openclaw": {
        "label": "OpenClaw",
        "source_label": "openclaw/openclaw GitHub tags",
        "report_only": True,
        "kind": "github_tags",
        "owner": "openclaw",
        "repo": "openclaw",
        "current_field": "version",
        "comparison": "major",
    },
    "opensmtpd": {
        "label": "OpenSMTPD Portable",
        "source_label": "OpenSMTPD/OpenSMTPD GitHub tags",
        "report_only": True,
        "kind": "github_tags",
        "owner": "OpenSMTPD",
        "repo": "OpenSMTPD",
        "current_field": "version",
        "comparison": "major",
    },
    "tailscale": {
        "label": "Tailscale stable",
        "source_label": "Tailscale stable package index",
        "report_only": True,
        "kind": "tailscale_stable",
        "current_field": "version",
        "comparison": "minor_family",
    },
    "aws-lb-controller": {
        "label": "AWS Load Balancer Controller",
        "source_label": "kubernetes-sigs/aws-load-balancer-controller GitHub tags",
        "report_only": True,
        "kind": "github_tags",
        "owner": "kubernetes-sigs",
        "repo": "aws-load-balancer-controller",
        "current_field": "helm_app_version",
        "comparison": "major",
    },
    "envoy-gateway": {
        "label": "Envoy Gateway",
        "source_label": "envoyproxy/gateway GitHub tags",
        "report_only": True,
        "kind": "github_tags",
        "owner": "envoyproxy",
        "repo": "gateway",
        "current_field": "helm_version",
        "comparison": "major",
    },
    "frpc": {
        "label": "FRP",
        "source_label": "fatedier/frp GitHub tags",
        "report_only": True,
        "kind": "github_tags",
        "owner": "fatedier",
        "repo": "frp",
        "current_field": "wodby_chart_image_tag",
        "comparison": "minor_family",
    },
    "kube-state-metrics": {
        "label": "Kube State Metrics",
        "source_label": "kubernetes/kube-state-metrics GitHub tags",
        "report_only": True,
        "kind": "github_tags",
        "owner": "kubernetes",
        "repo": "kube-state-metrics",
        "current_field": "helm_app_version",
        "comparison": "major",
    },
    "metrics-server": {
        "label": "Metrics Server",
        "source_label": "kubernetes-sigs/metrics-server GitHub tags",
        "report_only": True,
        "kind": "github_tags",
        "owner": "kubernetes-sigs",
        "repo": "metrics-server",
        "current_field": "helm_app_version",
        "comparison": "minor_family",
    },
    "monitoring": {
        "label": "Grafana Alloy",
        "source_label": "grafana/alloy GitHub tags",
        "report_only": True,
        "kind": "github_tags",
        "owner": "grafana",
        "repo": "alloy",
        "current_field": "helm_app_version",
        "comparison": "major",
    },
    "node-exporter": {
        "label": "Node Exporter",
        "source_label": "prometheus/node_exporter GitHub tags",
        "report_only": True,
        "kind": "github_tags",
        "owner": "prometheus",
        "repo": "node_exporter",
        "current_field": "helm_app_version",
        "comparison": "major",
    },
    "prometheus": {
        "label": "Prometheus",
        "source_label": "prometheus/prometheus GitHub tags",
        "report_only": True,
        "kind": "github_tags",
        "owner": "prometheus",
        "repo": "prometheus",
        "current_field": "wodby_chart_image_tag",
        "comparison": "minor_family",
    },
}

WODBY_TAG_RE = re.compile(r"^(?P<base>\d+(?:\.\d+)*)(?:-(?P<stability>\d+(?:\.\d+)*))?$")
EXTERNAL_TAG_RE = re.compile(r"^(?P<prefix>v?)(?P<base>\d+(?:\.\d+)*)$")
ISO_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
SEMVER_TAG_RE = re.compile(r"^(?P<prefix>v?)(?P<version>\d+\.\d+\.\d+)$")
CARET_CONSTRAINT_RE = re.compile(r"^\^(?P<version>v?\d+\.\d+\.\d+)$")
BASE_IMAGE_UPDATE_RE = re.compile(r"^Base image stability tag updated to (?P<tag>\S+)", re.IGNORECASE)
FROM_WODBY_IMAGE_RE = re.compile(r"^\s*FROM\s+wodby/(?P<repo>[A-Za-z0-9._-]+):", re.MULTILINE)
README_BASE_IMAGE_RE = re.compile(r"Base image:\s+\[wodby/(?P<repo>[A-Za-z0-9._-]+)\]", re.IGNORECASE)
SOURCE_VERSION_TAG_RE = re.compile(r"^[vV]?(?P<version>\d+(?:\.\d+){0,2})(?:p(?P<portable>\d+))?$")
TAILSCALE_STABLE_OPTION_RE = re.compile(r'<option value="(?P<version>\d+\.\d+\.\d+)"')
GITHUB_REPO_URL_RE = re.compile(
    r"^https://github\.com/(?P<owner>[A-Za-z0-9_.-]+)/(?P<repo>[A-Za-z0-9_.-]+?)(?:\.git)?/?$"
)
MAJOR_CONSTRAINT_RE = re.compile(r"^\^v?(?P<major>\d+)(?:\.x)?$")

NOTIFICATION_GROUP_ORDER = [
    "major_version",
    "helm_major_version",
    "missing_version_source",
    "missing_eol",
]
NOTIFICATION_GROUP_TITLES = {
    "major_version": "New Major Version Detected",
    "helm_major_version": "New Helm Major Version Detected",
    "missing_version_source": "No Source for Version Checks Found",
    "missing_eol": "No EOL Could Be Found",
}


def empty_notification_groups() -> dict[str, list[str]]:
    return {group: [] for group in NOTIFICATION_GROUP_ORDER}


def compact_notification_groups(groups: dict[str, list[str]]) -> dict[str, list[str]]:
    return {group: messages for group, messages in groups.items() if messages}


def add_grouped_notification(result: dict[str, Any], group: str, message: str) -> None:
    result.setdefault("notifications", []).append(message)
    result.setdefault("notification_groups", {}).setdefault(group, []).append(message)


def merge_notification_groups(target: dict[str, list[str]], source: dict[str, list[str]] | None) -> None:
    for group, messages in (source or {}).items():
        target.setdefault(group, []).extend(messages)


def add_repo_notification(
    notifications: list[str],
    notification_groups: dict[str, list[str]],
    group: str,
    message: str,
) -> None:
    notifications.append(message)
    notification_groups.setdefault(group, []).append(message)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Generate a service update report.")
    parser.add_argument("--readme", default="README.md", help="Path to the README that lists the service repos.")
    parser.add_argument("--owner", default="wodby", help="GitHub owner/org that owns the service repos.")
    parser.add_argument("--repo-filter", default="", help="Optional regex filter for repo names.")
    parser.add_argument(
        "--build-template-mode",
        choices=("off", "report"),
        default="report",
        help="Whether service build template references are checked and included in the report.",
    )
    parser.add_argument(
        "--output-dir",
        default="artifacts/service-update-report",
        help="Directory where markdown and JSON reports will be written.",
    )
    return parser.parse_args()


def parse_version(value: str | None) -> Version | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    if text.startswith("v"):
        text = text[1:]
    try:
        return Version(text)
    except InvalidVersion:
        return None


def parse_source_version(value: Any) -> Version | None:
    text = str(value or "").strip()
    if not text:
        return None

    lowered = text.lower()
    if any(marker in lowered for marker in ("alpha", "beta", "dev", "rc", "snapshot")):
        return None

    match = SOURCE_VERSION_TAG_RE.match(text)
    if not match:
        return None

    version = match.group("version")
    portable = match.group("portable")
    if portable is not None:
        version = f"{version}.post{portable}"

    parsed = parse_version(version)
    if parsed is None or parsed.is_prerelease:
        return None
    return parsed


def latest_source_version(values: list[str] | set[str]) -> tuple[Version, str] | None:
    candidates: list[tuple[Version, str]] = []
    for value in values:
        parsed = parse_source_version(value)
        if parsed is not None:
            candidates.append((parsed, str(value)))
    if not candidates:
        return None
    candidates.sort(key=lambda item: item[0], reverse=True)
    return candidates[0]


def configured_source_versions(options: list[Any], current_field: str) -> list[tuple[Version, str]]:
    candidates: list[tuple[Version, str]] = []
    fallback_fields = ["tag", "version"] if current_field != "tag" else ["version"]
    for option in options:
        if not isinstance(option, dict):
            continue
        value = option.get(current_field)
        if value is None:
            for field in fallback_fields:
                value = option.get(field)
                if value is not None:
                    break
        parsed = parse_source_version(value)
        if parsed is not None:
            candidates.append((parsed, str(value)))
    return candidates


def parse_github_repo_url(value: Any) -> tuple[str, str] | None:
    match = GITHUB_REPO_URL_RE.match(str(value or "").strip())
    if not match:
        return None
    return match.group("owner"), match.group("repo")


def latest_matching_major_tag(tags: set[str], major: int) -> tuple[str, Version] | None:
    candidates: list[tuple[Version, str]] = []
    for tag in tags:
        parsed = parse_source_version(tag)
        if parsed is None or parsed.is_prerelease or parsed.major != major:
            continue
        candidates.append((parsed, tag))
    if not candidates:
        return None
    candidates.sort(reverse=True)
    parsed, tag = candidates[0]
    return tag, parsed


def latest_source_tag(tags: set[str]) -> tuple[str, Version] | None:
    latest = latest_source_version(tags)
    if latest is None:
        return None
    parsed, tag = latest
    return tag, parsed


def manifest_relative_path(manifest_path: str, value: Any) -> str:
    path = str(value or "").strip().strip("/")
    if not path:
        return path
    if "/" not in manifest_path:
        return path
    manifest_dir = manifest_path.rsplit("/", 1)[0]
    return f"{manifest_dir}/{path}"


def version_family_label(version: Version, depth: int = 2) -> str:
    release = version.release
    if len(release) < depth:
        release = release + (0,) * (depth - len(release))
    return ".".join(str(part) for part in release[:depth])


def exact_match(base: str, wanted: str) -> bool:
    return base == wanted


def family_match(base: str, wanted: str) -> bool:
    return base == wanted or base.startswith(f"{wanted}.")


def normalized_manifest_eol(value: str) -> str:
    return f"{value}T00:00:00+00:00"


def eol_date_from_manifest(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    if ISO_DATE_RE.match(text):
        return text
    if len(text) >= 10 and ISO_DATE_RE.match(text[:10]):
        return text[:10]
    return None


def major_version(value: str | None) -> int | None:
    parsed = parse_version(value)
    return parsed.major if parsed is not None else None


def is_major_version_change(current: str, latest: str) -> bool:
    current_major = major_version(current)
    latest_major = major_version(latest)
    return current_major is not None and latest_major is not None and latest_major > current_major


def normalize_service_key(value: str | None) -> str:
    text = str(value or "").strip().lower()
    if text.startswith("service-"):
        text = text[len("service-"):]
    return text.replace("_", "-")


def normalize_change_value(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, date):
        return value.isoformat()
    return str(value)


def format_diff_value(value: Any) -> str:
    normalized = normalize_change_value(value)
    return normalized if normalized is not None else "null"


def make_planned_change(
    manifest_path: str,
    field_path: str,
    key: str,
    before: Any,
    after: Any,
    description: str,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    change = {
        "file": manifest_path,
        "path": field_path,
        "key": key,
        "before": normalize_change_value(before),
        "after": normalize_change_value(after),
        "description": description,
    }
    if extra:
        change.update(extra)
    return change


def duplicate_option_versions(options: list[Any]) -> set[str]:
    seen: set[str] = set()
    duplicates: set[str] = set()
    for option in options:
        if not isinstance(option, dict) or option.get("version") is None:
            continue
        version = str(option["version"])
        if version in seen:
            duplicates.add(version)
        seen.add(version)
    return duplicates


def raw_options_by_version(raw_options: list[Any]) -> dict[str, dict[str, Any]]:
    duplicates = duplicate_option_versions(raw_options)
    return {
        str(option.get("version")): option
        for option in raw_options
        if isinstance(option, dict) and option.get("version") is not None and str(option.get("version")) not in duplicates
    }


def duplicate_crd_chart_names(crd_charts: list[Any]) -> set[str]:
    seen: set[str] = set()
    duplicates: set[str] = set()
    for chart in crd_charts:
        if not isinstance(chart, dict) or chart.get("name") is None:
            continue
        name = str(chart["name"])
        if name in seen:
            duplicates.add(name)
        seen.add(name)
    return duplicates


def raw_crd_charts_by_name(raw_crd_charts: list[Any]) -> dict[str, dict[str, Any]]:
    duplicates = duplicate_crd_chart_names(raw_crd_charts)
    return {
        str(chart.get("name")): chart
        for chart in raw_crd_charts
        if isinstance(chart, dict) and chart.get("name") is not None and str(chart.get("name")) not in duplicates
    }


def render_planned_diffs(planned_changes: list[dict[str, Any]]) -> list[str]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    for change in planned_changes:
        manifest_path = str(change.get("file") or "service.yml")
        grouped.setdefault(manifest_path, []).append(change)

    diffs: list[str] = []
    for manifest_path, changes in grouped.items():
        lines = [
            f"diff --git a/{manifest_path} b/{manifest_path}",
            f"--- a/{manifest_path}",
            f"+++ b/{manifest_path}",
        ]
        for change in changes:
            key = str(change.get("key") or "value")
            path = str(change.get("path") or key)
            diff_before_lines = change.get("diff_before_lines")
            diff_after_lines = change.get("diff_after_lines")
            if isinstance(diff_before_lines, list) or isinstance(diff_after_lines, list):
                unified = list(
                    difflib.unified_diff(
                        diff_before_lines or [],
                        diff_after_lines or [],
                        fromfile=f"a/{manifest_path}",
                        tofile=f"b/{manifest_path}",
                        lineterm="",
                    )
                )
                rendered_lines = unified[2:]
                if not rendered_lines and change.get("before_sha256") != change.get("after_sha256"):
                    rendered_lines = [
                        "@@ exact file content @@",
                        f"-sha256: {change.get('before_sha256') or 'missing'}",
                        f"+sha256: {change.get('after_sha256') or 'missing'}",
                        " content differs only in line endings or a trailing newline",
                    ]
                lines.extend(rendered_lines)
            else:
                lines.append(f"@@ {path} @@")
                lines.append(f"-{key}: {format_diff_value(change.get('before'))}")
                lines.append(f"+{key}: {format_diff_value(change.get('after'))}")
        diffs.append("\n".join(lines))
    return diffs


def latest_stable_semver_tag(tags: set[str]) -> tuple[str, str, Version] | None:
    candidates: list[tuple[Version, str, str]] = []
    for tag in tags:
        match = SEMVER_TAG_RE.match(tag)
        if not match:
            continue
        parsed = parse_version(match.group("version"))
        if parsed is None or parsed.is_prerelease:
            continue
        candidates.append((parsed, tag, match.group("prefix")))
    if not candidates:
        return None
    candidates.sort(reverse=True)
    parsed, tag, prefix = candidates[0]
    return tag, prefix, parsed


def semver_tag_candidates(tags: set[str]) -> list[tuple[Version, str, str]]:
    candidates: list[tuple[Version, str, str]] = []
    for tag in tags:
        match = SEMVER_TAG_RE.match(tag)
        if not match:
            continue
        parsed = parse_version(match.group("version"))
        if parsed is None or parsed.is_prerelease:
            continue
        candidates.append((parsed, tag, match.group("prefix")))
    candidates.sort(reverse=True)
    return candidates


def parse_caret_constraint(value: Any) -> Version | None:
    match = CARET_CONSTRAINT_RE.match(str(value or "").strip())
    if not match:
        return None
    return parse_version(match.group("version"))


def caret_upper_bound(base: Version) -> Version:
    if base.major > 0:
        return Version(f"{base.major + 1}.0.0")
    if base.minor > 0:
        return Version(f"0.{base.minor + 1}.0")
    return Version(f"0.0.{base.micro + 1}")


def latest_matching_caret_tag(tags: set[str], base: Version) -> tuple[str, Version] | None:
    upper_bound = caret_upper_bound(base)
    for parsed, tag, _prefix in semver_tag_candidates(tags):
        if base <= parsed < upper_bound:
            return tag, parsed
    return None


def label_prefix(change: dict[str, Any]) -> str:
    label = str(change.get("service_label") or "").strip()
    return f"{label}: " if label else ""


def human_change_description(change: dict[str, Any]) -> str:
    before = format_diff_value(change.get("display_before", change.get("before")))
    after = format_diff_value(change.get("display_after", change.get("after")))
    prefix = label_prefix(change)
    change_type = change.get("change_type")

    if change_type == "image_tag":
        version = str(change.get("image_version") or "unknown")
        return f"{prefix}Tag updated from `{before}` to `{after}` for version `{version}`."
    if change_type == "helm_chart":
        chart = str(change.get("helm_chart") or "chart")
        return f"{prefix}Helm chart `{chart}` updated from `{before}` to `{after}`."
    if change_type == "crd_helm_chart":
        chart = str(change.get("crd_chart") or change.get("helm_chart") or "CRD chart")
        return f"{prefix}CRD Helm chart `{chart}` updated from `{before}` to `{after}`."
    if change_type == "eol":
        product = str(change.get("product_label") or "").strip()
        version = str(change.get("version") or "unknown")
        product_suffix = f" for {product}" if product else ""
        return f"{prefix}EOL updated to `{after}`{product_suffix} version `{version}`."
    if change_type == "parent_service_version":
        parent_repo = str(change.get("parent_repo") or "parent service")
        return f"{prefix}Parent service `{parent_repo}` updated from `{before}` to `{after}`."

    field_path = str(change.get("path") or change.get("key") or "value")
    return f"{prefix}`{field_path}` updated from `{before}` to `{after}`."


def render_release_description(
    _repo: str,
    _previous_tag: str,
    next_tag: str,
    planned_changes: list[dict[str, Any]],
) -> str:
    lines = [
        f"Release {next_tag}",
        "",
        "Changes:",
    ]
    for change in planned_changes:
        lines.append(f"- {human_change_description(change)}")

    image_note_blocks = render_image_change_notes(planned_changes)
    if image_note_blocks:
        lines.append("")
        lines.append("Image changes:")
        lines.extend(image_note_blocks)
    parent_note_blocks = render_parent_service_change_notes(planned_changes)
    if parent_note_blocks:
        lines.append("")
        lines.append("Parent service changes:")
        lines.extend(parent_note_blocks)
    return "\n".join(lines)


def render_tag_note(note: dict[str, Any], indent: int = 0) -> list[str]:
    prefix = "  " * indent
    repo = note.get("repo") or "unknown repo"
    tag = note.get("tag") or "unknown tag"
    lines = [f"{prefix}- {repo}:{tag}"]
    lines.extend(render_tag_note_details(note, indent + 1))
    return lines


def render_tag_note_details(note: dict[str, Any], indent: int = 0) -> list[str]:
    prefix = "  " * indent
    lines: list[str] = []
    message = str(note.get("message") or note.get("reason") or "").strip()
    if message:
        for message_line in message.splitlines():
            lines.append(f"{prefix}{message_line}")
    for child in note.get("base_changes") or []:
        lines.extend(render_tag_note(child, indent))
    return lines


def render_image_change_notes(planned_changes: list[dict[str, Any]]) -> list[str]:
    lines: list[str] = []
    grouped: dict[tuple[str, str], dict[str, Any]] = {}
    for change in planned_changes:
        if change.get("change_type") != "image_tag":
            continue
        image = str(change.get("image") or "")
        image_version = str(change.get("image_version") or "")
        target = str(change.get("after") or "")
        notes = change.get("image_change_notes") or []
        note = notes[0] if notes else None
        group_key = (
            str(note.get("repo") or image) if note else image,
            str(note.get("tag") or target) if note else target,
        )
        group = grouped.setdefault(
            group_key,
            {
                "image": image,
                "note": note,
                "updates": [],
            },
        )
        group["updates"].append(
            {
                "version": image_version,
                "before": format_diff_value(change.get("before")),
                "after": format_diff_value(change.get("after")),
            }
        )

    for group in grouped.values():
        note = group.get("note")
        if not note:
            continue
        lines.append(f"- {note.get('repo')}:{note.get('tag')}")
        lines.append("  Versions updated:")
        for item in group["updates"]:
            lines.append(f"  - {item['version']}: {item['before']} -> {item['after']}")
        lines.append("  Changes:")
        lines.extend(render_tag_note_details(note, 2))
    return lines


def render_parent_service_change_notes(planned_changes: list[dict[str, Any]]) -> list[str]:
    lines: list[str] = []
    grouped: dict[tuple[str, str], dict[str, Any]] = {}
    for change in planned_changes:
        if change.get("change_type") != "parent_service_version":
            continue
        parent_repo = str(change.get("parent_repo") or "unknown parent")
        parent_tag = str(change.get("parent_tag") or str(change.get("after") or "").lstrip("^"))
        notes = change.get("parent_change_notes") or []
        note = notes[0] if notes else None
        group_key = (
            str(note.get("repo") or parent_repo) if note else parent_repo,
            str(note.get("tag") or parent_tag) if note else parent_tag,
        )
        group = grouped.setdefault(
            group_key,
            {
                "repo": group_key[0],
                "tag": group_key[1],
                "note": note,
                "updates": [],
            },
        )
        group["updates"].append(
            {
                "file": str(change.get("file") or "service.yml"),
                "before": format_diff_value(change.get("before")),
                "after": format_diff_value(change.get("after")),
            }
        )

    for group in grouped.values():
        lines.append(f"- {group['repo']}:{group['tag']}")
        lines.append("  Resolved parent versions updated:")
        for item in group["updates"]:
            lines.append(f"  - {item['file']}: {item['before']} -> {item['after']}")
        note = group.get("note")
        if note:
            lines.append("  Changes:")
            lines.extend(render_tag_note_details(note, 2))
    return lines


def build_planned_release(repo: str, tags: set[str], planned_changes: list[dict[str, Any]]) -> dict[str, Any]:
    latest_tag = latest_stable_semver_tag(tags)
    if latest_tag is None:
        return {
            "status": "blocked",
            "reason": "no existing stable semantic git tag was found; patch tag cannot be calculated",
            "previous_tag": None,
            "tag": None,
            "title": None,
            "description": None,
            "commands": [],
        }

    previous_tag, prefix, previous_version = latest_tag
    next_tag = f"{prefix}{previous_version.major}.{previous_version.minor}.{previous_version.micro + 1}"
    description = render_release_description(repo, previous_tag, next_tag, planned_changes)
    return {
        "status": "planned",
        "reason": None,
        "previous_tag": previous_tag,
        "tag": next_tag,
        "title": next_tag,
        "description": description,
        "commands": [
            f"git tag -a {next_tag} -F release-notes.md",
            f"git push origin {next_tag}",
        ],
    }


@dataclass
class RepoResult:
    repo: str
    has_image: bool
    has_helm: bool
    has_options: bool
    expects_image: bool
    expects_helm: bool
    expects_options: bool
    comparable: bool
    external: bool
    service_type: str
    updates: list[str]
    current: list[str]
    warnings: list[str]
    notifications: list[str]
    notification_groups: dict[str, list[str]]
    eol_updates: list[str]
    major_updates: list[str]
    planned_changes: list[dict[str, Any]]
    planned_diffs: list[str]
    dry_run_changes: list[dict[str, Any]]
    dry_run_diffs: list[str]
    updates_without_local_diff: list[str]
    build_template_current: list[str]
    build_template_review_items: list[str]
    build_template_warnings: list[str]
    build_template_checks: list[dict[str, Any]]
    planned_release: dict[str, Any] | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "repo": self.repo,
            "has_image": self.has_image,
            "has_helm": self.has_helm,
            "has_options": self.has_options,
            "expects_image": self.expects_image,
            "expects_helm": self.expects_helm,
            "expects_options": self.expects_options,
            "comparable": self.comparable,
            "external": self.external,
            "service_type": self.service_type,
            "updates": self.updates,
            "current": self.current,
            "warnings": self.warnings,
            "notifications": self.notifications,
            "notification_groups": compact_notification_groups(self.notification_groups),
            "eol_updates": self.eol_updates,
            "major_updates": self.major_updates,
            "planned_changes": self.planned_changes,
            "planned_diffs": self.planned_diffs,
            "dry_run_changes": self.dry_run_changes,
            "dry_run_diffs": self.dry_run_diffs,
            "updates_without_local_diff": self.updates_without_local_diff,
            "build_template_current": self.build_template_current,
            "build_template_review_items": self.build_template_review_items,
            "build_template_warnings": self.build_template_warnings,
            "build_template_checks": self.build_template_checks,
            "planned_release": self.planned_release,
        }


class UpdateReportGenerator:
    def __init__(self, owner: str) -> None:
        self.owner = owner
        self.session = requests.Session()
        self.session.headers["Accept"] = "application/vnd.github+json"
        self.github_headers: dict[str, str] = {"Accept": "application/vnd.github+json"}
        token = (
            os.environ.get("WODBOT_GITHUB_PAT")
            or os.environ.get("WODBY_GITHUB_TOKEN")
            or os.environ.get("GITHUB_TOKEN")
            or os.environ.get("GH_TOKEN")
        )
        if token:
            self.github_headers["Authorization"] = f"Bearer {token}"
        self._content_cache: dict[tuple[str, str, str], str | None] = {}
        self._github_file_cache: dict[tuple[str, str, str, str | None], str | None] = {}
        self._github_ref_cache: dict[tuple[str, str, str], bool] = {}
        self._github_tags_cache: dict[tuple[str, str], set[str]] = {}
        self._github_tag_note_cache: dict[tuple[str, str, str], dict[str, Any] | None] = {}
        self._ref_file_cache: dict[tuple[str, str, str], str | None] = {}
        self._base_image_repo_cache: dict[tuple[str, str, str | None], str | None] = {}
        self._image_change_notes_cache: dict[tuple[str, str | None, str, str | None], list[dict[str, Any]]] = {}
        self._registry_tags_cache: dict[tuple[str, str], list[str]] = {}
        self._http_cache: dict[tuple[str, tuple[tuple[str, str], ...]], requests.Response] = {}
        self._helm_index_cache: dict[str, dict[str, Any]] = {}
        self._service_data_cache: dict[tuple[str, str], dict[str, Any] | None] = {}
        self._service_data_at_ref_cache: dict[tuple[str, str, str], dict[str, Any] | None] = {}
        self._wodby_chart_cache: dict[str, str] = {}
        self._wodby_chart_values_cache: dict[str, dict[str, Any]] = {}
        self._eol_product_index_cache: dict[str, str] | None = None
        self._eol_product_cache: dict[str, dict[str, Any] | None] = {}

    def git_ls_remote(self, *args: str) -> str:
        process = subprocess.run(
            ["git", "ls-remote", *args],
            capture_output=True,
            text=True,
            timeout=60,
        )
        if process.returncode != 0:
            raise RuntimeError(process.stderr.strip() or process.stdout.strip())
        return process.stdout

    def fetch(self, url: str, headers: dict[str, str] | None = None, *, timeout: int = 60) -> requests.Response:
        cache_key = (url, tuple(sorted((headers or {}).items())))
        if cache_key not in self._http_cache:
            response = self.session.get(url, headers=headers, timeout=timeout)
            response.raise_for_status()
            self._http_cache[cache_key] = response
        return self._http_cache[cache_key]

    def get_raw_github_file(self, owner: str, repo: str, path: str, ref: str | None = None) -> str | None:
        refs = [ref] if ref else ["main", "master"]
        for candidate_ref in refs:
            url = (
                f"https://raw.githubusercontent.com/{owner}/{repo}/"
                f"{quote(str(candidate_ref), safe='/')}/{quote(path, safe='/')}"
            )
            response = self.session.get(url, timeout=60)
            if response.status_code == 200:
                return response.text
            if response.status_code not in (404, 403):
                response.raise_for_status()
        return None

    def get_raw_repo_file(self, repo: str, path: str) -> str | None:
        return self.get_raw_github_file(self.owner, repo, path)

    def get_github_file(self, owner: str, repo: str, path: str, ref: str | None = None) -> str | None:
        cache_key = (owner, repo, path, ref)
        if cache_key in self._github_file_cache:
            return self._github_file_cache[cache_key]

        encoded_path = quote(path, safe="/")
        url = GITHUB_CONTENTS_URL.format(owner=owner, repo=repo, path=encoded_path)
        if ref:
            url = f"{url}?ref={quote(ref, safe='')}"
        response = self.session.get(url, headers=self.github_headers, timeout=60)
        if response.status_code in (401, 403, 404):
            content = self.get_raw_github_file(owner, repo, path, ref)
            self._github_file_cache[cache_key] = content
            return content
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, dict) or payload.get("type") == "dir":
            self._github_file_cache[cache_key] = None
            return None
        content = base64.b64decode(payload["content"]).decode("utf-8")
        self._github_file_cache[cache_key] = content
        return content

    def get_repo_file(self, repo: str, path: str) -> str | None:
        cache_key = (self.owner, repo, path)
        if cache_key in self._content_cache:
            return self._content_cache[cache_key]
        content = self.get_github_file(self.owner, repo, path)
        self._content_cache[cache_key] = content
        return content

    def github_ref_exists(self, owner: str, repo: str, ref_path: str) -> bool:
        cache_key = (owner, repo, ref_path)
        if cache_key in self._github_ref_cache:
            return self._github_ref_cache[cache_key]

        url = GITHUB_REF_URL.format(owner=owner, repo=repo, ref_path=quote(ref_path, safe="/"))
        response = self.session.get(url, headers=self.github_headers, timeout=60)
        if response.status_code == 200:
            self._github_ref_cache[cache_key] = True
            return True
        if response.status_code == 404:
            self._github_ref_cache[cache_key] = False
            return False
        if response.status_code in (401, 403):
            output = self.git_ls_remote(f"https://github.com/{owner}/{repo}.git", f"refs/{ref_path}")
            exists = bool(output.strip())
            self._github_ref_cache[cache_key] = exists
            return exists
        response.raise_for_status()
        self._github_ref_cache[cache_key] = False
        return False

    def get_service_data(self, repo: str, manifest_path: str = "service.yml") -> dict[str, Any] | None:
        cache_key = (repo, manifest_path)
        if cache_key in self._service_data_cache:
            return self._service_data_cache[cache_key]

        service_text = self.get_repo_file(repo, manifest_path)
        if service_text is None:
            self._service_data_cache[cache_key] = None
            return None

        service_data = yaml.safe_load(service_text) or {}
        if not isinstance(service_data, dict):
            raise RuntimeError(f"{repo} {manifest_path} did not decode to a mapping")

        self._service_data_cache[cache_key] = service_data
        return service_data

    def get_service_data_at_ref(
        self,
        repo: str,
        ref: str,
        manifest_path: str = "service.yml",
    ) -> dict[str, Any] | None:
        cache_key = (repo, ref, manifest_path)
        if cache_key in self._service_data_at_ref_cache:
            return self._service_data_at_ref_cache[cache_key]

        service_text = self.get_repo_file_at_ref(repo, ref, manifest_path)
        if service_text is None:
            self._service_data_at_ref_cache[cache_key] = None
            return None

        service_data = yaml.safe_load(service_text) or {}
        if not isinstance(service_data, dict):
            raise RuntimeError(f"{repo} {manifest_path} at {ref} did not decode to a mapping")

        self._service_data_at_ref_cache[cache_key] = service_data
        return service_data

    def get_repo_manifest_paths(self, repo: str) -> list[str]:
        root_manifest = self.get_service_data(repo, "service.yml")
        if root_manifest is not None:
            return ["service.yml"]

        index_text = self.get_repo_file(repo, "index.yml")
        if index_text is None:
            return []

        index_data = yaml.safe_load(index_text) or {}
        if not isinstance(index_data, dict):
            raise RuntimeError(f"{repo} index.yml did not decode to a mapping")

        paths = []
        for service_path in index_data.get("services") or []:
            service_dir = str(service_path).strip().strip("/")
            if not service_dir:
                continue
            paths.append(f"{service_dir}/service.yml")
        return paths

    @staticmethod
    def service_name_to_repo(service_name: str) -> str:
        if service_name.startswith("service-"):
            return service_name
        return f"service-{service_name}"

    def check_build_file_reference(
        self,
        repo: str,
        manifest_path: str,
        key: str,
        value: Any,
        prefix: str,
    ) -> dict[str, Any]:
        path = manifest_relative_path(manifest_path, value)
        message_path = f"`{path}`"
        check = {
            "kind": "build_file",
            "manifest": manifest_path,
            "field": f"build.{key}",
            "file": path,
        }
        if not path:
            message = f"{prefix}`build.{key}` is empty"
            return {**check, "status": "warning", "message": message}

        if self.get_repo_file(repo, path) is None:
            message = f"{prefix}`build.{key}` references missing file {message_path}"
            return {**check, "status": "missing", "message": message}

        message = f"{prefix}`build.{key}` file {message_path} exists"
        return {**check, "status": "current", "message": message}

    def check_build_template_entry(
        self,
        template: dict[str, Any],
        manifest_path: str,
        prefix: str,
    ) -> dict[str, Any]:
        name = str(template.get("name") or "unnamed").strip() or "unnamed"
        repo_url = str(template.get("repo") or "").strip()
        title = str(template.get("title") or name).strip()
        check: dict[str, Any] = {
            "kind": "build_template",
            "manifest": manifest_path,
            "name": name,
            "title": title,
            "repo": repo_url,
        }

        parsed_repo = parse_github_repo_url(repo_url)
        if parsed_repo is None:
            message = f"{prefix}build template `{name}` has unsupported repo `{repo_url or 'missing'}`"
            return {**check, "status": "warning", "message": message}

        template_owner, template_repo = parsed_repo
        check.update({"owner": template_owner, "repo_name": template_repo})
        branch = str(template.get("branch") or "").strip()
        tag = str(template.get("tag") or "").strip()
        pipeline = str(template.get("pipeline") or "").strip()

        if branch and tag:
            message = f"{prefix}build template `{name}` defines both `branch` and `tag`; manual review required"
            return {**check, "status": "warning", "message": message}

        ref_for_pipeline: str | None = None
        messages: list[str] = []
        status = "current"

        if branch:
            check.update({"ref_type": "branch", "ref": branch})
            if self.github_ref_exists(template_owner, template_repo, f"heads/{branch}"):
                messages.append(f"{prefix}build template `{name}` branch `{branch}` exists in `{repo_url}`")
                ref_for_pipeline = branch
            else:
                message = f"{prefix}build template `{name}` branch `{branch}` was not found in `{repo_url}`"
                return {**check, "status": "missing", "message": message}
        elif tag:
            check.update({"ref_type": "tag", "ref": tag})
            tags = self.get_github_tags(template_owner, template_repo)
            major_match = MAJOR_CONSTRAINT_RE.match(tag)
            caret_base = parse_caret_constraint(tag)
            latest_overall = latest_source_tag(tags)

            if major_match:
                wanted_major = int(major_match.group("major"))
                latest_major = latest_matching_major_tag(tags, wanted_major)
                if latest_major is None:
                    message = (
                        f"{prefix}build template `{name}` has tag constraint `{tag}`, "
                        f"but no matching stable tag was found in `{repo_url}`"
                    )
                    return {**check, "status": "missing", "message": message}
                latest_tag, latest_version = latest_major
                messages.append(
                    f"{prefix}build template `{name}` tag constraint `{tag}` resolves to `{latest_tag}`"
                )
                check.update({"resolved_tag": latest_tag})
                ref_for_pipeline = latest_tag
                if latest_overall and latest_overall[1].major > latest_version.major:
                    overall_tag, _overall_version = latest_overall
                    messages.append(
                        f"{prefix}new major build template tag `{overall_tag}` is available for `{name}` "
                        f"outside constraint `{tag}`; manual review required"
                    )
                    status = "update_available"
            elif caret_base is not None:
                latest = latest_matching_caret_tag(tags, caret_base)
                if latest is None:
                    message = (
                        f"{prefix}build template `{name}` has tag constraint `{tag}`, "
                        f"but no matching stable tag was found in `{repo_url}`"
                    )
                    return {**check, "status": "missing", "message": message}
                latest_tag, latest_version = latest
                messages.append(
                    f"{prefix}build template `{name}` tag constraint `{tag}` resolves to `{latest_tag}`"
                )
                check.update({"resolved_tag": latest_tag})
                ref_for_pipeline = latest_tag
                if latest_overall and latest_overall[1] >= caret_upper_bound(caret_base):
                    overall_tag, _overall_version = latest_overall
                    messages.append(
                        f"{prefix}new major build template tag `{overall_tag}` is available for `{name}` "
                        f"outside constraint `{tag}`; manual review required"
                    )
                    status = "update_available"
            else:
                parsed_current = parse_source_version(tag)
                if tag not in tags:
                    message = f"{prefix}build template `{name}` tag `{tag}` was not found in `{repo_url}`"
                    return {**check, "status": "missing", "message": message}
                messages.append(f"{prefix}build template `{name}` tag `{tag}` exists in `{repo_url}`")
                ref_for_pipeline = tag
                if parsed_current is not None and latest_overall and latest_overall[1] > parsed_current:
                    latest_tag, latest_version = latest_overall
                    if latest_version.major > parsed_current.major:
                        messages.append(
                            f"{prefix}new major build template tag `{latest_tag}` is available for `{name}` "
                            f"(current: `{tag}`); manual review required"
                        )
                    else:
                        messages.append(
                            f"{prefix}new build template tag `{latest_tag}` is available for `{name}` "
                            f"(current: `{tag}`); manual review required"
                        )
                    status = "update_available"
        else:
            message = f"{prefix}build template `{name}` has neither `branch` nor `tag`; ref cannot be evaluated"
            return {**check, "status": "warning", "message": message}

        if pipeline:
            check["pipeline"] = pipeline
            if ref_for_pipeline is None:
                messages.append(
                    f"{prefix}build template `{name}` pipeline `{pipeline}` could not be checked without a resolved ref"
                )
                if status == "current":
                    status = "warning"
            elif self.get_github_file(template_owner, template_repo, pipeline, ref_for_pipeline) is None:
                messages.append(
                    f"{prefix}build template `{name}` pipeline `{pipeline}` was not found at `{ref_for_pipeline}`"
                )
                status = "warning" if status == "current" else status
            else:
                messages.append(
                    f"{prefix}build template `{name}` pipeline `{pipeline}` exists at `{ref_for_pipeline}`"
                )

        return {**check, "status": status, "messages": messages, "message": messages[-1] if messages else ""}

    def check_build_templates(
        self,
        repo: str,
        manifest_path: str,
        service_data: dict[str, Any],
        prefix: str,
    ) -> dict[str, Any]:
        result: dict[str, Any] = {
            "current": [],
            "updates": [],
            "warnings": [],
            "checks": [],
        }
        build = service_data.get("build")
        if not isinstance(build, dict):
            return result

        for key in ("dockerfile", "dockerignore"):
            if build.get(key) is None:
                continue
            check = self.check_build_file_reference(repo, manifest_path, key, build.get(key), prefix)
            result["checks"].append(check)
            if check["status"] == "current":
                result["current"].append(check["message"])
            else:
                result["warnings"].append(check["message"])

        templates = build.get("templates") or []
        if templates and not isinstance(templates, list):
            result["warnings"].append(f"{prefix}`build.templates` is not a list")
            result["checks"].append(
                {
                    "kind": "build_template",
                    "manifest": manifest_path,
                    "status": "warning",
                    "message": f"{prefix}`build.templates` is not a list",
                }
            )
            return result

        for template in templates:
            if not isinstance(template, dict):
                message = f"{prefix}`build.templates` contains a non-mapping entry"
                result["warnings"].append(message)
                result["checks"].append(
                    {
                        "kind": "build_template",
                        "manifest": manifest_path,
                        "status": "warning",
                        "message": message,
                    }
                )
                continue
            check = self.check_build_template_entry(template, manifest_path, prefix)
            result["checks"].append(check)
            messages = check.get("messages") or [check.get("message")]
            messages = [str(message) for message in messages if message]
            if check["status"] == "current":
                result["current"].extend(messages)
            elif check["status"] == "update_available":
                warning_messages = [
                    message
                    for message in messages
                    if " was not found " in message or "could not be checked" in message
                ]
                current_messages = [
                    message
                    for message in messages
                    if "manual review required" not in message and message not in warning_messages
                ]
                update_messages = [message for message in messages if "manual review required" in message]
                result["current"].extend(current_messages)
                result["updates"].extend(update_messages or messages)
                result["warnings"].extend(warning_messages)
            else:
                result["warnings"].extend(messages)

        return result

    def get_github_tags(self, owner: str, repo: str) -> set[str]:
        cache_key = (owner, repo)
        if cache_key in self._github_tags_cache:
            return self._github_tags_cache[cache_key]

        tags: set[str] = set()
        page = 1
        while True:
            url = GITHUB_TAGS_URL.format(owner=owner, repo=repo, page=page)
            response = self.session.get(url, headers=self.github_headers, timeout=60)
            if response.status_code in (401, 403):
                output = self.git_ls_remote("--tags", "--refs", f"https://github.com/{owner}/{repo}.git")
                tags = {
                    line.split("refs/tags/", 1)[1]
                    for line in output.splitlines()
                    if "refs/tags/" in line
                }
                self._github_tags_cache[cache_key] = tags
                return tags
            response.raise_for_status()
            payload = response.json()
            if not payload:
                break
            tags.update(item["name"] for item in payload)
            if len(payload) < 100:
                break
            page += 1

        self._github_tags_cache[cache_key] = tags
        return tags

    def check_parent_service_version(
        self,
        parent_name: str,
        from_version_constraint: Any,
        from_version: Any,
        prefix: str,
        manifest_path: str = "service.yml",
    ) -> dict[str, Any]:
        result: dict[str, Any] = {
            "updates": [],
            "current": [],
            "warnings": [],
            "notifications": [],
            "notification_groups": empty_notification_groups(),
            "major_updates": [],
            "planned_changes": [],
            "comparable": False,
        }
        parent_repo = self.service_name_to_repo(str(parent_name))
        current_constraint = str(from_version_constraint or "").strip()
        current_version = str(from_version or "").strip()
        if not current_constraint:
            result["warnings"].append(
                f"{prefix}parent service `{parent_repo}` is set via `from`, but `fromVersionConstraint` is missing"
            )
            return result
        if not current_version:
            result["warnings"].append(
                f"{prefix}parent service `{parent_repo}` is set via `from`, but exact `fromVersion` is missing"
            )
            return result

        base_version = parse_caret_constraint(current_constraint)
        if base_version is None:
            result["warnings"].append(
                f"{prefix}`fromVersionConstraint` `{current_constraint}` is not a supported caret semver constraint"
            )
            return result

        current_parsed = parse_version(current_version)
        if current_parsed is None or current_parsed.is_prerelease:
            result["warnings"].append(
                f"{prefix}`fromVersion` `{current_version}` is not a supported stable semver tag"
            )
            return result
        if not (base_version <= current_parsed < caret_upper_bound(base_version)):
            result["warnings"].append(
                f"{prefix}`fromVersion` `{current_version}` is outside `fromVersionConstraint` `{current_constraint}`"
            )
            return result

        result["comparable"] = True
        tags = self.get_github_tags(self.owner, parent_repo)
        latest = latest_matching_caret_tag(tags, base_version)
        if latest is None:
            result["warnings"].append(
                f"{prefix}no stable `{parent_repo}` git tag matched `fromVersionConstraint` `{current_constraint}`"
            )
            return result

        latest_tag, latest_version = latest
        overall_latest = semver_tag_candidates(tags)
        if overall_latest:
            overall_version, overall_tag, _overall_prefix = overall_latest[0]
            if overall_version >= caret_upper_bound(base_version):
                message = (
                    f"{prefix}new major parent service `{parent_repo}` tag `{overall_tag}` is available "
                    f"outside `fromVersionConstraint` `{current_constraint}`; manual review required"
                )
                result["major_updates"].append(message)
                add_grouped_notification(result, "major_version", message)

        if current_version == latest_tag:
            result["current"].append(
                f"{prefix}parent service `{parent_repo}` latest compatible tag is current "
                f"(`{current_version}`, constraint: `{current_constraint}`)"
            )
            return result

        message = (
            f"{prefix}updating parent service `{parent_repo}` from `{current_version}` to `{latest_tag}` "
            f"(constraint: `{current_constraint}`)"
        )
        try:
            parent_change_notes = [self.build_wodby_tag_note_tree(parent_repo, latest_tag, None)]
        except Exception as exc:
            parent_change_notes = [
                {
                    "repo": f"{self.owner}/{parent_repo}",
                    "tag": latest_tag,
                    "message": f"Parent service tag description lookup failed: {exc}",
                    "url": f"https://github.com/{self.owner}/{parent_repo}/releases/tag/{latest_tag}",
                    "base_changes": [],
                }
            ]
        result["updates"].append(message)
        result["planned_changes"].append(
            make_planned_change(
                manifest_path,
                "fromVersion",
                "fromVersion",
                current_version,
                latest_tag,
                message,
                {
                    "change_type": "parent_service_version",
                    "parent_repo": parent_repo,
                    "parent_tag": latest_tag,
                    "from_version_constraint": current_constraint,
                    "parent_change_notes": parent_change_notes,
                    "service_label": prefix.strip("[] ") if prefix else "",
                },
            )
        )
        return result

    def get_github_tag_note(self, owner: str, repo: str, tag: str) -> dict[str, Any] | None:
        cache_key = (owner, repo, tag)
        if cache_key in self._github_tag_note_cache:
            return self._github_tag_note_cache[cache_key]

        encoded_tag = quote(tag, safe="")
        url = f"https://api.github.com/repos/{owner}/{repo}/git/ref/tags/{encoded_tag}"
        response = self.session.get(url, headers=self.github_headers, timeout=60)
        if response.status_code == 404:
            self._github_tag_note_cache[cache_key] = None
            return None
        response.raise_for_status()

        ref_data = response.json()
        ref_object = ref_data.get("object") or {}
        note = {
            "repo": f"{owner}/{repo}",
            "tag": tag,
            "message": "",
            "url": f"https://github.com/{owner}/{repo}/releases/tag/{tag}",
            "base_changes": [],
        }
        if ref_object.get("type") == "tag":
            tag_response = self.session.get(
                f"https://api.github.com/repos/{owner}/{repo}/git/tags/{ref_object['sha']}",
                headers=self.github_headers,
                timeout=60,
            )
            tag_response.raise_for_status()
            tag_data = tag_response.json()
            note["message"] = str(tag_data.get("message") or "").strip()
        else:
            note["message"] = "Tag is lightweight; no tag description was found."

        self._github_tag_note_cache[cache_key] = note
        return note

    def get_repo_file_at_ref(self, repo: str, ref: str, path: str) -> str | None:
        cache_key = (repo, ref, path)
        if cache_key in self._ref_file_cache:
            return self._ref_file_cache[cache_key]

        url = f"https://raw.githubusercontent.com/{self.owner}/{repo}/{quote(ref, safe='')}/{path}"
        response = self.session.get(url, timeout=60)
        if response.status_code == 404:
            self._ref_file_cache[cache_key] = None
            return None
        response.raise_for_status()
        self._ref_file_cache[cache_key] = response.text
        return response.text

    def find_base_image_repo(self, repo: str, tag: str, image_version: str | None) -> str | None:
        cache_key = (repo, tag, image_version)
        if cache_key in self._base_image_repo_cache:
            return self._base_image_repo_cache[cache_key]

        dockerfile_paths = ["Dockerfile"]
        if image_version:
            image_major = image_version.split(".", 1)[0]
            dockerfile_paths.extend([f"{image_version}/Dockerfile", f"{image_major}/Dockerfile"])

        for path in dockerfile_paths:
            dockerfile = self.get_repo_file_at_ref(repo, tag, path)
            if not dockerfile:
                continue
            match = FROM_WODBY_IMAGE_RE.search(dockerfile)
            if match:
                base_repo = match.group("repo")
                self._base_image_repo_cache[cache_key] = base_repo
                return base_repo

        readme = self.get_repo_file_at_ref(repo, tag, "README.md")
        if readme:
            match = README_BASE_IMAGE_RE.search(readme)
            if match:
                base_repo = match.group("repo")
                self._base_image_repo_cache[cache_key] = base_repo
                return base_repo

        self._base_image_repo_cache[cache_key] = None
        return None

    def build_wodby_tag_note_tree(
        self,
        repo: str,
        tag: str,
        image_version: str | None,
        *,
        depth: int = 0,
        seen: frozenset[tuple[str, str]] = frozenset(),
    ) -> dict[str, Any]:
        key = (repo, tag)
        if key in seen:
            return {
                "repo": f"{self.owner}/{repo}",
                "tag": tag,
                "message": "Tag note traversal stopped because a base-image cycle was detected.",
                "url": f"https://github.com/{self.owner}/{repo}/releases/tag/{tag}",
                "base_changes": [],
            }

        note = self.get_github_tag_note(self.owner, repo, tag)
        if note is None:
            return {
                "repo": f"{self.owner}/{repo}",
                "tag": tag,
                "message": "Tag description was not found.",
                "url": f"https://github.com/{self.owner}/{repo}/releases/tag/{tag}",
                "base_changes": [],
            }

        note = copy.deepcopy(note)
        if depth >= 4:
            return note

        message = str(note.get("message") or "").strip()
        match = BASE_IMAGE_UPDATE_RE.match(message)
        if not match:
            return note

        base_repo = self.find_base_image_repo(repo, tag, image_version)
        if base_repo is None:
            note["base_changes"] = [
                {
                    "repo": "unknown base image",
                    "tag": match.group("tag"),
                    "message": f"Base image repo could not be resolved for wodby/{repo}:{tag}.",
                    "url": "",
                    "base_changes": [],
                }
            ]
            return note

        note["base_changes"] = [
            self.build_wodby_tag_note_tree(
                base_repo,
                match.group("tag"),
                None,
                depth=depth + 1,
                seen=seen | {key},
            )
        ]
        return note

    def get_image_change_notes(
        self,
        image: str,
        previous_tag: str | None,
        target_tag: str,
        image_version: str | None,
    ) -> list[dict[str, Any]]:
        cache_key = (image, previous_tag, target_tag, image_version)
        if cache_key in self._image_change_notes_cache:
            return copy.deepcopy(self._image_change_notes_cache[cache_key])

        if not self.is_owner_image(image):
            self._image_change_notes_cache[cache_key] = []
            return []

        target_match = WODBY_TAG_RE.match(target_tag)
        stability_tag = target_match.group("stability") if target_match else None
        if stability_tag is None:
            self._image_change_notes_cache[cache_key] = []
            return []

        _registry, image_repo = self.image_repository(image)
        repo = image_repo.split("/", 1)[1]
        try:
            notes = [self.build_wodby_tag_note_tree(repo, stability_tag, image_version)]
        except Exception as exc:
            notes = [
                {
                    "repo": f"{self.owner}/{repo}",
                    "tag": stability_tag,
                    "message": f"Image tag change notes lookup failed: {exc}",
                    "url": f"https://github.com/{self.owner}/{repo}/releases/tag/{stability_tag}",
                    "base_changes": [],
                }
            ]
        self._image_change_notes_cache[cache_key] = copy.deepcopy(notes)
        return notes

    def get_dockerhub_tags(self, repo: str) -> list[str]:
        cache_key = ("dockerhub", repo)
        if cache_key in self._registry_tags_cache:
            return self._registry_tags_cache[cache_key]
        token = self.fetch(DOCKER_TOKEN_URL.format(repo=repo)).json()["token"]
        return self.get_registry_v2_tags(
            "registry-1.docker.io",
            repo,
            headers={"Authorization": f"Bearer {token}"},
            cache_key=cache_key,
        )

    def get_ghcr_tags(self, repo: str) -> list[str]:
        cache_key = ("ghcr", repo)
        if cache_key in self._registry_tags_cache:
            return self._registry_tags_cache[cache_key]
        token = self.fetch(GHCR_TOKEN_URL.format(repo=repo)).json()["token"]
        return self.get_registry_v2_tags(
            "ghcr.io",
            repo,
            headers={"Authorization": f"Bearer {token}"},
            cache_key=cache_key,
        )

    @staticmethod
    def bearer_challenge_params(header: str) -> dict[str, str]:
        if not header.lower().startswith("bearer "):
            return {}
        return {key: value for key, value in re.findall(r'(\w+)="([^"]*)"', header)}

    def registry_bearer_token(self, registry: str, repo: str, challenge: str) -> str | None:
        params = self.bearer_challenge_params(challenge)
        realm = params.get("realm")
        if not realm:
            return None

        token_params = {}
        if params.get("service"):
            token_params["service"] = params["service"]
        token_params["scope"] = params.get("scope") or f"repository:{repo}:pull"
        response = self.session.get(realm, params=token_params, timeout=60)
        response.raise_for_status()
        payload = response.json()
        token = payload.get("token") or payload.get("access_token")
        return str(token) if token else None

    def get_registry_v2_tags(
        self,
        registry: str,
        repo: str,
        *,
        headers: dict[str, str] | None = None,
        cache_key: tuple[str, str] | None = None,
    ) -> list[str]:
        key = cache_key or (registry, repo)
        if key in self._registry_tags_cache:
            return self._registry_tags_cache[key]

        tags: list[str] = []
        url: str | None = REGISTRY_TAGS_URL.format(registry=registry, repo=repo)
        request_headers = dict(headers or {})
        while url:
            response = self.session.get(url, headers=request_headers, timeout=120)
            if response.status_code == 401:
                token = self.registry_bearer_token(registry, repo, response.headers.get("WWW-Authenticate", ""))
                if token:
                    request_headers["Authorization"] = f"Bearer {token}"
                    response = self.session.get(url, headers=request_headers, timeout=120)
            response.raise_for_status()
            tags.extend(str(tag) for tag in (response.json().get("tags") or []))
            next_url = (response.links.get("next") or {}).get("url")
            url = urljoin(url, next_url) if next_url else None

        self._registry_tags_cache[key] = tags
        return tags

    @staticmethod
    def image_repository(image: str) -> tuple[str, str]:
        reference = str(image or "").strip()
        if not reference:
            raise RuntimeError("empty image reference")

        reference = reference.split("@", 1)[0]
        parts = reference.split("/")
        if len(parts) == 1:
            repo = f"library/{parts[0]}"
            registry = "docker.io"
        else:
            first = parts[0]
            if "." in first or ":" in first or first == "localhost":
                registry = first
                repo = "/".join(parts[1:])
            else:
                registry = "docker.io"
                repo = reference

        repo_parts = repo.rsplit("/", 1)
        last_part = repo_parts[-1]
        if ":" in last_part:
            last_part = last_part.split(":", 1)[0]
            repo = "/".join(repo_parts[:-1] + [last_part]) if len(repo_parts) > 1 else last_part
        if not repo:
            raise RuntimeError(f"unable to determine image repository for {image}")
        return registry, repo

    def is_owner_image(self, image: str) -> bool:
        registry, repo = self.image_repository(image)
        return registry in ("docker.io", "registry-1.docker.io", "index.docker.io") and repo.startswith(f"{self.owner}/")

    def get_image_tags(self, image: str) -> list[str]:
        registry, repo = self.image_repository(image)
        if registry in ("docker.io", "registry-1.docker.io", "index.docker.io"):
            return self.get_dockerhub_tags(repo)
        if registry == "ghcr.io":
            return self.get_ghcr_tags(repo)
        return self.get_registry_v2_tags(registry, repo)

    def get_tailscale_stable_versions(self) -> list[str]:
        cache_key = ("tailscale-stable", "versions")
        if cache_key in self._registry_tags_cache:
            return self._registry_tags_cache[cache_key]

        text = self.fetch(TAILSCALE_STABLE_URL).text
        versions = sorted({match.group("version") for match in TAILSCALE_STABLE_OPTION_RE.finditer(text)})
        self._registry_tags_cache[cache_key] = versions
        return versions

    @staticmethod
    def resolve_fallback_version_source(repo: str, service_name: str) -> dict[str, Any] | None:
        return (
            FALLBACK_VERSION_SOURCES.get(normalize_service_key(service_name))
            or FALLBACK_VERSION_SOURCES.get(normalize_service_key(repo))
        )

    def get_fallback_version_source_values(self, source: dict[str, Any]) -> list[str] | set[str]:
        kind = str(source.get("kind") or "")
        if kind == "github_tags":
            return self.get_github_tags(str(source["owner"]), str(source["repo"]))
        if kind == "image_tags":
            return self.get_image_tags(str(source["image"]))
        if kind == "tailscale_stable":
            return self.get_tailscale_stable_versions()
        raise RuntimeError(f"unsupported fallback version source kind {kind!r}")

    def get_helm_app_version(self, source: str | None, chart: str | None, version: str | None) -> str | None:
        if not source or not chart or not version or source.startswith("oci://"):
            return None

        if source not in self._helm_index_cache:
            index_url = f"{source.rstrip('/')}/index.yaml"
            payload = yaml.safe_load(self.fetch(index_url).content.decode("utf-8", "ignore"))
            self._helm_index_cache[source] = payload

        chart_name = chart.split("/", 1)[1] if "/" in chart else chart
        entries = self._helm_index_cache[source].get("entries", {}).get(chart_name, [])
        for entry in entries:
            if str(entry.get("version")) == version and entry.get("appVersion") is not None:
                return str(entry["appVersion"])
        return None

    def get_wodby_chart_values(self, chart_name: str) -> dict[str, Any]:
        if chart_name not in self._wodby_chart_values_cache:
            url = f"https://raw.githubusercontent.com/{self.owner}/charts/main/{chart_name}/values.yaml"
            payload = yaml.safe_load(self.fetch(url).text) or {}
            if not isinstance(payload, dict):
                raise RuntimeError(f"{chart_name} values.yaml did not decode to a mapping")
            self._wodby_chart_values_cache[chart_name] = payload
        return self._wodby_chart_values_cache[chart_name]

    def get_wodby_chart_image_tag(self, chart: str | None) -> str | None:
        if not chart:
            return None
        chart_name = chart.rsplit("/", 1)[-1]
        values = self.get_wodby_chart_values(chart_name)
        image = values.get("image") if isinstance(values, dict) else None
        if isinstance(image, dict) and image.get("tag") is not None:
            return str(image["tag"])
        return None

    def configured_fallback_source_versions(
        self,
        source: dict[str, Any],
        options: list[Any],
        helm_source: str | None,
        helm_chart: str | None,
        helm_version: str | None,
    ) -> list[tuple[Version, str]]:
        current_field = str(source.get("current_field") or "tag")
        if current_field in ("tag", "version") and options:
            return configured_source_versions(options, current_field)

        current_value = None
        if current_field == "helm_version":
            current_value = helm_version
        elif current_field == "helm_app_version":
            current_value = self.get_helm_app_version(helm_source, helm_chart, helm_version)
        elif current_field == "wodby_chart_image_tag":
            current_value = self.get_wodby_chart_image_tag(helm_chart)

        parsed = parse_source_version(current_value)
        if parsed is None:
            return []
        return [(parsed, str(current_value))]

    def check_fallback_version_source(
        self,
        source: dict[str, Any],
        options: list[Any],
        prefix: str,
        helm_source: str | None = None,
        helm_chart: str | None = None,
        helm_version: str | None = None,
    ) -> dict[str, Any]:
        result = {
            "current": [],
            "major_updates": [],
            "notifications": [],
            "notification_groups": empty_notification_groups(),
            "warnings": [],
        }
        source_label = str(source.get("label") or "fallback source")
        source_name = str(source.get("source_label") or source_label)
        report_only_suffix = "; report only, no manifest update will be planned" if source.get("report_only") else ""
        latest = latest_source_version(self.get_fallback_version_source_values(source))
        if latest is None:
            result["warnings"].append(f"{prefix}no stable {source_label} versions were found in the fallback source")
            return result

        configured = self.configured_fallback_source_versions(
            source, options, helm_source, helm_chart, helm_version
        )
        if not configured:
            result["warnings"].append(f"{prefix}no configured versions could be parsed for {source_label}")
            return result

        latest_version, latest_raw = latest
        configured.sort(key=lambda item: item[0], reverse=True)
        highest_configured, highest_configured_raw = configured[0]
        comparison = str(source.get("comparison") or "major")

        if comparison == "minor_family":
            latest_family = latest_version.release[:2]
            configured_family = highest_configured.release[:2]
            if latest_family > configured_family:
                message = (
                    f"{prefix}new {source_label} version family `{latest_raw}` is available "
                    f"from {source_name} "
                    f"(highest configured family: `{version_family_label(highest_configured)}`); "
                    f"manual review required{report_only_suffix}"
                )
                add_grouped_notification(result, "major_version", message)
            else:
                result["current"].append(
                    f"{prefix}{source_label} latest version family from {source_name} is current "
                    f"(`{latest_raw}`, configured: `{highest_configured_raw}`)"
                )
            return result

        if latest_version.major > highest_configured.major:
            message = (
                f"{prefix}new {source_label} major version `{latest_raw}` is available "
                f"from {source_name} "
                f"(highest configured major: `{highest_configured.major}`); "
                f"manual review required{report_only_suffix}"
            )
            result["major_updates"].append(message)
            add_grouped_notification(result, "major_version", message)
        else:
            result["current"].append(
                f"{prefix}{source_label} latest major version from {source_name} is current "
                f"(`{latest_raw}`, configured: `{highest_configured_raw}`)"
            )

        return result

    def get_oci_tags(self, reference: str) -> list[str]:
        parsed = urlparse(reference)
        if parsed.scheme != "oci":
            raise RuntimeError(f"unsupported OCI reference {reference}")

        registry = parsed.netloc
        repo = parsed.path.lstrip("/")
        if not repo:
            raise RuntimeError(f"unable to determine OCI repository for {reference}")

        if registry in ("docker.io", "registry-1.docker.io", "index.docker.io"):
            return self.get_dockerhub_tags(repo)
        if registry == "ghcr.io":
            return self.get_ghcr_tags(repo)
        return self.get_registry_v2_tags(registry, repo)

    def latest_stable_version_tag(self, published_tags: list[str]) -> str | None:
        candidates: list[tuple[Version, str]] = []
        for tag in published_tags:
            parsed = parse_version(tag)
            if parsed is None or parsed.is_prerelease:
                continue
            candidates.append((parsed, tag))
        if not candidates:
            return None
        candidates.sort(reverse=True)
        return candidates[0][1]

    def get_helm_latest(self, source: str, chart: str) -> str | None:
        if source.startswith("oci://registry-1.docker.io/wodby/"):
            chart_name = chart.rsplit("/", 1)[-1]
            if chart_name not in self._wodby_chart_cache:
                url = WODBY_CHART_URL.format(owner=self.owner, chart=chart_name)
                payload = yaml.safe_load(self.fetch(url).text)
                self._wodby_chart_cache[chart_name] = str(payload["version"])
            return self._wodby_chart_cache[chart_name]

        if source.startswith("oci://"):
            reference = chart if chart.startswith("oci://") else source
            return self.latest_stable_version_tag(self.get_oci_tags(reference))

        if source not in self._helm_index_cache:
            index_url = f"{source.rstrip('/')}/index.yaml"
            payload = yaml.safe_load(self.fetch(index_url).content.decode("utf-8", "ignore"))
            self._helm_index_cache[source] = payload

        index = self._helm_index_cache[source]
        chart_name = chart.split("/", 1)[1] if "/" in chart else chart
        entries = index.get("entries", {}).get(chart_name, [])
        best_version: Version | None = None
        best_raw: str | None = None
        for entry in entries:
            raw = str(entry["version"])
            parsed = parse_version(raw)
            if parsed is None or parsed.is_prerelease:
                continue
            if best_version is None or parsed > best_version:
                best_version = parsed
                best_raw = raw
        return best_raw

    def helm_version_exists(self, source: str, chart: str, version: str) -> bool:
        if source.startswith("oci://"):
            reference = chart if chart.startswith("oci://") else source
            return version in self.get_oci_tags(reference)

        if source not in self._helm_index_cache:
            index_url = f"{source.rstrip('/')}/index.yaml"
            payload = yaml.safe_load(self.fetch(index_url).content.decode("utf-8", "ignore"))
            self._helm_index_cache[source] = payload

        index = self._helm_index_cache[source]
        chart_name = chart.split("/", 1)[1] if "/" in chart else chart
        entries = index.get("entries", {}).get(chart_name, [])
        return any(str(entry.get("version")) == version for entry in entries)

    def check_crd_chart_updates(
        self,
        crd_charts: Any,
        raw_crd_charts: Any,
        target_version: str,
        prefix: str,
        manifest_path: str,
        service_label: str,
    ) -> dict[str, Any]:
        result = {
            "updates": [],
            "current": [],
            "warnings": [],
            "planned_changes": [],
            "updates_without_local_diff": [],
        }
        if not crd_charts:
            return result
        if not isinstance(crd_charts, list):
            result["warnings"].append(f"{prefix}`crdCharts` is not a list and cannot be compared automatically")
            return result
        if not isinstance(raw_crd_charts, list):
            raw_crd_charts = []

        duplicate_names = duplicate_crd_chart_names(raw_crd_charts)
        if duplicate_names:
            names = ", ".join(f"`{name}`" for name in sorted(duplicate_names))
            result["warnings"].append(
                f"{prefix}duplicate `crdCharts` name entries found for {names}; "
                "automated CRD chart updates for those names are disabled"
            )
        raw_crd_chart_index = raw_crd_charts_by_name(raw_crd_charts)

        for crd_chart in crd_charts:
            if not isinstance(crd_chart, dict):
                result["warnings"].append(f"{prefix}`crdCharts` contains a non-mapping entry")
                continue

            name = str(crd_chart.get("name") or "").strip()
            chart_source = crd_chart.get("source")
            chart = crd_chart.get("chart") or chart_source
            current_version = crd_chart.get("version")
            display = name or str(chart or "unnamed CRD chart")

            if not name:
                result["warnings"].append(
                    f"{prefix}CRD Helm chart `{display}` has no `name`; automated updates are disabled"
                )
                continue
            if not chart_source or not chart or current_version is None:
                result["warnings"].append(
                    f"{prefix}CRD Helm chart `{display}` is incomplete and cannot be compared automatically"
                )
                continue

            current_version = str(current_version)
            if current_version == target_version:
                result["current"].append(
                    f"{prefix}CRD Helm chart `{display}` already matches main Helm chart target `{target_version}`"
                )
                continue

            if not self.helm_version_exists(str(chart_source), str(chart), target_version):
                result["warnings"].append(
                    f"{prefix}CRD Helm chart `{display}` does not publish version `{target_version}`; "
                    f"leaving current version `{current_version}`"
                )
                continue

            message = (
                f"{prefix}updating CRD chart `{display}` to `{target_version}` "
                f"(current: `{current_version}`)"
            )
            result["updates"].append(message)
            raw_crd_chart = raw_crd_chart_index.get(name)
            if raw_crd_chart is not None and raw_crd_chart.get("version") is not None:
                result["planned_changes"].append(
                    make_planned_change(
                        manifest_path,
                        f"crdCharts[name={name}].version",
                        "version",
                        raw_crd_chart.get("version"),
                        target_version,
                        message,
                        {
                            "change_type": "crd_helm_chart",
                            "helm_chart": chart,
                            "crd_chart": display,
                            "service_label": service_label,
                        },
                    )
                )
            else:
                result["updates_without_local_diff"].append(
                    f"{message}; no unique local `crdCharts` entry with a `version` field in `{manifest_path}`"
                )

        return result

    def latest_wodby_tag(self, wanted: str, published_tags: list[str], valid_stabilities: set[str]) -> str | None:
        def pick(exact_only: bool) -> str | None:
            candidates: list[tuple[Version | None, Version | None, str]] = []
            for tag in published_tags:
                match = WODBY_TAG_RE.match(tag)
                if not match:
                    continue
                base = match.group("base")
                stability = match.group("stability")
                if stability is None or stability not in valid_stabilities:
                    continue
                if exact_only:
                    if not exact_match(base, wanted):
                        continue
                elif not family_match(base, wanted):
                    continue
                candidates.append((parse_version(base), parse_version(stability), tag))
            if not candidates:
                return None
            candidates.sort(reverse=True)
            return candidates[0][2]

        return pick(True) or pick(False)

    def latest_external_tag(self, wanted: str, published_tags: list[str], configured: str | None) -> str | None:
        candidates: list[tuple[Version | None, int, str]] = []
        prefer_v = configured.startswith("v") if configured else None
        for tag in published_tags:
            match = EXTERNAL_TAG_RE.match(tag)
            if not match:
                continue
            base = match.group("base")
            if not family_match(base, wanted):
                continue
            candidates.append((parse_version(base), 1 if match.group("prefix") == "v" else 0, tag))
        if not candidates:
            return None

        candidates.sort(reverse=True)
        top_version = candidates[0][0]
        tied = [candidate for candidate in candidates if candidate[0] == top_version]
        if prefer_v is True:
            tied.sort(key=lambda item: (item[1] == 1, item[2]), reverse=True)
        elif prefer_v is False:
            tied.sort(key=lambda item: (item[1] == 0, item[2]), reverse=True)
        else:
            tied.sort(key=lambda item: (item[1], item[2]), reverse=True)
        return tied[0][2]

    def get_eol_product_index(self) -> dict[str, str]:
        if self._eol_product_index_cache is not None:
            return self._eol_product_index_cache

        payload = self.fetch(ENDOFLIFE_PRODUCTS_URL).json()
        index: dict[str, str] = {}
        for product in payload.get("result") or []:
            if not isinstance(product, dict) or not product.get("name"):
                continue
            name = normalize_service_key(str(product["name"]))
            index[name] = str(product["name"])
            for alias in product.get("aliases") or []:
                index[normalize_service_key(str(alias))] = str(product["name"])

        self._eol_product_index_cache = index
        return index

    def resolve_eol_product_name(self, repo: str, service_name: str) -> str | None:
        keys = [normalize_service_key(service_name), normalize_service_key(repo)]
        candidates: list[str] = []
        for key in keys:
            if not key:
                continue
            if key in EOL_PRODUCT_ALIASES:
                candidates.append(EOL_PRODUCT_ALIASES[key])
            if key.endswith("-php"):
                candidates.append("php")
            if key.endswith("-nginx"):
                candidates.append("nginx")
            if key.endswith("-httpd"):
                candidates.append("apache-http-server")
            if key.endswith("-varnish") or key.endswith("-vinyl"):
                candidates.append("vinyl-cache")
            candidates.append(key)

        product_index = self.get_eol_product_index()
        for candidate in candidates:
            product = product_index.get(normalize_service_key(candidate))
            if product:
                return product
        return None

    def get_eol_product_data(self, product_name: str) -> dict[str, Any] | None:
        if product_name in self._eol_product_cache:
            return self._eol_product_cache[product_name]

        response = self.session.get(ENDOFLIFE_PRODUCT_URL.format(product=product_name), timeout=60)
        if response.status_code == 404:
            self._eol_product_cache[product_name] = None
            return None
        response.raise_for_status()
        payload = response.json().get("result")
        if not isinstance(payload, dict):
            payload = None
        self._eol_product_cache[product_name] = payload
        return payload

    @staticmethod
    def best_eol_release(product_data: dict[str, Any], version: str) -> dict[str, Any] | None:
        releases = [release for release in product_data.get("releases") or [] if isinstance(release, dict)]
        for release in releases:
            if str(release.get("name")) == version:
                return release

        version_parts = version.split(".")
        if len(version_parts) != 1:
            return None

        candidates: list[tuple[Version | None, dict[str, Any]]] = []
        for release in releases:
            name = str(release.get("name") or "")
            if family_match(name, version):
                candidates.append((parse_version(name), release))
        if not candidates:
            return None
        candidates.sort(key=lambda item: (item[0] is not None, item[0] or Version("0")), reverse=True)
        return candidates[0][1]

    @staticmethod
    def latest_non_eol_release(product_data: dict[str, Any]) -> dict[str, Any] | None:
        candidates: list[tuple[Version | None, dict[str, Any]]] = []
        for release in product_data.get("releases") or []:
            if not isinstance(release, dict) or release.get("isEol") is True:
                continue
            parsed = parse_version(str(release.get("name") or ""))
            if parsed is None:
                continue
            candidates.append((parsed, release))
        if not candidates:
            return None
        candidates.sort(key=lambda item: item[0], reverse=True)
        return candidates[0][1]

    def check_eol_options(
        self,
        repo: str,
        service_name: str,
        service_type: str,
        options: list[Any],
        raw_options: list[Any],
        prefix: str,
        manifest_path: str,
    ) -> dict[str, Any]:
        result = {
            "updates": [],
            "current": [],
            "major_updates": [],
            "notifications": [],
            "notification_groups": empty_notification_groups(),
            "warnings": [],
            "planned_changes": [],
        }
        if not options:
            return result

        configured_versions = [
            str(option.get("version"))
            for option in options
            if isinstance(option, dict) and option.get("version") is not None
        ]
        if not configured_versions:
            return result

        should_notify_missing_eol_support = service_type != "infrastructure"
        fallback_source = self.resolve_fallback_version_source(repo, service_name)

        def add_missing_version_source_notification() -> None:
            if not should_notify_missing_eol_support or fallback_source is not None:
                return
            versions = ", ".join(f"`{version}`" for version in configured_versions)
            add_grouped_notification(
                result,
                "missing_version_source",
                f"{prefix}no custom version-check source is configured for `{service_name}`; "
                f"manually review new major versions for configured versions: {versions}",
            )

        def add_missing_eol_support_notification(message: str) -> None:
            if not should_notify_missing_eol_support:
                return
            versions = ", ".join(f"`{version}`" for version in configured_versions)
            add_grouped_notification(
                result,
                "missing_eol",
                f"{prefix}{message}; manually review EOL dates for configured versions: {versions}",
            )

        product_name = self.resolve_eol_product_name(repo, service_name)
        if product_name is None:
            add_missing_version_source_notification()
            add_missing_eol_support_notification(
                f"no endoflife.date product support was found for `{service_name}`"
            )
            return result

        product_data = self.get_eol_product_data(product_name)
        if product_data is None:
            add_missing_eol_support_notification(
                f"endoflife.date product data could not be loaded for `{product_name}`"
            )
            return result

        product_label = str(product_data.get("label") or product_name)
        updateable_options = raw_options_by_version(raw_options)

        for option in options:
            if not isinstance(option, dict) or option.get("version") is None:
                continue
            version = str(option.get("version"))
            release = self.best_eol_release(product_data, version)
            if release is None:
                message = (
                    f"no endoflife.date release cycle was found for {product_label} version `{version}`"
                )
                if should_notify_missing_eol_support:
                    add_grouped_notification(result, "missing_eol", f"{prefix}{message}; manually review EOL status")
                else:
                    result["warnings"].append(f"{prefix}{message}")
                continue

            eol_from = release.get("eolFrom")
            if isinstance(eol_from, str) and ISO_DATE_RE.match(eol_from):
                raw_option = updateable_options.get(version)
                current_manifest_eol = raw_option.get("eol") if raw_option is not None else None
                current_eol = eol_date_from_manifest(current_manifest_eol) if raw_option is not None else None
                target_eol = normalized_manifest_eol(eol_from)
                if raw_option is not None and current_eol != eol_from:
                    message = f"{prefix}updating `eol` to `{target_eol}` for {product_label} version `{version}`"
                    result["updates"].append(message)
                    result["planned_changes"].append(
                        make_planned_change(
                            manifest_path,
                            f"options[version={version}].eol",
                            "eol",
                            current_manifest_eol,
                            target_eol,
                            message,
                            {
                                "change_type": "eol",
                                "product_label": product_label,
                                "version": version,
                                "service_label": prefix.strip("[] ") if prefix else "",
                            },
                        )
                    )

        configured_majors = [major_version(version) for version in configured_versions]
        configured_majors = [value for value in configured_majors if value is not None]
        latest_release = self.latest_non_eol_release(product_data)
        latest_name = str(latest_release.get("name")) if latest_release else None
        latest_major = major_version(latest_name)
        if configured_majors and latest_name and latest_major is not None and latest_major > max(configured_majors):
            message = (
                f"{prefix}new {product_label} major version `{latest_name}` is available "
                f"(highest configured major: `{max(configured_majors)}`)"
            )
            result["major_updates"].append(message)
            add_grouped_notification(result, "major_version", message)

        return result

    @staticmethod
    def get_service_images(service_data: dict[str, Any]) -> list[str]:
        primary_images: list[str] = []
        all_images: list[str] = []

        for workload in service_data.get("workloads") or []:
            if not isinstance(workload, dict):
                continue
            workload_images = [
                container["image"]
                for container in workload.get("containers") or []
                if isinstance(container, dict) and container.get("image")
            ]
            if workload.get("primary"):
                primary_images.extend(workload_images)
            all_images.extend(workload_images)

        ordered = primary_images + all_images
        unique_images: list[str] = []
        seen: set[str] = set()
        for image in ordered:
            if image in seen:
                continue
            seen.add(image)
            unique_images.append(image)
        return unique_images


def readme_managed_services_table_text(readme_text: str) -> str:
    lines = readme_text.splitlines()
    start = None
    for index, line in enumerate(lines):
        if line.strip().lower() == README_MANAGED_SERVICES_HEADING.lower():
            start = index + 1
            break
    if start is None:
        return ""

    section_lines = []
    for line in lines[start:]:
        if line.startswith("## "):
            break
        if line.lstrip().startswith("|"):
            section_lines.append(line)
    return "\n".join(section_lines)


def load_service_repos(readme_path: Path, owner: str, repo_filter: str) -> list[str]:
    readme_text = readme_path.read_text()
    repo_source_text = readme_managed_services_table_text(readme_text) or readme_text
    pattern = re.compile(README_SERVICE_REPO_RE_TEMPLATE.format(owner=re.escape(owner)), re.IGNORECASE)
    repos = sorted(set(pattern.findall(repo_source_text)) - EXCLUDED_README_REPOS)
    if not repo_filter:
        return repos
    matcher = re.compile(repo_filter)
    return [repo for repo in repos if matcher.search(repo)]


def generate_report(args: argparse.Namespace) -> dict[str, Any]:
    readme_path = Path(args.readme).resolve()
    repos = load_service_repos(readme_path, args.owner, args.repo_filter)
    generator = UpdateReportGenerator(args.owner)

    results: list[RepoResult] = []
    missing_service_yml: list[str] = []
    external_service_repos: list[str] = []
    no_image: list[str] = []
    no_helm: list[str] = []
    no_options: list[str] = []

    for repo in repos:
        manifest_paths = generator.get_repo_manifest_paths(repo)
        if not manifest_paths:
            missing_service_yml.append(repo)
            continue

        updates: list[str] = []
        current: list[str] = []
        warnings: list[str] = []
        notifications: list[str] = []
        notification_groups: dict[str, list[str]] = empty_notification_groups()
        eol_updates: list[str] = []
        major_updates: list[str] = []
        planned_changes: list[dict[str, Any]] = []
        dry_run_changes: list[dict[str, Any]] = []
        updates_without_local_diff: list[str] = []
        build_template_current: list[str] = []
        build_template_review_items: list[str] = []
        build_template_warnings: list[str] = []
        build_template_checks: list[dict[str, Any]] = []
        repo_expects_image = False
        repo_expects_helm = False
        repo_expects_options = False
        repo_missing_expected_image = False
        repo_missing_expected_helm = False
        repo_missing_expected_options = False
        repo_comparable = True
        repo_external = True
        repo_service_types: set[str] = set()
        repo_has_reportable_manifest = False

        multiple_manifests = len(manifest_paths) > 1

        for manifest_path in manifest_paths:
            raw_service_data = generator.get_service_data(repo, manifest_path)
            if raw_service_data is None:
                warnings.append(f"[{manifest_path}] manifest listed by index.yml could not be read")
                repo_comparable = False
                continue

            service_data = raw_service_data
            if not service_data:
                warnings.append(f"[{manifest_path}] service manifest is empty")
                repo_comparable = False
                continue

            service_type = str(raw_service_data.get("type") or "")
            external = bool(service_data.get("external"))
            label = str(raw_service_data.get("name") or manifest_path.rsplit("/", 1)[0])
            prefix = f"[{label}] " if multiple_manifests else ""
            if external:
                continue

            repo_has_reportable_manifest = True
            if args.build_template_mode == "report":
                try:
                    build_template_result = generator.check_build_templates(repo, manifest_path, raw_service_data, prefix)
                    build_template_current.extend(build_template_result["current"])
                    build_template_review_items.extend(build_template_result["updates"])
                    build_template_warnings.extend(build_template_result["warnings"])
                    build_template_checks.extend(build_template_result["checks"])
                except Exception as exc:
                    build_template_warnings.append(f"{prefix}build template evaluation failed: {exc}")

            parent_name = raw_service_data.get("from")
            from_version_constraint = raw_service_data.get("fromVersionConstraint")
            from_version = raw_service_data.get("fromVersion")
            images = generator.get_service_images(service_data)
            primary_image = images[0] if images else None
            options = service_data.get("options") or []
            raw_options = raw_service_data.get("options") or []
            duplicate_versions = duplicate_option_versions(raw_options)
            if duplicate_versions:
                versions = ", ".join(f"`{version}`" for version in sorted(duplicate_versions))
                warnings.append(
                    f"{prefix}duplicate `options` version entries found for {versions}; "
                    "automated updates for those versions are disabled"
                )
            raw_option_index = raw_options_by_version(raw_options)
            helm = service_data.get("helm") or None
            raw_helm = raw_service_data.get("helm") or None
            helm_source = helm.get("source") if helm else None
            helm_chart = (helm.get("chart") or helm_source) if helm else None
            helm_version = str(helm.get("version")) if helm and helm.get("version") is not None else None
            crd_charts = service_data.get("crdCharts")
            raw_crd_charts = raw_service_data.get("crdCharts")
            expects_image = not external and not parent_name and service_type != "infrastructure"
            expects_helm = not external and not parent_name
            expects_options = not external and not parent_name and service_type != "infrastructure"
            comparable = False

            repo_service_types.add(service_type)
            repo_external = repo_external and external
            repo_expects_image = repo_expects_image or expects_image
            repo_expects_helm = repo_expects_helm or expects_helm
            repo_expects_options = repo_expects_options or expects_options
            repo_missing_expected_image = repo_missing_expected_image or (expects_image and not primary_image)
            repo_missing_expected_helm = repo_missing_expected_helm or (expects_helm and not helm)
            repo_missing_expected_options = repo_missing_expected_options or (expects_options and not options)

            if parent_name:
                try:
                    parent_result = generator.check_parent_service_version(
                        str(parent_name), from_version_constraint, from_version, prefix, manifest_path
                    )
                    updates.extend(parent_result["updates"])
                    current.extend(parent_result["current"])
                    warnings.extend(parent_result["warnings"])
                    notifications.extend(parent_result["notifications"])
                    merge_notification_groups(notification_groups, parent_result.get("notification_groups"))
                    major_updates.extend(parent_result["major_updates"])
                    planned_changes.extend(parent_result["planned_changes"])
                    comparable = comparable or bool(parent_result["comparable"])
                except Exception as exc:
                    warnings.append(f"{prefix}parent service version lookup failed for `{parent_name}`: {exc}")

            if not parent_name:
                try:
                    eol_result = generator.check_eol_options(
                        repo, label, service_type, options, raw_options, prefix, manifest_path
                    )
                    updates.extend(eol_result["updates"])
                    current.extend(eol_result["current"])
                    eol_updates.extend(eol_result["updates"])
                    major_updates.extend(eol_result["major_updates"])
                    notifications.extend(eol_result["notifications"])
                    merge_notification_groups(notification_groups, eol_result.get("notification_groups"))
                    warnings.extend(eol_result["warnings"])
                    planned_changes.extend(eol_result["planned_changes"])
                except Exception as exc:
                    warnings.append(f"{prefix}endoflife.date lookup failed: {exc}")

            fallback_source = generator.resolve_fallback_version_source(repo, label)
            if fallback_source is not None:
                try:
                    source_result = generator.check_fallback_version_source(
                        fallback_source,
                        options,
                        prefix,
                        helm_source,
                        helm_chart,
                        helm_version,
                    )
                    current.extend(source_result["current"])
                    major_updates.extend(source_result["major_updates"])
                    notifications.extend(source_result["notifications"])
                    merge_notification_groups(notification_groups, source_result.get("notification_groups"))
                    warnings.extend(source_result["warnings"])
                    comparable = comparable or bool(
                        source_result["current"]
                        or source_result["major_updates"]
                        or source_result["notifications"]
                    )
                except Exception as exc:
                    warnings.append(
                        f"{prefix}{fallback_source.get('label', 'fallback source')} version source lookup failed: {exc}"
                    )

            if len(images) > 1:
                warnings.append(
                    f"{prefix}multiple explicit container images were found in workloads; "
                    f"manifest tag updates will only be planned from the primary image `{primary_image}`"
                )

            if images and options:
                comparable = True
                for image_index, image in enumerate(images):
                    can_plan_image_changes = image_index == 0
                    image_prefix = prefix if len(images) == 1 else f"{prefix}[image {image}] "
                    try:
                        published_tags = generator.get_image_tags(image)
                        valid_stabilities = None
                        if generator.is_owner_image(image):
                            _registry, image_repo = generator.image_repository(image)
                            valid_stabilities = generator.get_github_tags(args.owner, image_repo.split("/", 1)[1])
                    except Exception as exc:
                        warnings.append(f"{image_prefix}image lookup failed for `{image}`: {exc}")
                        published_tags = None
                        valid_stabilities = None

                    if published_tags is None:
                        continue

                    for option in options:
                        wanted = str(option.get("version"))
                        configured = option.get("tag") or wanted
                        configured_exists = configured in published_tags if configured else False
                        if generator.is_owner_image(image):
                            target = generator.latest_wodby_tag(wanted, published_tags, valid_stabilities or set())
                        else:
                            target = generator.latest_external_tag(wanted, published_tags, configured)

                        if target is None:
                            if configured and configured_exists:
                                current.append(
                                    f"{image_prefix}no newer published image tag family was found for version `{wanted}`; current tag `{configured}` exists"
                                )
                            else:
                                warnings.append(
                                    f"{image_prefix}no published image tag found for version `{wanted}` (current: `{configured}`)"
                                )
                        elif configured == target:
                            current.append(f"{image_prefix}tag `{configured}` is the latest published tag for version `{wanted}`")
                        else:
                            message = (
                                f"{image_prefix}updating tag to `{target}` for version `{wanted}` (current: `{configured}`)"
                            )
                            updates.append(message)
                            raw_option = raw_option_index.get(wanted)
                            if raw_option is not None and can_plan_image_changes:
                                planned_changes.append(
                                    make_planned_change(
                                        manifest_path,
                                        f"options[version={wanted}].tag",
                                        "tag",
                                        raw_option.get("tag"),
                                        target,
                                        message,
                                        {
                                            "change_type": "image_tag",
                                            "image": image,
                                            "image_version": wanted,
                                            "service_label": label if multiple_manifests else "",
                                            "display_before": configured,
                                            "image_change_notes": generator.get_image_change_notes(
                                                image,
                                                str(raw_option.get("tag") or configured) if configured else None,
                                                target,
                                                wanted,
                                            ),
                                        },
                                    )
                                )
                            elif raw_option is None:
                                updates_without_local_diff.append(
                                    f"{message}; no unique local `options` entry for version `{wanted}` in `{manifest_path}`"
                                )
                            else:
                                updates_without_local_diff.append(
                                    f"{message}; additional image comparison is report only"
                                )

            elif images and not options:
                if expects_options:
                    warnings.append(
                        f"{prefix}explicit images are defined, but there are no local `options` entries to compare against published tags"
                    )
            elif options and expects_image:
                warnings.append(f"{prefix}service options are defined, but no explicit container image was found in local workloads")

            if helm and helm_source and helm_chart and helm_version:
                comparable = True
                planned_helm_target: str | None = None
                try:
                    latest_chart = generator.get_helm_latest(helm_source, helm_chart)
                    if latest_chart is None:
                        warnings.append(
                            f"{prefix}could not resolve latest Helm chart version for `{helm_chart}` from `{helm_source}`"
                        )
                    elif latest_chart == helm_version:
                        current.append(f"{prefix}helm chart latest version is current (`{helm_version}`)")
                    elif is_major_version_change(helm_version, latest_chart):
                        message = (
                            f"{prefix}new major Helm chart version `{latest_chart}` is available "
                            f"for `{helm_chart}` (current: `{helm_version}`); manual review required"
                        )
                        major_updates.append(message)
                        add_repo_notification(notifications, notification_groups, "helm_major_version", message)
                    else:
                        message = (
                            f"{prefix}a new chart version is available `{latest_chart}` (current: `{helm_version}`)"
                        )
                        updates.append(message)
                        if isinstance(raw_helm, dict) and raw_helm.get("version") is not None:
                            planned_changes.append(
                                make_planned_change(
                                    manifest_path,
                                    "helm.version",
                                    "version",
                                    raw_helm.get("version"),
                                    latest_chart,
                                    message,
                                    {
                                        "change_type": "helm_chart",
                                        "helm_chart": helm_chart,
                                        "service_label": label if multiple_manifests else "",
                                    },
                                )
                            )
                            planned_helm_target = latest_chart
                        else:
                            updates_without_local_diff.append(
                                f"{message}; no local `helm.version` field in `{manifest_path}`"
                            )
                except Exception as exc:
                    warnings.append(f"{prefix}helm version lookup failed for `{helm_chart}` from `{helm_source}`: {exc}")

                if planned_helm_target is not None:
                    try:
                        crd_result = generator.check_crd_chart_updates(
                            crd_charts,
                            raw_crd_charts,
                            planned_helm_target,
                            prefix,
                            manifest_path,
                            label if multiple_manifests else "",
                        )
                        updates.extend(crd_result["updates"])
                        current.extend(crd_result["current"])
                        warnings.extend(crd_result["warnings"])
                        planned_changes.extend(crd_result["planned_changes"])
                        updates_without_local_diff.extend(crd_result["updates_without_local_diff"])
                    except Exception as exc:
                        warnings.append(f"{prefix}CRD Helm chart lookup failed for target `{planned_helm_target}`: {exc}")
            elif expects_helm:
                if not helm:
                    warnings.append(f"{prefix}no local `helm` section was found for this non-external service")
                else:
                    warnings.append(f"{prefix}local `helm` section is incomplete and cannot be compared automatically")

            if not comparable and not external:
                repo_comparable = False
                if not expects_helm and not expects_options and not expects_image:
                    current.append(f"{prefix}no automated version comparison target is defined in the service manifest")
            else:
                repo_comparable = repo_comparable and comparable

        if not repo_has_reportable_manifest:
            external_service_repos.append(repo)
            continue

        if repo_expects_image and repo_missing_expected_image:
            no_image.append(repo)
        if repo_expects_helm and repo_missing_expected_helm:
            no_helm.append(repo)
        if repo_expects_options and repo_missing_expected_options:
            no_options.append(repo)

        planned_release = None
        if planned_changes:
            try:
                planned_release = build_planned_release(repo, generator.get_github_tags(args.owner, repo), planned_changes)
            except Exception as exc:
                planned_release = {
                    "status": "blocked",
                    "reason": f"git tag lookup failed: {exc}",
                    "previous_tag": None,
                    "tag": None,
                    "title": None,
                    "description": None,
                    "commands": [],
                }

        results.append(
            RepoResult(
                repo=repo,
                has_image=not repo_missing_expected_image,
                has_helm=not repo_missing_expected_helm,
                has_options=not repo_missing_expected_options,
                expects_image=repo_expects_image,
                expects_helm=repo_expects_helm,
                expects_options=repo_expects_options,
                comparable=repo_comparable,
                external=repo_external,
                service_type="mixed" if len(repo_service_types) > 1 else (next(iter(repo_service_types)) if repo_service_types else ""),
                updates=updates,
                current=current,
                warnings=warnings,
                notifications=notifications,
                notification_groups=notification_groups,
                eol_updates=eol_updates,
                major_updates=major_updates,
                planned_changes=planned_changes,
                planned_diffs=render_planned_diffs(planned_changes),
                dry_run_changes=dry_run_changes,
                dry_run_diffs=render_planned_diffs(dry_run_changes),
                updates_without_local_diff=updates_without_local_diff,
                build_template_current=build_template_current,
                build_template_review_items=build_template_review_items,
                build_template_warnings=build_template_warnings,
                build_template_checks=build_template_checks,
                planned_release=planned_release,
            )
        )

    updates_section = [result for result in results if result.updates]
    no_changes_section = [
        result
        for result in results
        if not result.updates
        and not result.warnings
        and not result.notifications
        and not result.build_template_review_items
        and not result.build_template_warnings
        and result.current
        and result.comparable
    ]
    comparable_no_updates = {result.repo for result in updates_section + no_changes_section}
    special_section = [result for result in results if result.repo not in comparable_no_updates]
    notification_group_totals = {
        group: sum(1 for result in results if result.notification_groups.get(group))
        for group in NOTIFICATION_GROUP_ORDER
    }

    generated_at = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    return {
        "generated_at": generated_at,
        "owner": args.owner,
        "readme": str(readme_path),
        "repo_filter": args.repo_filter,
        "totals": {
            "repos_in_readme": len(repos),
            "repos_reported": len(results),
            "external_excluded": len(external_service_repos),
            "updates": len(updates_section),
            "no_changes": len(no_changes_section),
            "special": len(special_section) + len(missing_service_yml),
            "notifications": sum(1 for result in results if result.notifications),
            "major_version_notifications": notification_group_totals["major_version"],
            "helm_major_version_notifications": notification_group_totals["helm_major_version"],
            "missing_version_source_notifications": notification_group_totals["missing_version_source"],
            "missing_eol_notifications": notification_group_totals["missing_eol"],
            "eol_updates": sum(1 for result in results if result.eol_updates),
            "major_updates": sum(1 for result in results if result.major_updates),
            "planned_releases": sum(
                1
                for result in results
                if result.planned_release and result.planned_release.get("status") == "planned"
            ),
            "dry_run_updates": sum(1 for result in results if result.dry_run_diffs),
            "release_blockers": sum(
                1
                for result in results
                if result.planned_release and result.planned_release.get("status") == "blocked"
            ),
            "build_template_review_items": sum(1 for result in results if result.build_template_review_items),
            "build_template_warnings": sum(1 for result in results if result.build_template_warnings),
        },
        "updates": {result.repo: result.updates for result in updates_section},
        "no_changes": {result.repo: result.current for result in no_changes_section},
        "special": {result.repo: result.to_dict() for result in special_section},
        "missing_service_yml": missing_service_yml,
        "external_services": external_service_repos,
        "category_lists": {
            "no_image": no_image,
            "no_helm": no_helm,
            "no_options": no_options,
        },
        "per_repo": [result.to_dict() for result in results],
    }


def render_markdown(report: dict[str, Any]) -> str:
    lines: list[str] = []
    lines.append("# Service Update Report")
    lines.append("")
    lines.append(f"Report date: {report['generated_at']}")
    lines.append("")
    lines.append(f"- Repos in README: {report['totals']['repos_in_readme']}")
    lines.append(f"- Repos reported: {report['totals']['repos_reported']}")
    lines.append(f"- External repos excluded: {report['totals']['external_excluded']}")
    lines.append(f"- Repos needing updates: {report['totals']['updates']}")
    lines.append(f"- Fully comparable repos with no changes: {report['totals']['no_changes']}")
    lines.append(f"- Special-case repos: {report['totals']['special']}")
    lines.append(f"- Repos with manual-review notifications: {report['totals']['notifications']}")
    lines.append(f"- Repos with new major-version notifications: {report['totals'].get('major_version_notifications', 0)}")
    lines.append(
        f"- Repos with new Helm major-version notifications: "
        f"{report['totals'].get('helm_major_version_notifications', 0)}"
    )
    lines.append(
        f"- Repos without version-check sources: "
        f"{report['totals'].get('missing_version_source_notifications', 0)}"
    )
    lines.append(f"- Repos without EOL data: {report['totals'].get('missing_eol_notifications', 0)}")
    lines.append(f"- Repos with planned git tag releases: {report['totals'].get('planned_releases', 0)}")
    lines.append(f"- Repos with manual-review dry-run diffs: {report['totals'].get('dry_run_updates', 0)}")
    lines.append(f"- Repos with git tag release blockers: {report['totals'].get('release_blockers', 0)}")
    lines.append(f"- Repos with build-template review items: {report['totals'].get('build_template_review_items', 0)}")
    lines.append(f"- Repos with build-template warnings: {report['totals'].get('build_template_warnings', 0)}")
    if "applied_updates" in report["totals"]:
        lines.append(f"- Repos updated by workflow: {report['totals'].get('applied_updates', 0)}")
    if "apply_failures" in report["totals"]:
        lines.append(f"- Repos with update apply failures: {report['totals'].get('apply_failures', 0)}")
    lines.append("")

    planned_change_items = [
        item
        for item in sorted(report["per_repo"], key=lambda value: value["repo"])
        if item.get("planned_diffs") or (item.get("planned_release") or {}).get("status") == "planned"
    ]
    if planned_change_items:
        lines.append("## Service Changes and Git Tags")
        lines.append("")
        lines.append("The workflow applies these service changes and releases these git tags when the apply step succeeds.")
        lines.append("")
        for item in planned_change_items:
            release = item.get("planned_release") or {}
            lines.append(f"### {item['repo']}")
            if release.get("status") == "planned":
                lines.append(f"- Git tag: `{release['tag']}`")
                lines.append(f"- Previous tag: `{release['previous_tag']}`")
                lines.append("")
                lines.append("Tag description:")
                lines.extend(str(release.get("description") or "").splitlines())
                lines.append("")
            if item.get("planned_diffs"):
                lines.append("Planned diff:")
                lines.append("")
                for planned_diff in item["planned_diffs"]:
                    lines.append("```diff")
                    lines.extend(str(planned_diff).splitlines())
                    lines.append("```")
                    lines.append("")
            lines.append("")

    dry_run_items = [
        item
        for item in sorted(report["per_repo"], key=lambda value: value["repo"])
        if item.get("dry_run_diffs")
    ]
    if dry_run_items:
        lines.append("## Manual Review Dry Run Diffs")
        lines.append("")
        lines.append("These diffs are generated for manual review only. The workflow does not apply them.")
        lines.append("")
        for item in dry_run_items:
            lines.append(f"### {item['repo']}")
            for dry_run_diff in item["dry_run_diffs"]:
                lines.append("```diff")
                lines.extend(str(dry_run_diff).splitlines())
                lines.append("```")
                lines.append("")
            lines.append("")

    build_template_items = [
        item
        for item in sorted(report["per_repo"], key=lambda value: value["repo"])
        if item.get("build_template_review_items") or item.get("build_template_warnings")
    ]
    if build_template_items:
        lines.append("## Build Template Review")
        lines.append("")
        lines.append("These checks are report only. The workflow does not apply build template changes.")
        lines.append("")
        for item in build_template_items:
            lines.append(f"### {item['repo']}")
            if item.get("build_template_review_items"):
                lines.append("Review items:")
                for message in item["build_template_review_items"]:
                    lines.append(f"- {message}")
                lines.append("")
            if item.get("build_template_warnings"):
                lines.append("Warnings:")
                for message in item["build_template_warnings"]:
                    lines.append(f"- {message}")
                lines.append("")
            lines.append("")

    apply_result_items = [
        item for item in sorted(report["per_repo"], key=lambda value: value["repo"]) if item.get("apply_result")
    ]
    if apply_result_items:
        lines.append("## Apply Results")
        lines.append("")
        for item in apply_result_items:
            result = item["apply_result"]
            lines.append(f"### {item['repo']}")
            lines.append(f"- Status: `{result.get('status', 'unknown')}`")
            if result.get("message"):
                lines.append(f"- Message: {result['message']}")
            if result.get("branch"):
                lines.append(f"- Branch: `{result['branch']}`")
            if result.get("commit"):
                lines.append(f"- Commit: `{result['commit']}`")
            if result.get("tag"):
                lines.append(f"- Tag: `{result['tag']}`")
            changed_files = result.get("changed_files") or []
            if changed_files:
                lines.append(f"- Changed files: {', '.join(f'`{path}`' for path in changed_files)}")
            lines.append("")

    blocked_release_items = [
        item
        for item in sorted(report["per_repo"], key=lambda value: value["repo"])
        if (item.get("planned_release") or {}).get("status") == "blocked"
    ]
    if blocked_release_items:
        lines.append("## Git Tag Release Blockers")
        lines.append("")
        for item in blocked_release_items:
            release = item["planned_release"]
            lines.append(f"### {item['repo']}")
            lines.append(f"- {release.get('reason') or 'release tag could not be calculated'}")
            lines.append("")

    no_local_diff_items = [
        item
        for item in sorted(report["per_repo"], key=lambda value: value["repo"])
        if item.get("updates_without_local_diff")
    ]
    if no_local_diff_items:
        lines.append("## Updates Without Local Manifest Diff")
        lines.append("")
        for item in no_local_diff_items:
            lines.append(f"### {item['repo']}")
            for message in item["updates_without_local_diff"]:
                lines.append(f"- {message}")
            lines.append("")

    for group in NOTIFICATION_GROUP_ORDER:
        grouped_items = [
            (item, (item.get("notification_groups") or {}).get(group) or [])
            for item in sorted(report["per_repo"], key=lambda value: value["repo"])
            if (item.get("notification_groups") or {}).get(group)
        ]
        if grouped_items:
            lines.append(f"## {NOTIFICATION_GROUP_TITLES[group]}")
            lines.append("")
            for item, messages in grouped_items:
                lines.append(f"### {item['repo']}")
                for message in messages:
                    lines.append(f"- {message}")
                lines.append("")

    other_notification_items = []
    for item in sorted(report["per_repo"], key=lambda value: value["repo"]):
        grouped_messages = {
            message
            for messages in (item.get("notification_groups") or {}).values()
            for message in messages
        }
        other_messages = [message for message in item.get("notifications") or [] if message not in grouped_messages]
        if other_messages:
            other_notification_items.append((item, other_messages))

    if other_notification_items:
        lines.append("## Other Manual Review Notifications")
        lines.append("")
        for item, messages in other_notification_items:
            lines.append(f"### {item['repo']}")
            for message in messages:
                lines.append(f"- {message}")
            lines.append("")

    warning_items = [
        item for item in sorted(report["per_repo"], key=lambda value: value["repo"]) if item.get("warnings")
    ]
    if warning_items:
        lines.append("## Warnings")
        lines.append("")
        for item in warning_items:
            lines.append(f"### {item['repo']}")
            for message in item["warnings"]:
                lines.append(f"- {message}")
            lines.append("")

    lines.append("## No Changes")
    lines.append("")
    for repo in sorted(report["no_changes"]):
        lines.append(f"### {repo}")
        for message in report["no_changes"][repo]:
            lines.append(f"- {message}")
        lines.append("")

    lines.append("## Special Cases")
    lines.append("")
    lines.append("### Missing or Unreadable `service.yml`")
    for repo in report["missing_service_yml"]:
        lines.append(f"- {repo}")
    lines.append("")

    lines.append("### Repos Requiring Manual Review")
    for repo in sorted(report["special"]):
        details = report["special"][repo]
        flags: list[str] = []
        if details.get("expects_image") and not details["has_image"]:
            flags.append("no explicit image comparison target")
        if details.get("expects_helm") and not details["has_helm"]:
            flags.append("no local helm configuration")
        if details.get("expects_options") and not details["has_options"]:
            flags.append("no local options")
        if details.get("build_template_review_items"):
            flags.append("build-template review items")
        if details.get("build_template_warnings"):
            flags.append("build-template warnings")
        if flags:
            lines.append(f"- {repo}: {', '.join(flags)}")
        else:
            lines.append(f"- {repo}")
        for message in details["current"]:
            lines.append(f"  current: {message}")
        for message in details["warnings"]:
            lines.append(f"  warning: {message}")
    lines.append("")

    lines.append("### Category Lists")
    lines.append(
        "- `Missing or unreadable service.yml` can mean the file does not exist in the repo or the workflow token does not have enough read access."
    )
    category_lists = report["category_lists"]
    lines.append(
        f"- No explicit image comparison target: {', '.join(category_lists['no_image']) if category_lists['no_image'] else 'none'}"
    )
    lines.append(
        f"- No local `helm`: {', '.join(category_lists['no_helm']) if category_lists['no_helm'] else 'none'}"
    )
    lines.append(
        f"- No local `options`: {', '.join(category_lists['no_options']) if category_lists['no_options'] else 'none'}"
    )
    return "\n".join(lines) + "\n"


def main() -> int:
    args = parse_args()
    report = generate_report(args)

    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    markdown_path = output_dir / "service-update-report.md"
    json_path = output_dir / "service-update-report.json"

    markdown_path.write_text(render_markdown(report))
    json_path.write_text(json.dumps(report, indent=2))

    print(f"Wrote {markdown_path}")
    print(f"Wrote {json_path}")
    print(
        "Summary: "
        f"{report['totals']['updates']} repos need updates, "
        f"{report['totals']['no_changes']} fully comparable repos have no changes, "
        f"{report['totals']['special']} repos are special cases, "
        f"{report['totals']['notifications']} repos have manual-review notifications, "
        f"{report['totals'].get('planned_releases', 0)} repos have planned git tag releases, "
        f"{report['totals'].get('dry_run_updates', 0)} repos have manual-review dry-run diffs, "
        f"{report['totals'].get('release_blockers', 0)} repos have git tag release blockers, "
        f"{report['totals'].get('build_template_review_items', 0)} repos have build-template review items, "
        f"{report['totals'].get('build_template_warnings', 0)} repos have build-template warnings, "
        f"{report['totals']['external_excluded']} external repos were excluded."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
