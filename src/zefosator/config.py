"""Target and vendor YAML loading, Jinja2 rendering and plan resolution."""

import re
from collections.abc import Mapping
from pathlib import Path, PurePosixPath
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
ARCHIVE_NAME_RE = re.compile(r"^[A-Za-z0-9._-]+$")


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


class ShimPin(_Model):
    version: ScalarStr
    release: ScalarStr


class TargetBoot(_Model):
    shim: ShimPin
    cloud_image: str


class TargetFile(_Model):
    vendor: str
    os_version: ScalarStr
    arch: str
    kernel: KernelSpec
    buildid: str | None = None
    zfs: ZfsConfig
    boot: TargetBoot | None = None
    # archive name -> Jinja2 templates of paths under output/, rendered with the resolved plan
    archives: dict[str, tuple[str, ...]] = {}

    @field_validator("archives")
    @classmethod
    def _archive_names(cls, value: dict[str, tuple[str, ...]]) -> dict[str, tuple[str, ...]]:
        for name, files in value.items():
            if not ARCHIVE_NAME_RE.match(name):
                msg = f"archive name {name!r} may only contain letters, digits, '.', '_' and '-'"
                raise ValueError(msg)
            if not files:
                msg = f"archive {name!r} lists no files"
                raise ValueError(msg)
        return value


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


class VendorBoot(_Model):
    common: bool = False
    esp_dir: str = Field(pattern=r"^[A-Za-z0-9_-]+$")
    shim_url: str
    drop_requires: tuple[str, ...] = ()


class VendorSettings(_Model):
    image: ImageConfig
    dist_tag: str
    gpg_keys: tuple[str, ...] = ()
    kernel_url: str
    packages: tuple[VendorPackage, ...]
    boot: VendorBoot | None = None


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


class BootPlan(_Model):
    common: bool
    esp_dir: str
    shim_version: str
    shim_release: str
    shim_file: str
    shim_url: str
    drop_requires: tuple[str, ...]
    cloud_image: str

    @property
    def zefosa_shim_release(self) -> str:
        """Vendor release + .zefosa sorts above the vendor's own build in every distro."""
        return f"{self.shim_release}.zefosa"


class ArchivePlan(_Model):
    name: str
    files: tuple[str, ...]  # relative to output/


class SigningInfo(_Model):
    """Public facts about the signing certificate; the key itself never enters the plan."""

    cert_sha256: str
    cert_subject: str


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
    signing: SigningInfo | None = None
    boot: BootPlan | None = None
    archives: tuple[ArchivePlan, ...] = ()

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
    # archives are rendered later, with the resolved plan
    rendered = {
        k: v if k == "archives" else _render_strings(v, raw, f"{path}: {k}") for k, v in raw.items()
    }
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

    boot = _boot_plan(target, settings, variables, (target_path, vendor_path))
    plan = Plan(
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
        boot=boot,
    )
    if not target.archives:
        return plan
    return plan.model_copy(update={"archives": _archive_plans(target, plan, root, target_path)})


def _archive_plans(
    target: TargetFile, plan: Plan, root: Path, target_path: Path
) -> tuple[ArchivePlan, ...]:
    variables: dict[str, object] = plan.model_dump(mode="json")
    if (root / "common.yaml").is_file():
        variables["boot_packages"] = load_boot_config(root).model_dump(mode="json")
    return tuple(
        ArchivePlan(name=name, files=_archive_files(name, files, variables, target_path))
        for name, files in target.archives.items()
    )


def _archive_files(
    name: str, templates: tuple[str, ...], variables: Mapping[str, object], source: Path
) -> tuple[str, ...]:
    seen: dict[str, str] = {}
    for index, template in enumerate(templates):
        where = f"{source}: archives.{name}[{index}]"
        path = render(template, variables, where=where)
        pure = PurePosixPath(path)
        problem = None
        if pure.is_absolute():
            problem = "must be relative to output/"
        elif ".." in pure.parts:
            problem = "must not contain '..'"
        elif path.endswith("/") or not pure.name:
            problem = "must name a file"
        elif pure.name in seen.values():
            first = next(p for p, n in seen.items() if n == pure.name)
            problem = f"has the same file name as {first!r}; archives are flat"
        if problem is not None:
            msg = f"{where}: {path!r} {problem}"
            raise ConfigError(msg)
        seen[str(pure)] = pure.name
    return tuple(seen)


