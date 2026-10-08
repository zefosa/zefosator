"""Target and vendor YAML loading, Jinja2 rendering and plan resolution."""

import re
from collections.abc import Mapping
from pathlib import Path
from typing import Annotated, Literal

import yaml
from packaging.specifiers import InvalidSpecifier, SpecifierSet
from packaging.version import InvalidVersion, Version
from pydantic import BaseModel, BeforeValidator, ConfigDict, Field, ValidationError, field_validator

from zefosator.errors import ConfigError
from zefosator.templating import render

Role = Literal["core", "modules", "devel"]
ROLES: tuple[Role, ...] = ("core", "modules", "devel")
DEFAULT_ZFS_REPO = "https://github.com/openzfs/zfs.git"
BUILDID_RE = re.compile(r"^[a-z][a-z0-9]*$")
ZFS_VERSION_RE = re.compile(r"^\d+(\.\d+)*$")


def _scalar_to_str(value: object) -> object:
    """YAML turns 44 into int and 9.8 into float; accept ints, reject lossy floats."""
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        msg = f"must be quoted (YAML read {value!r} as a number)"
        raise ValueError(msg)  # noqa: TRY004 -- pydantic turns ValueError into a validation error
    return value


ScalarStr = Annotated[str, BeforeValidator(_scalar_to_str)]


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class KernelSpec(_Model):
    version: ScalarStr
    release: ScalarStr


class ZfsConfig(_Model):
    tag: ScalarStr
    version: ScalarStr
    release: int = Field(default=0, ge=0)
    repo: str = DEFAULT_ZFS_REPO
    cherry_picks: tuple[str, ...] = ()
    patches: tuple[str, ...] = ()

    @field_validator("version")
    @classmethod
    def _plain_version(cls, value: str) -> str:
        if not ZFS_VERSION_RE.match(value):
            msg = f"must be a plain dotted version like 2.3.9, got {value!r}"
            raise ValueError(msg)
        return value


class TargetFile(_Model):
    vendor: str
    os_version: ScalarStr
    arch: str
    kernel: KernelSpec
    buildid: str | None = None
    zfs: ZfsConfig


class ImageConfig(_Model):
    base: str
    setup_packages: tuple[str, ...] = ()
    enable_repos: tuple[str, ...] = ()
    packages: tuple[str, ...] = ()


class VendorPackage(_Model):
    name: str
    repo: str | None = None
    arch: str | None = None
    role: Role | None = None


class VendorSettings(_Model):
    image: ImageConfig
    dist_tag: str
    gpg_keys: tuple[str, ...] = ()
    kernel_url: str
    packages: tuple[VendorPackage, ...]


class When(_Model):
    os_version: str | None = None
    arch: str | None = None


class Override(_Model):
    model_config = ConfigDict(extra="forbid", frozen=True, populate_by_name=True)

    when: When
    values: dict[str, object] = Field(alias="set")


class VendorFile(_Model):
    defaults: dict[str, object]
    overrides: tuple[Override, ...] = ()


class ResolvedPackage(_Model):
    name: str
    role: Role | None
    pkg_arch: str
    file: str
    url: str


class Plan(_Model):
    target_id: str
    vendor: str
    os_version: str
    os_major: int
    os_minor: int
    arch: str
    kernel_version: str
    kernel_release: str
    dist: str
    upstream_release: str
    uname_r: str
    zefosa_release: str
    buildid: str
    zfs: ZfsConfig
    image: ImageConfig
    gpg_keys: tuple[str, ...]
    packages: tuple[ResolvedPackage, ...]

    def role(self, role: Role) -> ResolvedPackage:
        return next(p for p in self.packages if p.role == role)


def _validation_error(source: str, exc: ValidationError) -> ConfigError:
    lines = []
    for err in exc.errors():
        loc = ".".join(str(part) for part in err["loc"])
        msg = err["msg"].removeprefix("Value error, ")
        lines.append(f"{source}: {loc}: {msg}")
    return ConfigError("\n".join(lines))


