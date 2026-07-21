#!/usr/bin/env python3

import argparse
import html
import json
import os
import smtplib
import ssl
from email.message import EmailMessage
from pathlib import Path
from typing import Any


SUCCESSFUL_WORKFLOW_RESULTS = {"success", "skipped"}
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


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Send a consolidated service update report email.")
    parser.add_argument("reports_dir", help="Directory containing downloaded service report artifacts.")
    parser.add_argument("--run-url", default="", help="GitHub Actions run URL.")
    parser.add_argument("--event", default="", help="GitHub Actions event name.")
    parser.add_argument("--sha", default="", help="Git commit SHA.")
    parser.add_argument("--workflow-result", default="", help="Aggregated update job result.")
    parser.add_argument("--artifact-result", default="", help="Report artifact download step result.")
    return parser.parse_args()


def load_reports(reports_dir: Path) -> list[dict[str, Any]]:
    reports = []
    for path in sorted(reports_dir.rglob("service-update-report.json")):
        reports.append(json.loads(path.read_text()))
    return reports


def repo_items(reports: list[dict[str, Any]]) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for report in reports:
        items.extend(item for item in report.get("per_repo") or [] if isinstance(item, dict))
    return sorted(items, key=lambda item: str(item.get("repo") or ""))


def workflow_result_failed(workflow_result: str) -> bool:
    text = workflow_result.strip()
    if not text:
        return False

    parts = [part.strip() for part in text.split(",") if part.strip()]
    for part in parts:
        result = part.rsplit("=", 1)[-1].strip().lower()
        if result not in SUCCESSFUL_WORKFLOW_RESULTS:
            return True
    return False


def artifact_result_failed(artifact_result: str) -> bool:
    text = artifact_result.strip().lower()
    return bool(text and text not in SUCCESSFUL_WORKFLOW_RESULTS)


def event_counts(
    reports: list[dict[str, Any]],
    items: list[dict[str, Any]],
    workflow_result: str,
    artifact_result: str,
) -> dict[str, int]:
    return {
        "workflow_failures": 1 if workflow_result_failed(workflow_result) else 0,
        "artifact_failures": 1 if artifact_result_failed(artifact_result) else 0,
        "updates": sum(1 for item in items if item.get("updates")),
        "major_version_notifications": sum(
            1 for item in items if (item.get("notification_groups") or {}).get("major_version")
        ),
        "helm_major_version_notifications": sum(
            1 for item in items if (item.get("notification_groups") or {}).get("helm_major_version")
        ),
        "missing_version_source_notifications": sum(
            1 for item in items if (item.get("notification_groups") or {}).get("missing_version_source")
        ),
        "missing_eol_notifications": sum(
            1 for item in items if (item.get("notification_groups") or {}).get("missing_eol")
        ),
        "planned_releases": sum(
            1 for item in items if (item.get("planned_release") or {}).get("status") == "planned"
        ),
        "dry_run_updates": sum(1 for item in items if item.get("dry_run_diffs")),
        "release_blockers": sum(
            1 for item in items if (item.get("planned_release") or {}).get("status") == "blocked"
        ),
        "boilerplate_updates": sum(1 for item in items if item.get("boilerplate_updates")),
        "boilerplate_warnings": sum(1 for item in items if item.get("boilerplate_warnings")),
        "boilerplate_drift": sum(1 for item in items if item.get("boilerplate_diffs")),
        "applied_updates": sum(
            1
            for item in items
            if (item.get("apply_result") or {}).get("status") in ("applied", "tagged", "already_applied")
        ),
        "apply_failures": sum(1 for item in items if (item.get("apply_result") or {}).get("status") == "failed"),
        "warnings": sum(1 for item in items if item.get("warnings")),
        "special": sum(int((report.get("totals") or {}).get("special") or 0) for report in reports),
    }


def has_email_worthy_events(counts: dict[str, int]) -> bool:
    return any(value > 0 for value in counts.values())


