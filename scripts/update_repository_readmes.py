#!/usr/bin/env python3
"""Render README files for the public Wodby service repositories.

The aggregate README is the repository inventory. Service manifests remain the
source of truth for service type, capabilities, build boilerplates, and the stack
references used for cross-links.
"""

from __future__ import annotations

import argparse
import re
import sys
import textwrap
from pathlib import Path
from typing import Any

import yaml


SERVICES_REPOSITORY = Path(__file__).resolve().parents[1]
WORKSPACE = SERVICES_REPOSITORY.parent
STACKS_REPOSITORY = WORKSPACE / "stacks"

INFRASTRUCTURE_SUMMARIES = {
    "service-aws-lb-controller": (
        "AWS Load Balancer Controller connects Kubernetes Services and ingress "
        "resources to AWS Elastic Load Balancing for Wodby-managed clusters."
    ),
    "service-envoy-gateway": (
        "Envoy Gateway supplies the Kubernetes Gateway API control plane used "
        "for Wodby cluster ingress and application routing."
    ),
    "service-frpc": (
        "FRPC supplies the client-side tunnel used by supported Wodby cluster "
        "networking configurations."
    ),
    "service-ingress-nginx": (
        "Ingress Nginx supplies ingress routing for Wodby Kubernetes clusters "
        "that use the Nginx ingress controller."
    ),
    "service-kube-state-metrics": (
        "Kube State Metrics exposes Kubernetes object state used by the Wodby "
        "cluster monitoring pipeline."
    ),
    "service-metrics-server": (
        "Metrics Server supplies Kubernetes resource metrics for cluster "
        "autoscaling and operational visibility."
    ),
    "service-monitoring": (
        "Monitoring collects and forwards Kubernetes cluster telemetry for "
        "Wodby infrastructure observability."
    ),
    "service-node-exporter": (
        "Node Exporter exposes host and node metrics used by the Wodby cluster "
        "monitoring pipeline."
    ),
}

SERVICE_TYPE_LABELS = {
    "service": "Application service",
    "db": "Database",
    "database": "Database",
    "infrastructure": "Kubernetes infrastructure",
    "ssh": "SSH service",
    "datastore": "Data store",
    "storage": "Storage",
    "operator": "Kubernetes operator",
    "search": "Search service",
    "vpn": "VPN service",
}


def load_yaml(path: Path) -> dict[str, Any]:
    with path.open(encoding="utf-8") as source:
        value = yaml.safe_load(source)
    return value if isinstance(value, dict) else {}


def repository_names(index_path: Path, prefix: str) -> list[str]:
    pattern = re.compile(rf"https://github\.com/wodby/({re.escape(prefix)}[a-z0-9-]+)")
    return sorted(set(pattern.findall(index_path.read_text(encoding="utf-8"))))


def indexed_manifest_paths(repo_dir: Path, entity: str) -> list[Path]:
    root_manifest = repo_dir / f"{entity}.yml"
    if root_manifest.exists():
        return [root_manifest]

    index_path = repo_dir / "index.yml"
    if not index_path.exists():
        return []

    plural = f"{entity}s"
    result: list[Path] = []
    for entry in load_yaml(index_path).get(plural, []):
        name = entry if isinstance(entry, (str, int)) else entry.get("name", "")
        path = repo_dir / str(name) / f"{entity}.yml"
        if path.exists():
            result.append(path)
    return result


def repository_display_name(repo_name: str, manifests: list[dict[str, Any]], readme: str) -> str:
    heading = readme.splitlines()[0].removeprefix("# ").strip() if readme else ""
    for suffix in (
        " Kubernetes system service for Wodby",
        " service for Kubernetes on Wodby",
        " services for Wodby",
        " service for Wodby",
        " monitoring service",
        " service",
    ):
        if heading.lower().endswith(suffix.lower()):
            heading = heading[: -len(suffix)].strip()
            break

    if len(manifests) == 1:
        manifest_title = str(manifests[0].get("title", "")).strip()
        if manifest_title:
            return manifest_title
    if heading and not heading.startswith("service-"):
        return heading
    return repo_name.removeprefix("service-").replace("-", " ").title()


