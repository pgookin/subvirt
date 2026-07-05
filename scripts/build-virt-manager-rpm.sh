#!/usr/bin/env bash
set -euo pipefail

TOPDIR=${SUBVIRT_VIRT_MANAGER_RPM_TOPDIR:-$(pwd)/provider-build/virt-manager-rpmbuild}
RPM_BUILD_JOBS=${SUBVIRT_RPM_BUILD_JOBS:-2}

rm -rf "$TOPDIR"
mkdir -p "$TOPDIR"/{BUILD,RPMS,SOURCES,SPECS,SRPMS} dist
if ! command -v dnf >/dev/null 2>&1; then
  echo "dnf is required to build virt-manager RPMs" >&2
  exit 1
fi

dnf download --source --destdir "$TOPDIR" virt-manager
SRPM=$(find "$TOPDIR" -maxdepth 1 -type f -name 'virt-manager-*.src.rpm' | sort | tail -1)
if [[ -z "$SRPM" ]]; then
  echo "failed to download virt-manager source RPM" >&2
  exit 1
fi
rpm -ivh --define "_topdir $TOPDIR" "$SRPM"
SPEC="$TOPDIR/SPECS/virt-manager.spec"
python3 - "$TOPDIR" <<'PYGENPATCH'
from pathlib import Path
import os
import subprocess
import sys
import tarfile
import tempfile

root = Path(sys.argv[1])
workspace = Path.cwd()
sources = root / "SOURCES"
tarballs = sorted(sources.glob("virt-manager-*.tar.*"))
if not tarballs:
    raise SystemExit("could not find virt-manager source tarball")
git_env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}

def git(source_root, *args, **kwargs):
    return subprocess.run(["git", "-C", str(source_root), *args], env=git_env, check=True, **kwargs)

with tempfile.TemporaryDirectory() as tmpdir:
    tmp = Path(tmpdir)
    with tarfile.open(tarballs[-1]) as tar:
        tar.extractall(tmp)
    source_root = next(path for path in tmp.iterdir() if path.is_dir() and path.name.startswith("virt-manager-"))
    for extracted_path in (source_root, *source_root.rglob("*")):
        os.chown(extracted_path, os.getuid(), os.getgid(), follow_symlinks=False)
    git(source_root, "init", "-q")
    git(source_root, "config", "user.name", "SubVirt Build")
    git(source_root, "config", "user.email", "subvirt@example.invalid")
    git(source_root, "add", ".")
    git(source_root, "commit", "-q", "--allow-empty", "-m", "virt-manager base")
    subprocess.run([str(workspace / "scripts" / "patch-virt-manager-truenas.py"), str(source_root)], env=git_env, check=True)
    git(source_root, "add", "virtManager/object/storagepool.py")
    git(source_root, "add", "virtManager/createpool.py")
    git(source_root, "add", "virtinst/storage.py")
    git(source_root, "add", "ui/createpool.ui")
    git(source_root, "commit", "-q", "-m", "Enable TrueNAS storage pool volume creation")
    patch = git(
        source_root,
        "format-patch",
        "--stdout",
        "HEAD~1..HEAD",
        text=True,
        capture_output=True,
    )
    (sources / "subvirt-truenas-volume-creation.patch").write_text(patch.stdout)
PYGENPATCH
export SUBVIRT_ALMA_VIRT_MANAGER_REVISION=$(./scripts/subvirt_versions.py alma-virt-manager-revision)
python3 - "$SPEC" <<'PY'
from pathlib import Path
import os
import re
import sys

spec = Path(sys.argv[1])
text = spec.read_text()
if "subvirt-truenas-volume-creation.patch" not in text:
    lines = text.splitlines()
    insert_at = None
    for idx, line in enumerate(lines):
        if re.match(r"^(Patch|Source)\d*:", line):
            insert_at = idx + 1
    if insert_at is None:
        raise SystemExit("could not find Source/Patch insertion point")
    lines.insert(insert_at, "Patch9999: subvirt-truenas-volume-creation.patch")
    text = "\n".join(lines) + "\n"

release_re = re.compile(r"^(Release:\s*)(.+)$", re.M)
match = release_re.search(text)
if not match:
    raise SystemExit("could not find Release tag")
release = match.group(2)
revision = os.environ["SUBVIRT_ALMA_VIRT_MANAGER_REVISION"]
suffix = f".truenas{revision}"
if suffix not in release:
    if "%{?dist}" in release:
        release = release.replace("%{?dist}", f"{suffix}%{{?dist}}", 1)
    else:
        release = release + suffix
    text = text[:match.start(2)] + release + text[match.end(2):]

spec.write_text(text)
PY
dnf builddep -y "$SPEC"
rpmbuild --define "_topdir $TOPDIR" --define "_smp_mflags -j$RPM_BUILD_JOBS" --define "_smp_build_ncpus $RPM_BUILD_JOBS" -bp "$SPEC"
BUILD_SRC=$(find "$TOPDIR/BUILD" -maxdepth 2 -type d -name 'virt-manager-*' ! -name '*-SPECPARTS' | sort | tail -1)
if [[ -z "$BUILD_SRC" ]]; then
  echo "failed to locate prepared virt-manager build tree" >&2
  exit 1
fi
./scripts/check-virt-manager-truenas.py --static --source-root "$BUILD_SRC"
rpmbuild --define "_topdir $TOPDIR" --define "_smp_mflags -j$RPM_BUILD_JOBS" --define "_smp_build_ncpus $RPM_BUILD_JOBS" -ba "$SPEC"
find "$TOPDIR/RPMS" "$TOPDIR/SRPMS" -type f -name '*.rpm' -exec cp -a {} dist/ \;
