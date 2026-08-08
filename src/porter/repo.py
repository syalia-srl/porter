"""Flat apt repo on a USB tree, and the one command that installs *and* updates.

The index is emitted from `dpkg-deb --field` rather than dpkg-scanpackages:
that tool lives in dpkg-dev, which is absent on demos (the build host) and on
the Debian base image. The format is six lines to write, so porter depends on
nothing extra.

apt does NOT copy packages from a `file:` repo into /var/cache/apt/archives --
measured 2026-08-07: 1 MB of cache for a 2 GB package; it installs in place.
Peak transient disk for an upgrade is therefore 2x payload, the same as a plain
`dpkg -i`, and not the 3x a caching repo would cost.

Why a repo at all, when `dpkg -i` would install these files: dpkg installs, apt
*resolves*. A multi-component project is several .debs with `Depends:` between
them, and the same `install.sh` has to work whether the client has the previous
version, an older one, or nothing at all. That is apt's job, and a flat
`file:` repo is the smallest thing that gives us apt without a network.
"""
from __future__ import annotations

import gzip
import hashlib
import re
import shutil
import subprocess
from pathlib import Path

# Debian policy §5.6.1, minus the length floor's edge case. `app` is
# interpolated into install.sh and into a filename under
# /etc/apt/sources.list.d/, so a name carrying a space, a quote or a slash
# builds a USB tree at rc=0 whose install.sh either does not parse or points
# apt at a file nobody wrote.
PACKAGE_NAME = re.compile(r"^[a-z0-9][a-z0-9+.-]+$")

INSTALL_SH = """#!/usr/bin/env bash
# Installs OR updates {app}. Same command either way -- apt knows which.
#
# Autonomous by construction: no prompt is reachable from here. There is no
# interactive fallback on an airgapped client, so a script that can block is a
# script that hangs invisibly at 3am.
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"

# Lift to root BEFORE parsing flags, so "$@" is still the caller's argv.
# Parsing first consumes every argument, and the re-exec then silently drops
# --version: the client asks for one version and installs whichever is newest.
#
# sudo -n or an explicit refusal, never a password prompt. Blocking on a
# password is indistinguishable from a hang in an unattended run.
if [ "$(id -u)" -ne 0 ]; then
  if sudo -n true 2>/dev/null; then exec sudo -n -E bash "$0" "$@"; fi
  echo "ERROR: not root, and passwordless sudo is unavailable." >&2
  echo "       Re-run as root:  sudo bash $0 $*" >&2
  exit 1
fi

VERSION=""
while [ $# -gt 0 ]; do
  case "$1" in
    --yes|-y)   shift ;;                      # accepted and implied; never prompts
    --version)  [ $# -ge 2 ] || {{ echo "--version needs a value" >&2; exit 2; }}
                VERSION="=$2"; shift 2 ;;
    *) echo "unknown flag: $1" >&2; exit 2 ;;
  esac
done

export DEBIAN_FRONTEND=noninteractive
export NEEDRESTART_MODE=a      # auto-restart; never the whiptail service list.
                               # NEEDRESTART_SUSPEND is NOT a real variable --
                               # absent from needrestart 3.6's code (measured).
export UCF_FORCE_CONFOLD=1

LIST=/etc/apt/sources.list.d/{app}.list
# Removed on the way out, whatever the outcome. Left behind, it names a path on
# a stick that gets unplugged, and every later `apt-get update` on this client
# fails on a repository that no longer exists -- porter breaking apt for good on
# a machine nobody can ssh into.
trap 'rm -f "$LIST"' EXIT
echo "deb [trusted=yes] file:${{HERE}}/repo ./" > "$LIST"

# Scoped update: a client with a stale or unreachable source in sources.list.d/
# must not be able to break an offline install. Verified with a broken source
# present, against an unscoped `apt-get update` that fails on the same client.
apt-get update -qq \\
  -o Dir::Etc::sourcelist="sources.list.d/{app}.list" \\
  -o Dir::Etc::sourceparts="-" \\
  -o APT::Get::List-Cleanup="0"
apt-get install -y -qq --allow-downgrades \\
  -o Dpkg::Options::=--force-confold \\
  -o Dpkg::Options::=--force-confdef \\
  -o Dpkg::Use-Pty=0 \\
  "{app}${{VERSION}}"
echo "INSTALL_OK {app}=$(dpkg-query -W -f='${{Version}}' {app})"
"""


