import argparse
import copy
import io
import sqlite3
import struct
import sys
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import image_digests
from image_digests import (
    ImageBuild,
    Package,
    RebuildFacts,
    package_changes,
    package_database,
    parse_apk,
    parse_dpkg,
    parse_rpm,
    render_rebuild_note,
    runtime_changes,
)
from service_apply_updates import apply_manifest_changes, validate_planned_image_tags
from service_update_report import UpdateReportGenerator, generate_report

OLD = "sha256:" + "1" * 64
NEW = "sha256:" + "2" * 64
DEV = "sha256:" + "3" * 64
OTHER = "sha256:" + "4" * 64


def report(manifest, tags, digests, git_tags=None, rebuild_note="note"):
    """Run the real report over one in-memory manifest with stubbed registry answers."""
    generator = UpdateReportGenerator("wodby")
    generator.get_repo_manifest_paths = lambda _: ["service.yml"]
    generator.get_service_data = lambda *_: manifest
    generator.get_image_tags = lambda _: tags
    generator.get_github_tags = lambda _, repo: {"1.0.0"} if repo.startswith("service-") else set(git_tags or tags)
    generator.get_image_change_notes = lambda *_: []
    generator.get_image_digest = lambda image, tag: digests[f"{image}:{tag}"]
    rebuilds = []
    generator.describe_image_rebuild = lambda *args: rebuilds.append(args) or rebuild_note
    empty = {key: [] for key in ("updates", "current", "warnings", "notifications", "major_updates", "planned_changes")}
    generator.check_eol_options = lambda *_: copy.deepcopy(empty)
    generator.check_parent_service_version = lambda *_: {**copy.deepcopy(empty), "comparable": True}
    generator.resolve_fallback_version_source = lambda *_: None
    with tempfile.TemporaryDirectory() as directory:
        readme = Path(directory) / "README.md"
        readme.write_text("[Example](https://github.com/wodby/service-example)")
        args = argparse.Namespace(readme=str(readme), owner="wodby", repo_filter="", build_boilerplate_mode="off")
        with mock.patch("service_update_report.UpdateReportGenerator", return_value=generator):
            result = generate_report(args)
    return result["per_repo"][0], rebuilds


def manifest(options, *images, title="phpMyAdmin"):
    return {
        "name": "example",
        "title": title,
        "type": "service",
        "options": copy.deepcopy(options),
        "workloads": [{"name": "main", "primary": True, "containers": [{"name": f"c{i}", "image": image} for i, image in enumerate(images)]}],
    }


def digest_changes(item):
    return [(c["key"], c["before"], c["after"], c["digest_kind"]) for c in item["planned_changes"] if c.get("change_type") == "image_digest"]


