"""Image digest pinning and descriptions of upstream rebuilds.

A service option pins its image with `digest`, the multi-platform index digest of
the image its tag selects. Third-party publishers rebuild images under unchanged
tags, so a new digest for the same tag is a release of its own. This module
resolves digests and describes such a rebuild from the image itself: version
variables in the image configuration and the operating system package database.
Base images do not need to be traversed; the final image already contains every
package its bases changed.
"""

from __future__ import annotations

import hashlib
import io
import re
import sqlite3
import struct
import tarfile
import tempfile
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable

import requests

DIGEST_RE = re.compile(r"^sha256:[a-f0-9]{64}$")

MANIFEST_ACCEPT = ", ".join(
    [
        "application/vnd.oci.image.index.v1+json",
        "application/vnd.docker.distribution.manifest.list.v2+json",
        "application/vnd.oci.image.manifest.v1+json",
        "application/vnd.docker.distribution.manifest.v2+json",
    ]
)
INDEX_MEDIA_TYPES = {
    "application/vnd.oci.image.index.v1+json",
    "application/vnd.docker.distribution.manifest.list.v2+json",
}

DOCKER_HUB_HOSTS = {"docker.io", "index.docker.io", "registry-1.docker.io"}

# Package databases by path inside the image, without a leading slash.
PACKAGE_DATABASES = {
    "var/lib/dpkg/status": "dpkg",
    "lib/apk/db/installed": "apk",
    "var/lib/rpm/rpmdb.sqlite": "rpm",
    "usr/lib/sysimage/rpm/rpmdb.sqlite": "rpm",
}
PACKAGE_FORMAT_LABELS = {"dpkg": "Debian", "apk": "Alpine", "rpm": "RPM"}

# Version variables that name a component, shown by its usual name.
RUNTIME_LABELS = {
    "PHP": "PHP",
    "NODE": "Node.js",
    "PYTHON": "Python",
    "JAVA": "Java",
    "RUBY": "Ruby",
    "GOLANG": "Go",
    "GO": "Go",
    "NGINX": "nginx",
    "HTTPD": "Apache",
    "YARN": "Yarn",
    "PERL": "Perl",
    "ERLANG": "Erlang",
    "OTP": "Erlang/OTP",
    "ELIXIR": "Elixir",
    "RUST": "Rust",
    "DOTNET": ".NET",
    "ASPNETCORE": "ASP.NET Core",
}
ENV_VERSION_RE = re.compile(r"^(?P<name>[A-Z][A-Z0-9]*)_VERSION$")
VERSION_VALUE_RE = re.compile(r"^v?\d+(?:[.\-+_~][0-9A-Za-z]+)*$")

# Packages important enough to name on their own line, by source package.
KEY_PACKAGES: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"^(apache2|httpd)$"), "Apache"),
    (re.compile(r"^nginx$"), "nginx"),
    (re.compile(r"^openssl[0-9]*$"), "OpenSSL"),
    (re.compile(r"^(java-(?P<java>\d+)-openjdk|openjdk-?(?P<openjdk>\d+))"), "Java"),
    (re.compile(r"^nodejs$"), "Node.js"),
    (re.compile(r"^python3(\.\d+)?$"), "Python"),
    (re.compile(r"^php\d*(\.\d+)?$"), "PHP"),
    (re.compile(r"^ruby\d*(\.\d+)?$"), "Ruby"),
    (re.compile(r"^postgresql(-\d+)?$"), "PostgreSQL"),
    (re.compile(r"^(mariadb|mysql)"), "MariaDB/MySQL"),
    (re.compile(r"^openssh$"), "OpenSSH"),
]

# Other packages worth naming in the summary line, in order of preference.
NOTABLE_PACKAGES = [
    "curl",
    "glibc",
    "musl",
    "zlib",
    "expat",
    "libxml2",
    "sqlite3",
    "sqlite",
    "gnutls28",
    "krb5",
    "libssh2",
    "pcre2",
    "xz-utils",
    "xz",
    "libpng1.6",
    "busybox",
    "ca-certificates",
    "tzdata",
]
NOTABLE_LABELS = {"sqlite3": "SQLite", "sqlite": "SQLite", "gnutls28": "GnuTLS", "libpng1.6": "libpng", "xz-utils": "xz"}


