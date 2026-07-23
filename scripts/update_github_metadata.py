#!/usr/bin/env python3
"""Check or update GitHub About metadata for Wodby service repositories."""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

from update_repository_readmes import (
    SERVICES_REPOSITORY,
    WORKSPACE,
    indexed_manifest_paths,
    load_yaml,
    repository_display_name,
    repository_names,
    service_summary,
)


def github_environment() -> dict[str, str]:
    token = os.environ.get("WODBY_GITHUB_TOKEN", "").strip()
    if not token:
        raise RuntimeError("WODBY_GITHUB_TOKEN is required")
    env = os.environ.copy()
    env["GH_TOKEN"] = token
    return env


def gh_api(
    endpoint: str,
    *,
    method: str = "GET",
    payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    command = ["gh", "api", endpoint, "--method", method]
    if payload is not None:
        command.extend(["--input", "-"])
    result = subprocess.run(
        command,
        input=json.dumps(payload) if payload is not None else None,
        text=True,
        capture_output=True,
        check=False,
        env=github_environment(),
    )
    if result.returncode:
        raise RuntimeError(result.stderr.strip() or f"gh api failed for {endpoint}")
    return json.loads(result.stdout) if result.stdout.strip() else {}


def topic(value: str) -> str:
    value = re.sub(r"[^a-z0-9-]+", "-", value.lower()).strip("-")
    return value[:50].rstrip("-")


def service_type_topics(service_type: str) -> set[str]:
    return {
        "db": {"database"},
        "database": {"database"},
        "datastore": {"cache"},
        "infrastructure": {"kubernetes-infrastructure"},
        "operator": {"kubernetes-operator"},
        "search": {"search"},
        "storage": {"storage"},
        "vpn": {"vpn"},
    }.get(service_type, set())


def desired_metadata(repo_name: str, current_topics: list[str]) -> dict[str, Any]:
    del current_topics
    repo_dir = WORKSPACE / repo_name
    readme_path = repo_dir / "README.md"
    readme = readme_path.read_text(encoding="utf-8") if readme_path.exists() else ""
    manifest_paths = indexed_manifest_paths(repo_dir, "service")
    manifests = [load_yaml(path) for path in manifest_paths]
    display_name = repository_display_name(repo_name, manifests, readme)
    infrastructure = any(manifest.get("type") == "infrastructure" for manifest in manifests)
    description = (
        f"{display_name} Kubernetes system service installed and managed with Wodby clusters."
        if infrastructure
        else service_summary(repo_name, display_name, manifests)
    )

    required_topics = {
        "wodby",
        "kubernetes",
        topic(repo_name.removeprefix("service-")),
    }
    additional_topics: set[str] = set()
    for manifest in manifests:
        service_type = str(manifest.get("type") or "service")
        additional_topics.update(service_type_topics(service_type))
        additional_topics.update(
            topic(str(label)) for label in manifest.get("labels") or []
        )
        if (manifest.get("build") or {}).get("connect"):
            additional_topics.update({"application-runtime", "cicd"})
    if infrastructure:
        required_topics.update(
            {"kubernetes-infrastructure", "platform-engineering"}
        )
    required_topics.discard("")
    additional_topics.discard("")
    selected_topics = set(required_topics)
    for value in sorted(additional_topics - selected_topics):
        if len(selected_topics) == 20:
            break
        selected_topics.add(value)

    return {
        "description": description[:160],
        "homepage": "https://wodby.com/services",
        "topics": sorted(selected_topics),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--check", action="store_true")
    mode.add_argument("--write", action="store_true")
    parser.add_argument("repositories", nargs="*")
    args = parser.parse_args()

    available = repository_names(SERVICES_REPOSITORY / "README.md", "service-")
    repositories = args.repositories or available
    unknown = sorted(set(repositories) - set(available))
    if unknown:
        parser.error(f"repositories are not in the managed index: {', '.join(unknown)}")

    changed = 0
    for repo_name in repositories:
        endpoint = f"repos/wodby/{repo_name}"
        current = gh_api(endpoint)
        current_topics = gh_api(f"{endpoint}/topics").get("names", [])
        desired = desired_metadata(repo_name, current_topics)
        fields = []
        if current.get("description") != desired["description"]:
            fields.append("description")
        if (current.get("homepage") or "") != desired["homepage"]:
            fields.append("homepage")
        if sorted(current_topics) != desired["topics"]:
            fields.append("topics")
        if not fields:
            continue
        changed += 1
        if args.write:
            if "description" in fields or "homepage" in fields:
                gh_api(
                    endpoint,
                    method="PATCH",
                    payload={
                        "description": desired["description"],
                        "homepage": desired["homepage"],
                    },
                )
            if "topics" in fields:
                gh_api(
                    f"{endpoint}/topics",
                    method="PUT",
                    payload={"names": desired["topics"]},
                )
        state = "updated" if args.write else "out of date"
        print(f"{repo_name}: {', '.join(fields)} {state}")

    state = "updated" if args.write else "out of date"
    print(f"checked {len(repositories)} service repositories; {changed} {state}")
    return 1 if args.check and changed else 0


if __name__ == "__main__":
    sys.exit(main())
