#!/usr/bin/env python3

import argparse
import base64
import copy
import json
import os
import re
import subprocess
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import requests
import yaml
from packaging.version import InvalidVersion, Version


README_SERVICE_REPO_RE_TEMPLATE = r"https://github\.com/{owner}/(?P<repo>service-[A-Za-z0-9._-]+)(?:/)?(?=[)\s|]|$)"
README_MANAGED_SERVICES_HEADING = "## Managed services"
DOCKER_TOKEN_URL = "https://auth.docker.io/token?service=registry.docker.io&scope=repository:{repo}:pull"
DOCKER_TAGS_URL = "https://registry-1.docker.io/v2/{repo}/tags/list?n=10000"
GHCR_TOKEN_URL = "https://ghcr.io/token?scope=repository:{repo}:pull"
GHCR_TAGS_URL = "https://ghcr.io/v2/{repo}/tags/list?n=10000"
GITHUB_CONTENTS_URL = "https://api.github.com/repos/{owner}/{repo}/contents/{path}"
GITHUB_TAGS_URL = "https://api.github.com/repos/{owner}/{repo}/tags?per_page=100&page={page}"
WODBY_CHART_URL = "https://raw.githubusercontent.com/{owner}/charts/main/{chart}/Chart.yaml"

WODBY_TAG_RE = re.compile(r"^(?P<base>\d+(?:\.\d+)*)(?:-(?P<stability>\d+(?:\.\d+)*))?$")
EXTERNAL_TAG_RE = re.compile(r"^(?P<prefix>v?)(?P<base>\d+(?:\.\d+)*)$")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Generate a dry-run service update report.")
    parser.add_argument("--readme", default="README.md", help="Path to the README that lists the service repos.")
    parser.add_argument("--owner", default="wodby", help="GitHub owner/org that owns the service repos.")
    parser.add_argument("--repo-filter", default="", help="Optional regex filter for repo names.")
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


def exact_match(base: str, wanted: str) -> bool:
    return base == wanted