class ImageAnalysisError(RuntimeError):
    """The registry or image content prevented a description of the rebuild."""


@dataclass
class Package:
    name: str
    version: str
    source: str


@dataclass
class RebuildFacts:
    """What changed between two builds of one image tag."""

    image: str
    tag: str
    before: str
    after: str
    created: str | None = None
    revision_before: str | None = None
    revision_after: str | None = None
    base_name: str | None = None
    base_changed: bool = False
    runtimes: list[tuple[str, str, str]] = field(default_factory=list)
    package_format: str | None = None
    # (source package, old version or "", new version or "")
    packages: list[tuple[str, str, str]] = field(default_factory=list)
    error: str | None = None


def registry_host(registry: str) -> str:
    return "registry-1.docker.io" if registry in DOCKER_HUB_HOSTS else registry


class ImageRegistry:
    """Minimal OCI distribution client that reuses the report's HTTP session and token logic."""

    def __init__(self, session: requests.Session, bearer_token: Callable[[str, str, str], str | None]):
        self.session = session
        self.bearer_token = bearer_token
        self._tokens: dict[tuple[str, str], str] = {}

    def request(self, method: str, registry: str, repo: str, path: str, *, accept: str | None = None,
                stream: bool = False) -> requests.Response:
        host = registry_host(registry)
        url = f"https://{host}/v2/{repo}/{path}"
        headers = {"Accept": accept} if accept else {}
        key = (host, repo)
        if key in self._tokens:
            headers["Authorization"] = f"Bearer {self._tokens[key]}"
        response = self.session.request(method, url, headers=headers, timeout=120, stream=stream)
        if response.status_code == 401:
            token = self.bearer_token(host, repo, response.headers.get("WWW-Authenticate", ""))
            if token:
                self._tokens[key] = token
                headers["Authorization"] = f"Bearer {token}"
                response = self.session.request(method, url, headers=headers, timeout=120, stream=stream)
        response.raise_for_status()
        return response

    def digest(self, registry: str, repo: str, tag: str) -> str:
        """Return the digest the tag points to now: the index digest for multi-platform images."""
        response = self.request("HEAD", registry, repo, f"manifests/{tag}", accept=MANIFEST_ACCEPT)
        digest = response.headers.get("Docker-Content-Digest", "")
        if not DIGEST_RE.match(digest):
            # Some registries omit the header on HEAD; the digest is the hash of the manifest bytes.
            body = self.request("GET", registry, repo, f"manifests/{tag}", accept=MANIFEST_ACCEPT).content
            digest = "sha256:" + hashlib.sha256(body).hexdigest()
        return digest

    def manifest(self, registry: str, repo: str, reference: str) -> dict[str, Any]:
        response = self.request("GET", registry, repo, f"manifests/{reference}", accept=MANIFEST_ACCEPT)
        data = response.json()
        data.setdefault("mediaType", response.headers.get("Content-Type", "").split(";")[0])
        return data

    def blob(self, registry: str, repo: str, digest: str, *, stream: bool = False) -> requests.Response:
        return self.request("GET", registry, repo, f"blobs/{digest}", stream=stream)


@dataclass
class ImageBuild:
    """One platform build of an image: its manifest, configuration and index annotations."""

    manifest: dict[str, Any]
    config: dict[str, Any]
    annotations: dict[str, str]


def load_build(registry: ImageRegistry, host: str, repo: str, digest: str, platform: tuple[str, str] = ("linux", "amd64")) -> ImageBuild:
    manifest = registry.manifest(host, repo, digest)
    annotations: dict[str, str] = {}
    if manifest.get("mediaType") in INDEX_MEDIA_TYPES or "manifests" in manifest:
        entry = next(
            (
                item
                for item in manifest.get("manifests") or []
                if (item.get("platform") or {}).get("os") == platform[0]
                and (item.get("platform") or {}).get("architecture") == platform[1]
            ),
            None,
        )
        if entry is None:
            raise ImageAnalysisError(f"no {platform[0]}/{platform[1]} image in {digest}")
        annotations = dict(entry.get("annotations") or {})
        manifest = registry.manifest(host, repo, entry["digest"])
    config = registry.blob(host, repo, manifest["config"]["digest"]).json()
    return ImageBuild(manifest=manifest, config=config, annotations=annotations)


