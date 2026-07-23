#!/usr/bin/env python3

import argparse
import hashlib
import json
import stat
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Callable

import yaml


CONFIG_CHANGE_TYPE = "config_snapshot"
DEFAULT_PLATFORM = "linux/amd64"
MAX_CONFIG_BYTES = 1024 * 1024
COMMAND_TIMEOUT_SECONDS = 10 * 60


class ConfigSyncError(RuntimeError):
    pass


@dataclass(frozen=True)
class ExtractedConfig:
    content: str
    content_sha256: str
    image: str
    image_digest: str
    image_ref: str
    source_path: str
    platform: str


@dataclass
class ConfigSyncPlan:
    changes: list[dict[str, Any]]
    current: list[str]
    blockers: list[str]
    eligible: int
    skipped: list[str]


CommandRunner = Callable[[list[str]], subprocess.CompletedProcess[str]]


def sha256_text(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def run_command(args: list[str]) -> subprocess.CompletedProcess[str]:
    try:
        process = subprocess.run(
            args,
            check=False,
            capture_output=True,
            text=True,
            timeout=COMMAND_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired as exc:
        raise ConfigSyncError(
            f"{' '.join(args[:2])} timed out after {COMMAND_TIMEOUT_SECONDS} seconds"
        ) from exc
    if process.returncode != 0:
        output = (process.stderr or process.stdout).strip()
        raise ConfigSyncError(f"{' '.join(args[:2])} failed: {output}")
    return process


def validate_source_path(value: Any) -> str:
    source_path = str(value or "").strip()
    path = PurePosixPath(source_path)
    if not source_path.startswith("/") or ".." in path.parts:
        raise ConfigSyncError(f"config source path must be an absolute safe path: {source_path!r}")
    return source_path


def validate_repo_path(value: Any) -> str:
    repo_path = str(value or "").strip()
    path = PurePosixPath(repo_path)
    if not repo_path or path.is_absolute() or ".." in path.parts or repo_path.endswith("/"):
        raise ConfigSyncError(f"config output must be a safe repository-relative file path: {repo_path!r}")
    return path.as_posix()


def manifest_relative_path(manifest_path: str, value: Any) -> str:
    relative = validate_repo_path(value)
    manifest_dir = PurePosixPath(manifest_path).parent
    if str(manifest_dir) == ".":
        return relative
    return validate_repo_path((manifest_dir / relative).as_posix())


class DockerConfigExtractor:
    def __init__(
        self,
        command_runner: CommandRunner = run_command,
        max_config_bytes: int = MAX_CONFIG_BYTES,
    ) -> None:
        self.command_runner = command_runner
        self.max_config_bytes = max_config_bytes
        self._cache: dict[tuple[str, str, str], ExtractedConfig] = {}
        self._resolved_images: dict[tuple[str, str], tuple[str, str]] = {}

    def resolve_image(self, image: str, platform: str) -> tuple[str, str]:
        cache_key = (image, platform)
        if cache_key in self._resolved_images:
            return self._resolved_images[cache_key]

        self.command_runner(["docker", "pull", "--platform", platform, image])
        inspect = self.command_runner(
            ["docker", "image", "inspect", "--format", "{{json .RepoDigests}}", image]
        )
        try:
            repo_digests = json.loads(inspect.stdout.strip())
        except json.JSONDecodeError as exc:
            raise ConfigSyncError(f"docker returned invalid RepoDigests for {image}: {inspect.stdout!r}") from exc
        if not isinstance(repo_digests, list) or not repo_digests:
            raise ConfigSyncError(f"pulled image {image} has no immutable repository digest")

        image_ref = str(repo_digests[0])
        if "@" not in image_ref:
            raise ConfigSyncError(f"docker returned invalid repository digest for {image}: {image_ref}")
        image_digest = image_ref.rsplit("@", 1)[1]
        result = (image_ref, image_digest)
        self._resolved_images[cache_key] = result
        self._resolved_images[(image_ref, platform)] = result
        return result

    def extract(self, image: str, source_path: str, platform: str = DEFAULT_PLATFORM) -> ExtractedConfig:
        source_path = validate_source_path(source_path)
        platform = str(platform or DEFAULT_PLATFORM).strip()
        if not platform:
            raise ConfigSyncError("config extraction platform cannot be empty")

        cache_key = (image, source_path, platform)
        if cache_key in self._cache:
            return self._cache[cache_key]

        image_ref, image_digest = self.resolve_image(image, platform)

        create = self.command_runner(["docker", "create", "--platform", platform, image_ref])
        container_id = create.stdout.strip()
        if not container_id:
            raise ConfigSyncError(f"docker create returned no container id for {image_ref}")

        try:
            with tempfile.TemporaryDirectory(prefix="service-config-sync-") as tmp_dir:
                output_path = Path(tmp_dir) / "config"
                self.command_runner(
                    ["docker", "cp", "-L", f"{container_id}:{source_path}", str(output_path)]
                )
                try:
                    file_stat = output_path.stat()
                except FileNotFoundError as exc:
                    raise ConfigSyncError(f"docker did not copy {source_path} from {image_ref}") from exc
                if not stat.S_ISREG(file_stat.st_mode):
                    raise ConfigSyncError(f"image path {source_path} in {image_ref} is not a regular file")
                if file_stat.st_size > self.max_config_bytes:
                    raise ConfigSyncError(
                        f"image config {source_path} in {image_ref} is {file_stat.st_size} bytes; "
                        f"maximum is {self.max_config_bytes}"
                    )
                content_bytes = output_path.read_bytes()
        finally:
            try:
                self.command_runner(["docker", "rm", "-f", container_id])
            except ConfigSyncError:
                pass

        if b"\x00" in content_bytes:
            raise ConfigSyncError(f"image config {source_path} in {image_ref} contains NUL bytes")
        try:
            content = content_bytes.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ConfigSyncError(f"image config {source_path} in {image_ref} is not UTF-8 text") from exc

        result = ExtractedConfig(
            content=content,
            content_sha256=hashlib.sha256(content_bytes).hexdigest(),
            image=image,
            image_digest=image_digest,
            image_ref=image_ref,
            source_path=source_path,
            platform=platform,
        )
        self._cache[cache_key] = result
        self._cache[(image_ref, source_path, platform)] = result
        return result


def load_inventory(path: Path) -> dict[str, Any]:
    data = yaml.safe_load(path.read_text()) or {}
    if not isinstance(data, dict):
        raise ConfigSyncError("config sync inventory must be a mapping")
    if not isinstance(data.get("autoDiscover", False), bool):
        raise ConfigSyncError("config sync inventory autoDiscover must be a boolean")
    if "services" not in data:
        data["services"] = {}
    if not isinstance(data.get("services"), dict):
        raise ConfigSyncError("config sync inventory services must be a mapping")
    return data


def auto_discover_enabled(inventory: dict[str, Any]) -> bool:
    return bool(inventory.get("autoDiscover", False))


def inventory_for_repo(inventory: dict[str, Any], repo: str) -> dict[str, Any] | None:
    services = inventory.get("services") or {}
    entry = services.get(repo)
    if entry is None:
        return None
    if not isinstance(entry, dict) or not isinstance(entry.get("manifests"), dict):
        raise ConfigSyncError(f"config sync inventory for {repo} must contain a manifests mapping")
    return entry


def service_name_to_repo(generator: Any, service_name: str) -> str:
    resolver = getattr(generator, "service_name_to_repo", None)
    if callable(resolver):
        return str(resolver(service_name))
    return service_name if service_name.startswith("service-") else f"service-{service_name}"


def planned_parent_ref(
    manifest_path: str,
    parent_repo: str,
    service_data: dict[str, Any],
    planned_changes: list[dict[str, Any]],
) -> str:
    for change in planned_changes:
        if change.get("change_type") != "parent_service_version":
            continue
        if str(change.get("file") or "service.yml") != manifest_path:
            continue
        if str(change.get("parent_repo") or "") != parent_repo:
            continue
        target = str(change.get("after") or "").strip()
        if target:
            return target

    return str(service_data.get("fromVersion") or "").strip()


def resolve_source_context(
    repo: str,
    manifest_path: str,
    service_data: dict[str, Any],
    planned_changes: list[dict[str, Any]],
    generator: Any,
    seen: set[tuple[str, str]] | None = None,
) -> dict[str, Any]:
    """Resolve image options/workloads through immutable service inheritance tags."""
    options = service_data.get("options") or []
    workloads = service_data.get("workloads") or []
    parent_name = str(service_data.get("from") or "").strip()
    if (options and workloads) or not parent_name:
        return {"options": options, "workloads": workloads}

    parent_repo = service_name_to_repo(generator, parent_name)
    parent_ref = planned_parent_ref(manifest_path, parent_repo, service_data, planned_changes)
    if not parent_ref:
        raise ConfigSyncError(
            f"service {service_data.get('name') or repo} inherits from {parent_repo} "
            "but does not define fromVersion"
        )

    seen = set(seen or set())
    cycle_key = (parent_repo, parent_ref)
    if cycle_key in seen:
        raise ConfigSyncError(f"service inheritance cycle detected at {parent_repo}@{parent_ref}")
    seen.add(cycle_key)

    loader = getattr(generator, "get_service_data_at_ref", None)
    if not callable(loader):
        raise ConfigSyncError("service data provider cannot load parent manifests at a release tag")
    parent_data = loader(parent_repo, parent_ref, "service.yml")
    if not isinstance(parent_data, dict):
        raise ConfigSyncError(f"parent manifest service.yml was not found in {parent_repo}@{parent_ref}")

    parent_context = resolve_source_context(
        parent_repo,
        "service.yml",
        parent_data,
        [],
        generator,
        seen,
    )
    return {
        "options": options or parent_context.get("options") or [],
        "workloads": workloads or parent_context.get("workloads") or [],
    }


def infer_source_container(source_data: dict[str, Any]) -> tuple[str, str]:
    """Choose an unambiguous image container, preferring the primary workload."""
    candidates: list[tuple[str, str, bool]] = []
    for workload in source_data.get("workloads") or []:
        if not isinstance(workload, dict):
            continue
        workload_name = str(workload.get("name") or "").strip()
        primary = bool(workload.get("primary"))
        for container in workload.get("containers") or []:
            if not isinstance(container, dict) or not str(container.get("image") or "").strip():
                continue
            container_name = str(container.get("name") or "").strip()
            if workload_name and container_name:
                candidates.append((workload_name, container_name, primary))

    primary_candidates = [candidate for candidate in candidates if candidate[2]]
    selected = primary_candidates if len(primary_candidates) == 1 else candidates
    if len(selected) != 1:
        labels = ", ".join(f"{workload}/{container}" for workload, container, _ in candidates)
        detail = labels or "none"
        raise ConfigSyncError(
            f"could not infer one image source container; candidates: {detail}; "
            "configure workload and container explicitly"
        )
    return selected[0][0], selected[0][1]


def discover_config_selections(
    service_data: dict[str, Any],
    source_data: dict[str, Any],
) -> tuple[list[dict[str, Any]], list[str]]:
    """Build sync selections for image-backed configs and explain other config types."""
    eligible_configs: list[dict[str, Any]] = []
    skipped: list[str] = []
    for config in service_data.get("configs") or []:
        if not isinstance(config, dict):
            skipped.append("ignored a config entry that is not a mapping")
            continue
        identity = config_identity(config)
        if config.get("helm"):
            skipped.append(f"config `{identity}` uses Helm values rather than an image filepath")
            continue
        if config.get("filename"):
            skipped.append(f"config `{identity}` uses a generated filename rather than an image filepath")
            continue
        if not config.get("filepath"):
            skipped.append(f"config `{identity}` does not define an image filepath")
            continue
        eligible_configs.append(config)

    if not eligible_configs:
        return [], skipped

    workload, container = infer_source_container(source_data)
    selections = []
    seen: set[tuple[str, str | None]] = set()
    for config in eligible_configs:
        version = str(config.get("version") or "").strip() or None
        key = (str(config.get("name") or ""), version)
        if key in seen:
            continue
        seen.add(key)
        selection: dict[str, Any] = {
            "name": key[0],
            "workload": workload,
            "container": container,
        }
        if version is not None:
            selection["version"] = version
        selections.append(selection)
    return selections, skipped


def merge_config_selections(
    discovered: list[dict[str, Any]],
    overrides: list[dict[str, Any]],
    defaults: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Apply sparse inventory overrides without disabling automatic discovery."""
    defaults = defaults or {}
    merged = [{**defaults, **selection} for selection in discovered]
    positions = {
        (
            str(selection.get("name") or "").strip(),
            str(selection.get("version") or "").strip() or None,
        ): index
        for index, selection in enumerate(merged)
    }
    for override in overrides:
        if not isinstance(override, dict):
            raise ConfigSyncError("config sync selection override must be a mapping")
        name = str(override.get("name") or "").strip()
        version = str(override.get("version") or "").strip() or None
        key = (name, version)
        if not name:
            raise ConfigSyncError("config sync selection override must specify a config name")
        matching_keys = [
            candidate
            for candidate in positions
            if candidate[0] == name and (version is None or candidate[1] == version)
        ]
        if matching_keys:
            for matching_key in matching_keys:
                merged[positions[matching_key]].update(override)
        else:
            positions[key] = len(merged)
            selection = {**defaults, **override}
            # Preserve the legacy explicit-inventory behavior where a name
            # without a version selects every matching config. Auto-discovered
            # entries always carry their concrete fallback/exact identity.
            if version is None and not discovered:
                selection["_matchAllVersions"] = True
            merged.append(selection)
    return merged


def find_container_image(service_data: dict[str, Any], workload_name: str, container_name: str) -> str:
    for workload in service_data.get("workloads") or []:
        if not isinstance(workload, dict) or str(workload.get("name")) != workload_name:
            continue
        for container in workload.get("containers") or []:
            if not isinstance(container, dict) or str(container.get("name")) != container_name:
                continue
            image = str(container.get("image") or "").strip()
            if not image:
                raise ConfigSyncError(
                    f"container {workload_name}/{container_name} does not define an image"
                )
            if "@" in image:
                raise ConfigSyncError(
                    f"container {workload_name}/{container_name} image must not contain a digest"
                )
            return image
    raise ConfigSyncError(f"container {workload_name}/{container_name} was not found")


def target_option_tags(
    manifest_path: str,
    image: str,
    options: list[Any],
    planned_changes: list[dict[str, Any]],
) -> dict[str, str]:
    planned: dict[str, str] = {}
    for change in planned_changes:
        if change.get("change_type") != "image_tag":
            continue
        if str(change.get("file") or "service.yml") != manifest_path:
            continue
        if str(change.get("image") or "") != image:
            continue
        version = str(change.get("image_version") or "")
        target = str(change.get("after") or "")
        if version and target:
            planned[version] = target

    result: dict[str, str] = {}
    for option in options:
        if not isinstance(option, dict):
            continue
        version = str(option.get("version") or "").strip()
        if not version:
            continue
        tag = planned.get(version) or str(option.get("tag") or version).strip()
        if not tag:
            raise ConfigSyncError(f"service option {version} has no image tag")
        result[version] = tag
    return result


def image_with_tag(image: str, tag: str) -> str:
    if not image or not tag:
        raise ConfigSyncError("image repository and tag are required")
    if "@" in image:
        raise ConfigSyncError(f"cannot append tag to digest-qualified image {image}")
    last_component = image.rsplit("/", 1)[-1]
    if ":" in last_component:
        raise ConfigSyncError(f"config source image must not already contain a tag: {image}")
    return f"{image}:{tag}"


def matching_configs(service_data: dict[str, Any], selection: dict[str, Any]) -> list[dict[str, Any]]:
    name = str(selection.get("name") or "").strip()
    if not name:
        raise ConfigSyncError("config sync selection must specify a config name")
    selected_version = selection.get("version")
    match_all_versions = bool(selection.get("_matchAllVersions"))
    matches = []
    for config in service_data.get("configs") or []:
        if not isinstance(config, dict) or str(config.get("name") or "") != name:
            continue
        if selected_version is not None and str(config.get("version") or "") != str(selected_version):
            continue
        if (
            selected_version is None
            and not match_all_versions
            and str(config.get("version") or "").strip()
        ):
            continue
        matches.append(config)
    if not matches:
        suffix = f" version {selected_version}" if selected_version is not None else ""
        raise ConfigSyncError(f"config {name}{suffix} was not found")
    return matches


def config_identity(config: dict[str, Any]) -> str:
    version = str(config.get("version") or "").strip()
    return f"{config.get('name')}@{version}" if version else f"{config.get('name')}@default"


def source_dict(extracted: ExtractedConfig, version: str) -> dict[str, str]:
    return {
        "version": version,
        "image": extracted.image,
        "image_digest": extracted.image_digest,
        "image_ref": extracted.image_ref,
        "source_path": extracted.source_path,
        "platform": extracted.platform,
        "content_sha256": extracted.content_sha256,
    }


def plan_config_selection(
    repo: str,
    manifest_path: str,
    service_data: dict[str, Any],
    source_data: dict[str, Any],
    options: list[Any],
    selection: dict[str, Any],
    planned_changes: list[dict[str, Any]],
    generator: Any,
    extractor: Any,
    changes_by_file: dict[str, dict[str, Any]],
    current: list[str],
) -> None:
    workload = str(selection.get("workload") or "").strip()
    container = str(selection.get("container") or "").strip()
    if not workload or not container:
        raise ConfigSyncError(
            f"config sync selection {selection.get('name')} in {repo} {manifest_path} "
            "must specify workload and container"
        )
    image = find_container_image(source_data, workload, container)
    option_tags = target_option_tags(manifest_path, image, options, planned_changes)
    platform = str(selection.get("platform") or DEFAULT_PLATFORM)

    for config in matching_configs(service_data, selection):
        identity = config_identity(config)
        if config.get("helm") or config.get("filename") or not config.get("filepath"):
            raise ConfigSyncError(
                f"config {identity} in {repo} {manifest_path} must be a filepath config"
            )
        output_file = manifest_relative_path(manifest_path, config.get("config"))
        source_path_template = str(selection.get("sourcePath") or config.get("filepath") or "")
        config_version = str(config.get("version") or "").strip()
        if config_version:
            if config_version not in option_tags:
                raise ConfigSyncError(
                    f"config {identity} has no matching service option in {repo} {manifest_path}"
                )
            versions = [config_version]
        else:
            exact_versions = {
                str(candidate.get("version") or "").strip()
                for candidate in service_data.get("configs") or []
                if isinstance(candidate, dict)
                and str(candidate.get("name") or "") == str(config.get("name") or "")
                and str(candidate.get("version") or "").strip()
            }
            versions = sorted(version for version in option_tags if version not in exact_versions)
            if not versions:
                current.append(
                    f"config `{identity}` fallback is shadowed by exact variants for all service options"
                )
                continue

        extracted_versions = []
        for version in versions:
            source_path = validate_source_path(source_path_template.replace("{{version}}", version))
            image_ref = image_with_tag(image, option_tags[version])
            extracted = extractor.extract(image_ref, source_path, platform)
            extracted_versions.append((version, extracted))

        hashes = {extracted.content_sha256 for _, extracted in extracted_versions}
        if len(hashes) != 1:
            details = ", ".join(
                f"{version}={extracted.content_sha256[:12]}"
                for version, extracted in extracted_versions
            )
            raise ConfigSyncError(
                f"unversioned config {identity} in {repo} {manifest_path} differs across options: "
                f"{details}; add exact version overrides for the differing defaults"
            )

        content = extracted_versions[0][1].content
        current_content = generator.get_repo_file(repo, output_file)
        if current_content == content:
            current.append(f"config `{identity}` snapshot `{output_file}` matches its image source")
            continue

        before_sha256 = sha256_text(current_content) if current_content is not None else None
        after_sha256 = sha256_text(content)
        sources = [source_dict(extracted, version) for version, extracted in extracted_versions]
        change = {
            "change_type": CONFIG_CHANGE_TYPE,
            "file": output_file,
            "manifest": manifest_path,
            "path": f"configs[name={identity}].snapshot",
            "key": "config",
            "config_name": str(config.get("name")),
            "config_version": config_version or None,
            "before": before_sha256,
            "after": after_sha256,
            "before_sha256": before_sha256,
            "after_sha256": after_sha256,
            "diff_before_lines": (current_content or "").splitlines(),
            "diff_after_lines": content.splitlines(),
            "sources": sources,
            "message": f"refreshing config `{identity}` snapshot `{output_file}` from its image source",
        }

        existing = changes_by_file.get(output_file)
        if existing is not None:
            if existing["after_sha256"] != after_sha256:
                raise ConfigSyncError(
                    f"multiple config sources produce different content for output file {output_file}"
                )
            existing["sources"].extend(sources)
        else:
            changes_by_file[output_file] = change


def plan_repository_configs(
    repo: str,
    report_item: dict[str, Any],
    inventory: dict[str, Any],
    generator: Any,
    extractor: Any,
) -> ConfigSyncPlan | None:
    repo_inventory = inventory_for_repo(inventory, repo)
    auto_discover = auto_discover_enabled(inventory)
    if repo_inventory is None and not auto_discover:
        return None

    changes_by_file: dict[str, dict[str, Any]] = {}
    current: list[str] = []
    blockers: list[str] = []
    skipped: list[str] = []
    eligible = 0
    configured_manifests = (repo_inventory or {}).get("manifests") or {}
    if auto_discover:
        manifest_paths = set(generator.get_repo_manifest_paths(repo))
        manifest_paths.update(configured_manifests)
    else:
        manifest_paths = set(configured_manifests)

    for manifest_path in sorted(manifest_paths):
        service_data = generator.get_service_data(repo, manifest_path)
        if not isinstance(service_data, dict):
            blockers.append(f"service manifest {manifest_path} could not be loaded for {repo}")
            continue
        try:
            source_data = resolve_source_context(
                repo,
                manifest_path,
                service_data,
                report_item.get("planned_changes") or [],
                generator,
            )
        except ConfigSyncError as exc:
            blockers.append(str(exc))
            continue

        manifest_inventory = configured_manifests.get(manifest_path)
        overrides: list[dict[str, Any]] = []
        selection_defaults: dict[str, Any] = {}
        if manifest_inventory is not None:
            if not isinstance(manifest_inventory, dict):
                blockers.append(
                    f"config sync inventory for {repo} {manifest_path} must be a mapping"
                )
                continue
            overrides = manifest_inventory.get("configs") or []
            selection_defaults = manifest_inventory.get("defaults") or {}
            if not isinstance(overrides, list) or not isinstance(selection_defaults, dict):
                blockers.append(
                    f"config sync inventory for {repo} {manifest_path} must contain mappings "
                    "in defaults and a configs list"
                )
                continue

        if auto_discover:
            try:
                selections, manifest_skipped = discover_config_selections(service_data, source_data)
                selections = merge_config_selections(selections, overrides, selection_defaults)
                skipped.extend(f"{manifest_path}: {message}" for message in manifest_skipped)
            except ConfigSyncError as exc:
                blockers.append(f"{manifest_path}: {exc}")
                continue
        elif manifest_inventory is not None:
            selections = merge_config_selections([], overrides, selection_defaults)
        else:
            continue

        options = source_data.get("options") or []
        if not options:
            if selections:
                blockers.append(
                    f"service manifest {manifest_path} has no options for config image resolution"
                )
            continue

        for selection in selections:
            if not isinstance(selection, dict):
                blockers.append(f"config sync selection in {repo} {manifest_path} must be a mapping")
                continue
            try:
                selected_configs = matching_configs(service_data, selection)
                if selection.get("skip"):
                    reason = str(selection["skip"])
                    skipped.extend(
                        f"{manifest_path}: config `{config_identity(config)}` is not synchronized: {reason}"
                        for config in selected_configs
                    )
                    continue
                eligible += len(selected_configs)
                plan_config_selection(
                    repo,
                    manifest_path,
                    service_data,
                    source_data,
                    options,
                    selection,
                    report_item.get("planned_changes") or [],
                    generator,
                    extractor,
                    changes_by_file,
                    current,
                )
            except ConfigSyncError as exc:
                blockers.append(str(exc))

    return ConfigSyncPlan(
        changes=list(changes_by_file.values()),
        current=current,
        blockers=blockers,
        eligible=eligible,
        skipped=skipped,
    )


def checked_out_file(repo_dir: Path, relative_path: str) -> Path:
    safe_path = validate_repo_path(relative_path)
    repo_root = repo_dir.resolve()
    output_path = (repo_root / safe_path).resolve()
    if repo_root != output_path and repo_root not in output_path.parents:
        raise ConfigSyncError(f"config output escapes repository root: {relative_path}")
    return output_path


def apply_snapshot_changes(
    repo_dir: Path,
    planned_changes: list[dict[str, Any]],
    extractor: Any | None = None,
) -> list[str]:
    extractor = extractor or DockerConfigExtractor()
    changed_files: list[str] = []
    for change in planned_changes:
        if change.get("change_type") != CONFIG_CHANGE_TYPE:
            continue
        relative_path = validate_repo_path(change.get("file"))
        output_path = checked_out_file(repo_dir, relative_path)
        before_sha256 = change.get("before_sha256")
        if output_path.exists():
            if not output_path.is_file():
                raise ConfigSyncError(f"planned config output is not a regular file: {relative_path}")
            current_content = output_path.read_text()
            current_sha256 = sha256_text(current_content)
        else:
            current_sha256 = None
        if current_sha256 != before_sha256:
            raise ConfigSyncError(
                f"config snapshot {relative_path} changed since report generation: "
                f"expected {before_sha256}, found {current_sha256}"
            )

        sources = change.get("sources") or []
        if not sources or not isinstance(sources[0], dict):
            raise ConfigSyncError(f"planned config snapshot {relative_path} has no image source")
        source = sources[0]
        extracted = extractor.extract(
            str(source.get("image_ref") or ""),
            str(source.get("source_path") or ""),
            str(source.get("platform") or DEFAULT_PLATFORM),
        )
        expected_digest = str(source.get("image_digest") or "")
        if extracted.image_digest != expected_digest:
            raise ConfigSyncError(
                f"config snapshot {relative_path} image digest changed: "
                f"expected {expected_digest}, found {extracted.image_digest}"
            )
        expected_sha256 = str(change.get("after_sha256") or "")
        if extracted.content_sha256 != expected_sha256:
            raise ConfigSyncError(
                f"config snapshot {relative_path} content changed: "
                f"expected {expected_sha256}, found {extracted.content_sha256}"
            )

        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(extracted.content)
        changed_files.append(relative_path)
    return changed_files


def blocked_release(reason: str, previous_release: dict[str, Any] | None = None) -> dict[str, Any]:
    previous_release = previous_release or {}
    return {
        "status": "blocked",
        "reason": reason,
        "previous_tag": previous_release.get("previous_tag"),
        "tag": None,
        "title": None,
        "description": None,
        "commands": [],
    }


def refresh_report_totals(report: dict[str, Any]) -> None:
    items = report.get("per_repo") or []
    totals = report.setdefault("totals", {})
    updates = [item for item in items if item.get("updates")]
    no_changes = [
        item
        for item in items
        if not item.get("updates")
        and not item.get("warnings")
        and not item.get("notifications")
        and not item.get("build_template_review_items")
        and not item.get("build_template_warnings")
        and item.get("current")
        and item.get("comparable", True)
    ]
    categorized = {item.get("repo") for item in updates + no_changes}
    special = [item for item in items if item.get("repo") not in categorized]
    report["updates"] = {item["repo"]: item.get("updates") or [] for item in updates}
    report["no_changes"] = {item["repo"]: item.get("current") or [] for item in no_changes}
    report["special"] = {item["repo"]: item for item in special}
    totals["updates"] = len(updates)
    totals["no_changes"] = len(no_changes)
    totals["special"] = len(special) + len(report.get("missing_service_yml") or [])
    totals["planned_releases"] = sum(
        1 for item in items if (item.get("planned_release") or {}).get("status") == "planned"
    )
    totals["dry_run_updates"] = sum(1 for item in items if item.get("dry_run_diffs"))
    totals["release_blockers"] = sum(
        1 for item in items if (item.get("planned_release") or {}).get("status") == "blocked"
    )
    config_statuses = [
        (item.get("config_sync") or {}).get("status")
        for item in items
        if item.get("config_sync")
    ]
    totals["config_sync_eligible"] = sum(
        int((item.get("config_sync") or {}).get("eligible") or 0) for item in items
    )
    totals["config_sync_skipped"] = sum(
        len((item.get("config_sync") or {}).get("skipped") or []) for item in items
    )
    for status in ("current", "drift", "blocked", "failed", "not_applicable", "not_configured", "off"):
        totals[f"config_sync_{status}"] = config_statuses.count(status)


def find_report_item(report: dict[str, Any], repo: str) -> dict[str, Any]:
    for item in report.get("per_repo") or []:
        if item.get("repo") == repo:
            return item
    raise ConfigSyncError(f"report does not contain repository {repo}")


def augment_report(
    report_dir: Path,
    repo: str,
    inventory_path: Path,
    owner: str,
    mode: str,
    generator: Any | None = None,
    extractor: Any | None = None,
) -> dict[str, Any]:
    from service_update_report import (
        UpdateReportGenerator,
        build_planned_release,
        render_markdown,
        render_planned_diffs,
    )

    report_path = report_dir / "service-update-report.json"
    report = json.loads(report_path.read_text())
    if mode not in ("off", "report", "apply"):
        raise ConfigSyncError(f"unsupported config sync mode: {mode}")

    if repo in report.get("external_services", []) or repo in report.get("missing_service_yml", []):
        if mode != "off":
            inventory = load_inventory(inventory_path)
            if inventory_for_repo(inventory, repo) is not None:
                raise ConfigSyncError(f"config sync cannot run for excluded repository {repo}")
        return report

    item = find_report_item(report, repo)
    if mode == "off":
        item["config_sync"] = {"mode": mode, "status": "off", "changes": 0, "eligible": 0}
    else:
        inventory = load_inventory(inventory_path)
        if inventory_for_repo(inventory, repo) is None and not auto_discover_enabled(inventory):
            item["config_sync"] = {
                "mode": mode,
                "status": "not_configured",
                "changes": 0,
                "eligible": 0,
            }
        else:
            generator = generator or UpdateReportGenerator(owner)
            extractor = extractor or DockerConfigExtractor()
            try:
                plan = plan_repository_configs(repo, item, inventory, generator, extractor)
                if plan is None:
                    raise ConfigSyncError(f"config sync inventory unexpectedly missing for {repo}")
                item.setdefault("current", []).extend(plan.current)
                if plan.blockers:
                    item.setdefault("dry_run_changes", []).extend(plan.changes)
                    for blocker in plan.blockers:
                        item.setdefault("warnings", []).append(f"config synchronization blocked: {blocker}")
                    reason = "; ".join(plan.blockers)
                    item["config_sync"] = {
                        "mode": mode,
                        "status": "blocked",
                        "changes": len(plan.changes),
                        "eligible": plan.eligible,
                        "skipped": plan.skipped,
                        "blockers": plan.blockers,
                        "diffs": render_planned_diffs(plan.changes),
                    }
                    if item.get("planned_changes"):
                        item["planned_release"] = blocked_release(
                            f"config synchronization blocked: {reason}",
                            item.get("planned_release"),
                        )
                else:
                    status = "not_applicable" if plan.eligible == 0 else (
                        "drift" if plan.changes else "current"
                    )
                    item["config_sync"] = {
                        "mode": mode,
                        "status": status,
                        "changes": len(plan.changes),
                        "eligible": plan.eligible,
                        "skipped": plan.skipped,
                        "diffs": render_planned_diffs(plan.changes),
                    }
                    if mode == "apply":
                        item.setdefault("planned_changes", []).extend(plan.changes)
                        item.setdefault("updates", []).extend(change["message"] for change in plan.changes)
                        if plan.changes:
                            item["planned_release"] = build_planned_release(
                                repo,
                                generator.get_github_tags(owner, repo),
                                item["planned_changes"],
                            )
                    elif mode == "report":
                        item.setdefault("dry_run_changes", []).extend(plan.changes)
                        if plan.changes and item.get("planned_changes"):
                            reason = "config snapshot drift requires review while config sync is in report mode"
                            item["planned_release"] = blocked_release(reason, item.get("planned_release"))
                            item.setdefault("warnings", []).append(reason)
            except Exception as exc:
                reason = f"config synchronization failed: {exc}"
                item["config_sync"] = {
                    "mode": mode,
                    "status": "failed",
                    "changes": 0,
                    "eligible": 0,
                    "reason": str(exc),
                    "diffs": [],
                }
                item.setdefault("warnings", []).append(reason)
                if item.get("planned_changes"):
                    item["planned_release"] = blocked_release(reason, item.get("planned_release"))

    item["planned_diffs"] = render_planned_diffs(
        [
            change
            for change in item.get("planned_changes") or []
            if change.get("change_type") != CONFIG_CHANGE_TYPE
        ]
    )
    item["dry_run_diffs"] = render_planned_diffs(
        [
            change
            for change in item.get("dry_run_changes") or []
            if change.get("change_type") != CONFIG_CHANGE_TYPE
        ]
    )
    refresh_report_totals(report)
    report_path.write_text(json.dumps(report, indent=2))
    (report_dir / "service-update-report.md").write_text(render_markdown(report))
    return report


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Plan image-derived service config snapshot updates.")
    parser.add_argument("--report-dir", required=True, help="Directory containing service-update-report.json.")
    parser.add_argument("--repo", required=True, help="Service repository name, for example service-nginx.")
    parser.add_argument("--inventory", default="config-sync.yml", help="Config synchronization inventory path.")
    parser.add_argument("--owner", default="wodby", help="GitHub owner/org that owns the service repo.")
    parser.add_argument("--mode", choices=("off", "report", "apply"), default="report")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        augment_report(
            Path(args.report_dir),
            args.repo,
            Path(args.inventory),
            args.owner,
            args.mode,
        )
        return 0
    except Exception as exc:
        print(f"Service config sync planning failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