def service_summary(repo_name: str, display_name: str, manifests: list[dict[str, Any]]) -> str:
    if repo_name in INFRASTRUCTURE_SUMMARIES:
        return INFRASTRUCTURE_SUMMARIES[repo_name]

    types = {str(manifest.get("type", "service")) for manifest in manifests}
    has_build = any(manifest.get("build", {}).get("connect") for manifest in manifests)
    has_boilerplates = any(build_boilerplates(manifest) for manifest in manifests)
    external = any(manifest.get("external") for manifest in manifests)

    if has_build or has_boilerplates:
        return f"Build and run {display_name} applications on Kubernetes with Wodby."
    if external:
        return (
            f"Connect Wodby Kubernetes applications to an externally managed "
            f"{display_name} service."
        )
    if types & {"db", "database"}:
        return f"Run {display_name} as a database for Kubernetes applications managed by Wodby."
    if "datastore" in types:
        return f"Run {display_name} as a data store for Kubernetes applications managed by Wodby."
    if "search" in types:
        return f"Run {display_name} as a search service for Kubernetes applications managed by Wodby."
    if "storage" in types:
        return f"Provide {display_name} storage to Kubernetes applications managed by Wodby."
    if "vpn" in types:
        return f"Connect Kubernetes applications through {display_name} with Wodby."
    if "operator" in types:
        return f"Operate {display_name} resources on Kubernetes with Wodby."
    return f"Run {display_name} as a reusable Kubernetes application service with Wodby."


def wrapped(value: str) -> str:
    links: list[str] = []

    def preserve_link(match: re.Match[str]) -> str:
        links.append(match.group(0))
        return f"WODBYMARKDOWNLINK{len(links) - 1}"

    protected = re.sub(r"\[[^\]]+\]\([^)]+\)", preserve_link, value)
    result = textwrap.fill(
        protected,
        width=79,
        break_long_words=False,
        break_on_hyphens=False,
    )
    for index in range(len(links) - 1, -1, -1):
        link = links[index]
        result = result.replace(f"WODBYMARKDOWNLINK{index}", link)
    return result


def build_boilerplates(manifest: dict[str, Any]) -> list[dict[str, Any]]:
    build = manifest.get("build") or {}
    boilerplates = build.get("boilerplates")
    templates = build.get("templates")
    if boilerplates is not None and templates is not None:
        raise RuntimeError('service build cannot define both "boilerplates" and legacy "templates"')
    value = boilerplates if boilerplates is not None else templates
    return value if isinstance(value, list) else []


def boilerplate_entries(manifests: list[dict[str, Any]]) -> list[dict[str, str]]:
    boilerplates: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for manifest in manifests:
        for boilerplate in build_boilerplates(manifest):
            repo = str(boilerplate.get("repo", "")).strip()
            title = str(
                boilerplate.get("title")
                or boilerplate.get("name")
                or "Starter boilerplate"
            ).strip()
            key = (title, repo)
            if not repo or key in seen:
                continue
            seen.add(key)
            boilerplates.append({"title": title, "repo": repo})
    return boilerplates


def service_name_index(service_repositories: list[str]) -> dict[str, str]:
    result: dict[str, str] = {}
    for repo_name in service_repositories:
        repo_dir = WORKSPACE / repo_name
        for path in indexed_manifest_paths(repo_dir, "service"):
            name = str(load_yaml(path).get("name", "")).strip()
            if name:
                result[name] = repo_name
    return result


def related_stacks(service_names: set[str]) -> list[tuple[str, str]]:
    if not (STACKS_REPOSITORY / "README.md").exists():
        return []

    result: list[tuple[str, str]] = []
    for repo_name in repository_names(STACKS_REPOSITORY / "README.md", "stack-"):
        repo_dir = WORKSPACE / repo_name
        paths = indexed_manifest_paths(repo_dir, "stack")
        manifests = [load_yaml(path) for path in paths]
        references = {
            str(service.get("service", "")).split("@", 1)[0]
            for manifest in manifests
            for service in manifest.get("services", [])
        }
        if not service_names & references:
            continue
        titles = [str(manifest.get("title", "")).strip() for manifest in manifests]
        title = titles[0] if len(titles) == 1 else repo_name.removeprefix("stack-").title()
        result.append((title, repo_name))
    return sorted(set(result))


def format_versions(manifest: dict[str, Any]) -> str:
    options = manifest.get("options") or []
    versions = [str(option.get("version", "")).strip() for option in options if option.get("version")]
    if not versions:
        return "Not versioned"
    default = next(
        (str(option.get("version")) for option in options if option.get("default")),
        versions[0],
    )
    others = [version for version in versions if version != default]
    value = f"`{default}` by default"
    if others:
        value += "; also available: " + ", ".join(f"`{version}`" for version in others)
    return value