def layer_databases(registry: ImageRegistry, host: str, repo: str, layer: dict[str, Any]) -> dict[str, bytes | None]:
    """Return package databases a layer adds (bytes) or deletes (None)."""
    media_type = str(layer.get("mediaType") or "")
    if "zstd" in media_type:
        raise ImageAnalysisError("zstd-compressed layers are not supported")
    mode = "r|gz" if ("gzip" in media_type or media_type.endswith("tar.gzip")) else "r|*"
    response = registry.blob(host, repo, layer["digest"], stream=True)
    found: dict[str, bytes | None] = {}
    try:
        with tarfile.open(fileobj=response.raw, mode=mode) as tar:
            for member in tar:
                name = member.name.lstrip("./")
                directory, _, base = name.rpartition("/")
                if base == ".wh..wh..opq":
                    for path in PACKAGE_DATABASES:
                        if path.startswith(f"{directory}/"):
                            found[path] = None
                elif base.startswith(".wh."):
                    deleted = f"{directory}/{base[4:]}" if directory else base[4:]
                    if deleted in PACKAGE_DATABASES:
                        found[deleted] = None
                elif name in PACKAGE_DATABASES and member.isfile():
                    handle = tar.extractfile(member)
                    found[name] = handle.read() if handle else b""
    except tarfile.TarError as exc:
        raise ImageAnalysisError(f"layer {layer['digest'][:19]} could not be read: {exc}") from exc
    finally:
        response.close()
    return found


def package_database(registry: ImageRegistry, host: str, repo: str, build: ImageBuild,
                     cache: dict[str, dict[str, bytes | None]]) -> tuple[str | None, bytes | None]:
    """Fold layers in order and return the final package database. Layers are cached by digest."""
    files: dict[str, bytes | None] = {}
    for layer in build.manifest.get("layers") or []:
        digest = layer["digest"]
        if digest not in cache:
            cache[digest] = layer_databases(registry, host, repo, layer)
        files.update(cache[digest])
    for path, kind in PACKAGE_DATABASES.items():
        if files.get(path) is not None:
            return kind, files[path]
    return None, None


def parse_dpkg(data: bytes) -> dict[str, Package]:
    packages: dict[str, Package] = {}
    for block in data.decode(errors="replace").split("\n\n"):
        fields: dict[str, str] = {}
        for line in block.splitlines():
            if line and not line[0].isspace() and ": " in line:
                key, value = line.split(": ", 1)
                fields[key] = value
        name = fields.get("Package")
        if not name or "installed" not in fields.get("Status", "").split():
            continue
        source = fields.get("Source", name).split(" ", 1)[0]
        packages[name] = Package(name, fields.get("Version", ""), source)
    return packages


def parse_apk(data: bytes) -> dict[str, Package]:
    packages: dict[str, Package] = {}
    for block in data.decode(errors="replace").split("\n\n"):
        fields = {line[0]: line[2:] for line in block.splitlines() if len(line) > 2 and line[1] == ":"}
        if fields.get("P"):
            packages[fields["P"]] = Package(fields["P"], fields.get("V", ""), fields.get("o") or fields["P"])
    return packages


RPM_NAME, RPM_VERSION, RPM_RELEASE, RPM_EPOCH, RPM_SOURCERPM = 1000, 1001, 1002, 1003, 1044


def rpm_header_fields(header: bytes) -> dict[int, Any]:
    """Read string and int32 tags from an RPM header blob as stored in rpmdb.sqlite."""
    count, size = struct.unpack(">II", header[:8])
    store = header[8 + 16 * count: 8 + 16 * count + size]
    fields: dict[int, Any] = {}
    for index in range(count):
        tag, kind, offset, items = struct.unpack(">IIII", header[8 + 16 * index: 24 + 16 * index])
        if kind == 6 and tag in (RPM_NAME, RPM_VERSION, RPM_RELEASE, RPM_SOURCERPM):
            end = store.index(b"\0", offset)
            fields[tag] = store[offset:end].decode(errors="replace")
        elif kind == 4 and tag == RPM_EPOCH and items:
            fields[tag] = struct.unpack(">I", store[offset: offset + 4])[0]
    return fields


