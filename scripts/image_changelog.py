"""Collect what a Wodby image revision update actually changed, from the image release notes.

Wodby images publish one annotated tag per repository revision (`r18`), and every
major/minor alias of that revision (`8.5-r18`, `8.5-dev-r18`) carries the same note.
A note either describes a change in the image itself or only records a new base
image, such as `Base image wodby/alpine: 3.23-r17 -> 3.23-r18`. For the latter the
changes are collected from the base image's notes, recursively, until a note that
describes a real change is found.

Notes of a shared base image can describe several version lines at once. Bullets
that name another line of the same product (`Alpine 3.24 ...` for an image built on
Alpine 3.23) and titles that exclude the image (`(except wodby/redis:8.6)`) are
dropped, so a note only appears when it applies to the version in use.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Callable

# `8.5-r18`, `8.5-dev-r18`, `3.23-r18`, `1-r4`: version lines with at most one dot
# share the repository revision counter. Full-version aliases (`11.4.8-r0`) don't.
SHARED_REVISION_TAG_RE = re.compile(
    r"^(?P<line>\d+(?:\.\d+)?)(?:-(?P<variant>[a-z][a-z0-9]*(?:-[a-z][a-z0-9]*)*))?-r(?P<revision>0|[1-9]\d*)$"
)
FULL_VERSION_TAG_RE = re.compile(
    r"^(?P<line>\d+\.\d+)\.\d+(?:\.\d+)*(?:-[a-z][a-z0-9]*(?:-[a-z][a-z0-9]*)*)?-r(?:0|[1-9]\d*)$"
)
RELEASE_ALIAS_RE = re.compile(r"^\S+ from image release r(?P<revision>0|[1-9]\d*)$", re.MULTILINE)
BASE_EDGE_RE = re.compile(
    r"^\s*(?:[-*]\s+)?Base image (?:wodby/)?(?P<repo>[a-z0-9_.-]+):\s+(?P<old>\S+)\s+->\s+(?P<new>\S+)\s*$",
    re.IGNORECASE,
)
LEGACY_BASE_UPDATE_RE = re.compile(r"^Base image (?:stability|revision) tag updated to \S+", re.IGNORECASE)
EXCEPT_RE = re.compile(r"\(except (?P<images>[^)]*)\)", re.IGNORECASE)
EXCEPT_IMAGE_RE = re.compile(r"(?:wodby/)?(?P<repo>[a-z0-9_.-]+):(?P<line>\d+(?:\.\d+)?)")
VERSION_TOKEN_RE = re.compile(r"(?<![\w.])(?P<major>\d+)\.(?P<minor>\d+)(?:\.\d+)*")
SSH_SIGNATURE_RE = re.compile(r"-----BEGIN SSH SIGNATURE-----.*?-----END SSH SIGNATURE-----", re.DOTALL)
MAX_DEPTH = 4
# Product names that release notes use for an image repository, when they differ from the name.
PRODUCT_NAMES = {"php": "PHP", "mariadb": "MariaDB", "postgres": "PostgreSQL", "alpine": "Alpine"}


@dataclass
class ChangelogEntry:
    """One image release note that applies to the version in use."""

    image: str  # wodby/php
    revision: str  # r17
    title: str
    bullets: list[str] = field(default_factory=list)
    base_of: str | None = None  # wodby/nginx when this note comes from its base image


@dataclass
class ImageChangelog:
    entries: list[ChangelogEntry] = field(default_factory=list)
    # Why some of the update couldn't be followed; release notes fall back to the image contents.
    gaps: list[str] = field(default_factory=list)


def parse_shared_revision_tag(tag: str) -> tuple[str, int] | None:
    """Return (version line, revision number) for an alias that shares the repository counter."""
    match = SHARED_REVISION_TAG_RE.match(tag)
    if not match:
        return None
    return match.group("line"), int(match.group("revision"))


def product_name(repo: str) -> str:
    return PRODUCT_NAMES.get(repo, repo)


def bullet_applies(bullet: str, repo: str, line: str) -> bool:
    """Drop a bullet that names another version line of this product and not the one in use."""
    tokens = [(m.group("major"), m.group("minor")) for m in VERSION_TOKEN_RE.finditer(bullet)]
    line_parts = tuple(line.split("."))
    if len(line_parts) == 2 and line_parts in tokens:
        return True
    if len(line_parts) == 1 and any(major == line_parts[0] for major, _minor in tokens):
        return True
    leading = re.match(rf"^{re.escape(product_name(repo))}\s+(?P<major>\d+)(?:\.(?P<minor>\d+))?", bullet, re.IGNORECASE)
    if not leading:
        return True
    if len(line_parts) == 2:
        return (leading.group("major"), leading.group("minor")) == line_parts
    return leading.group("major") == line_parts[0]


def excluded_by_title(title: str, repo: str, line: str) -> bool:
    """A title excludes the line in use, or names this product's versions without it."""
    match = EXCEPT_RE.search(title)
    if match and any(
        item.group("repo") == repo and item.group("line") == line
        for item in EXCEPT_IMAGE_RE.finditer(match.group("images"))
    ):
        return True
    title = EXCEPT_RE.sub("", title)
    product = re.search(rf"\b{re.escape(product_name(repo))}\b", title, re.IGNORECASE)
    if not product:
        return False
    tokens = [(m.group("major"), m.group("minor")) for m in VERSION_TOKEN_RE.finditer(title[product.end():])]
    if not tokens:
        return False
    line_parts = tuple(line.split("."))
    if len(line_parts) == 2:
        return line_parts not in tokens
    return all(major != line_parts[0] for major, _minor in tokens)