def append_repo_messages(lines: list[str], title: str, items: list[dict[str, Any]], key: str) -> None:
    selected = [(item, item.get(key) or []) for item in items if item.get(key)]
    if not selected:
        return
    lines.append(title)
    lines.append("")
    for item, messages in selected:
        lines.append(f"{item['repo']}:")
        for message in messages:
            lines.append(f"- {message}")
        lines.append("")


def append_grouped_notifications(lines: list[str], items: list[dict[str, Any]]) -> None:
    for group in NOTIFICATION_GROUP_ORDER:
        selected = [
            (item, (item.get("notification_groups") or {}).get(group) or [])
            for item in items
            if (item.get("notification_groups") or {}).get(group)
        ]
        if not selected:
            continue
        lines.append(NOTIFICATION_GROUP_TITLES[group])
        lines.append("")
        for item, messages in selected:
            lines.append(f"{item['repo']}:")
            for message in messages:
                lines.append(f"- {message}")
            lines.append("")

    other_selected = []
    for item in items:
        grouped_messages = {
            message
            for messages in (item.get("notification_groups") or {}).values()
            for message in messages
        }
        messages = [message for message in item.get("notifications") or [] if message not in grouped_messages]
        if messages:
            other_selected.append((item, messages))

    if other_selected:
        lines.append("Other Manual Review Notifications")
        lines.append("")
        for item, messages in other_selected:
            lines.append(f"{item['repo']}:")
            for message in messages:
                lines.append(f"- {message}")
            lines.append("")


def append_repo_planned_changes(lines: list[str], items: list[dict[str, Any]]) -> None:
    selected = [
        (item, item.get("planned_release") or {}, item.get("planned_diffs") or [])
        for item in items
        if item.get("planned_diffs") or (item.get("planned_release") or {}).get("status") in ("planned", "blocked")
    ]
    if not selected:
        return

    lines.append("Service Changes and Git Tags")
    lines.append("")
    lines.append("The workflow applies these service changes and releases these git tags when the apply step succeeds.")
    lines.append("")
    for item, release, planned_diffs in selected:
        lines.append(f"{item['repo']}:")
        if release.get("status") == "planned":
            lines.append(f"- git tag: {release['tag']}")
            lines.append(f"- previous tag: {release['previous_tag']}")
            lines.append("tag description:")
            lines.extend(str(release.get("description") or "").splitlines())
        elif release.get("status") == "blocked":
            lines.append(f"- git tag release blocked: {release.get('reason') or 'release tag could not be calculated'}")
        if planned_diffs:
            lines.append("planned diff:")
            for planned_diff in planned_diffs:
                lines.extend(str(planned_diff).rstrip().splitlines())
        lines.append("")


def append_repo_dry_run_changes(lines: list[str], items: list[dict[str, Any]]) -> None:
    selected = [(item, item.get("dry_run_diffs") or []) for item in items if item.get("dry_run_diffs")]
    if not selected:
        return

    lines.append("Manual Review Dry Run Diffs")
    lines.append("")
    lines.append("These diffs are generated for manual review only. The workflow does not apply them.")
    lines.append("")
    for item, dry_run_diffs in selected:
        lines.append(f"{item['repo']}:")
        for dry_run_diff in dry_run_diffs:
            lines.extend(str(dry_run_diff).rstrip().splitlines())
        lines.append("")


def append_repo_boilerplate_review(lines: list[str], items: list[dict[str, Any]]) -> None:
    selected = [
        (
            item,
            item.get("boilerplate_updates") or [],
            item.get("boilerplate_warnings") or [],
            item.get("boilerplate_diffs") or [],
        )
        for item in items
        if item.get("boilerplate_updates") or item.get("boilerplate_warnings") or item.get("boilerplate_diffs")
    ]
    if not selected:
        return

    lines.append("Boilerplate and Build Template Review")
    lines.append("")
    lines.append("These checks are report only. The workflow does not apply boilerplate or build template changes.")
    lines.append("")
    for item, updates, warnings, diffs in selected:
        lines.append(f"{item['repo']}:")
        if updates:
            lines.append("review items:")
            for message in updates:
                lines.append(f"- {message}")
        if warnings:
            lines.append("warnings:")
            for message in warnings:
                lines.append(f"- {message}")
        if diffs:
            lines.append("configured boilerplate diffs:")
            for diff in diffs:
                lines.extend(str(diff).rstrip().splitlines())
        lines.append("")


