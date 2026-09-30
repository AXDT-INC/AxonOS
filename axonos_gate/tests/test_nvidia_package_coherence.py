"""Exercise NVIDIA pinning with native APT and an isolated synthetic repository.

These tests never install a package, contact an external repository, or inspect
the host's package database. APT_CONFIG redirects every state/configuration path
into a temporary directory; the only configured repository uses file://.
"""

from __future__ import annotations

import os
from pathlib import Path
import runpy
import subprocess
import tempfile
import unittest


REPO = Path(__file__).resolve().parents[2]
PLANNER = REPO / "scripts" / "plan-nvidia-userspace.py"
RESOLVER = REPO / "scripts" / "resolve-nvidia-driver-pkg-version.sh"
OLD = "580.173.02-1ubuntu1"
NEW = "580.178.04-1ubuntu1"
ROOTS = (
    "xserver-xorg-video-nvidia-580", "libnvidia-gl-580",
    "libnvidia-cfg1-580", "libnvidia-common-580",
    "libnvidia-compute-580", "nvidia-kernel-common-580",
)
DRIVER_PACKAGES = (*ROOTS, "libnvidia-gpucomp-580", "nvidia-firmware-580",
                   "libnvidia-decode-580", "nvidia-modprobe", "nvidia-persistenced")
APT_PYTHON = Path("/usr/bin/python3")
HAS_PYTHON_APT = APT_PYTHON.exists() and subprocess.run(
    [str(APT_PYTHON), "-c", "import apt_pkg"], capture_output=True,
).returncode == 0


def driver_records() -> list[dict[str, str]]:
    records = []
    for version in (OLD, NEW):
        triplet = version.split("-", 1)[0]
        for name in DRIVER_PACKAGES:
            record = {
                "Package": name, "Version": version, "Architecture": "amd64",
                "Source": name if name in ("nvidia-modprobe", "nvidia-persistenced")
                else "nvidia-graphics-drivers-580",
            }
            if name == "xserver-xorg-video-nvidia-580":
                record["Depends"] = f"libnvidia-cfg1-580 (= {version}), libnvidia-gl-580 (= {version})"
            elif name == "libnvidia-gl-580":
                record["Depends"] = (
                    f"libnvidia-gpucomp-580 (= {version}), libnvidia-compute-580 (= {version}), "
                    f"libnvidia-common-580 (= {version}), libnvidia-egl-wayland1 (>= 1.1.10)"
                )
            elif name == "libnvidia-compute-580":
                record["Depends"] = (
                    f"libnvidia-gpucomp-580 (= {version}), libnvidia-decode-580 (>= {triplet}), "
                    f"nvidia-persistenced (>= {version}), nvidia-kernel-common-580-{triplet}"
                )
            elif name == "nvidia-kernel-common-580":
                record["Depends"] = f"nvidia-firmware-580 (= {version}), nvidia-modprobe (>= {version})"
                record["Provides"] = f"nvidia-kernel-common-580-{triplet}"
            records.append(record)
    records.append({
        "Package": "libnvidia-egl-wayland1", "Version": "1:1.1.20-1ubuntu1",
        "Architecture": "amd64", "Source": "egl-wayland",
    })
    return records


def stanza(record: dict[str, str], installed: bool = False) -> str:
    fields = dict(record)
    fields.update({"Maintainer": "Synthetic build regression <synthetic@invalid>",
                   "Description": "synthetic NVIDIA dependency metadata"})
    if installed:
        fields["Status"] = "install ok installed"
    else:
        fields["Filename"] = f"pool/{fields['Package']}_{fields['Version']}_amd64.deb"
        fields["Size"] = "1"
    return "".join(f"{key}: {value}\n" for key, value in fields.items()) + "\n"


