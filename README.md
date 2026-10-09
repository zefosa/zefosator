# Zefosator

OpenZFS-enabled kernel packages for Fedora and AlmaLinux, for root-on-ZFS systems.

Zefosator does **not** compile the kernel. It downloads the vendor's binary kernel RPMs, builds
OpenZFS against the matching `kernel-devel`, and repackages the upstream RPMs unchanged except for:

- the release, which becomes `<release>.zefosa.<dist>` (e.g. `7.2.9-200.zefosa.fc44`);
- the ZFS kernel modules, added to `kernel-modules-core` (so they install like any other core module)
  together with `Provides: zfs-kmod = <version>`;
- `kernel-core` requiring `zfs-dracut >= <version>` and conflicting with `zfs-dkms`, so the initramfs
  built when the kernel is installed always contains the ZFS dracut module;
- a changelog entry and `Packager: Zefosa`.

Every rebuilt package is compared with its upstream header: file digests, modes, owners, flags,
scripts and dependencies must be identical apart from the changes above, or the build fails.

`uname -r` is unchanged (`7.2.9-200.fc44.x86_64`): it is compiled into the kernel image and modules,
so only the RPM release carries the `.zefosa` build ID.

## Requirements

- `podman` (rootless is fine)
- [`uv`](https://docs.astral.sh/uv/) (`dnf install uv`)

Everything else runs in a per-target container built from the vendor's base image.

## Usage

```
uv run zefosator plan   targets/fedora-44-x86_64.yaml   # show the resolved plan (JSON)
uv run zefosator build  targets/fedora-44-x86_64.yaml   # build into output/fedora-44-x86_64/
uv run zefosator verify targets/fedora-44-x86_64.yaml   # install and check in a clean container
```

`build` writes:

- `output/<target-id>/kernel/` — the repackaged kernel RPMs;
- `output/<target-id>/zfs/` — every RPM the OpenZFS build produced (userspace, dracut, devel,
  debuginfo, test, SRPMs) except the `kmod-zfs` package whose modules went into the kernel;
- `output/<target-id>/manifest.json` — input URLs and checksums, the OpenZFS commit, and every
  output's NEVRA and checksum.

Choosing which of these to publish is up to the publishing step.

`verify` copies the output into a container of the vendor's plain base image, adds it as a local
repository with `priority=10`, and checks that dnf prefers the zefosa kernel over a newer upstream, that
`dnf install kernel zfs` pulls `zfs-dracut`, that `modinfo` finds the ZFS module, that a
`dracut --no-hostonly` initramfs contains ZFS, and that `rpm -V` passes.

Downloads and OpenZFS clones are cached in `cache/`; logs of the last build are in
`cache/build/<target-id>/logs/`.

## Targets and vendors

A target file pins one kernel and one OpenZFS version:

```yaml
# targets/almalinux-9-x86_64.yaml
vendor: almalinux            # vendors/almalinux.yaml
os_version: "9.8"            # quote it: YAML would read 9.10 as 9.1
arch: x86_64
kernel:
  version: 5.14.0
  release: 687.52.1
zfs:
  tag: zfs-2.3.9
  version: 2.3.9
  # release: 0               # default
  # repo: https://github.com/openzfs/zfs.git
  # cherry_picks: []         # commits to cherry-pick onto the tag
  # patches: []              # files under patches/, applied with `git apply`
# buildid: zefosa            # default: derived from zfs.release
```

A vendor file describes where packages come from and which package plays which role:

```yaml
defaults:
  image: {base: ..., setup_packages: [...], enable_repos: [...], packages: [...]}
  dist_tag: "el{{ os_major }}_{{ os_minor }}"
  gpg_keys: ["/etc/pki/rpm-gpg/RPM-GPG-KEY-AlmaLinux-{{ os_major }}"]
  kernel_url: "https://{{ 'raw.' if os_major == 9 }}repo.almalinux.org/..."
  packages:
    - {name: kernel, repo: BaseOS}                       # first entry: the spec's main package
    - {name: kernel-core, repo: BaseOS, role: core}      # gets zfs-dracut / zfs-dkms deps
    - {name: kernel-modules-core, repo: BaseOS, role: modules}  # gets the ZFS modules
    - {name: kernel-devel, repo: AppStream, role: devel} # OpenZFS is built against it
overrides:
  - when: {os_version: ">=10", arch: aarch64}
    set: {packages: [...]}
```

- String values in both files are Jinja2 templates (sandboxed, undefined variables are errors).
  Values starting with `{{` must be quoted. Variables: target fields (`vendor`, `os_version`, `arch`,
  `buildid`, `kernel.*`, `zfs.*`), `os_major`/`os_minor` (integers), `dist`, `upstream_release`,
  `uname_r`, `zefosa_release`, `target_id`, and in `kernel_url` also `name`, `repo`, `pkg_arch`, `file`.
- `overrides` apply in order when every `when` key matches (`os_version` is a version range).
  Mappings merge key by key; lists are replaced. Use inline `{{ … if … }}` for small string
  differences and `overrides` for structural ones. When a distro version changes fundamentally,
  copy the vendor file and point the target at the new name.
- Each role (`core`, `modules`, `devel`) must appear exactly once. Every listed package is rebuilt.

## Release scheme

Kernel packages get release `<kernel.release>.<buildid>.<dist>`; OpenZFS packages get
`<zfs.version>-<zfs.release>`.

| `zfs.release` | buildid (default) | kernel release example | when |
|---|---|---|---|
| `0` (default) | `zefosa` | `200.zefosa.fc44` | normal builds; OpenZFS updates wait for the next kernel |
| `1`, `2`, … | `zefosa1`, `zefosa2`, … | `200.zefosa1.fc44` | emergency rebuild of the same kernel |

RPM orders `200.fc44 < 200.zefosa.fc44 < 200.zefosa1.fc44 < 201.fc44`. `--buildid` (or `buildid:` in
the target) overrides the derived value.

## Making dnf prefer zefosa kernels

Publish the output in a repository whose `.repo` file sets `priority=` lower than the vendor repos
(default 99), e.g. `priority=10`. When dnf resolves a package name that the zefosa repository
provides (`kernel`, `kernel-core`, …), it takes it from that repository even if a vendor repository
has a newer version, so `dnf upgrade` only installs zefosa kernels and waits for the next zefosa
build. Vendor kernels still appear in `dnf list` and `dnf repoquery`; they are just never chosen.
`zefosator verify` checks this against a newer stand-in `kernel-core` in a priority 99 repository.
Disabling the repository returns the system to vendor kernels.

Use a new repository for these packages: earlier (v1) repositories published OpenZFS with release `1`,
which sorts above this tool's default release `0`.

## Secure Boot

zefosa systems can boot with UEFI Secure Boot and without GRUB:

```
firmware ─► vendor shim ─► rEFInd ─► ZFSBootMenu ─kexec─► zefosa kernel ─► signed ZFS modules
            (Microsoft CA)  (zefosa)   (zefosa)            (vendor-signed)   (zefosa)
```

Vendor-signed binaries (shim, `vmlinuz`, vendor modules) are never re-signed. zefosa signs only what
it builds: the ZFS modules, rEFInd and the ZFSBootMenu images, with a leaf key issued by the zefosa
Secure Boot CA. The CA is enrolled once per machine as a MOK key; the kernel then trusts it for
modules (`.machine` keyring) and shim trusts it for rEFInd and ZFSBootMenu. ZFSBootMenu is built
with the zefosa Fedora kernel, which locks down under Secure Boot: it loads only signed modules and
kexecs only kernels whose signature verifies against the vendor CA that shim provides.

### Keys

```
uv run zefosator keys ca   --out /secure/zefosa-ca        # keep ca.key offline
uv run zefosator keys leaf --ca /secure/zefosa-ca --out /secure/zefosa-signing
```

The CA certificate may only sign certificates (`keyCertSign`, no `digitalSignature`): only such MOK
keys reach the kernel's `.machine` keyring. Leaves are code-signing certificates valid for at most
three years; issuing a new leaf needs no action from users. Keys must live **outside** the project
tree: build containers mount it. The leaf key reaches build containers only as a podman secret.

### Building

```
uv run zefosator build targets/fedora-44-x86_64.yaml \
    --signing-key /secure/zefosa-signing/signing.key --signing-cert /secure/zefosa-signing/signing.pem
uv run zefosator build-boot targets/fedora-44-x86_64.yaml \
    --signing-key ... --signing-cert ... --ca-cert /secure/zefosa-ca/ca.pem
uv run zefosator build-boot targets/almalinux-9-x86_64.yaml     # shim only, no key needed
```

Instead of `--signing-key`, the key's PEM text may come from `ZEFOSA_SIGNING_KEY` (and the
certificate path from `ZEFOSA_SIGNING_CERT`). `build --unsigned` makes a development build.