def append_repo_apply_results(lines: list[str], items: list[dict[str, Any]]) -> None:
    selected = [(item, item.get("apply_result") or {}) for item in items if item.get("apply_result")]
    if not selected:
        return

    lines.append("Apply Results")
    lines.append("")
    for item, result in selected:
        lines.append(f"{item['repo']}:")
        lines.append(f"- status: {result.get('status', 'unknown')}")
        if result.get("message"):
            lines.append(f"- message: {result['message']}")
        if result.get("branch"):
            lines.append(f"- branch: {result['branch']}")
        if result.get("commit"):
            lines.append(f"- commit: {result['commit']}")
        if result.get("tag"):
            lines.append(f"- tag: {result['tag']}")
        changed_files = result.get("changed_files") or []
        if changed_files:
            lines.append(f"- changed files: {', '.join(changed_files)}")
        lines.append("")


def build_body(
    reports: list[dict[str, Any]],
    items: list[dict[str, Any]],
    counts: dict[str, int],
    *,
    run_url: str,
    event: str,
    sha: str,
    workflow_result: str,
    artifact_result: str,
) -> str:
    lines: list[str] = []
    lines.append("Service update report events were detected.")
    lines.append("")
    lines.append(f"Run: {run_url or 'unknown'}")
    lines.append(f"Event: {event or 'unknown'}")
    lines.append(f"Commit: {sha or 'unknown'}")
    lines.append(f"Update job result: {workflow_result or 'unknown'}")
    lines.append(f"Artifact download result: {artifact_result or 'unknown'}")
    lines.append(f"Report artifacts: {len(reports)}")
    lines.append(f"Repos reported: {len(items)}")
    lines.append("")
    lines.append("Summary:")
    for key, value in counts.items():
        lines.append(f"- {key.replace('_', ' ')}: {value}")
    lines.append("")

    if counts["workflow_failures"]:
        lines.append("Workflow Failure")
        lines.append("")
        lines.append("One or more report jobs did not complete successfully. Check the run URL above.")
        lines.append("")

    if counts["artifact_failures"]:
        lines.append("Report Artifact Failure")
        lines.append("")
        lines.append("Report artifacts could not be downloaded. Check the workflow run logs for collection errors.")
        lines.append("")

    append_repo_planned_changes(lines, items)
    append_repo_dry_run_changes(lines, items)
    append_repo_boilerplate_review(lines, items)
    append_repo_apply_results(lines, items)
    append_repo_messages(lines, "Updates Without Local Manifest Diff", items, "updates_without_local_diff")
    append_grouped_notifications(lines, items)
    append_repo_messages(lines, "Warnings", items, "warnings")
    return "\n".join(lines).rstrip() + "\n"


def html_inline_markdown(text: Any) -> str:
    parts = str(text).split("`")
    rendered = []
    for index, part in enumerate(parts):
        escaped = html.escape(part)
        if index % 2:
            rendered.append(
                f"<code style=\"background:#f3f4f6;border-radius:3px;padding:1px 4px;"
                f"font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;\">{escaped}</code>"
            )
        else:
            rendered.append(escaped)
    return "".join(rendered)


def html_message_list(messages: list[Any]) -> str:
    if not messages:
        return ""
    items = "".join(f"<li>{html_inline_markdown(message)}</li>" for message in messages)
    return f"<ul style=\"margin:8px 0 0 20px;padding:0;\">{items}</ul>"