def format_workloads(manifest: dict[str, Any]) -> str:
    workloads = manifest.get("workloads") or []
    if not workloads:
        return "Inherited or externally managed"
    values = []
    for workload in workloads:
        name = str(workload.get("name", "main"))
        kind = str(workload.get("kind", "workload")).replace("_", " ").title()
        suffix = ", primary" if workload.get("primary") else ""
        values.append(f"`{name}` ({kind}{suffix})")
    return "; ".join(values)


def format_containers(manifest: dict[str, Any]) -> str:
    containers = []
    for workload in manifest.get("workloads") or []:
        for container in workload.get("containers") or []:
            name = str(container.get("name", "container"))
            image = str(container.get("image", "")).strip()
            value = f"`{name}`"
            if image:
                value += f" using `{image}`"
            if container.get("build"):
                value += ", build target"
            containers.append(value)
    return "; ".join(containers) if containers else "Inherited or chart-managed"


def format_endpoints(manifest: dict[str, Any]) -> str:
    endpoints = []
    for endpoint in manifest.get("endpoints") or []:
        ports = []
        for port in endpoint.get("ports") or []:
            protocol = str(port.get("protocol", "")).upper()
            number = port.get("number")
            value = " ".join(part for part in (protocol, str(number or "")) if part)
            if port.get("main"):
                value += " (main)"
            ports.append(value)
        endpoints.append(f"`{endpoint.get('name', 'endpoint')}`: {', '.join(ports)}")
    return "; ".join(endpoints) if endpoints else "None"


def format_links(manifest: dict[str, Any]) -> str:
    links = []
    for link in manifest.get("links") or []:
        title = str(link.get("title") or link.get("name") or "link")
        requirement = "required" if link.get("required") else "optional"
        links.append(f"{title} (`{link.get('name', '')}`), {requirement}")
    return "; ".join(links) if links else "None"


def format_build(manifest: dict[str, Any]) -> str:
    build = manifest.get("build") or {}
    if not build:
        return "Not buildable from application source"
    values = []
    if build.get("connect"):
        values.append("Git source connection enabled")
    if build.get("dockerfile"):
        values.append(f"Dockerfile: `{build['dockerfile']}`")
    boilerplates = boilerplate_entries([manifest])
    if boilerplates:
        values.append(
            "boilerplates: "
            + ", ".join(
                f"[{entry['title']}]({entry['repo']})"
                for entry in boilerplates
            )
        )
    return "; ".join(values) if values else "Build configuration provided"


def generated_overview(
    repo_dir: Path,
    manifests: list[dict[str, Any]],
    manifest_paths: list[Path],
) -> str:
    plural = len(manifests) > 1
    lines = ["## Service entries" if plural else "## Service overview", ""]
    for manifest, path in zip(manifests, manifest_paths):
        if plural:
            lines.extend([f"### {manifest.get('title', manifest.get('name', 'Service'))}", ""])
        lines.extend(
            [
                "| Property | Manifest configuration |",
                "| --- | --- |",
                f"| Service name | `{manifest.get('name', '')}` |",
                (
                    "| Type | "
                    + SERVICE_TYPE_LABELS.get(
                        str(manifest.get("type", "service")),
                        str(manifest.get("type", "service")).replace("_", " ").title(),
                    )
                    + " |"
                ),
            ]
        )
        if manifest.get("from"):
            inherited = f"`{manifest['from']}`"
            if manifest.get("fromVersionConstraint"):
                inherited += f" with version constraint `{manifest['fromVersionConstraint']}`"
            lines.append(f"| Inherits from | {inherited} |")
        lines.extend(
            [
                f"| Versions | {format_versions(manifest)} |",
                f"| Workloads | {format_workloads(manifest)} |",
                f"| Containers | {format_containers(manifest)} |",
                f"| Endpoints | {format_endpoints(manifest)} |",
                f"| Service links | {format_links(manifest)} |",
                f"| Application build | {format_build(manifest)} |",
            ]
        )
        helm = manifest.get("helm") or {}
        if helm:
            chart = helm.get("chart") or helm.get("source") or helm.get("name")
            version = f"; version `{helm['version']}`" if helm.get("version") else ""
            lines.append(f"| Helm | chart `{chart}`{version} |")
        counts = []
        for key, label in (
            ("configs", "configuration files"),
            ("settings", "settings"),
            ("integrations", "integration slots"),
            ("volumes", "volumes"),
            ("actions", "actions"),
            ("cron", "cron schedules"),
        ):
            if manifest.get(key):
                counts.append(f"{len(manifest[key])} {label}")
        if counts:
            lines.append(f"| Configuration and operations | {', '.join(counts)} |")
        relative = path.relative_to(repo_dir).as_posix()
        if relative != "service.yml":
            lines.extend(["", f"Manifest: [`{relative}`]({relative})"])
        lines.append("")
    return "\n".join(lines).rstrip()


