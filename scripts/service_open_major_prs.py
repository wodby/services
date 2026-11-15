#!/usr/bin/env python3
"""Open a pull request in a service repository for each Helm chart major update the report found.

The update robot releases chart minor and patch updates itself. A new chart major version can change the chart's
values or resources, so the robot proposes it in a pull request for a person to review, test and merge instead. Each
pull request has a branch named after the chart and the target major version, so a later run finds it and never opens
a duplicate. A closed pull request is a reviewer's decision, so the robot doesn't reopen it for the same major version.
"""

import argparse
import json
import os
import re
import sys
from pathlib import Path
from typing import Any

import requests

from service_apply_updates import (
    YAML_RT,
    configure_git_identity,
    default_branch,
    ensure_clean_worktree,
    fetch_origin,
    load_report,
    repo_item,
    run_git,
    set_yaml_value,
    write_report,
)
from service_update_report import major_version

GITHUB_API = "https://api.github.com"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Open pull requests for a service repo's Helm chart major updates.")
    parser.add_argument("--report-dir", required=True, help="Directory containing service-update-report.json.")
    parser.add_argument("--repo", required=True, help="Service repository name, for example service-nginx.")
    parser.add_argument("--repo-dir", required=True, help="Checked-out service repository directory.")
    parser.add_argument("--owner", default="wodby", help="GitHub owner/org that owns the service repo.")
    return parser.parse_args()


def branch_name(update: dict[str, Any]) -> str:
    """Name the pull request's branch after the chart and the target major, such as chore/helm-chart-alloy-2."""
    chart = str(update["helm_chart"]).rstrip("/").rsplit("/", 1)[-1]
    chart = re.sub(r"[^A-Za-z0-9._-]+", "-", chart).strip("-") or "chart"
    major = major_version(str(update["target"]))
    label = str(update.get("service_label") or "")
    label = re.sub(r"[^A-Za-z0-9._-]+", "-", label).strip("-")
    parts = [chart, label] if label else [chart]
    return f"chore/helm-chart-{'-'.join(parts)}-{major}"


class GitHub:
    """The few GitHub API calls the robot needs, authenticated as the robot."""

    def __init__(self, token: str, session: Any = None) -> None:
        self.session = session or requests.Session()
        self.session.headers.update({
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        })

    def pull_requests(self, owner: str, repo: str, branch: str) -> list[dict[str, Any]]:
        response = self.session.get(
            f"{GITHUB_API}/repos/{owner}/{repo}/pulls",
            params={"state": "all", "head": f"{owner}:{branch}", "per_page": 20},
            timeout=30,
        )
        response.raise_for_status()
        return list(response.json())

    def create_pull_request(self, owner: str, repo: str, payload: dict[str, Any]) -> dict[str, Any]:
        response = self.session.post(f"{GITHUB_API}/repos/{owner}/{repo}/pulls", json=payload, timeout=30)
        response.raise_for_status()
        return dict(response.json())


def remote_branch_exists(repo_dir: Path, branch: str) -> bool:
    process = run_git(repo_dir, "ls-remote", "--exit-code", "--heads", "origin", branch, check=False)
    return process.returncode == 0


def pull_request_body(update: dict[str, Any], before: str) -> str:
    chart = update["helm_chart"]
    target = update["target"]
    return "\n".join([
        f"The `{chart}` Helm chart has a new major version, `{target}`. This service uses `{before}`.",
        "",
        "A new major chart version can rename or remove values, change resources, or need a newer Kubernetes "
        "version, so the update robot doesn't release it on its own. Until this pull request is merged, the robot "
        "keeps the service on the newest chart of its current major version.",
        "",
        "## Before merging",
        "",
        "- Read the chart's changes between the two versions, especially breaking changes and upgrade notes.",
        "- Update the service's values, links, CRD charts and docs as the new version needs. This pull request "
        "changes only `helm.version`; push those changes to this branch.",
        "- Validate the manifest and test an install and an upgrade of an existing instance.",
        "",
        "## After merging",
        "",
        "Tag a release of this repository. The robot releases only the changes it makes itself, so it doesn't tag "
        "merged pull requests.",
        "",
        f"Proposed by the service update robot: {update.get('message', '')}",
    ])