def _load_yaml(path: Path) -> dict[str, object]:
    try:
        data = yaml.safe_load(path.read_text())
    except yaml.YAMLError as exc:
        msg = f"{path}: invalid YAML (template values starting with '{{{{' must be quoted): {exc}"
        raise ConfigError(msg) from exc
    except OSError as exc:
        msg = f"{path}: cannot read: {exc.strerror}"
        raise ConfigError(msg) from exc
    if not isinstance(data, dict):
        msg = f"{path}: expected a YAML mapping at the top level"
        raise ConfigError(msg)
    return data


def _render_strings(value: object, variables: Mapping[str, object], where: str) -> object:
    """Render every string leaf of target YAML data; lists and mappings keep their shape."""
    if isinstance(value, str):
        return render(value, variables, where=where)
    if isinstance(value, list):
        return [_render_strings(v, variables, f"{where}[{i}]") for i, v in enumerate(value)]
    if isinstance(value, dict):
        return {k: _render_strings(v, variables, f"{where}.{k}") for k, v in value.items()}
    return value


def _merge(base: Mapping[str, object], over: Mapping[str, object]) -> dict[str, object]:
    merged = dict(base)
    for key, value in over.items():
        current = merged.get(key)
        if isinstance(current, dict) and isinstance(value, dict):
            merged[key] = _merge(current, value)
        else:
            merged[key] = value
    return merged


def _matches(when: When, os_version: str, arch: str, source: str) -> bool:
    if when.arch is not None and when.arch != arch:
        return False
    if when.os_version is None:
        return True
    try:
        return Version(os_version) in SpecifierSet(when.os_version)
    except (InvalidSpecifier, InvalidVersion) as exc:
        msg = f"{source}: overrides: when.os_version {when.os_version!r}: {exc}"
        raise ConfigError(msg) from exc


def _parse_os_version(os_version: str, source: str) -> tuple[int, int]:
    parts = os_version.split(".")
    if not 1 <= len(parts) <= 2 or not all(p.isdigit() for p in parts):  # noqa: PLR2004 -- major[.minor]
        msg = f"{source}: os_version: expected MAJOR or MAJOR.MINOR, got {os_version!r}"
        raise ConfigError(msg)
    return int(parts[0]), int(parts[1]) if len(parts) == 2 else 0  # noqa: PLR2004 -- major.minor


def _load_target(path: Path) -> TargetFile:
    raw = _load_yaml(path)
    rendered = {k: _render_strings(v, raw, f"{path}: {k}") for k, v in raw.items()}
    try:
        return TargetFile.model_validate(rendered)
    except ValidationError as exc:
        raise _validation_error(str(path), exc) from exc


def _load_vendor(path: Path, os_version: str, arch: str) -> VendorSettings:
    try:
        vendor = VendorFile.model_validate(_load_yaml(path))
    except ValidationError as exc:
        raise _validation_error(str(path), exc) from exc
    settings = vendor.defaults
    for override in vendor.overrides:
        if _matches(override.when, os_version, arch, str(path)):
            settings = _merge(settings, override.values)
    try:
        return VendorSettings.model_validate(settings)
    except ValidationError as exc:
        source = f"{path}: (merged settings)"
        raise _validation_error(source, exc) from exc


def _choose_buildid(target: TargetFile, override: str | None, source: Path) -> str:
    if override is not None:
        buildid, where = override, "--buildid"
    elif target.buildid is not None:
        buildid, where = target.buildid, f"{source}: buildid"
    else:
        return "zefosa" if target.zfs.release == 0 else f"zefosa{target.zfs.release}"
    if not BUILDID_RE.match(buildid):
        msg = f"{where}: invalid buildid {buildid!r}, must match {BUILDID_RE.pattern}"
        raise ConfigError(msg)
    return buildid