def html_diff(diff: str) -> str:
    lines = []
    for line in str(diff).rstrip().splitlines():
        color = "#374151"
        background = "transparent"
        if line.startswith("+") and not line.startswith("+++"):
            color = "#166534"
            background = "#ecfdf3"
        elif line.startswith("-") and not line.startswith("---"):
            color = "#991b1b"
            background = "#fef2f2"
        elif line.startswith("@@"):
            color = "#1d4ed8"
            background = "#eff6ff"
        elif line.startswith(("diff --git", "---", "+++")):
            color = "#4b5563"
            background = "#f9fafb"
        lines.append(
            f"<span style=\"display:block;color:{color};background:{background};\">"
            f"{html.escape(line) or ' '}</span>"
        )
    return (
        "<pre style=\"margin:10px 0 0 0;padding:12px;border:1px solid #d1d5db;"
        "border-radius:6px;background:#ffffff;font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;"
        "font-size:12px;line-height:1.45;overflow-x:auto;white-space:pre;\">"
        + "".join(lines)
        + "</pre>"
    )


def html_release_description(description: str) -> str:
    parts: list[str] = []
    in_list = False
    for raw_line in str(description or "").splitlines():
        line = raw_line.strip()
        if not line:
            if in_list:
                parts.append("</ul>")
                in_list = False
            continue
        if line.startswith("- "):
            if not in_list:
                parts.append("<ul style=\"margin:8px 0 0 20px;padding:0;\">")
                in_list = True
            parts.append(f"<li>{html_inline_markdown(line[2:])}</li>")
            continue
        if in_list:
            parts.append("</ul>")
            in_list = False
        if line.endswith(":"):
            parts.append(
                f"<h4 style=\"margin:14px 0 6px 0;font-size:14px;color:#111827;\">"
                f"{html_inline_markdown(line[:-1])}</h4>"
            )
        else:
            parts.append(f"<p style=\"margin:6px 0;color:#374151;\">{html_inline_markdown(line)}</p>")
    if in_list:
        parts.append("</ul>")
    return "".join(parts)


def html_repo_messages(title: str, items: list[dict[str, Any]], key: str) -> str:
    selected = [(item, item.get(key) or []) for item in items if item.get(key)]
    if not selected:
        return ""
    blocks = [
        f"<h2 style=\"margin:28px 0 12px 0;font-size:20px;color:#111827;\">{html.escape(title)}</h2>"
    ]
    for item, messages in selected:
        blocks.append(
            "<div style=\"margin:0 0 14px 0;padding:12px;border:1px solid #e5e7eb;border-radius:6px;\">"
            f"<h3 style=\"margin:0;font-size:16px;color:#111827;\">{html.escape(str(item['repo']))}</h3>"
            f"{html_message_list(messages)}"
            "</div>"
        )
    return "".join(blocks)


def html_grouped_notifications(items: list[dict[str, Any]]) -> str:
    blocks: list[str] = []
    for group in NOTIFICATION_GROUP_ORDER:
        selected = [
            (item, (item.get("notification_groups") or {}).get(group) or [])
            for item in items
            if (item.get("notification_groups") or {}).get(group)
        ]
        if not selected:
            continue
        blocks.append(
            f"<h2 style=\"margin:28px 0 12px 0;font-size:20px;color:#111827;\">"
            f"{html.escape(NOTIFICATION_GROUP_TITLES[group])}</h2>"
        )
        for item, messages in selected:
            blocks.append(
                "<div style=\"margin:0 0 14px 0;padding:12px;border:1px solid #e5e7eb;border-radius:6px;\">"
                f"<h3 style=\"margin:0;font-size:16px;color:#111827;\">{html.escape(str(item['repo']))}</h3>"
                f"{html_message_list(messages)}"
                "</div>"
            )

    other_selected = []
    for item in items:
        grouped_messages = {
            message
            for messages in (item.get("notification_groups") or {}).values()
            for message in messages
        }
        messages = [message for message in item.get("notifications") or [] if message not in grouped_messages]
        if messages:
            other_selected.append((item, messages))

    if other_selected:
        blocks.append(
            "<h2 style=\"margin:28px 0 12px 0;font-size:20px;color:#111827;\">"
            "Other Manual Review Notifications</h2>"
        )
        for item, messages in other_selected:
            blocks.append(
                "<div style=\"margin:0 0 14px 0;padding:12px;border:1px solid #e5e7eb;border-radius:6px;\">"
                f"<h3 style=\"margin:0;font-size:16px;color:#111827;\">{html.escape(str(item['repo']))}</h3>"
                f"{html_message_list(messages)}"
                "</div>"
            )

    return "".join(blocks)