def propose_major_update(
    github: GitHub,
    repo_dir: Path,
    owner: str,
    repo: str,
    base: str,
    update: dict[str, Any],
) -> dict[str, Any]:
    """Open one major update pull request, or say why none was opened."""
    branch = branch_name(update)
    result: dict[str, Any] = {"branch": branch, "target": update["target"], "helm_chart": update["helm_chart"]}

    pulls = github.pull_requests(owner, repo, branch)
    for pull in pulls:
        if pull.get("state") == "open":
            return {**result, "status": "already_open", "url": pull.get("html_url"),
                    "message": f"Pull request {pull.get('html_url')} already proposes this update."}
    if pulls:
        url = pulls[0].get("html_url")
        return {**result, "status": "closed", "url": url,
                "message": f"Pull request {url} for this major version was closed; not reopening it."}
    if remote_branch_exists(repo_dir, branch):
        return {**result, "status": "branch_exists",
                "message": f"Branch {branch} exists without a pull request; leaving it to whoever pushed it."}

    run_git(repo_dir, "checkout", "-B", branch, f"origin/{base}")
    try:
        manifest_path = str(update.get("manifest_path") or "service.yml")
        path = repo_dir / manifest_path
        data = YAML_RT.load(path.read_text()) or {}
        helm = data.get("helm") if isinstance(data, dict) else None
        if not isinstance(helm, dict) or helm.get("version") is None:
            raise RuntimeError(f"{manifest_path} has no helm.version to update")
        before = str(helm.get("version"))
        before_major, target_major = major_version(before), major_version(str(update["target"]))
        if before_major is not None and target_major is not None and before_major >= target_major:
            return {**result, "status": "current",
                    "message": f"{manifest_path} already uses `{before}`; no pull request is needed."}
        set_yaml_value(data, {"path": "helm.version", "key": "version", "before": before, "after": update["target"]})
        with path.open("w") as fh:
            YAML_RT.dump(data, fh)

        configure_git_identity(repo_dir)
        run_git(repo_dir, "add", manifest_path)
        title = f"Update the {update['helm_chart']} Helm chart to {update['target']}"
        run_git(repo_dir, "commit", "-m", title, "-m",
                f"A new major chart version, proposed for review by the service update robot. It was `{before}`.")
        run_git(repo_dir, "push", "origin", f"HEAD:refs/heads/{branch}")
        pull = github.create_pull_request(owner, repo, {
            "title": title,
            "head": branch,
            "base": base,
            "body": pull_request_body(update, before),
            "maintainer_can_modify": True,
        })
        return {**result, "status": "opened", "url": pull.get("html_url"),
                "message": f"Opened {pull.get('html_url')} to update `{update['helm_chart']}` to `{update['target']}`."}
    finally:
        run_git(repo_dir, "checkout", "--force", base, check=False)


def open_major_pull_requests(args: argparse.Namespace, github: GitHub | None = None) -> list[dict[str, Any]]:
    report_dir = Path(args.report_dir)
    report = load_report(report_dir)
    item = repo_item(report, args.repo)
    updates = item.get("planned_major_updates") or []
    if not updates:
        return []

    if github is None:
        token = os.environ.get("WODBOT_GITHUB_PAT")
        if not token:
            raise RuntimeError("WODBOT_GITHUB_PAT is required to open major update pull requests")
        github = GitHub(token)

    repo_dir = Path(args.repo_dir)
    ensure_clean_worktree(repo_dir)
    fetch_origin(repo_dir)
    base = default_branch(repo_dir)
    results = []
    for update in updates:
        try:
            results.append(propose_major_update(github, repo_dir, args.owner, args.repo, base, update))
        except Exception as exc:  # One failing chart doesn't stop the others; the report records it.
            results.append({"branch": branch_name(update), "target": update.get("target"),
                            "helm_chart": update.get("helm_chart"), "status": "failed", "message": str(exc)})
    item["major_pull_requests"] = results
    write_report(report_dir, report)
    return results


def main() -> int:
    args = parse_args()
    results = open_major_pull_requests(args)
    for result in results:
        print(result["message"])
    print(json.dumps(results, indent=2))
    return 1 if any(result["status"] == "failed" for result in results) else 0


if __name__ == "__main__":
    sys.exit(main())