def _check_roles(settings: VendorSettings, source: Path) -> None:
    for role in ROLES:
        count = sum(1 for p in settings.packages if p.role == role)
        if count != 1:
            msg = f"{source}: packages: role {role!r} must appear exactly once, found {count}"
            raise ConfigError(msg)


def project_root(target_path: Path) -> Path:
    """Targets live in <root>/targets/; vendors, patches, cache and output are under <root>."""
    return target_path.resolve().parent.parent


def load_plan(target_path: Path, *, buildid: str | None = None) -> Plan:
    """Load a target file and its vendor profile and resolve every value."""
    root = project_root(target_path)
    target = _load_target(target_path)
    vendor_path = root / "vendors" / f"{target.vendor}.yaml"
    if not vendor_path.is_file():
        msg = f"{target_path}: vendor {target.vendor!r}: {vendor_path} not found"
        raise ConfigError(msg)
    for patch in target.zfs.patches:
        if not (root / patch).is_file():
            msg = f"{target_path}: zfs.patches: file not found: {patch}"
            raise ConfigError(msg)
    settings = _load_vendor(vendor_path, target.os_version, target.arch)
    _check_roles(settings, vendor_path)
    os_major, os_minor = _parse_os_version(target.os_version, str(target_path))
    chosen_buildid = _choose_buildid(target, buildid, target_path)

    variables: dict[str, object] = {
        "vendor": target.vendor,
        "os_version": target.os_version,
        "os_major": os_major,
        "os_minor": os_minor,
        "arch": target.arch,
        "buildid": chosen_buildid,
        "kernel": target.kernel.model_dump(),
        "zfs": target.zfs.model_dump(),
    }
    dist = render(settings.dist_tag, variables, where=f"{vendor_path}: dist_tag")
    upstream_release = f"{target.kernel.release}.{dist}"
    variables |= {
        "dist": dist,
        "upstream_release": upstream_release,
        "uname_r": f"{target.kernel.version}-{upstream_release}.{target.arch}",
        "zefosa_release": f"{target.kernel.release}.{chosen_buildid}.{dist}",
        "target_id": f"{target.vendor}-{target.os_version}-{target.arch}",
    }

    def rv(template: str, key: str) -> str:
        return render(template, variables, where=f"{vendor_path}: {key}")

    image = ImageConfig(
        base=rv(settings.image.base, "image.base"),
        setup_packages=tuple(rv(v, "image.setup_packages") for v in settings.image.setup_packages),
        enable_repos=tuple(rv(v, "image.enable_repos") for v in settings.image.enable_repos),
        packages=tuple(rv(v, "image.packages") for v in settings.image.packages),
    )
    packages = []
    for pkg in settings.packages:
        pkg_arch = pkg.arch or target.arch
        file = f"{pkg.name}-{target.kernel.version}-{upstream_release}.{pkg_arch}.rpm"
        pkg_vars: dict[str, object] = {"name": pkg.name, "pkg_arch": pkg_arch, "file": file}
        if pkg.repo is not None:
            pkg_vars["repo"] = pkg.repo
        url = render(
            settings.kernel_url,
            variables | pkg_vars,
            where=f"{vendor_path}: kernel_url (package {pkg.name})",
        )
        packages.append(
            ResolvedPackage(name=pkg.name, role=pkg.role, pkg_arch=pkg_arch, file=file, url=url)
        )

    return Plan(
        target_id=str(variables["target_id"]),
        vendor=target.vendor,
        os_version=target.os_version,
        os_major=os_major,
        os_minor=os_minor,
        arch=target.arch,
        kernel_version=target.kernel.version,
        kernel_release=target.kernel.release,
        dist=dist,
        upstream_release=upstream_release,
        uname_r=str(variables["uname_r"]),
        zefosa_release=str(variables["zefosa_release"]),
        buildid=chosen_buildid,
        zfs=target.zfs,
        image=image,
        gpg_keys=tuple(rv(k, "gpg_keys") for k in settings.gpg_keys),
        packages=tuple(packages),
    )