def family_match(base: str, wanted: str) -> bool:
    return base == wanted or base.startswith(f"{wanted}.")


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
        self._github_tags_cache: dict[tuple[str, str], set[str]] = {}
        self._registry_tags_cache: dict[tuple[str, str], list[str]] = {}
        self._http_cache: dict[tuple[str, tuple[tuple[str, str], ...]], requests.Response] = {}
        self._helm_index_cache: dict[str, dict[str, Any]] = {}
        self._service_data_cache: dict[tuple[str, str], dict[str, Any] | None] = {}
        self._resolved_service_cache: dict[tuple[str, str], dict[str, Any] | None] = {}
        self._wodby_chart_cache: dict[str, str] = {}

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

    def get_raw_repo_file(self, repo: str, path: str) -> str | None:
        for branch in ("main", "master"):
            url = f"https://raw.githubusercontent.com/{self.owner}/{repo}/{branch}/{path}"
            response = self.session.get(url, timeout=60)
            if response.status_code == 200:
                return response.text
            if response.status_code not in (404, 403):
                response.raise_for_status()
        return None

    def get_repo_file(self, repo: str, path: str) -> str | None:
        cache_key = (self.owner, repo, path)
        if cache_key in self._content_cache:
            return self._content_cache[cache_key]
        url = GITHUB_CONTENTS_URL.format(owner=self.owner, repo=repo, path=path)
        response = self.session.get(url, headers=self.github_headers, timeout=60)
        if response.status_code in (403, 404):
            content = self.get_raw_repo_file(repo, path)
            self._content_cache[cache_key] = content
            return content
        response.raise_for_status()
        payload = response.json()
        content = base64.b64decode(payload["content"]).decode("utf-8")
        self._content_cache[cache_key] = content
        return content

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

    @staticmethod
    def _is_named_object_list(items: list[Any]) -> bool:
        return bool(items) and all(isinstance(item, dict) and "name" in item for item in items)

    def merge_values(self, base: Any, override: Any) -> Any:
        if base is None:
            return copy.deepcopy(override)
        if override is None:
            return copy.deepcopy(base)

        if isinstance(base, dict) and isinstance(override, dict):
            result = copy.deepcopy(base)
            for key, value in override.items():
                if key == "from":
                    continue
                if key in result:
                    result[key] = self.merge_values(result[key], value)
                else:
                    result[key] = copy.deepcopy(value)
            return result

        if isinstance(base, list) and isinstance(override, list):
            if self._is_named_object_list(base + override):
                merged = [copy.deepcopy(item) for item in base]
                index_by_name = {
                    item["name"]: position
                    for position, item in enumerate(merged)
                    if isinstance(item, dict) and "name" in item
                }
                for item in override:
                    name = item["name"]
                    if name in index_by_name:
                        merged[index_by_name[name]] = self.merge_values(merged[index_by_name[name]], item)
                    else:
                        index_by_name[name] = len(merged)
                        merged.append(copy.deepcopy(item))
                return merged
            return copy.deepcopy(override)

        return copy.deepcopy(override)

    def get_resolved_service_data(
        self,
        repo: str,
        manifest_path: str = "service.yml",
        chain: tuple[tuple[str, str], ...] = (),
    ) -> dict[str, Any] | None:
        cache_key = (repo, manifest_path)
        if cache_key in self._resolved_service_cache:
            return self._resolved_service_cache[cache_key]

        if cache_key in chain:
            cycle = " -> ".join(f"{repo_name}:{path}" for repo_name, path in chain + (cache_key,))
            raise RuntimeError(f"detected inheritance cycle: {cycle}")

        service_data = self.get_service_data(repo, manifest_path)
        if service_data is None:
            self._resolved_service_cache[cache_key] = None
            return None

        parent_name = service_data.get("from")
        if not parent_name:
            resolved = copy.deepcopy(service_data)
        else:
            parent_repo = self.service_name_to_repo(str(parent_name))
            parent_data = self.get_resolved_service_data(parent_repo, "service.yml", chain + (cache_key,))
            if parent_data is None:
                raise RuntimeError(f"unable to resolve inherited service `{parent_repo}` for `{repo}`")
            resolved = self.merge_values(parent_data, service_data)
            resolved["from"] = parent_name

        self._resolved_service_cache[cache_key] = resolved
        return resolved

    def get_github_tags(self, owner: str, repo: str) -> set[str]:
        cache_key = (owner, repo)
        if cache_key in self._github_tags_cache:
            return self._github_tags_cache[cache_key]

        tags: set[str] = set()
        page = 1
        while True:
            url = GITHUB_TAGS_URL.format(owner=owner, repo=repo, page=page)
            response = self.session.get(url, headers=self.github_headers, timeout=60)
            if response.status_code == 403:
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

    def get_dockerhub_tags(self, repo: str) -> list[str]:
        cache_key = ("dockerhub", repo)
        if cache_key in self._registry_tags_cache:
            return self._registry_tags_cache[cache_key]

        token = self.fetch(DOCKER_TOKEN_URL.format(repo=repo)).json()["token"]
        response = self.session.get(
            DOCKER_TAGS_URL.format(repo=repo),
            headers={"Authorization": f"Bearer {token}"},
            timeout=120,
        )
        response.raise_for_status()
        tags = response.json().get("tags", []) or []
        self._registry_tags_cache[cache_key] = tags
        return tags

    def get_ghcr_tags(self, repo: str) -> list[str]:
        cache_key = ("ghcr", repo)
        if cache_key in self._registry_tags_cache:
            return self._registry_tags_cache[cache_key]

        token = self.fetch(GHCR_TOKEN_URL.format(repo=repo)).json()["token"]
        response = self.session.get(
            GHCR_TAGS_URL.format(repo=repo),
            headers={"Authorization": f"Bearer {token}"},
            timeout=120,
        )
        response.raise_for_status()
        tags = response.json().get("tags", []) or []
        self._registry_tags_cache[cache_key] = tags
        return tags

    def get_image_tags(self, image: str) -> list[str]:
        if image.startswith("ghcr.io/"):
            return self.get_ghcr_tags(image[len("ghcr.io/"):])
        if image.count("/") == 1 and "." not in image.split("/")[0]:
            return self.get_dockerhub_tags(image)
        raise RuntimeError(f"unsupported image registry for {image}")

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
        raise RuntimeError(f"unsupported OCI registry for {reference}")

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
    repos = sorted(set(pattern.findall(repo_source_text)))
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
        repo_expects_image = False
        repo_expects_helm = False
        repo_expects_options = False
        repo_missing_expected_image = False
        repo_missing_expected_helm = False
        repo_missing_expected_options = False
        repo_comparable = True
        repo_external = True
        repo_service_types: set[str] = set()

        multiple_manifests = len(manifest_paths) > 1

        for manifest_path in manifest_paths:
            raw_service_data = generator.get_service_data(repo, manifest_path)
            if raw_service_data is None:
                warnings.append(f"[{manifest_path}] manifest listed by index.yml could not be read")
                repo_comparable = False
                continue

            try:
                service_data = generator.get_resolved_service_data(repo, manifest_path)
            except Exception as exc:
                warnings.append(f"[{manifest_path}] inheritance resolution failed: {exc}")
                service_data = copy.deepcopy(raw_service_data)

            if service_data is None:
                warnings.append(f"[{manifest_path}] resolved service manifest is empty")
                repo_comparable = False
                continue

            service_type = str(service_data.get("type") or raw_service_data.get("type") or "")
            external = bool(service_data.get("external"))
            images = generator.get_service_images(service_data)
            image = images[0] if images else None
            options = service_data.get("options") or []
            helm = service_data.get("helm") or None
            helm_source = helm.get("source") if helm else None
            helm_chart = (helm.get("chart") or helm_source) if helm else None
            helm_version = str(helm.get("version")) if helm and helm.get("version") is not None else None
            expects_image = not external and service_type != "infrastructure"
            expects_helm = not external
            expects_options = not external and service_type != "infrastructure"
            comparable = False

            repo_service_types.add(service_type)
            repo_external = repo_external and external
            repo_expects_image = repo_expects_image or expects_image
            repo_expects_helm = repo_expects_helm or expects_helm
            repo_expects_options = repo_expects_options or expects_options
            repo_missing_expected_image = repo_missing_expected_image or (expects_image and not image)
            repo_missing_expected_helm = repo_missing_expected_helm or (expects_helm and not helm)
            repo_missing_expected_options = repo_missing_expected_options or (expects_options and not options)

            label = str(raw_service_data.get("name") or service_data.get("name") or manifest_path.rsplit("/", 1)[0])
            prefix = f"[{label}] " if multiple_manifests else ""

            if len(images) > 1:
                warnings.append(
                    f"{prefix}multiple explicit container images were found in workloads; comparing only the first one `{image}`"
                )

            if image and options:
                comparable = True
                try:
                    published_tags = generator.get_image_tags(image)
                    valid_stabilities = None
                    if image.startswith(f"{args.owner}/"):
                        valid_stabilities = generator.get_github_tags(args.owner, image.split("/", 1)[1])
                except Exception as exc:
                    warnings.append(f"{prefix}image lookup failed for `{image}`: {exc}")
                    published_tags = None
                    valid_stabilities = None

                if published_tags is not None:
                    for option in options:
                        wanted = str(option.get("version"))
                        configured = option.get("tag") or wanted
                        configured_exists = configured in published_tags if configured else False
                        if image.startswith(f"{args.owner}/"):
                            target = generator.latest_wodby_tag(wanted, published_tags, valid_stabilities or set())
                        else:
                            target = generator.latest_external_tag(wanted, published_tags, configured)

                        if target is None:
                            if configured and configured_exists:
                                current.append(
                                    f"{prefix}no newer published image tag family was found for version `{wanted}`; current tag `{configured}` exists"
                                )
                            else:
                                warnings.append(
                                    f"{prefix}no published image tag found for version `{wanted}` (current: `{configured}`)"
                                )
                        elif configured == target:
                            current.append(f"{prefix}tag `{configured}` is the latest published tag for version `{wanted}`")
                        else:
                            updates.append(
                                f"{prefix}updating tag to `{target}` for version `{wanted}` (current: `{configured}`)"
                            )
            elif image and not options:
                if expects_options:
                    warnings.append(
                        f"{prefix}explicit image `{image}` is defined, but there are no resolved `options` entries to compare against published tags"
                    )
            elif options and expects_image:
                warnings.append(f"{prefix}service options are defined, but no explicit container image was found in resolved workloads")
            elif external:
                current.append(f"{prefix}external service is managed outside Wodby; automated image and Helm version checks are not available")

            if helm and helm_source and helm_chart and helm_version:
                comparable = True
                try:
                    latest_chart = generator.get_helm_latest(helm_source, helm_chart)
                    if latest_chart is None:
                        warnings.append(
                            f"{prefix}could not resolve latest Helm chart version for `{helm_chart}` from `{helm_source}`"
                        )
                    elif latest_chart == helm_version:
                        current.append(f"{prefix}helm chart latest version is current (`{helm_version}`)")
                    else:
                        updates.append(
                            f"{prefix}a new chart version is available `{latest_chart}` (current: `{helm_version}`)"
                        )
                except Exception as exc:
                    warnings.append(f"{prefix}helm version lookup failed for `{helm_chart}` from `{helm_source}`: {exc}")
            elif expects_helm:
                if not helm:
                    warnings.append(f"{prefix}no resolved `helm` section was found for this non-external service")
                else:
                    warnings.append(f"{prefix}resolved `helm` section is incomplete and cannot be compared automatically")

            if not comparable and not external:
                repo_comparable = False
                if not expects_helm and not expects_options and not expects_image:
                    current.append(f"{prefix}no automated version comparison target is defined in the resolved service manifest")
            else:
                repo_comparable = repo_comparable and comparable

        if repo_expects_image and repo_missing_expected_image:
            no_image.append(repo)
        if repo_expects_helm and repo_missing_expected_helm:
            no_helm.append(repo)
        if repo_expects_options and repo_missing_expected_options:
            no_options.append(repo)

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
            )
        )

    updates_section = [result for result in results if result.updates]
    no_changes_section = [
        result
        for result in results
        if not result.updates
        and not result.warnings
        and result.current
        and result.comparable
    ]
    comparable_no_updates = {result.repo for result in updates_section + no_changes_section}
    special_section = [result for result in results if result.repo not in comparable_no_updates]

    generated_at = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    return {
        "generated_at": generated_at,
        "owner": args.owner,
        "readme": str(readme_path),
        "repo_filter": args.repo_filter,
        "totals": {
            "repos_in_readme": len(repos),
            "updates": len(updates_section),
            "no_changes": len(no_changes_section),
            "special": len(special_section) + len(missing_service_yml),
        },
        "updates": {result.repo: result.updates for result in updates_section},
        "no_changes": {result.repo: result.current for result in no_changes_section},
        "special": {result.repo: result.to_dict() for result in special_section},
        "missing_service_yml": missing_service_yml,
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
    lines.append(f"- Repos needing updates: {report['totals']['updates']}")
    lines.append(f"- Fully comparable repos with no changes: {report['totals']['no_changes']}")
    lines.append(f"- Special-case repos: {report['totals']['special']}")
    lines.append("")

    lines.append("## Updates Needed")
    lines.append("")
    for repo in sorted(report["updates"]):
        lines.append(f"### {repo}")
        for message in report["updates"][repo]:
            lines.append(f"- {message}")
        repo_details = next(item for item in report["per_repo"] if item["repo"] == repo)
        for message in repo_details["current"]:
            lines.append(f"- No change: {message}")
        for message in repo_details["warnings"]:
            lines.append(f"- Warning: {message}")
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
            flags.append("no resolved helm configuration")
        if details.get("expects_options") and not details["has_options"]:
            flags.append("no resolved options")
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
        f"- No resolved `helm`: {', '.join(category_lists['no_helm']) if category_lists['no_helm'] else 'none'}"
    )
    lines.append(
        f"- No resolved `options`: {', '.join(category_lists['no_options']) if category_lists['no_options'] else 'none'}"
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
        f"{report['totals']['special']} repos are special cases."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