def split_note(message: str) -> tuple[str, list[str], list[re.Match[str]]]:
    """Split a release note into its title, bullets and base-image edges; prose paragraphs are dropped."""
    message = SSH_SIGNATURE_RE.sub("", message).strip()
    title = ""
    bullets: list[str] = []
    edges: list[re.Match[str]] = []
    current: list[str] | None = None
    for raw in message.splitlines():
        line = raw.rstrip()
        edge = BASE_EDGE_RE.match(line)
        if edge:
            edges.append(edge)
            current = None
            continue
        stripped = line.strip()
        if not stripped:
            current = None
            continue
        if stripped.startswith(("- ", "* ")):
            current = [stripped[2:].strip()]
            bullets.append(current[0])
            continue
        if current is not None and raw.startswith((" ", "\t")):
            bullets[-1] = f"{bullets[-1]} {stripped}"
            continue
        if not title:
            title = stripped
        current = None
    return title, bullets, edges


class WodbyImageChangelog:
    """Follow Wodby image release notes, and their base images, across a revision update."""

    def __init__(self, release_message: Callable[[str, str], str | None], tag_exists: Callable[[str, str], bool],
                 owner: str = "wodby"):
        # release_message(repo, tag) returns the annotated tag message, or None when the tag doesn't exist.
        self.release_message = release_message
        self.tag_exists = tag_exists
        self.owner = owner

    def collect(self, repo: str, old_tag: str, new_tag: str) -> ImageChangelog:
        result = ImageChangelog()
        self._collect(repo, old_tag, new_tag, None, result, 0, set())
        return result

    def _collect(self, repo: str, old_tag: str, new_tag: str, base_of: str | None, result: ImageChangelog,
                 depth: int, seen: set[tuple[str, str, str]]) -> None:
        image = f"{self.owner}/{repo}"
        old, new = self.revision_of(repo, old_tag), self.revision_of(repo, new_tag)
        if not old or not new:
            result.gaps.append(f"{image}: {old_tag} -> {new_tag} can't be mapped to repository revisions")
            return
        line = new[0]
        if (repo, old_tag, new_tag) in seen or depth > MAX_DEPTH:
            result.gaps.append(f"{image}: base image chain too deep at {new_tag}")
            return
        seen = seen | {(repo, old_tag, new_tag)}
        if new[1] <= old[1]:
            return
        for number in range(old[1] + 1, new[1] + 1):
            revision = f"r{number}"
            message = self.release_message(repo, revision)
            if message is None:
                continue  # Revision counters can skip numbers.
            title, bullets, edges = split_note(message)
            if title and LEGACY_BASE_UPDATE_RE.match(title):
                result.gaps.append(f"{image} {revision}: base image update without the previous base revision")
                title = ""
            if title and not excluded_by_title(title, repo, line):
                applicable = [bullet for bullet in bullets if bullet_applies(bullet, repo, line)]
                # A note whose bullets all concern other version lines doesn't apply to this one.
                if applicable or not bullets:
                    result.entries.append(ChangelogEntry(image, revision, title, applicable, base_of))
            for edge in self._edges_for_line(edges, line):
                base_repo = edge.group("repo").lower()
                base_old, base_new = self._base_tags(base_repo, edge.group("old"), edge.group("new"), line)
                self._collect(base_repo, base_old, base_new, base_of or image, result, depth + 1, seen)

    def revision_of(self, repo: str, tag: str) -> tuple[str, int] | None:
        """Map an image tag to (version line, repository revision number).

        Major/minor aliases share the repository counter. A full-version alias such as
        `11.4.13-r0` has its own counter, and its annotation names the repository
        release (`wodby/mariadb:11.4.13-r0 from image release r37`).
        """
        shared = parse_shared_revision_tag(tag)
        if shared:
            return shared
        full = FULL_VERSION_TAG_RE.match(tag)
        if not full:
            return None
        release = RELEASE_ALIAS_RE.search(self.release_message(repo, tag) or "")
        if not release:
            return None
        return full.group("line"), int(release.group("revision"))

    @staticmethod
    def _edges_for_line(edges: list[re.Match[str]], line: str) -> list[re.Match[str]]:
        """Follow the edge for the line in use when a note lists one per line, else each distinct edge once."""
        for_line = [edge for edge in edges if (parse_shared_revision_tag(edge.group("new")) or ("",))[0] == line]
        if for_line:
            return for_line[:1]
        unique: dict[tuple[str, str, str], re.Match[str]] = {}
        for edge in edges:
            key = (edge.group("repo").lower(), edge.group("old"), edge.group("new"))
            unique.setdefault(key, edge)
        return list(unique.values())

    def _base_tags(self, base_repo: str, old_tag: str, new_tag: str, line: str) -> tuple[str, str]:
        """Use the image's own version line on the base when the base publishes it (drupal-php 8.4 on php 8.4)."""
        old, new = parse_shared_revision_tag(old_tag), parse_shared_revision_tag(new_tag)
        if old and new and old[0] != line:
            own_new = f"{line}-r{new[1]}"
            if self.tag_exists(base_repo, own_new):
                return f"{line}-r{old[1]}", own_new
        return old_tag, new_tag


def render_changelog_entry(entry: ChangelogEntry) -> list[str]:
    """Render one entry as a Markdown bullet with its sub-bullets."""
    source = f"{entry.image} {entry.revision}"
    if entry.base_of:
        source += f", base image of {entry.base_of}"
    lines = [f"- {entry.title} ({source})"]
    lines.extend(f"  - {bullet}" for bullet in entry.bullets)
    return lines