def html_planned_changes(items: list[dict[str, Any]]) -> str:
    selected = [
        (item, item.get("planned_release") or {}, item.get("planned_diffs") or [])
        for item in items
        if item.get("planned_diffs") or (item.get("planned_release") or {}).get("status") in ("planned", "blocked")
    ]
    if not selected:
        return ""

    blocks = [
        "<h2 style=\"margin:28px 0 12px 0;font-size:20px;color:#111827;\">"
        "Service Changes and Git Tags</h2>",
        "<p style=\"margin:0 0 12px 0;color:#4b5563;\">"
        "The workflow applies these service changes and releases these git tags when the apply step succeeds.</p>",
    ]
    for item, release, planned_diffs in selected:
        blocks.append(
            "<div style=\"margin:0 0 18px 0;padding:14px;border:1px solid #d1d5db;border-radius:6px;\">"
            f"<h3 style=\"margin:0 0 8px 0;font-size:17px;color:#111827;\">{html.escape(str(item['repo']))}</h3>"
        )
        if release.get("status") == "planned":
            blocks.append(
                "<table role=\"presentation\" cellspacing=\"0\" cellpadding=\"0\" "
                "style=\"border-collapse:collapse;margin:0 0 12px 0;\">"
                f"<tr><td style=\"padding:2px 14px 2px 0;color:#6b7280;\">Git tag</td>"
                f"<td style=\"padding:2px 0;color:#111827;\"><strong>{html.escape(str(release['tag']))}</strong></td></tr>"
                f"<tr><td style=\"padding:2px 14px 2px 0;color:#6b7280;\">Previous tag</td>"
                f"<td style=\"padding:2px 0;color:#111827;\">{html.escape(str(release['previous_tag']))}</td></tr>"
                "</table>"
            )
            description = str(release.get("description") or "").strip()
            if description:
                blocks.append(
                    "<h4 style=\"margin:12px 0 6px 0;font-size:14px;color:#111827;\">Tag Description</h4>"
                    f"{html_release_description(description)}"
                )
        elif release.get("status") == "blocked":
            blocks.append(
                "<p style=\"margin:0 0 10px 0;color:#991b1b;\"><strong>Git tag release blocked:</strong> "
                f"{html_inline_markdown(release.get('reason') or 'release tag could not be calculated')}</p>"
            )
        if planned_diffs:
            blocks.append(
                "<h4 style=\"margin:14px 0 6px 0;font-size:14px;color:#111827;\">Planned Diff</h4>"
            )
            for planned_diff in planned_diffs:
                blocks.append(html_diff(str(planned_diff)))
        blocks.append("</div>")
    return "".join(blocks)


def html_dry_run_changes(items: list[dict[str, Any]]) -> str:
    selected = [(item, item.get("dry_run_diffs") or []) for item in items if item.get("dry_run_diffs")]
    if not selected:
        return ""

    blocks = [
        "<h2 style=\"margin:28px 0 12px 0;font-size:20px;color:#111827;\">"
        "Manual Review Dry Run Diffs</h2>",
        "<p style=\"margin:0 0 12px 0;color:#4b5563;\">"
        "These diffs are generated for manual review only. The workflow does not apply them.</p>",
    ]
    for item, dry_run_diffs in selected:
        blocks.append(
            "<div style=\"margin:0 0 18px 0;padding:14px;border:1px solid #d1d5db;border-radius:6px;\">"
            f"<h3 style=\"margin:0 0 8px 0;font-size:17px;color:#111827;\">{html.escape(str(item['repo']))}</h3>"
        )
        for dry_run_diff in dry_run_diffs:
            blocks.append(html_diff(str(dry_run_diff)))
        blocks.append("</div>")
    return "".join(blocks)