_GIT_REF_RE = r"^[A-Za-z0-9._][A-Za-z0-9._/-]*$"  # never an option, never whitespace
_SHA256_RE = r"^[0-9a-f]{64}$"


class ZbmPin(_Model):
    version: ScalarStr
    repo: str
    tag: str = Field(pattern=_GIT_REF_RE)  # a tag or a commit; the resolved commit is recorded
    release: int = Field(ge=1)
    build_packages: tuple[str, ...]
    recovery_packages: tuple[str, ...]
    recovery_tools: tuple[str, ...]


class RefindPin(_Model):
    version: ScalarStr
    upstream_release: ScalarStr  # Fedora's release of the rEFInd-unsigned-x64 RPM
    url: str
    sha256: str = Field(pattern=_SHA256_RE)
    release: int = Field(ge=1)


class VersionPin(_Model):
    version: ScalarStr = "1"
    release: int = Field(ge=1)


class BootConfig(_Model):
    zfsbootmenu: ZbmPin
    refind: RefindPin
    ca: VersionPin
    tools: VersionPin


class UploadConfig(_Model):
    webdav_url: str  # archives go to <webdav_url><name>.tar and <name>.sha256

    @field_validator("webdav_url")
    @classmethod
    def _http_directory(cls, value: str) -> str:
        if not value.startswith(("https://", "http://")) or len(value.split("://", 1)[1]) == 0:
            msg = f"expected an http(s) URL, got {value!r}"
            raise ValueError(msg)
        return value if value.endswith("/") else value + "/"


class CommonConfig(_Model):
    """common.yaml: settings shared by every target."""

    boot: BootConfig
    upload: UploadConfig | None = None


def load_common_config(root: Path) -> CommonConfig:
    """common.yaml; its string values are Jinja2 templates over the file's own values."""
    path = root / "common.yaml"
    old = root / "boot" / "boot.yaml"
    if old.exists():
        msg = f"{old}: moved to the boot section of {path}; delete the old file"
        raise ConfigError(msg)
    raw = _load_yaml(path)
    rendered = {k: _render_strings(v, raw, f"{path}: {k}") for k, v in raw.items()}
    try:
        return CommonConfig.model_validate(rendered)
    except ValidationError as exc:
        raise _validation_error(str(path), exc) from exc


def load_boot_config(root: Path) -> BootConfig:
    return load_common_config(root).boot


def _boot_plan(
    target: TargetFile,
    settings: VendorSettings,
    variables: Mapping[str, object],
    sources: tuple[Path, Path],
) -> BootPlan | None:
    target_path, vendor_path = sources
    if target.boot is None:
        return None
    if settings.boot is None:
        msg = f"{target_path}: boot: vendor {target.vendor!r} has no boot settings"
        raise ConfigError(msg)
    for pattern in settings.boot.drop_requires:
        try:
            re.compile(pattern)
        except re.error as exc:
            msg = f"{vendor_path}: boot.drop_requires: invalid regex {pattern!r}: {exc}"
            raise ConfigError(msg) from exc
    shim = target.boot.shim
    shim_file = f"shim-x64-{shim.version}-{shim.release}.{target.arch}.rpm"
    url = render(
        settings.boot.shim_url,
        {**variables, "shim": shim.model_dump(), "shim_file": shim_file},
        where=f"{vendor_path}: boot.shim_url",
    )
    return BootPlan(
        common=settings.boot.common,
        esp_dir=settings.boot.esp_dir,
        shim_version=shim.version,
        shim_release=shim.release,
        shim_file=shim_file,
        shim_url=url,
        drop_requires=settings.boot.drop_requires,
        cloud_image=target.boot.cloud_image,
    )