`build-boot` repackages the vendor `shim-x64` for every target into `output/<target-id>/boot/`
(EFI binaries byte-identical, GRUB requirements dropped, installed in `\EFI\zefosa\`). On the
Fedora target it also builds the distro-independent packages into `output/boot/`:

| Package | Contents |
|---|---|
| `zefosa-secureboot-ca` | `/usr/share/zefosa/keys/zefosa-ca.{der,pem,sha256}` |
| `zefosa-refind` | Fedora's rEFInd (pinned in `boot/boot.yaml`), signed, as `\EFI\zefosa\grubx64.efi` |
| `zefosa-zfsbootmenu`, `zefosa-zfsbootmenu-recovery` | signed ZFSBootMenu UKIs (recovery adds rescue tools) |
| `zefosa-boot` | `zefosa-boot` command, `zbm-kcl`, the `20-zbm.install` kernel-install plugin; replaces GRUB |

Rebuild the ZFSBootMenu images (bump `release` in `boot/boot.yaml`) after zefosa Fedora kernel or
ZFSBootMenu updates.

### Testing in QEMU

```
uv run zefosator boottest targets/fedora-44-x86_64.yaml --ca-cert /secure/zefosa-ca/ca.pem
```

A builder VM installs a root-on-ZFS system with the built kernel and boot packages, then the target
boots with OVMF Secure Boot and the CA pre-enrolled as MOK. Checks: Secure Boot on, lockdown
`integrity`, root on ZFS, `zfs` signed by the CA, CA in `.machine`, a kernel without signature
refused by ZFSBootMenu, an unsigned ZFSBootMenu image refused by shim. Needs `qemu-system-x86-core`,
`qemu-img`, `guestfs-tools`, `libguestfs`, `python3-virt-firmware`, `sbsigntools` and `edk2-ovmf`.

### On a machine

```
dnf install zefosa-boot             # from the zefosa-boot repository; replaces shim-x64 and grub2-efi-x64
zefosa-boot setup                   # refind.conf, firmware boot entry, CA enrollment request
reboot                              # MokManager: Enroll MOK -> Continue -> Yes -> root password
zefosa-boot status
zefosa-boot cmdline -r quiet -a loglevel=7    # final OS kernel command line (zbm-kcl)
```

On AlmaLinux, dnf 4 counts replacing the protected `grub2-efi-x64` as removing it and refuses;
install with `dnf install --setopt=protected_packages= zefosa-boot` (Fedora's dnf 5 needs no option).

`/boot/efi/EFI/zefosa/refind.conf` holds the ZFSBootMenu options (`zbm.prefer=<pool>`, timeout,
recovery entry) and is never overwritten. The firmware entry starts
`\EFI\zefosa\shimx64.efi` without load options, so firmware that drops boot options still works.

## Limitations

- Repackaged packages are unsigned at the RPM level: AlmaLinux RPM signatures and IMA file signatures
  (EL kernels ship them) cannot be carried over. Sign the packages or the repository when publishing.
- Upstream inputs are verified before use: AlmaLinux packages by signature, Fedora Koji packages
  (unsigned) by digest.
- Scriptlet triggers and file triggers are not supported; a vendor package using an unknown header
  tag fails the build instead of being repackaged incompletely.

## Development

```
uv run zefosator-check            # ruff format --check, ruff check, mypy --strict, pytest
uv run pytest -m container        # repackage every real package of every target (needs
                                  # rpmbuild and the RPMs in cache/downloads/)
uv run python -m tests.fixtures.make_fixtures   # regenerate header fixtures from cache/downloads/
```

All code is fully type-annotated; `mypy --strict` and `ruff` with `select = ["ALL"]` must pass.