class AptRepository:
    def __init__(self, directory: str, records: list[dict[str, str]]) -> None:
        self.root = Path(directory)
        self.records = records
        for part in ("repository", "etc/preferences.d", "state/lists/partial", "cache/archives/partial"):
            (self.root / part).mkdir(parents=True, exist_ok=True)
        self.status = self.root / "state/status"
        self.status.write_text("")
        self.preferences = self.root / "etc/preferences.d/axonos-nvidia.pref"
        self.manifest = self.root / "manifest.txt"
        repository = self.root / "repository"
        (repository / "Packages").write_text("".join(stanza(record) for record in records))
        (self.root / "etc/sources.list").write_text(
            f"deb [trusted=yes arch=amd64] file:{repository} ./\n"
        )
        config = self.root / "apt.conf"
        config.write_text(
            f'Dir "{self.root}";\n'
            'Dir::Etc "etc";\nDir::Etc::main "-";\nDir::Etc::parts "-";\n'
            'Dir::Etc::sourcelist "sources.list";\nDir::Etc::sourceparts "-";\n'
            'Dir::Etc::preferences "-";\nDir::Etc::preferencesparts "preferences.d";\n'
            'Dir::State "state";\nDir::State::status "status";\n'
            'Dir::State::lists "lists";\nDir::Cache "cache";\n'
            'Dir::Cache::pkgcache "";\nDir::Cache::srcpkgcache "";\n'
            'APT::Architecture "amd64";\nAPT::Architectures { "amd64"; };\n'
            'APT::Install-Recommends "false";\nAPT::Install-Suggests "false";\n'
            'Acquire::Languages "none";\nDebug::NoLocking "true";\n'
        )
        self.env = {**os.environ, "APT_CONFIG": str(config), "LC_ALL": "C",
                    "PYTHONDONTWRITEBYTECODE": "1"}
        update = self.run("apt-get", "update")
        if update.returncode:
            raise AssertionError(update.stdout + update.stderr)

    def run(self, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(args, env=self.env, capture_output=True, text=True, timeout=30)

    def plan(self, extras: tuple[str, ...] = ()) -> subprocess.CompletedProcess[str]:
        return self.run(str(APT_PYTHON), str(PLANNER), "plan", "--version", OLD,
                        "--preferences", str(self.preferences), "--manifest", str(self.manifest),
                        *(argument for name in extras for argument in ("--extra", name)),
                        *ROOTS)

    def installed(self, version: str, omit: str | None = None) -> None:
        self.status.write_text("".join(
            stanza(record, installed=True) for record in self.records
            if record["Package"] != omit and (
                record["Version"] == version or record["Package"] == "libnvidia-egl-wayland1"
            )
        ))


@unittest.skipUnless(HAS_PYTHON_APT, "native python3-apt is required for isolated APT regression tests")
class NvidiaDependencyCoherenceTests(unittest.TestCase):
    def fixture(self, records: list[dict[str, str]] | None = None) -> AptRepository:
        directory = tempfile.TemporaryDirectory(prefix="axonos-nvidia-apt-test-")
        self.addCleanup(directory.cleanup)
        return AptRepository(directory.name, records if records is not None else driver_records())

    def assert_plan(self, fixture: AptRepository, expected: tuple[str, ...] = DRIVER_PACKAGES,
                    extras: tuple[str, ...] = ()) -> None:
        result = fixture.plan(extras)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        manifest = fixture.manifest.read_text().splitlines()
        self.assertEqual(set(manifest), {f"{package}={OLD}" for package in expected})
        self.assertEqual(len(manifest), len(set(manifest)), "duplicate package pins")
        for package in expected:
            policy = fixture.run("apt-cache", "policy", package)
            self.assertEqual(policy.returncode, 0, policy.stderr)
            self.assertIn(f"Candidate: {OLD}", policy.stdout, policy.stdout)
        independent = fixture.run("apt-cache", "policy", "libnvidia-egl-wayland1")
        self.assertIn("Candidate: 1:1.1.20-1ubuntu1", independent.stdout)

    def test_current_repository_drift_resolves_full_old_driver_closure(self) -> None:
        fixture = self.fixture()
        # This is the original six explicit pins, without a closure preference.
        before = fixture.run("apt-get", "-s", "--no-install-recommends", "install",
                             *(f"{package}={OLD}" for package in ROOTS))
        self.assertNotEqual(before.returncode, 0)
        self.assertIn(NEW, before.stdout + before.stderr)
        self.assert_plan(fixture)
        after = fixture.run("apt-get", "-s", "--no-install-recommends", "--allow-downgrades",
                            "install", *ROOTS)
        self.assertEqual(after.returncode, 0, after.stdout + after.stderr)
        installed = [line for line in after.stdout.splitlines() if line.startswith("Inst ")]
        self.assertTrue(installed)
        self.assertFalse(any(NEW in line for line in installed), installed)
        for name in DRIVER_PACKAGES:
            self.assertTrue(any(line.startswith(f"Inst {name} ") and OLD in line for line in installed), name)

    def test_required_old_transitive_package_missing_fails_closed(self) -> None:
        for missing in ("libnvidia-gpucomp-580", "nvidia-firmware-580", "libnvidia-decode-580"):
            with self.subTest(package=missing):
                records = [record for record in driver_records()
                           if (record["Package"], record["Version"]) != (missing, OLD)]
                fixture = self.fixture(records)
                result = fixture.plan()
                self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertFalse(fixture.manifest.exists(), "failed resolution published a manifest")
                self.assertFalse(fixture.preferences.exists(), "failed resolution left discovery policy")

    def test_failed_plan_restores_preexisting_preference_exactly(self) -> None:
        records = [record for record in driver_records()
                   if (record["Package"], record["Version"]) != ("libnvidia-gpucomp-580", OLD)]
        fixture = self.fixture(records)
        original = b"# Existing policy must survive a failed plan.\n"
        fixture.preferences.write_bytes(original)
        result = fixture.plan()
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(fixture.preferences.read_bytes(), original)
        self.assertFalse(fixture.manifest.exists())

    def test_cli_rejects_empty_roots_without_publishing_policy(self) -> None:
        fixture = self.fixture()
        result = fixture.run(str(APT_PYTHON), str(PLANNER), "plan", "--version", OLD,
                             "--preferences", str(fixture.preferences), "--manifest", str(fixture.manifest))
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertFalse(fixture.preferences.exists())
        self.assertFalse(fixture.manifest.exists())

    def test_nested_dependency_predepends_alternatives_cycle_and_virtual_provider(self) -> None:
        records = driver_records()
        for version in (OLD, NEW):
            records.append({
                "Package": "libnvidia-nested-580", "Version": version, "Architecture": "amd64",
                "Source": "nvidia-graphics-drivers-580",
                "Depends": f"libnvidia-compute-580 (= {version})",
            })
            for record in records:
                if (record["Package"], record["Version"]) == ("libnvidia-gpucomp-580", version):
                    record["Depends"] = f"missing-alternative | libnvidia-nested-580 (= {version})"
                if (record["Package"], record["Version"]) == ("libnvidia-cfg1-580", version):
                    record["Pre-Depends"] = f"libnvidia-common-580 (= {version})"
                if (record["Package"], record["Version"]) == ("nvidia-kernel-common-580", version):
                    record["Provides"] += f", synthetic-nvidia-abi (= {version})"
                if (record["Package"], record["Version"]) == ("libnvidia-decode-580", version):
                    record["Depends"] = f"synthetic-nvidia-abi (= {version})"
        fixture = self.fixture(records)
        self.assert_plan(fixture, (*DRIVER_PACKAGES, "libnvidia-nested-580"))

    def test_preinstalled_newer_driver_is_downgraded_as_a_coherent_set(self) -> None:
        fixture = self.fixture()
        fixture.installed(NEW)
        self.assert_plan(fixture)
        # APT may retain a newer installed >= dependency even under candidate
        # pinning. The real installer must request ALL manifest entries exactly.
        simulation = fixture.run("apt-get", "-s", "--allow-downgrades", "install",
                                 *fixture.manifest.read_text().splitlines())
        self.assertEqual(simulation.returncode, 0, simulation.stdout + simulation.stderr)
        for name in DRIVER_PACKAGES:
            self.assertIn(f"Inst {name} [{NEW}] ({OLD} ", simulation.stdout)

    def test_support_package_dependency_is_included_in_driver_closure(self) -> None:
        records = driver_records()
        for version in (OLD, NEW):
            records.append({"Package": "libnvidia-support-driver-580", "Version": version,
                            "Architecture": "amd64", "Source": "nvidia-graphics-drivers-580"})
        records.append({"Package": "libglvnd0", "Version": "1.0", "Architecture": "amd64",
                        "Source": "libglvnd", "Depends": f"libnvidia-support-driver-580 (>= {OLD})"})
        fixture = self.fixture(records)
        self.assert_plan(fixture, (*DRIVER_PACKAGES, "libnvidia-support-driver-580"), ("libglvnd0",))
        self.assertNotIn("libglvnd0=", fixture.manifest.read_text())
        after = fixture.run("apt-get", "-s", "--no-install-recommends", "install",
                            *fixture.manifest.read_text().splitlines(), "libglvnd0")
        self.assertEqual(after.returncode, 0, after.stdout + after.stderr)
        self.assertIn(f"Inst libnvidia-support-driver-580 ({OLD} ", after.stdout)
        self.assertNotIn(NEW, after.stdout)

    def test_driver_source_discovers_binary_outside_nvidia_name_patterns(self) -> None:
        records = driver_records()
        for version in (OLD, NEW):
            records.append({"Package": "libcuda-synthetic1", "Version": version,
                            "Architecture": "amd64", "Source": "nvidia-graphics-drivers-580"})
            for record in records:
                if (record["Package"], record["Version"]) == ("libnvidia-compute-580", version):
                    record["Depends"] += f", libcuda-synthetic1 (>= {version})"
        fixture = self.fixture(records)
        self.assert_plan(fixture, (*DRIVER_PACKAGES, "libcuda-synthetic1"))

    def test_already_correct_noop_plan_still_attests_entire_closure(self) -> None:
        fixture = self.fixture()
        fixture.installed(OLD)
        self.assert_plan(fixture)
        verify = fixture.run(str(APT_PYTHON), str(PLANNER), "verify", "--manifest", str(fixture.manifest))
        self.assertEqual(verify.returncode, 0, verify.stdout + verify.stderr)

    def test_postflight_rejects_missing_or_mismatched_transitive_packages(self) -> None:
        fixture = self.fixture()
        self.assert_plan(fixture)
        for omission in ("nvidia-firmware-580", "libnvidia-decode-580"):
            with self.subTest(missing=omission):
                fixture.installed(OLD, omit=omission)
                verify = fixture.run(str(APT_PYTHON), str(PLANNER), "verify", "--manifest", str(fixture.manifest))
                self.assertNotEqual(verify.returncode, 0, verify.stdout + verify.stderr)
        fixture.installed(NEW)
        verify = fixture.run(str(APT_PYTHON), str(PLANNER), "verify", "--manifest", str(fixture.manifest))
        self.assertNotEqual(verify.returncode, 0, verify.stdout + verify.stderr)

    def test_postflight_rejects_newer_driver_package_outside_manifest(self) -> None:
        fixture = self.fixture()
        self.assert_plan(fixture)
        fixture.installed(OLD)
        outside = {"Package": "libnvidia-unexpected-580", "Version": NEW,
                   "Architecture": "amd64", "Source": "nvidia-graphics-drivers-580"}
        fixture.status.write_text(fixture.status.read_text() + stanza(outside, installed=True))
        verify = fixture.run(str(APT_PYTHON), str(PLANNER), "verify", "--manifest", str(fixture.manifest))
        self.assertNotEqual(verify.returncode, 0, verify.stdout + verify.stderr)

    def test_postflight_rejects_exact_version_that_is_only_half_configured(self) -> None:
        fixture = self.fixture()
        self.assert_plan(fixture)
        fixture.installed(OLD)
        records = fixture.status.read_text().split("\n\n")
        for index, record in enumerate(records):
            if record.startswith("Package: libnvidia-decode-580\n"):
                records[index] = record.replace("Status: install ok installed", "Status: install ok half-configured")
        fixture.status.write_text("\n\n".join(records))
        verify = fixture.run(str(APT_PYTHON), str(PLANNER), "verify", "--manifest", str(fixture.manifest))
        self.assertNotEqual(verify.returncode, 0, verify.stdout + verify.stderr)

    def test_kernel_driver_dependency_fails_closed(self) -> None:
        records = driver_records()
        records.append({"Package": "nvidia-dkms-580", "Version": OLD,
                        "Architecture": "amd64", "Source": "nvidia-graphics-drivers-580"})
        for record in records:
            if (record["Package"], record["Version"]) == ("nvidia-kernel-common-580", OLD):
                record["Depends"] += f", nvidia-dkms-580 (= {OLD})"
        fixture = self.fixture(records)
        result = fixture.plan()
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertFalse(fixture.manifest.exists())

    def test_plan_does_not_remove_unrelated_installed_package(self) -> None:
        records = driver_records()
        unrelated = {"Package": "synthetic-unrelated", "Version": "1.0",
                     "Architecture": "amd64", "Source": "synthetic-unrelated"}
        records.append(unrelated)
        for record in records:
            if (record["Package"], record["Version"]) == ("libnvidia-common-580", OLD):
                record["Conflicts"] = "synthetic-unrelated"
        fixture = self.fixture(records)
        fixture.status.write_text(stanza(unrelated, installed=True))
        result = fixture.plan()
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertFalse(fixture.manifest.exists())


class NvidiaVersionResolverTests(unittest.TestCase):
    def resolve(self, candidates: dict[str, list[str]], requested: str) -> subprocess.CompletedProcess[str]:
        with tempfile.TemporaryDirectory(prefix="axonos-nvidia-resolver-test-") as directory:
            root = Path(directory)
            fake = root / "apt-cache"
            branches = []
            for package, versions in candidates.items():
                rows = "".join(f"{package} | {version} | synthetic\n" for version in versions)
                branches.append(f"{package}) printf '%s' '{rows}' ;;\n")
            fake.write_text("#!/bin/sh\n[ \"$1\" = madison ] || exit 2\ncase \"$2\" in\n" + "".join(branches) + "esac\n")
            fake.chmod(0o755)
            return subprocess.run(["bash", str(RESOLVER)], capture_output=True, text=True,
                                  timeout=10, env={**os.environ, "PATH": f"{root}:{os.environ['PATH']}",
                                                  "NVIDIA_DRIVER_VERSION": "580",
                                                  "NVIDIA_DRIVER_PKG_VERSION": requested})

    def test_repository_suffix_mapping_preserves_host_triplet(self) -> None:
        result = self.resolve({package: [NEW, OLD] for package in ROOTS[:4]}, "580.173.02-0ubuntu0.22.04.1")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), OLD)

    def test_missing_fourth_root_version_is_not_promoted_to_newer_release(self) -> None:
        candidates = {package: [NEW, OLD] for package in ROOTS[:4]}
        candidates[ROOTS[3]] = [NEW]
        result = self.resolve(candidates, "580.173.02-0ubuntu0.22.04.1")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(result.stdout.strip(), "")

    def test_triplet_prefix_collision_is_rejected(self) -> None:
        result = self.resolve({package: ["580.173.020-1ubuntu1", NEW] for package in ROOTS[:4]}, "580.173.02")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(result.stdout.strip(), "")


