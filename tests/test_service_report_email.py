import html
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from service_report_email import (  # noqa: E402
    RELEASES_DISABLED_NOTE,
    RELEASES_ENABLED_NOTE,
    append_repo_planned_changes,
    html_apply_results,
    html_planned_changes,
)


PLANNED_ITEMS = [
    {
        "repo": "service-example",
        "planned_release": {"status": "planned", "tag": "1.0.1", "previous_tag": "1.0.0", "description": "Update"},
        "planned_diffs": [],
        "apply_result": {"status": "disabled", "message": "Automatic releases are disabled."},
    }
]


class ServiceReportEmailTest(unittest.TestCase):
    def test_planned_changes_say_when_releases_are_disabled(self) -> None:
        lines: list[str] = []
        append_repo_planned_changes(lines, PLANNED_ITEMS, releases_disabled=True)
        self.assertIn(RELEASES_DISABLED_NOTE, lines)
        self.assertNotIn(RELEASES_ENABLED_NOTE, lines)
        self.assertIn(html.escape(RELEASES_DISABLED_NOTE), html_planned_changes(PLANNED_ITEMS, releases_disabled=True))

    def test_planned_changes_keep_the_release_note_when_enabled(self) -> None:
        lines: list[str] = []
        append_repo_planned_changes(lines, PLANNED_ITEMS)
        self.assertIn(RELEASES_ENABLED_NOTE, lines)
        self.assertIn(html.escape(RELEASES_ENABLED_NOTE), html_planned_changes(PLANNED_ITEMS))

    def test_disabled_apply_result_is_not_shown_as_success(self) -> None:
        self.assertIn("color:#92400e;\">disabled</strong>", html_apply_results(PLANNED_ITEMS))


if __name__ == "__main__":
    unittest.main()