def parse_rpm(data: bytes) -> dict[str, Package]:
    with tempfile.NamedTemporaryFile(suffix=".sqlite") as handle:
        handle.write(data)
        handle.flush()
        connection = sqlite3.connect(handle.name)
        try:
            rows = connection.execute("SELECT blob FROM Packages").fetchall()
        finally:
            connection.close()
    packages: dict[str, Package] = {}
    for (blob,) in rows:
        fields = rpm_header_fields(bytes(blob))
        name = fields.get(RPM_NAME)
        if not name:
            continue
        version = f"{fields.get(RPM_VERSION, '')}-{fields.get(RPM_RELEASE, '')}"
        if fields.get(RPM_EPOCH):
            version = f"{fields[RPM_EPOCH]}:{version}"
        source = name
        sourcerpm = str(fields.get(RPM_SOURCERPM) or "")
        match = re.match(r"^(?P<name>.+)-[^-]+-[^-]+\.src\.rpm$", sourcerpm)
        if match:
            source = match.group("name")
        packages[name] = Package(name, version, source)
    return packages


PARSERS = {"dpkg": parse_dpkg, "apk": parse_apk, "rpm": parse_rpm}


def env_map(config: dict[str, Any]) -> dict[str, str]:
    values = {}
    for item in (config.get("config") or {}).get("Env") or []:
        key, _, value = str(item).partition("=")
        values[key] = value
    return values


def labels(build: ImageBuild) -> dict[str, str]:
    merged = dict((build.config.get("config") or {}).get("Labels") or {})
    merged.update(build.annotations)
    return merged


def runtime_changes(before: dict[str, str], after: dict[str, str]) -> list[tuple[str, str, str]]:
    changes = []
    for key in sorted(set(before) & set(after)):
        match = ENV_VERSION_RE.match(key)
        if not match or before[key] == after[key]:
            continue
        if not VERSION_VALUE_RE.match(before[key]) or not VERSION_VALUE_RE.match(after[key]):
            continue
        name = match.group("name")
        changes.append((RUNTIME_LABELS.get(name, name.replace("_", " ").title()), before[key], after[key]))
    return changes


def package_changes(before: dict[str, Package], after: dict[str, Package]) -> list[tuple[str, str, str]]:
    """Return one change per source package: the first binary package's versions stand for the group."""
    groups: dict[str, tuple[str, str, str]] = {}
    for name in sorted(set(before) | set(after)):
        old, new = before.get(name), after.get(name)
        if old and new and old.version == new.version:
            continue
        source = (new or old).source
        groups.setdefault(source, (source, old.version if old else "", new.version if new else ""))
    return sorted(groups.values())


def analyze_rebuild(registry: ImageRegistry, image: str, host: str, repo: str, tag: str, before: str, after: str) -> RebuildFacts:
    """Describe what changed between two digests of one tag. Errors are recorded, not raised."""
    facts = RebuildFacts(image=image, tag=tag, before=before, after=after)
    try:
        old = load_build(registry, host, repo, before)
        new = load_build(registry, host, repo, after)
        facts.created = new.config.get("created")
        old_labels, new_labels = labels(old), labels(new)
        facts.revision_before = old_labels.get("org.opencontainers.image.revision")
        facts.revision_after = new_labels.get("org.opencontainers.image.revision")
        facts.base_name = new_labels.get("org.opencontainers.image.base.name")
        old_base = old_labels.get("org.opencontainers.image.base.digest")
        new_base = new_labels.get("org.opencontainers.image.base.digest")
        facts.base_changed = bool(old_base and new_base and old_base != new_base)
        facts.runtimes = runtime_changes(env_map(old.config), env_map(new.config))

        cache: dict[str, dict[str, bytes | None]] = {}
        old_kind, old_db = package_database(registry, host, repo, old, cache)
        new_kind, new_db = package_database(registry, host, repo, new, cache)
        if new_kind and old_kind == new_kind:
            facts.package_format = new_kind
            facts.packages = package_changes(PARSERS[old_kind](old_db), PARSERS[new_kind](new_db))
    except (ImageAnalysisError, requests.RequestException, ValueError, KeyError, sqlite3.Error, struct.error) as exc:
        facts.error = str(exc) or exc.__class__.__name__
    return facts