class NvidiaInstallerIntegrationTests(unittest.TestCase):
    def test_preference_normalizes_and_deduplicates_architecture_qualified_names(self) -> None:
        preference = runpy.run_path(str(PLANNER))["preference"]
        actual = preference(("libnvidia-gl-580:amd64", "libnvidia-gl-580:i386", "libnvidia-gl-580"), OLD)
        self.assertEqual(actual, preference(("libnvidia-gl-580",), OLD))
        self.assertIn("Package: libnvidia-gl-580:any\n", actual)
        self.assertNotIn(":amd64", actual)
        self.assertNotIn(":i386", actual)

    def test_installer_consumes_all_manifest_pins_and_preserves_original_postchecks(self) -> None:
        installer = (REPO / "scripts/install-nvidia-xorg-userspace.sh").read_text()
        self.assertIn('mapfile -t pins < "${manifest}"', installer)
        flattened = installer.replace("\\\n", " ")
        transactions = [line for line in flattened.splitlines()
                        if line.startswith("apt-get ") and " install " in line and "--reinstall" not in line]
        self.assertEqual(len(transactions), 2, transactions)
        for command in transactions:
            self.assertIn('"${pins[@]}"', command)
            self.assertIn("--no-remove", command)
            self.assertIn("--allow-downgrades", command)
        self.assertIn("--simulate", transactions[0])
        self.assertIn("--extra libglvnd0 --extra libglx0 --extra libegl1", installer)
        postcheck = installer[installer.index("for pkg in"):]
        for package in ROOTS[:5]:
            self.assertIn(package.replace("580", "${ver_major}"), postcheck)
        self.assertIn('[ "${inst}" = "${NVIDIA_PKG_RESOLVED}" ]', postcheck)
        self.assertIn('plan-nvidia-userspace.py verify --manifest "${manifest}"', postcheck)

    def test_dockerfile_supplies_native_apt_and_verifies_after_last_package_layer(self) -> None:
        dockerfile = (REPO / "Dockerfile").read_text()
        self.assertIn("software-properties-common", dockerfile)
        self.assertIn("/usr/bin/python3 -c 'import apt, apt_pkg' &&", dockerfile)
        self.assertLess(dockerfile.index("/usr/bin/python3 -c 'import apt, apt_pkg'"),
                        dockerfile.index("    /usr/local/bin/install-nvidia-xorg-userspace.sh"))
        self.assertIn("COPY scripts/plan-nvidia-userspace.py /usr/local/bin/plan-nvidia-userspace.py", dockerfile)
        final_verify = dockerfile.rindex("plan-nvidia-userspace.py verify")
        self.assertGreater(final_verify, dockerfile.rindex("apt-get install"))
        self.assertGreater(final_verify, dockerfile.rindex("pip install"))
        self.assertIn("--manifest /usr/local/share/axonos/nvidia-userspace.txt", dockerfile[final_verify:])


if __name__ == "__main__":
    unittest.main()
