#!/usr/bin/env python3

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

from ruamel.yaml import YAML
from ruamel.yaml.scalarstring import ScalarString

from service_update_report import UpdateReportGenerator, latest_stable_semver_tag, render_markdown


OPTION_PATH_RE = re.compile(r"^options\[version=(?P<version>.+)]\.(?P<field>[A-Za-z0-9_-]+)$")
CRD_CHART_PATH_RE = re.compile(r"^crdCharts\[name=(?P<name>.+)]\.(?P<field>[A-Za-z0-9_-]+)$")
YAML_RT = YAML()
YAML_RT.preserve_quotes = True


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Apply planned service update report changes to a service repo.")
    parser.add_argument("--report-dir", required=True, help="Directory containing service-update-report.json.")
    parser.add_argument("--repo", required=True, help="Service repository name, for example service-nginx.")
    parser.add_argument("--repo-dir", required=True, help="Checked-out service repository directory.")
    parser.add_argument("--owner", default="wodby", help="GitHub owner/org that owns the service repo.")
    return parser.parse_args()


def run_git(repo_dir: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    process = subprocess.run(
        ["git", *args],
        cwd=repo_dir,
        check=False,
        capture_output=True,
        text=True,
    )
    if check and process.returncode != 0:
        output = (process.stderr or process.stdout).strip()
        raise RuntimeError(f"git {' '.join(args)} failed: {output}")
    return process


def normalize(value: Any) -> str | None:
    if value is None:
        return None
    return str(value)


def replacement_value(current: Any, after: Any) -> Any:
    if isinstance(current, ScalarString) and after is not None:
        return type(current)(str(after))
    return after


def load_report(report_dir: Path) -> dict[str, Any]:
    return json.loads((report_dir / "service-update-report.json").read_text())


def write_report(report_dir: Path, report: dict[str, Any]) -> None:
    apply_results = [
        item.get("apply_result")
        for item in report.get("per_repo") or []
        if isinstance(item, dict) and isinstance(item.get("apply_result"), dict)
    ]
    report.setdefault("totals", {})["applied_updates"] = sum(
        1 for result in apply_results if result.get("status") in ("applied", "tagged", "already_applied")
    )
    report.setdefault("totals", {})["apply_failures"] = sum(
        1 for result in apply_results if result.get("status") == "failed"
    )

    (report_dir / "service-update-report.json").write_text(json.dumps(report, indent=2))
    (report_dir / "service-update-report.md").write_text(render_markdown(report))


def repo_item(report: dict[str, Any], repo: str) -> dict[str, Any]:
    for item in report.get("per_repo") or []:
        if isinstance(item, dict) and item.get("repo") == repo:
            return item
    raise RuntimeError(f"report does not contain per_repo details for {repo}")


def fetch_origin(repo_dir: Path) -> None:
    run_git(repo_dir, "fetch", "origin", "--prune", "--tags")


def ensure_clean_worktree(repo_dir: Path) -> None:
    status = run_git(repo_dir, "status", "--porcelain").stdout.strip()
    if status:
        raise RuntimeError("checked-out service repo has uncommitted changes; refusing to apply updates")


def default_branch(repo_dir: Path) -> str:
    remote_head_process = run_git(repo_dir, "symbolic-ref", "--short", "refs/remotes/origin/HEAD", check=False)
    remote_head = remote_head_process.stdout.strip()
    if remote_head.startswith("origin/"):
        return remote_head.split("/", 1)[1]

    for candidate in ("master", "main"):
        if run_git(repo_dir, "rev-parse", "--verify", f"refs/remotes/origin/{candidate}", check=False).returncode == 0:
            return candidate
    raise RuntimeError("unable to determine default branch from origin/HEAD, origin/master, or origin/main")


def ensure_branch(repo_dir: Path) -> str:
    branch = default_branch(repo_dir)
    origin_ref = f"origin/{branch}"
    current_branch = run_git(repo_dir, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip()

    if current_branch == "HEAD":
        run_git(repo_dir, "checkout", "-B", branch, origin_ref)
        return branch

    if current_branch != branch:
        raise RuntimeError(f"checked-out branch {current_branch} is not the default branch {branch}")

    current_sha = run_git(repo_dir, "rev-parse", "HEAD").stdout.strip()
    origin_sha = run_git(repo_dir, "rev-parse", origin_ref).stdout.strip()
    if current_sha != origin_sha:
        raise RuntimeError(f"checked-out branch {branch} is not at {origin_ref}; refusing to apply stale updates")
    return branch


def prepare_repo_for_apply(repo_dir: Path) -> str:
    ensure_clean_worktree(repo_dir)
    fetch_origin(repo_dir)
    branch = ensure_branch(repo_dir)
    ensure_clean_worktree(repo_dir)
    return branch


def set_yaml_value(data: dict[str, Any], change: dict[str, Any]) -> None:
    path = str(change.get("path") or "")
    key = str(change.get("key") or "")
    expected_before = normalize(change.get("before"))
    after = change.get("after")

    if path == "fromVersion" and key == "fromVersion":
        current = data.get("fromVersion")
        if normalize(current) != expected_before:
            raise RuntimeError(f"fromVersion changed since report generation: expected {expected_before}, found {current}")
        data["fromVersion"] = replacement_value(current, after)
        return

    if path == "helm.version" and key == "version":
        helm = data.get("helm")
        if not isinstance(helm, dict):
            raise RuntimeError("helm.version cannot be updated because helm is not a mapping")
        current = helm.get("version")
        if normalize(current) != expected_before:
            raise RuntimeError(f"helm.version changed since report generation: expected {expected_before}, found {current}")
        helm["version"] = replacement_value(current, after)
        return

    match = CRD_CHART_PATH_RE.match(path)
    if match and key == match.group("field"):
        crd_charts = data.get("crdCharts")
        if not isinstance(crd_charts, list):
            raise RuntimeError(f"{path} cannot be updated because crdCharts is not a list")
        wanted_name = match.group("name")
        matches = [
            chart
            for chart in crd_charts
            if isinstance(chart, dict) and normalize(chart.get("name")) == wanted_name
        ]
        if len(matches) > 1:
            raise RuntimeError(f"{path} cannot be updated because name {wanted_name} appears more than once")
        if not matches:
            raise RuntimeError(f"{path} cannot be updated because name {wanted_name} was not found")
        crd_chart = matches[0]
        current = crd_chart.get(key)
        if normalize(current) != expected_before:
            raise RuntimeError(
                f"{path} changed since report generation: expected {expected_before}, found {current}"
            )
        crd_chart[key] = replacement_value(current, after)
        return

    match = OPTION_PATH_RE.match(path)
    if match and key == match.group("field"):
        options = data.get("options")
        if not isinstance(options, list):
            raise RuntimeError(f"{path} cannot be updated because options is not a list")
        wanted_version = match.group("version")
        matches = [
            option
            for option in options
            if isinstance(option, dict) and normalize(option.get("version")) == wanted_version
        ]
        if len(matches) > 1:
            raise RuntimeError(f"{path} cannot be updated because version {wanted_version} appears more than once")
        if not matches:
            raise RuntimeError(f"{path} cannot be updated because version {wanted_version} was not found")
        option = matches[0]
        current = option.get(key)
        if normalize(current) != expected_before:
            raise RuntimeError(
                f"{path} changed since report generation: expected {expected_before}, found {current}"
            )
        option[key] = replacement_value(current, after)
        return

    raise RuntimeError(f"unsupported planned change path: {path}")


def apply_manifest_changes(repo_dir: Path, planned_changes: list[dict[str, Any]]) -> list[str]:
    changed_files: list[str] = []
    changes_by_file: dict[str, list[dict[str, Any]]] = {}
    for change in planned_changes:
        manifest_path = str(change.get("file") or "service.yml")
        changes_by_file.setdefault(manifest_path, []).append(change)

    for manifest_path, changes in changes_by_file.items():
        path = repo_dir / manifest_path
        if not path.is_file():
            raise RuntimeError(f"{manifest_path} does not exist in the checked-out service repo")

        data = YAML_RT.load(path.read_text()) or {}
        if not isinstance(data, dict):
            raise RuntimeError(f"{manifest_path} did not decode to a mapping")
        if data.get("from") and any(change.get("change_type") == "eol" for change in changes):
            raise RuntimeError(f"{manifest_path} is a child service manifest; refusing to apply option EOL updates")

        for change in changes:
            set_yaml_value(data, change)

        with path.open("w") as fh:
            YAML_RT.dump(data, fh)
        changed_files.append(manifest_path)

    return changed_files


def remote_tag_exists(repo_dir: Path, tag: str) -> bool:
    process = run_git(repo_dir, "ls-remote", "--tags", "--refs", "origin", tag, check=False)
    if process.returncode != 0:
        output = (process.stderr or process.stdout).strip()
        raise RuntimeError(f"unable to check remote tag {tag}: {output}")
    return bool(process.stdout.strip())


def remote_tags(repo_dir: Path) -> set[str]:
    process = run_git(repo_dir, "ls-remote", "--tags", "--refs", "origin", check=False)
    if process.returncode != 0:
        output = (process.stderr or process.stdout).strip()
        raise RuntimeError(f"unable to list remote tags: {output}")
    return {
        line.split("refs/tags/", 1)[1]
        for line in process.stdout.splitlines()
        if "refs/tags/" in line
    }


def local_tag_exists(repo_dir: Path, tag: str) -> bool:
    return run_git(repo_dir, "rev-parse", "--verify", f"refs/tags/{tag}", check=False).returncode == 0


def validate_planned_release(repo_dir: Path, release: dict[str, Any]) -> None:
    tag = normalize(release.get("tag"))
    previous_tag = normalize(release.get("previous_tag"))
    if not tag or not previous_tag:
        raise RuntimeError("planned release must include tag and previous_tag")

    tags = remote_tags(repo_dir)
    if tag in tags:
        return

    latest_tag = latest_stable_semver_tag(tags)
    if latest_tag is None:
        raise RuntimeError("no existing stable semantic git tag was found; planned release cannot be validated")

    remote_previous_tag, prefix, remote_previous_version = latest_tag
    expected_tag = f"{prefix}{remote_previous_version.major}.{remote_previous_version.minor}.{remote_previous_version.micro + 1}"
    if remote_previous_tag != previous_tag:
        raise RuntimeError(
            f"planned release is stale: report used previous tag {previous_tag}, "
            f"but latest remote stable tag is {remote_previous_tag}; regenerate the report"
        )
    if tag != expected_tag:
        raise RuntimeError(
            f"planned release tag {tag} no longer matches next patch tag {expected_tag}; regenerate the report"
        )


def validate_planned_image_tags(
    owner: str,
    planned_changes: list[dict[str, Any]],
    generator: UpdateReportGenerator | None = None,
) -> None:
    image_changes = [
        change
        for change in planned_changes
        if isinstance(change, dict) and change.get("change_type") == "image_tag"
    ]
    if not image_changes:
        return

    tag_source = generator or UpdateReportGenerator(owner)
    for change in image_changes:
        image = normalize(change.get("image"))
        tag = normalize(change.get("after"))
        if not image or not tag:
            raise RuntimeError("planned image tag change must include image and target tag")

        published_tags = set(tag_source.get_image_tags(image))
        if tag not in published_tags:
            raise RuntimeError(f"planned image tag `{tag}` was not found for image `{image}`")


def configure_git_identity(repo_dir: Path) -> None:
    name = os.environ.get("GIT_AUTHOR_NAME") or os.environ.get("GIT_COMMITTER_NAME")
    email = os.environ.get("GIT_AUTHOR_EMAIL") or os.environ.get("GIT_COMMITTER_EMAIL")

    if name:
        run_git(repo_dir, "config", "user.name", name)
    if email:
        run_git(repo_dir, "config", "user.email", email)

    configured_name = run_git(repo_dir, "config", "user.name", check=False).stdout.strip()
    configured_email = run_git(repo_dir, "config", "user.email", check=False).stdout.strip()
    if not configured_name or not configured_email:
        raise RuntimeError(
            "git user.name and user.email must be configured, or provided with "
            "GIT_AUTHOR_NAME/GIT_AUTHOR_EMAIL"
        )


def commit_push_and_tag(
    repo_dir: Path,
    repo: str,
    branch: str,
    release: dict[str, Any],
    changed_files: list[str],
) -> dict[str, Any]:
    configure_git_identity(repo_dir)

    run_git(repo_dir, "add", *changed_files)
    has_diff = run_git(repo_dir, "diff", "--cached", "--quiet", check=False).returncode != 0
    tag = str(release["tag"])
    tag_existed = remote_tag_exists(repo_dir, tag)

    if tag_existed and has_diff:
        raise RuntimeError(f"remote tag {tag} already exists; refusing to push a new commit without a release tag")

    if has_diff:
        commit_message = f"Update service manifest for {tag}"
        run_git(repo_dir, "commit", "-m", commit_message)
        commit_sha = run_git(repo_dir, "rev-parse", "HEAD").stdout.strip()
        committed = True
    else:
        commit_sha = run_git(repo_dir, "rev-parse", "HEAD").stdout.strip()
        committed = False

    if tag_existed:
        return {
            "status": "already_applied",
            "repo": repo,
            "branch": branch,
            "commit": commit_sha,
            "tag": tag,
            "changed_files": changed_files if committed else [],
            "message": f"Remote tag {tag} already exists; no tag was created.",
        }

    if local_tag_exists(repo_dir, tag):
        raise RuntimeError(f"local tag {tag} already exists but remote tag was not found")

    validate_planned_release(repo_dir, release)

    description = str(release.get("description") or f"Release {tag}")
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", delete=False) as fh:
        fh.write(description.rstrip() + "\n")
        release_notes_path = fh.name

    tag_created = False
    push_succeeded = False
    try:
        run_git(repo_dir, "tag", "-a", tag, "-F", release_notes_path)
        tag_created = True
        if committed:
            run_git(repo_dir, "push", "--atomic", "origin", f"HEAD:{branch}", f"refs/tags/{tag}")
        else:
            run_git(repo_dir, "push", "origin", f"refs/tags/{tag}")
        push_succeeded = True
    finally:
        if tag_created and not push_succeeded:
            run_git(repo_dir, "tag", "-d", tag, check=False)
        Path(release_notes_path).unlink(missing_ok=True)

    if committed:
        status = "applied"
        message = f"Committed manifest changes and atomically pushed branch and tag {tag}."
    else:
        status = "tagged"
        message = f"No manifest diff remained; pushed tag {tag} on the current HEAD."

    return {
        "status": status,
        "repo": repo,
        "branch": branch,
        "commit": commit_sha,
        "tag": tag,
        "changed_files": changed_files if committed else [],
        "message": message,
    }


def apply_updates(args: argparse.Namespace) -> tuple[dict[str, Any], dict[str, Any]]:
    report_dir = Path(args.report_dir)
    repo_dir = Path(args.repo_dir)
    report = load_report(report_dir)
    item = repo_item(report, args.repo)
    planned_changes = item.get("planned_changes") or []
    release = item.get("planned_release") or {}

    if not planned_changes:
        result = {"status": "skipped", "repo": args.repo, "message": "No planned manifest changes were found."}
        item["apply_result"] = result
        return report, result

    if release.get("status") != "planned":
        result = {
            "status": "skipped",
            "repo": args.repo,
            "message": release.get("reason") or "No planned git tag release was found.",
        }
        item["apply_result"] = result
        return report, result

    branch = prepare_repo_for_apply(repo_dir)
    validate_planned_release(repo_dir, release)
    validate_planned_image_tags(args.owner, planned_changes)
    changed_files = apply_manifest_changes(repo_dir, planned_changes)
    result = commit_push_and_tag(repo_dir, args.repo, branch, release, changed_files)
    item["apply_result"] = result
    return report, result


def main() -> int:
    args = parse_args()
    report_dir = Path(args.report_dir)
    try:
        report, result = apply_updates(args)
        write_report(report_dir, report)
        print(result["message"])
        print(json.dumps(result, indent=2))
        return 0
    except Exception as exc:
        report = load_report(report_dir)
        item = repo_item(report, args.repo)
        result = {
            "status": "failed",
            "repo": args.repo,
            "message": str(exc),
        }
        item["apply_result"] = result
        write_report(report_dir, report)
        print(f"Service update apply failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