def upstream_version(version: str, package_format: str | None) -> str:
    """Drop the epoch and distribution revision so versions read as the upstream release."""
    value = version.split(":", 1)[1] if re.match(r"^\d+:", version) else version
    if package_format in ("dpkg", "rpm") and "-" in value:
        value = value.rsplit("-", 1)[0]
    if package_format == "apk":
        value = re.sub(r"-r\d+$", "", value)
    value = re.sub(r"^\d+\.\d+\.dfsg\+really", "", value)
    return value


def display_versions(old: str, new: str, package_format: str | None) -> tuple[str, str]:
    short_old, short_new = upstream_version(old, package_format), upstream_version(new, package_format)
    if short_old and short_new and short_old != short_new:
        return short_old, short_new
    return old, new


def key_package_label(source: str) -> str | None:
    for pattern, label in KEY_PACKAGES:
        match = pattern.match(source)
        if match:
            if label == "Java":
                major = match.groupdict().get("java") or match.groupdict().get("openjdk")
                return f"Java (OpenJDK {major})" if major else "Java (OpenJDK)"
            return label
    return None


def format_date(created: str | None) -> str | None:
    if not created:
        return None
    try:
        moment = datetime.fromisoformat(created.replace("Z", "+00:00")[:32])
    except ValueError:
        match = re.match(r"^(\d{4}-\d{2}-\d{2})", created)
        if not match:
            return None
        moment = datetime.fromisoformat(match.group(1))
    return f"{moment.day} {moment.strftime('%b %Y')}"


def join_names(names: list[str]) -> str:
    return names[0] if len(names) == 1 else ", ".join(names[:-1]) + " and " + names[-1]


def render_rebuild_note(facts: RebuildFacts, product: str | None = None) -> str:
    """Render one customer-facing summary of an upstream rebuild."""
    reference = f"`{facts.image}:{facts.tag}`"
    when = format_date(facts.created)
    headline = f"{reference} was rebuilt upstream" + (f" on {when}" if when else "") + "."
    if facts.error:
        return f"{headline} Its changes could not be determined ({facts.error})."

    product_name = product or "The application"
    unchanged = bool(facts.revision_before and facts.revision_before == facts.revision_after)
    if unchanged and facts.base_changed and facts.base_name:
        headline += f" {product_name} itself is unchanged; its `{facts.base_name}` base image was updated:"
    elif unchanged:
        headline += f" {product_name} itself is unchanged:"
    elif facts.revision_before and facts.revision_after:
        headline += " It was built from a new source revision:"
    else:
        headline += " Changes:"

    lines: list[str] = []
    shown_labels: set[str] = set()
    for label, old, new in facts.runtimes:
        lines.append(f"{label} {old} → {new}")
        shown_labels.add(label)

    others: list[str] = []
    for source, old, new in facts.packages:
        label = key_package_label(source)
        if label and label not in shown_labels:
            if old and new:
                old_version, new_version = display_versions(old, new, facts.package_format)
                lines.append(f"{label} {old_version} → {new_version}")
            elif new:
                lines.append(f"{label} {upstream_version(new, facts.package_format)} added")
            else:
                lines.append(f"{label} removed")
            shown_labels.add(label)
        elif not label:
            others.append(source)

    if others:
        distribution = PACKAGE_FORMAT_LABELS.get(facts.package_format or "", "OS")
        notable = [NOTABLE_LABELS.get(name, name) for name in NOTABLE_PACKAGES if name in others][:4]
        noun = "package" if len(others) == 1 else "packages"
        qualifier = "other " if lines else ""
        summary = f"Updates to {len(others)} {qualifier}{distribution} {noun}"
        if notable and len(others) > len(notable):
            summary += f", including {join_names(notable)}"
        elif notable:
            summary += f": {join_names(notable)}"
        elif len(others) <= 3:
            summary += f": {join_names(others)}"
        lines.append(summary)

    if not lines:
        return f"{reference} was rebuilt upstream" + (f" on {when}" if when else "") + \
            ". No version or OS package changes were found."
    return "\n".join([headline] + [f"- {line}" for line in lines])