class DigestPlanningTest(unittest.TestCase):
    def test_pins_an_unpinned_third_party_option(self):
        item, rebuilds = report(manifest([{"version": "5", "tag": "5.2.3"}], "phpmyadmin"), ["5.2.3"], {"phpmyadmin:5.2.3": NEW})
        self.assertEqual(digest_changes(item), [("digest", None, NEW, "pin")])
        self.assertEqual(rebuilds, [])
        description = item["planned_release"]["description"]
        self.assertIn("phpmyadmin image pinned by digest for tag `5.2.3`", description)
        self.assertNotIn("Image changes:", description)

    def test_a_current_pin_needs_no_release(self):
        item, _ = report(manifest([{"version": "5", "tag": "5.2.3", "digest": NEW}], "phpmyadmin"), ["5.2.3"], {"phpmyadmin:5.2.3": NEW})
        self.assertEqual(item["planned_changes"], [])
        self.assertIsNone(item["planned_release"])
        self.assertTrue(any("`digest` pins `phpmyadmin:5.2.3`" in line for line in item["current"]))

    def test_upstream_rebuild_is_released_with_its_note(self):
        note = "`phpmyadmin:5.2.3` was rebuilt upstream on 19 Nov 2026. phpMyAdmin itself is unchanged:\n- PHP 8.3.33 → 8.3.37"
        item, rebuilds = report(
            manifest([{"version": "5", "tag": "5.2.3", "digest": OLD}], "phpmyadmin"),
            ["5.2.3"], {"phpmyadmin:5.2.3": NEW}, rebuild_note=note,
        )
        self.assertEqual(digest_changes(item), [("digest", OLD, NEW, "rebuild")])
        self.assertEqual(rebuilds, [("phpmyadmin", "5.2.3", OLD, NEW, "phpMyAdmin")])
        description = item["planned_release"]["description"]
        self.assertIn("Image changes:\n- `phpmyadmin:5.2.3` was rebuilt upstream on 19 Nov 2026.", description)
        self.assertIn("\n  - PHP 8.3.33 → 8.3.37", description)
        self.assertIn("phpmyadmin image digest updated for rebuilt tag `5.2.3`", description)

    def test_digest_follows_a_tag_update_without_a_rebuild_note(self):
        item, rebuilds = report(
            manifest([{"version": "5", "tag": "5.2.3", "digest": OLD}], "phpmyadmin"),
            ["5.2.3", "5.2.4"], {"phpmyadmin:5.2.4": NEW},
        )
        keys = [(c["key"], c["after"]) for c in item["planned_changes"]]
        self.assertEqual(keys, [("tag", "5.2.4"), ("digest", NEW)])
        self.assertEqual(digest_changes(item)[0][3], "tag")
        self.assertEqual(rebuilds, [])
        self.assertNotIn("pinned by digest", item["planned_release"]["description"])

    def test_moved_wodby_revision_is_reported_not_released(self):
        item, _ = report(
            manifest([{"version": "8.5", "tag": "8.5-r9", "digest": OLD}], "wodby/php", title="PHP"),
            ["8.5-r9"], {"wodby/php:8.5-r9": NEW},
        )
        self.assertEqual(item["planned_changes"], [])
        self.assertTrue(any("Wodby revision tags are not rebuilt" in warning for warning in item["warnings"]))

    def test_development_pair_pins_both_images_after_a_revision_update(self):
        option = {"version": "8.5", "tag": "8.5-r8", "devTag": "8.5-dev-r8", "digest": OLD, "devDigest": OTHER}
        item, _ = report(
            manifest([option], "wodby/php", title="PHP"),
            ["8.5-r8", "8.5-dev-r8", "8.5-r9", "8.5-dev-r9"],
            {"wodby/php:8.5-r9": NEW, "wodby/php:8.5-dev-r9": DEV},
        )
        self.assertEqual(
            [(c["key"], c["after"]) for c in item["planned_changes"]],
            [("tag", "8.5-r9"), ("digest", NEW), ("devDigest", DEV), ("devTag", "8.5-dev-r9")],
        )
        self.assertEqual({c[3] for c in digest_changes(item)}, {"tag"})

    def test_images_sharing_the_option_tag_are_not_pinned(self):
        item, _ = report(
            manifest([{"version": "1", "tag": "1.0"}], "n8nio/n8n", "n8nio/runners"),
            ["1.0"], {"n8nio/n8n:1.0": NEW, "n8nio/runners:1.0": OTHER},
        )
        self.assertEqual(digest_changes(item), [])
        self.assertTrue(any("one digest can pin only one image" in warning for warning in item["warnings"]))

    def test_a_sidecar_with_its_own_tag_does_not_block_pinning(self):
        item, _ = report(
            manifest([{"version": "1", "tag": "1.0"}], "n8nio/n8n", "n8nio/runners:2.0"),
            ["1.0"], {"n8nio/n8n:1.0": NEW},
        )
        self.assertEqual(digest_changes(item), [("digest", None, NEW, "pin")])

    def test_digest_lookup_failure_is_a_warning(self):
        item, _ = report(manifest([{"version": "5", "tag": "5.2.3"}], "phpmyadmin"), ["5.2.3"], {})
        self.assertEqual(digest_changes(item), [])
        self.assertTrue(any("digest lookup failed for `phpmyadmin:5.2.3`" in warning for warning in item["warnings"]))