def html_boilerplate_review(items: list[dict[str, Any]]) -> str:
    selected = [
        (
            item,
            item.get("boilerplate_updates") or [],
            item.get("boilerplate_warnings") or [],
            item.get("boilerplate_diffs") or [],
        )
        for item in items
        if item.get("boilerplate_updates") or item.get("boilerplate_warnings") or item.get("boilerplate_diffs")
    ]
    if not selected:
        return ""

    blocks = [
        "<h2 style=\"margin:28px 0 12px 0;font-size:20px;color:#111827;\">"
        "Boilerplate and Build Template Review</h2>",
        "<p style=\"margin:0 0 12px 0;color:#4b5563;\">"
        "These checks are report only. The workflow does not apply boilerplate or build template changes.</p>",
    ]
    for item, updates, warnings, diffs in selected:
        blocks.append(
            "<div style=\"margin:0 0 18px 0;padding:14px;border:1px solid #d1d5db;border-radius:6px;\">"
            f"<h3 style=\"margin:0 0 8px 0;font-size:17px;color:#111827;\">{html.escape(str(item['repo']))}</h3>"
        )
        if updates:
            blocks.append("<h4 style=\"margin:10px 0 4px 0;font-size:14px;color:#111827;\">Review Items</h4>")
            blocks.append(html_message_list(updates))
        if warnings:
            blocks.append("<h4 style=\"margin:10px 0 4px 0;font-size:14px;color:#111827;\">Warnings</h4>")
            blocks.append(html_message_list(warnings))
        if diffs:
            blocks.append(
                "<h4 style=\"margin:14px 0 6px 0;font-size:14px;color:#111827;\">"
                "Configured Boilerplate Diffs</h4>"
            )
            for diff in diffs:
                blocks.append(html_diff(str(diff)))
        blocks.append("</div>")
    return "".join(blocks)


def html_apply_results(items: list[dict[str, Any]]) -> str:
    selected = [(item, item.get("apply_result") or {}) for item in items if item.get("apply_result")]
    if not selected:
        return ""

    blocks = [
        "<h2 style=\"margin:28px 0 12px 0;font-size:20px;color:#111827;\">Apply Results</h2>"
    ]
    for item, result in selected:
        status = str(result.get("status") or "unknown")
        color = "#991b1b" if status == "failed" else "#166534"
        rows = [
            ("Status", f"<strong style=\"color:{color};\">{html.escape(status)}</strong>"),
        ]
        for key, label in (
            ("message", "Message"),
            ("branch", "Branch"),
            ("commit", "Commit"),
            ("tag", "Tag"),
        ):
            if result.get(key):
                rows.append((label, html_inline_markdown(result[key])))
        changed_files = result.get("changed_files") or []
        if changed_files:
            rows.append(("Changed files", html.escape(", ".join(changed_files))))
        table_rows = "".join(
            f"<tr><td style=\"padding:2px 14px 2px 0;color:#6b7280;\">{label}</td>"
            f"<td style=\"padding:2px 0;color:#111827;\">{value}</td></tr>"
            for label, value in rows
        )
        blocks.append(
            "<div style=\"margin:0 0 14px 0;padding:12px;border:1px solid #e5e7eb;border-radius:6px;\">"
            f"<h3 style=\"margin:0 0 8px 0;font-size:16px;color:#111827;\">{html.escape(str(item['repo']))}</h3>"
            f"<table role=\"presentation\" cellspacing=\"0\" cellpadding=\"0\" style=\"border-collapse:collapse;\">{table_rows}</table>"
            "</div>"
        )
    return "".join(blocks)