def preserved_overview(readme: str) -> str | None:
    start = re.search(r"^## (?:Service overview|Service entries)\s*$", readme, re.MULTILINE)
    if not start:
        return None
    end = re.search(r"^## (?:Use this service|Role in Wodby infrastructure)\s*$", readme[start.end() :], re.MULTILINE)
    if not end:
        return readme[start.start() :].strip()
    return readme[start.start() : start.end() + end.start()].strip()


def preserved_custom_use_sections(readme: str) -> str | None:
    use = re.search(r"^## Use this service\s*$", readme, re.MULTILINE)
    if not use:
        return None
    maintain = re.search(
        r"^## Maintain a custom version\s*$",
        readme[use.end() :],
        re.MULTILINE,
    )
    if not maintain:
        return None
    block = readme[use.end() : use.end() + maintain.start()]
    custom = re.search(r"^## .+\s*$", block, re.MULTILINE)
    return block[custom.start() :].strip() if custom else None


def validation_commands(repo_dir: Path, manifest_paths: list[Path]) -> str:
    return "\n".join(
        f"wodby service validate-manifest {path.relative_to(repo_dir).as_posix()} --org <org-id>"
        for path in manifest_paths
    )


def render_service_readme(repo_name: str) -> tuple[str, bool, str]:
    repo_dir = WORKSPACE / repo_name
    readme_path = repo_dir / "README.md"
    old_readme = readme_path.read_text(encoding="utf-8") if readme_path.exists() else ""
    manifest_paths = indexed_manifest_paths(repo_dir, "service")
    if not manifest_paths:
        raise RuntimeError(f"{repo_name}: no service manifests found")
    manifests = [load_yaml(path) for path in manifest_paths]
    infrastructure = any(manifest.get("type") == "infrastructure" for manifest in manifests)
    display_name = repository_display_name(repo_name, manifests, old_readme)
    summary = service_summary(repo_name, display_name, manifests)
    names = {str(manifest.get("name", "")).strip() for manifest in manifests}
    boilerplates = boilerplate_entries(manifests)
    stacks = related_stacks(names)
    overview = preserved_overview(old_readme) or generated_overview(repo_dir, manifests, manifest_paths)
    custom_use_sections = preserved_custom_use_sections(old_readme)

    title = (
        f"# {display_name} Kubernetes system service for Wodby"
        if infrastructure
        else f"# {display_name} service for Kubernetes on Wodby"
    )
    lines = [
        title,
        "",
        wrapped(summary),
        "",
        wrapped(
            f"This repository defines the Wodby service manifests and operational "
            f"configuration for {display_name}."
        ),
        "",
        (
            "- [Wodby Kubernetes platform](https://wodby.com)"
            if infrastructure
            else "- [Browse Wodby services](https://wodby.com/services)"
        ),
        "- [Wodby service documentation](https://wodby.com/docs/2.0/services/)",
        "- [Service manifest reference](https://wodby.com/docs/2.0/services/template/)",
    ]

    if boilerplates and not infrastructure:
        lines.extend(["", "## Start with a boilerplate", ""])
        lines.append(
            wrapped(
                "Use one of the boilerplates exposed by this service to start "
                "with compatible build configuration and Wodby CI:"
            )
        )
        lines.append("")
        for boilerplate in boilerplates:
            lines.append(
                f"- [{boilerplate['title']}]({boilerplate['repo']})"
            )

    if stacks:
        heading = (
            "## Wodby system stacks using this service"
            if infrastructure
            else "## Wodby stacks using this service"
        )
        lines.extend(["", heading, ""])
        for title_value, stack_repo in stacks:
            role = "system stack" if infrastructure else "application stack"
            lines.append(
                f"- [{title_value} {role}](https://github.com/wodby/{stack_repo})"
            )

    lines.extend(["", overview, ""])

    commands = validation_commands(repo_dir, manifest_paths)
    manifest_names = ", ".join(f"`{name}`" for name in sorted(names))
    if infrastructure:
        lines.extend(
            [
                "## Role in Wodby infrastructure",
                "",
                wrapped(
                    "Wodby installs this service through a Kubernetes system stack "
                    "when it is required by the cluster provider or selected "
                    "infrastructure configuration. It runs as a cluster-owned system "
                    "app and is not offered as a user-deployable application service."
                ),
                "",
                "## Platform maintenance",
                "",
                wrapped(
                    "Changes to this repository can affect cluster provisioning, "
                    "upgrades, networking, or observability. Coordinate manifest and "
                    "Helm changes with every dependent system stack and preserve "
                    "service, workload, endpoint, config, and volume identifiers."
                ),
                "",
                "Wodby platform maintainers can validate the manifests with:",
            ]
        )
    else:
        if len(stacks) > 3:
            use_sentence = (
                "Use this service through one of the Wodby application stacks listed "
                f"above, or reference {manifest_names} from a custom Wodby stack."
            )
        elif stacks:
            stack_links = ", ".join(
                f"[{title_value} application stack](https://github.com/wodby/{stack_repo})"
                for title_value, stack_repo in stacks
            )
            use_sentence = (
                f"Use this service through {stack_links}, or reference "
                f"{manifest_names} from a custom Wodby stack."
            )
        else:
            use_sentence = (
                f"Reference {manifest_names} from a Wodby stack to use this service."
            )
        lines.extend(
            [
                "## Use this service",
                "",
                wrapped(use_sentence),
                "",
                wrapped(
                    "A service is a reusable component and does not deploy by itself. "
                    "The stack defines its links, settings, versions, resources, and "
                    "relationship to the rest of the application."
                ),
            ]
        )
        if custom_use_sections:
            lines.extend(["", custom_use_sections])
        lines.extend(
            [
                "",
                "## Maintain a custom version",
                "",
                "1. Fork this repository.",
                "2. Edit the service manifest and referenced files.",
                "3. Import the repository as a "
                "[Git-backed service](https://wodby.com/docs/2.0/services/create/#create-a-git-backed-service).",
                "4. Reference the service from a stack manifest.",
                "",
                wrapped(
                    "Keep service, workload, container, endpoint, link, volume, config, "
                    "and derivative names stable unless dependent stacks and app-level "
                    "overrides are updated at the same time."
                ),
                "",
                "Validate the manifests with:",
            ]
        )

    lines.extend(
        [
            "",
            "```bash",
            commands,
            "```",
            "",
            (
                "See the [service manifest "
                "reference](https://wodby.com/docs/2.0/services/template/) and the "
                "[managed services index](https://github.com/wodby/services)."
            ),
            "",
        ]
    )
    rendered = "\n".join(lines)
    return rendered, infrastructure, display_name