class DigestApplyTest(unittest.TestCase):
    def change(self, key, before, after, tag="5.2.3"):
        return {
            "file": "service.yml", "path": f"options[version=5].{key}", "key": key, "before": before, "after": after,
            "change_type": "image_digest", "image": "phpmyadmin", "image_tag": tag,
        }

    def test_new_digest_is_written_after_the_tag(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "service.yml"
            path.write_text("name: phpmyadmin\noptions:\n- version: '5'\n  tag: '5.2.3'\n  default: true\n")
            apply_manifest_changes(Path(directory), [self.change("digest", None, NEW)])
            self.assertEqual(
                path.read_text(),
                f"name: phpmyadmin\noptions:\n- version: '5'\n  tag: '5.2.3'\n  digest: {NEW}\n  default: true\n",
            )
            apply_manifest_changes(Path(directory), [self.change("digest", NEW, OTHER)])
            self.assertEqual(yaml.safe_load(path.read_text())["options"][0]["digest"], OTHER)

    def test_apply_refuses_a_digest_the_tag_no_longer_selects(self):
        generator = UpdateReportGenerator("wodby")
        generator.get_image_digest = lambda image, tag: OTHER
        with self.assertRaisesRegex(RuntimeError, "now points to"):
            validate_planned_image_tags("wodby", [self.change("digest", None, NEW)], generator)
        generator.get_image_digest = lambda image, tag: NEW
        validate_planned_image_tags("wodby", [self.change("digest", None, NEW)], generator)


def rpm_header(fields):
    """Build a minimal RPM header blob with string tags, as stored in rpmdb.sqlite."""
    store = b""
    entries = b""
    for tag, value in fields.items():
        entries += struct.pack(">IIII", tag, 6, len(store), 1)
        store += value.encode() + b"\0"
    return struct.pack(">II", len(fields), len(store)) + entries + store


def tar_layer(files):
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as tar:
        for name, data in files.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    return buffer.getvalue()


class FakeBlobResponse:
    def __init__(self, data):
        self.raw = io.BytesIO(data)

    def close(self):
        pass


class FakeRegistry:
    def __init__(self, blobs):
        self.blobs = blobs
        self.fetched = []

    def blob(self, host, repo, digest, stream=False):
        self.fetched.append(digest)
        return FakeBlobResponse(self.blobs[digest])


class PackageDatabaseTest(unittest.TestCase):
    def test_dpkg_groups_binary_packages_by_source(self):
        old = parse_dpkg(
            b"Package: apache2\nStatus: install ok installed\nVersion: 2.4.68-1\n\n"
            b"Package: apache2-bin\nStatus: install ok installed\nSource: apache2\nVersion: 2.4.68-1\n\n"
            b"Package: removed\nStatus: deinstall ok config-files\nVersion: 1\n"
        )
        new = parse_dpkg(
            b"Package: apache2\nStatus: install ok installed\nVersion: 2.4.71-1\n\n"
            b"Package: apache2-bin\nStatus: install ok installed\nSource: apache2 (2.4.71-1)\nVersion: 2.4.71-1\n"
        )
        self.assertNotIn("removed", old)
        self.assertEqual(package_changes(old, new), [("apache2", "2.4.68-1", "2.4.71-1")])

    def test_apk_uses_origin(self):
        packages = parse_apk(b"P:libssl3\nV:3.5.6-r0\no:openssl\n\nP:musl\nV:1.2.5-r9\n")
        self.assertEqual(packages["libssl3"], Package("libssl3", "3.5.6-r0", "openssl"))
        self.assertEqual(packages["musl"].source, "musl")

    def test_rpm_headers(self):
        with tempfile.NamedTemporaryFile(suffix=".sqlite") as handle:
            connection = sqlite3.connect(handle.name)
            connection.execute("CREATE TABLE Packages (hnum INTEGER PRIMARY KEY, blob BLOB)")
            connection.execute("INSERT INTO Packages (blob) VALUES (?)", (rpm_header({
                1000: "java-21-openjdk-headless", 1001: "21.0.13.1.2", 1002: "1.2.el9",
                1044: "java-21-openjdk-21.0.13.1.2-1.2.el9.src.rpm",
            }),))
            connection.commit()
            connection.close()
            packages = parse_rpm(Path(handle.name).read_bytes())
        self.assertEqual(packages["java-21-openjdk-headless"], Package("java-21-openjdk-headless", "21.0.13.1.2-1.2.el9", "java-21-openjdk"))

    def test_layers_fold_in_order_with_whiteouts_and_cache(self):
        status = b"Package: zlib1g\nStatus: install ok installed\nVersion: 1\n"
        blobs = {
            "sha256:base": tar_layer({"var/lib/dpkg/status": status}),
            "sha256:wipe": tar_layer({"var/lib/dpkg/.wh.status": b""}),
            "sha256:top": tar_layer({"etc/motd": b"hi"}),
        }
        registry = FakeRegistry(blobs)
        build = lambda *layers: ImageBuild({"layers": [{"digest": d, "mediaType": "application/vnd.oci.image.layer.v1.tar"} for d in layers]}, {}, {})
        cache = {}
        self.assertEqual(package_database(registry, "docker.io", "library/x", build("sha256:base", "sha256:top"), cache), ("dpkg", status))
        self.assertEqual(package_database(registry, "docker.io", "library/x", build("sha256:base", "sha256:wipe"), cache), (None, None))
        self.assertEqual(registry.fetched, ["sha256:base", "sha256:top", "sha256:wipe"])

    def test_runtime_versions_come_from_version_variables(self):
        old = {"PHP_VERSION": "8.3.33", "PHP_SHA256": "a", "GOSU_VERSION": "1.17", "BUILD_VERSION": "abc def"}
        new = {"PHP_VERSION": "8.3.37", "PHP_SHA256": "b", "GOSU_VERSION": "1.17", "BUILD_VERSION": "abc xyz"}
        self.assertEqual(runtime_changes(old, new), [("PHP", "8.3.33", "8.3.37")])


class RebuildNoteTest(unittest.TestCase):
    def facts(self, **values):
        base = dict(image="phpmyadmin", tag="5.2.3", before=OLD, after=NEW, created="2026-11-19T14:52:06.163267642Z",
                    revision_before="452a995", revision_after="452a995", package_format="dpkg")
        base.update(values)
        return RebuildFacts(**base)

    def test_base_image_update(self):
        facts = self.facts(
            base_name="php:8.3-apache", base_changed=True, runtimes=[("PHP", "8.3.33", "8.3.37")],
            packages=[
                ("apache2", "2.4.68-1~deb13u1", "2.4.71-1~deb13u1"),
                ("curl", "8.14.1-2+deb13u4", "8.14.1-2+deb13u7"),
                ("glibc", "2.41-12+deb13u3", "2.41-12+deb13u4"),
                ("openssl", "3.5.6-1~deb13u2", "3.5.7-1~deb13u3"),
                ("tzdata", "2026b-0+deb13u1", "2026f-0+deb13u1"),
                ("zlib", "1:1.3.dfsg+really1.3.1-1+b1", "1:1.3.dfsg+really1.3.1-1+deb13u1"),
                ("util-linux", "2.41-5", "2.41.5-0+deb13u1"),
            ],
        )
        self.assertEqual(render_rebuild_note(facts, "phpMyAdmin"), "\n".join([
            "`phpmyadmin:5.2.3` was rebuilt upstream on 19 Nov 2026. phpMyAdmin itself is unchanged; "
            "its `php:8.3-apache` base image was updated:",
            "- PHP 8.3.33 → 8.3.37",
            "- Apache 2.4.68 → 2.4.71",
            "- OpenSSL 3.5.6 → 3.5.7",
            "- Updates to 5 other Debian packages, including curl, glibc, zlib and tzdata",
        ]))

    def test_single_package_rebuild(self):
        facts = self.facts(image="quay.io/keycloak/keycloak", tag="26.8.2", package_format="rpm",
                           packages=[("java-21-openjdk", "21.0.13.0.9-1.2.el9", "21.0.13.1.2-1.2.el9")])
        self.assertEqual(render_rebuild_note(facts, "Keycloak"), "\n".join([
            "`quay.io/keycloak/keycloak:26.8.2` was rebuilt upstream on 19 Nov 2026. Keycloak itself is unchanged:",
            "- Java (OpenJDK 21) 21.0.13.0.9 → 21.0.13.1.2",
        ]))

    def test_few_unremarkable_packages_are_named(self):
        facts = self.facts(packages=[("libopenexr", "3.2.12-1~deb13u1", "3.2.12-1~deb13u2"), ("linux", "6.12.115-2", "6.12.117-1")])
        self.assertIn("- Updates to 2 Debian packages: libopenexr and linux", render_rebuild_note(facts, "Nextcloud"))

    def test_new_source_revision_and_failures(self):
        facts = self.facts(revision_after="b0c1d2e", runtimes=[("Node.js", "24.1.0", "24.1.1")])
        self.assertIn("It was built from a new source revision:", render_rebuild_note(facts))
        failed = self.facts(error="zstd-compressed layers are not supported")
        self.assertEqual(
            render_rebuild_note(failed),
            "`phpmyadmin:5.2.3` was rebuilt upstream on 19 Nov 2026. Its changes could not be determined "
            "(zstd-compressed layers are not supported).",
        )
        self.assertIn("No version or OS package changes were found", render_rebuild_note(self.facts()))


if __name__ == "__main__":
    unittest.main()
