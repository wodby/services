#!/usr/bin/env python3
"""Validate and prepare both GitLab repositories before publishing either release."""

import argparse
import hashlib
import json
import sys
from pathlib import Path

from service_apply_updates import (
    apply_manifest_changes,
    commit_push_and_tag,
    load_report,
    prepare_repo_for_apply,
    repo_item,
    run_git,
    validate_planned_image_tags,
    validate_planned_release,
    write_report,
)
from service_update_report import GITLAB_REPOS


def apply_batch(report: dict, repos_dir: Path, *, releases_disabled: bool = False) -> None:
    """Preflight every member, prepare every release, then publish in dependency order."""
    batch = report.get("update_batches", {}).get("gitlab", {})
    if batch.get("status") != "ready" or batch.get("repos") != list(GITLAB_REPOS):
        raise RuntimeError(f"GitLab batch is not ready: {batch.get('reason') or 'missing complete batch plan'}")
    items = {repo: repo_item(report, repo) for repo in GITLAB_REPOS}
    if releases_disabled:
        for repo, item in items.items():
            item["apply_result"] = {"status": "disabled", "repo": repo, "message": "Automatic releases are disabled; the GitLab batch was not published."}
        return

    pending = {repo: item for repo, item in items.items() if item.get("planned_changes")}
    # A tag lookup failure for one member must block the entire group.
    for repo, item in pending.items():
        if (item.get("planned_release") or {}).get("status") != "planned":
            raise RuntimeError(f"{repo} has changes but no valid release plan")
    if not pending:
        for repo, item in items.items():
            item["apply_result"] = {"status": "skipped", "repo": repo, "message": "GitLab batch is current."}
        return

    branches = {}
    for repo in GITLAB_REPOS:
        repo_dir = repos_dir / repo
        branches[repo] = prepare_repo_for_apply(repo_dir)
        digest = hashlib.sha256((repo_dir / "service.yml").read_bytes()).hexdigest()
        if digest != batch.get("manifests", {}).get(repo):
            raise RuntimeError(f"{repo} changed since batch validation; regenerate the whole batch report")
        if repo in pending:
            validate_planned_release(repo_dir, items[repo]["planned_release"])
            validate_planned_image_tags(report["owner"], items[repo]["planned_changes"])

    changed = {}
    for repo, item in pending.items():
        changed[repo] = apply_manifest_changes(repos_dir / repo, item["planned_changes"])
    prepared = {}
    for repo, item in pending.items():
        prepared[repo] = commit_push_and_tag(
            repos_dir / repo, repo, branches[repo], item["planned_release"], changed[repo], prepare_only=True,
        )
        item["apply_result"] = prepared[repo]

    for repo, result in prepared.items():
        if result["status"] == "prepared":
            run_git(
                repos_dir / repo, "push", "--dry-run", "--atomic", "origin",
                f"HEAD:{branches[repo]}", f"refs/tags/{result['tag']}",
            )

    # GitHub cannot atomically push across repositories. Finish all local work
    # first, and retain each result if a later push fails so retries can replan.
    for repo, result in prepared.items():
        if result["status"] == "already_applied":
            continue
        repo_dir = repos_dir / repo
        # Detect concurrent default-branch changes even for an unchanged partner.
        for member in GITLAB_REPOS:
            remote = run_git(repos_dir / member, "ls-remote", "origin", f"refs/heads/{branches[member]}").stdout.split()
            member_result = prepared.get(member, {})
            if member_result.get("status") == "applied":
                expected = member_result["commit"]
            else:
                expected = run_git(
                    repos_dir / member, "rev-parse", f"origin/{branches[member]}",
                ).stdout.strip()
            if not remote or remote[0] != expected:
                raise RuntimeError(f"{member} remote branch changed before publishing; regenerate the batch report")
        run_git(repo_dir, "push", "--atomic", "origin", f"HEAD:{branches[repo]}", f"refs/tags/{result['tag']}")
        result["status"] = "applied"
        result["message"] = f"Published validated GitLab batch member with tag {result['tag']}."
    for repo in set(GITLAB_REPOS) - set(pending):
        items[repo]["apply_result"] = {"status": "skipped", "repo": repo, "message": "Validated batch member is already current."}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report-dir", required=True)
    parser.add_argument("--repos-dir", default="service-repos")
    parser.add_argument("--releases-disabled", action="store_true")
    args = parser.parse_args()
    report_dir = Path(args.report_dir)
    report = load_report(report_dir)
    try:
        apply_batch(report, Path(args.repos_dir), releases_disabled=args.releases_disabled)
    except Exception as exc:
        for item in report.get("per_repo", []):
            repo = item.get("repo")
            if repo not in GITLAB_REPOS:
                continue
            if (item.get("apply_result") or {}).get("status") not in {"applied", "tagged", "already_applied"}:
                item["apply_result"] = {"status": "failed", "repo": repo, "message": str(exc)}
        print(f"GitLab batch failed: {exc}", file=sys.stderr)
        write_report(report_dir, report)
        return 1
    write_report(report_dir, report)
    print(json.dumps([repo_item(report, repo)["apply_result"] for repo in GITLAB_REPOS], indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