def _checksum_block(key: str, digest, paths: list[Path]) -> str:
    return f"{key}:\n" + "".join(
        f" {digest(p.read_bytes()).hexdigest()} {p.stat().st_size} {p.name}\n"
        for p in paths)


def write_index(repo_dir: Path) -> Path:
    """Write `Packages`, `Packages.gz` and `Release` for a flat repo. -> Packages."""
    repo_dir = Path(repo_dir)
    debs = sorted(repo_dir.glob("*.deb"))
    # Refused BEFORE anything is written. An empty index is a USB stick that
    # copies fine and fails at the client, and publishing one and *then*
    # raising leaves exactly that stick behind for a caller that logs the error
    # and carries on.
    if not debs:
        raise ValueError(f"no .deb found in {repo_dir}: refusing to publish an empty index")

    stanzas = []
    for deb in debs:
        proc = subprocess.run(["dpkg-deb", "--field", str(deb)],
                              capture_output=True, text=True)
        # Read the rc directly. Unchecked, a truncated or corrupt package
        # contributes an empty stanza and the index still writes at rc=0.
        if proc.returncode != 0:
            raise RuntimeError(
                f"dpkg-deb --field {deb.name} rc={proc.returncode}: {proc.stderr.strip()}")
        digest = hashlib.sha256(deb.read_bytes()).hexdigest()
        stanzas.append(
            proc.stdout.rstrip("\n")
            + f"\nFilename: ./{deb.name}\nSize: {deb.stat().st_size}\nSHA256: {digest}\n")

    body = "\n".join(stanzas)
    packages = repo_dir / "Packages"
    packages.write_text(body)
    gz = repo_dir / "Packages.gz"
    gz.write_bytes(gzip.compress(body.encode()))
    # apt fetches Release for a flat repo and, from 1.6 on, refuses one that
    # carries no checksums for the index it just read. Written last, because it
    # hashes the two files above.
    (repo_dir / "Release").write_text(
        "Origin: porter\nLabel: porter\nSuite: stable\n"
        + _checksum_block("MD5Sum", hashlib.md5, [packages, gz])
        + _checksum_block("SHA256", hashlib.sha256, [packages, gz]))
    return packages


def usb_tree(debs: list[Path], out: Path, app: str, readme: str) -> Path:
    """Lay out `out/` as a USB tree: repo/, install.sh, README.txt. -> out."""
    # Before `out` exists: a refusal that has already written half a stick is
    # one an operator can still copy by mistake.
    if not PACKAGE_NAME.match(app):
        raise ValueError(f"{app!r} is not a Debian package name: install.sh "
                         "interpolates it into a command and a sources filename")

    out = Path(out)
    repo = out / "repo"
    repo.mkdir(parents=True, exist_ok=True)
    for d in debs:
        shutil.copy2(d, repo / Path(d).name)
    body = write_index(repo).read_text()

    # install.sh names exactly one package. A tree built for `app` out of
    # somebody else's .debs copies fine, boots fine, and tells the client
    # `Unable to locate package <app>` -- with no network to fix it from.
    if not re.search(rf"^Package: {re.escape(app)}$", body, re.M):
        raise ValueError(f"{app!r} is not in the index of {repo}: install.sh "
                         "would install a package this tree does not carry")

    script = out / "install.sh"
    script.write_text(INSTALL_SH.format(app=app))
    script.chmod(0o755)
    # install.sh is generated shell and it is the only thing the client runs.
    # deb.py already paid for this one: a script that does not parse is written
    # at rc=0 and dies on the machine with no network to fix it from.
    proc = subprocess.run(["bash", "-n", str(script)], capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(f"generated install.sh does not parse: {proc.stderr.strip()}")

    (out / "README.txt").write_text(readme)
    return out