def build_html_body(
    reports: list[dict[str, Any]],
    items: list[dict[str, Any]],
    counts: dict[str, int],
    *,
    run_url: str,
    event: str,
    sha: str,
    workflow_result: str,
    artifact_result: str,
) -> str:
    status_color = "#991b1b" if counts["workflow_failures"] or counts["artifact_failures"] else "#166534"
    summary_rows = "".join(
        "<tr>"
        f"<td style=\"padding:6px 12px;border-bottom:1px solid #e5e7eb;color:#374151;\">{html.escape(key.replace('_', ' '))}</td>"
        f"<td style=\"padding:6px 12px;border-bottom:1px solid #e5e7eb;color:#111827;text-align:right;\"><strong>{value}</strong></td>"
        "</tr>"
        for key, value in counts.items()
    )
    run_value = (
        f"<a href=\"{html.escape(run_url)}\" style=\"color:#2563eb;\">{html.escape(run_url)}</a>"
        if run_url
        else "unknown"
    )
    body = [
        "<!doctype html><html><body style=\"margin:0;padding:0;background:#ffffff;color:#111827;"
        "font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Arial,sans-serif;font-size:14px;line-height:1.5;\">",
        "<div style=\"max-width:980px;margin:0 auto;padding:24px;\">",
        "<h1 style=\"margin:0 0 8px 0;font-size:24px;color:#111827;\">Service Update Report</h1>",
        "<p style=\"margin:0 0 18px 0;color:#4b5563;\">Service update report events were detected.</p>",
        "<table role=\"presentation\" cellspacing=\"0\" cellpadding=\"0\" style=\"border-collapse:collapse;margin:0 0 18px 0;\">",
        f"<tr><td style=\"padding:2px 14px 2px 0;color:#6b7280;\">Run</td><td style=\"padding:2px 0;\">{run_value}</td></tr>",
        f"<tr><td style=\"padding:2px 14px 2px 0;color:#6b7280;\">Event</td><td style=\"padding:2px 0;\">{html.escape(event or 'unknown')}</td></tr>",
        f"<tr><td style=\"padding:2px 14px 2px 0;color:#6b7280;\">Commit</td><td style=\"padding:2px 0;\">{html.escape(sha or 'unknown')}</td></tr>",
        f"<tr><td style=\"padding:2px 14px 2px 0;color:#6b7280;\">Update job result</td><td style=\"padding:2px 0;color:{status_color};\"><strong>{html.escape(workflow_result or 'unknown')}</strong></td></tr>",
        f"<tr><td style=\"padding:2px 14px 2px 0;color:#6b7280;\">Artifact download result</td><td style=\"padding:2px 0;color:{status_color};\"><strong>{html.escape(artifact_result or 'unknown')}</strong></td></tr>",
        f"<tr><td style=\"padding:2px 14px 2px 0;color:#6b7280;\">Report artifacts</td><td style=\"padding:2px 0;\">{len(reports)}</td></tr>",
        f"<tr><td style=\"padding:2px 14px 2px 0;color:#6b7280;\">Repos reported</td><td style=\"padding:2px 0;\">{len(items)}</td></tr>",
        "</table>",
        "<h2 style=\"margin:24px 0 10px 0;font-size:20px;color:#111827;\">Summary</h2>",
        "<table role=\"presentation\" cellspacing=\"0\" cellpadding=\"0\" style=\"border-collapse:collapse;min-width:360px;border:1px solid #e5e7eb;border-radius:6px;\">",
        summary_rows,
        "</table>",
    ]
    if counts["workflow_failures"]:
        body.append(
            "<div style=\"margin:20px 0;padding:12px;border:1px solid #fecaca;border-radius:6px;background:#fef2f2;color:#991b1b;\">"
            "<strong>Workflow Failure</strong><br>One or more report jobs did not complete successfully. Check the run URL above."
            "</div>"
        )
    if counts["artifact_failures"]:
        body.append(
            "<div style=\"margin:20px 0;padding:12px;border:1px solid #fecaca;border-radius:6px;background:#fef2f2;color:#991b1b;\">"
            "<strong>Report Artifact Failure</strong><br>Report artifacts could not be downloaded. Check the workflow run logs for collection errors."
            "</div>"
        )
    body.append(html_planned_changes(items))
    body.append(html_dry_run_changes(items))
    body.append(html_boilerplate_review(items))
    body.append(html_apply_results(items))
    body.append(html_repo_messages("Updates Without Local Manifest Diff", items, "updates_without_local_diff"))
    body.append(html_grouped_notifications(items))
    body.append(html_repo_messages("Warnings", items, "warnings"))
    body.append("</div></body></html>")
    return "".join(body)


