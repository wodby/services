#!/usr/bin/env python3

import argparse
import json
import os
import smtplib
import ssl
from email.message import EmailMessage
from pathlib import Path
from typing import Any


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Send a consolidated service update report email.")
    parser.add_argument("reports_dir", help="Directory containing downloaded service report artifacts.")
    parser.add_argument("--run-url", default="", help="GitHub Actions run URL.")
    parser.add_argument("--event", default="", help="GitHub Actions event name.")
    parser.add_argument("--sha", default="", help="Git commit SHA.")
    parser.add_argument("--workflow-result", default="", help="Aggregated dry-run-report job result.")
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


def event_counts(reports: list[dict[str, Any]], items: list[dict[str, Any]], workflow_result: str) -> dict[str, int]:
    return {
        "workflow_failures": 0 if workflow_result in ("", "success") else 1,
        "updates": sum(1 for item in items if item.get("updates")),
        "eol_updates": sum(1 for item in items if item.get("eol_updates")),
        "eol_alerts": sum(1 for item in items if item.get("eol_alerts")),
        "major_updates": sum(1 for item in items if item.get("major_updates")),
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


def append_repo_diffs(lines: list[str], title: str, items: list[dict[str, Any]]) -> None:
    selected = [(item, item.get("planned_diffs") or []) for item in items if item.get("planned_diffs")]
    if not selected:
        return

    lines.append(title)
    lines.append("")
    for item, planned_diffs in selected:
        lines.append(f"{item['repo']}:")
        for planned_diff in planned_diffs:
            lines.append("```diff")
            lines.extend(str(planned_diff).rstrip().splitlines())
            lines.append("```")
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
) -> str:
    lines: list[str] = []
    lines.append("Service update report events were detected.")
    lines.append("")
    lines.append(f"Run: {run_url or 'unknown'}")
    lines.append(f"Event: {event or 'unknown'}")
    lines.append(f"Commit: {sha or 'unknown'}")
    lines.append(f"Dry-run job result: {workflow_result or 'unknown'}")
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

    append_repo_messages(lines, "Updates Needed", items, "updates")
    append_repo_diffs(lines, "Planned Manifest Diffs", items)
    append_repo_messages(lines, "Updates Without Local Manifest Diff", items, "updates_without_local_diff")
    append_repo_messages(lines, "EOL Field Updates", items, "eol_updates")
    append_repo_messages(lines, "EOL Alerts", items, "eol_alerts")
    append_repo_messages(lines, "Major Version Notifications", items, "major_updates")
    append_repo_messages(lines, "Warnings", items, "warnings")
    return "\n".join(lines).rstrip() + "\n"


def build_subject(counts: dict[str, int], workflow_result: str, sha: str) -> str:
    status = "failed" if workflow_result not in ("", "success") else "events"
    short_sha = sha[:7] if sha else "unknown"
    return (
        f"[services] report {status}: "
        f"{counts['updates']} update repos, "
        f"{counts['eol_alerts']} EOL alert repos, "
        f"{counts['major_updates']} major-version repos ({short_sha})"
    )


def split_recipients(value: str) -> list[str]:
    return [item.strip() for item in value.replace(";", ",").split(",") if item.strip()]


def send_email(subject: str, body: str) -> bool:
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
    counts = event_counts(reports, items, args.workflow_result)

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
    )
    print(subject)
    print("")
    print(body)
    if send_email(subject, body):
        print("Email sent.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