def main() -> int:
    parser = argparse.ArgumentParser()
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--check", action="store_true", help="Fail if a README is out of date")
    mode.add_argument("--write", action="store_true", help="Write rendered README files")
    parser.add_argument("repositories", nargs="*", help="Optional service repository names")
    args = parser.parse_args()

    available = repository_names(SERVICES_REPOSITORY / "README.md", "service-")
    repositories = args.repositories or available
    unknown = sorted(set(repositories) - set(available))
    if unknown:
        parser.error(f"repositories are not in the managed index: {', '.join(unknown)}")

    changed: list[str] = []
    infrastructure_count = 0
    for repo_name in repositories:
        readme, infrastructure, _ = render_service_readme(repo_name)
        infrastructure_count += int(infrastructure)
        readme_path = WORKSPACE / repo_name / "README.md"
        current = readme_path.read_text(encoding="utf-8") if readme_path.exists() else ""
        if current == readme:
            continue
        changed.append(repo_name)
        if args.write:
            readme_path.write_text(readme, encoding="utf-8")

    action = "updated" if args.write else "out of date"
    for repo_name in changed:
        print(f"{repo_name}: README {action}")
    print(
        f"checked {len(repositories)} service repositories; "
        f"{infrastructure_count} infrastructure; {len(changed)} {action}"
    )
    return 1 if args.check and changed else 0


if __name__ == "__main__":
    sys.exit(main())