def build_subject(counts: dict[str, int], workflow_result: str, sha: str) -> str:
    status = "failed" if counts["workflow_failures"] or counts["artifact_failures"] else "events"
    short_sha = sha[:7] if sha else "unknown"
    return (
        f"[services] report {status}: "
        f"{counts['updates']} update repos, "
        f"{counts['dry_run_updates']} dry-run repos, "
        f"{counts['boilerplate_updates']} boilerplate repos, "
        f"{counts['major_version_notifications']} major-version repos, "
        f"{counts['helm_major_version_notifications']} Helm-major repos ({short_sha})"
    )


def split_recipients(value: str) -> list[str]:
    return [item.strip() for item in value.replace(";", ",").split(",") if item.strip()]


def send_email(subject: str, body: str, html_body: str) -> bool:
    smtp_host = os.environ.get("SMTP_HOST", "").strip()
    smtp_port = int(os.environ.get("SMTP_PORT") or "587")
    smtp_user = os.environ.get("SMTP_USERNAME", "").strip()
    smtp_password = os.environ.get("SMTP_PASSWORD", "")
    mail_from = os.environ.get("REPORT_EMAIL_FROM", "").strip() or smtp_user
    recipients = split_recipients(os.environ.get("REPORT_EMAIL_TO", ""))
    use_ssl = os.environ.get("SMTP_SSL", "").lower() in ("1", "true", "yes")
    use_starttls = os.environ.get("SMTP_STARTTLS", "true").lower() not in ("0", "false", "no")

    missing = []
    if not smtp_host:
        missing.append("SMTP_HOST")
    if not mail_from:
        missing.append("REPORT_EMAIL_FROM")
    if not recipients:
        missing.append("REPORT_EMAIL_TO")
    if missing:
        print(f"Email not sent because required configuration is missing: {', '.join(missing)}")
        return False

    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = mail_from
    message["To"] = ", ".join(recipients)
    message.set_content(body)
    message.add_alternative(html_body, subtype="html")

    context = ssl.create_default_context()
    if use_ssl:
        with smtplib.SMTP_SSL(smtp_host, smtp_port, context=context, timeout=60) as smtp:
            if smtp_user or smtp_password:
                smtp.login(smtp_user, smtp_password)
            smtp.send_message(message)
    else:
        with smtplib.SMTP(smtp_host, smtp_port, timeout=60) as smtp:
            smtp.ehlo()
            if use_starttls:
                smtp.starttls(context=context)
                smtp.ehlo()
            if smtp_user or smtp_password:
                smtp.login(smtp_user, smtp_password)
            smtp.send_message(message)
    return True


def main() -> int:
    args = parse_args()
    reports = load_reports(Path(args.reports_dir))
    items = repo_items(reports)
    counts = event_counts(reports, items, args.workflow_result, args.artifact_result)

    if not has_email_worthy_events(counts):
        print("No email-worthy service report events were found.")
        return 0

    subject = build_subject(counts, args.workflow_result, args.sha)
    body = build_body(
        reports,
        items,
        counts,
        run_url=args.run_url,
        event=args.event,
        sha=args.sha,
        workflow_result=args.workflow_result,
        artifact_result=args.artifact_result,
    )
    html_body = build_html_body(
        reports,
        items,
        counts,
        run_url=args.run_url,
        event=args.event,
        sha=args.sha,
        workflow_result=args.workflow_result,
        artifact_result=args.artifact_result,
    )
    print(subject)
    print("")
    print(body)
    if send_email(subject, body, html_body):
        print("Email sent.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
