"""Plan and attest a coherent NVIDIA userspace closure; never install packages.

Use Ubuntu's python3-apt, not a pip resolver or a hand-written Depends parser.
Independent EGL/GLVND helpers have their own release series and are not pinned.
"""

import argparse
from pathlib import Path
import re
import sys


NVIDIA_NAME = re.compile(r"^(?:libnvidia-|nvidia-|xserver-xorg-video-nvidia-)")
DRIVER_RELEASE = re.compile(r"^(?:\d+:)?\d{3,}\.\d+\.\d+(?:-|$)")
KERNEL_PACKAGE = re.compile(r"^(?:linux-|nvidia-(?:dkms|kernel-source|driver)-)")


def driver_package(version):
    """Driver sources and release-numbered NVIDIA helpers, not EGL 1.x."""
    return bool(
        NVIDIA_NAME.match(version.package.name) and DRIVER_RELEASE.match(version.version)
    ) or version.source_name.startswith("nvidia-graphics-drivers")


def preference(names, version):
    # The first matching specific record wins. Reject all non-target versions,
    # rather than allowing fallback when the target disappears from an archive.
    packages = " ".join(sorted({f"{name.split(':', 1)[0]}:any" for name in names}))
    return (
        "# AxonOS host-compatible NVIDIA userspace; generated from APT metadata.\n"
        f"Package: {packages}\nPin: version {version}\nPin-Priority: 1001\n\n"
        f"Package: {packages}\nPin: version *\nPin-Priority: -1\n"
    )


def resolve(version, roots, extras=()):
    import apt
    import apt_pkg

    apt_pkg.config.set("APT::Install-Recommends", "false")
    apt_pkg.config.set("APT::Install-Suggests", "false")
    cache = apt.Cache(progress=None)
    resolver = apt_pkg.ProblemResolver(cache._depcache)
    for name in roots:
        if name not in cache or version not in cache[name].versions:
            raise RuntimeError(f"Missing exact NVIDIA package: {name}={version}")
        package = cache[name]
        if not package.versions[version].downloadable:
            raise RuntimeError(f"NVIDIA package is not downloadable: {name}={version}")
        package.candidate = package.versions[version]
        resolver.protect(package._pkg)
        package.mark_install(auto_fix=False, auto_inst=True, from_user=True)
    for name in extras:
        if name not in cache or cache[name].candidate is None:
            raise RuntimeError(f"Missing userspace support package: {name}")
        resolver.protect(cache[name]._pkg)
        cache[name].mark_install(auto_fix=False, auto_inst=True, from_user=True)
    resolver.resolve(True)
    if cache.broken_count:
        raise RuntimeError("No coherent installable NVIDIA dependency closure")

    selected = {}
    for package in cache:
        if package.marked_delete:
            raise RuntimeError(f"NVIDIA installation would remove {package.name}")
        changed = package.marked_install or package.marked_upgrade or package.marked_downgrade
        chosen = package.candidate if changed else package.installed
        if chosen is None:
            continue
        if changed and KERNEL_PACKAGE.match(package.name):
            raise RuntimeError(f"Not a userspace-only install: {package.name}")
        if driver_package(chosen):
            if chosen.version != version:
                raise RuntimeError(f"Mixed NVIDIA release: {package.name}={chosen.version}")
            selected[package.name] = chosen.version
    if not set(roots).issubset(selected):
        raise RuntimeError("APT did not retain every required NVIDIA root")
    return selected


def plan(version, roots, preferences, manifest, extras=()):
    import apt

    if not re.fullmatch(r"(?:\d+:)?\d{3,}\.\d+\.\d+-[A-Za-z0-9.+~]+", version):
        raise RuntimeError("Expected an exact NVIDIA driver Debian package version")
    cache = apt.Cache(progress=None)
    # Discovery policy covers driver-release packages, including unversioned
    # nvidia-modprobe/persistenced. libapt derives actual Depends/Pre-Depends,
    # versioned Provides, alternatives, conflicts and the resulting closure.
    names = set(roots)
    for package in cache:
        if any(driver_package(v) for v in package.versions):
            names.add(package.name)
            if package.installed and driver_package(package.installed):
                roots.add(package.name)
    original = preferences.read_bytes() if preferences.exists() else None
    try:
        preferences.write_text(preference(names, version), encoding="utf-8")
        selected = resolve(version, sorted(roots), extras)
        # Persist ONLY the derived installed/required driver package names.
        # Re-resolve under that final policy, not the broader discovery policy.
        preferences.write_text(preference(selected, version), encoding="utf-8")
        final = resolve(version, sorted(selected), extras)
        if final != selected:
            raise RuntimeError("NVIDIA closure changed under final pin policy")
        manifest.write_text(
            "".join(f"{name}={selected[name]}\n" for name in sorted(selected)),
            encoding="utf-8",
        )
        for name in sorted(selected):
            print(f"{name}={selected[name]}")
    except Exception:
        if original is None:
            preferences.unlink(missing_ok=True)
        else:
            preferences.write_bytes(original)
        raise


def verify(manifest):
    import apt
    import apt_pkg

    expected = dict(line.split("=", 1) for line in manifest.read_text().splitlines())
    if not expected or len(set(expected.values())) != 1:
        raise RuntimeError("Invalid NVIDIA closure manifest")
    version = next(iter(expected.values()))
    cache = apt.Cache(progress=None)
    for name, wanted in expected.items():
        installed = cache[name].installed if name in cache else None
        if (installed is None or installed.version != wanted
                or cache[name]._pkg.current_state != apt_pkg.CURSTATE_INSTALLED):
            raise RuntimeError(f"NVIDIA installed version mismatch: {name}, want {wanted}")
        print(f"axonos: verified {name}={wanted}")
    for package in cache:
        installed = package.installed
        if installed and driver_package(installed) and installed.version != version:
            raise RuntimeError(f"Mixed installed NVIDIA release: {package.name}={installed.version}")
    if cache.broken_count:
        raise RuntimeError("Installed NVIDIA environment has broken dependencies")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    prepare = commands.add_parser("plan")
    prepare.add_argument("--version", required=True)
    prepare.add_argument("--preferences", required=True, type=Path)
    prepare.add_argument("--manifest", required=True, type=Path)
    prepare.add_argument("--extra", action="append", default=[], help="unversioned support package")
    prepare.add_argument("roots", nargs="+")
    attest = commands.add_parser("verify")
    attest.add_argument("--manifest", required=True, type=Path)
    args = parser.parse_args()
    try:
        if args.command == "plan":
            plan(args.version, set(args.roots), args.preferences, args.manifest, args.extra)
        else:
            verify(args.manifest)
    except Exception as error:
        print(f"axonos: NVIDIA coherence check failed: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
