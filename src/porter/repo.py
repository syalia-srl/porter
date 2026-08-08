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

**Signing is optional and its mode is decided at BUILD time.** A signed tree
carries `Release.gpg` and the exported public key beside the index, and its
`install.sh` names the keyring unconditionally. An unsigned tree keeps
`[trusted=yes]`, so a dev loop needs no key at all.

The two are separate templates on purpose. A single script that looked for the
keyring and fell back to `[trusted=yes]` when it was absent would turn a stick
whose key file did not copy into an install that verifies nothing and exits 0 --
porter's characteristic bug, in the one place whose entire job is verification.

What the signature buys, precisely: apt verifies a package's SHA256 against
`Packages` whether or not the repo is signed, so a flipped byte in a `.deb` is
caught either way. What is caught only when signed is a rewritten *index* --
without a signature, anyone who can write to the stick can point `Packages` at
their own package, with a matching hash, and apt installs it without complaint.
Both are measured in `tests/test_signing.py`.
"""
from __future__ import annotations

import gzip
import hashlib
import os
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

# The exported public key, beside the index it verifies. Named rather than
# derived from the app, because `write_index` signs a repo directory and knows
# nothing about which app's install.sh will point at it -- a multi-component
# USB is one repo and several packages.
KEYRING_NAME = "porter-archive-key.gpg"

# The two trust blocks. Which one is baked in is decided by `usb_tree`'s
# `sign_key`, at build time, and there is deliberately no run-time branch
# between them: see the module docstring.
TRUST_UNSIGNED = """LIST=/etc/apt/sources.list.d/{app}.list
# Removed on the way out, whatever the outcome. Left behind, it names a path on
# a stick that gets unplugged, and every later `apt-get update` on this client
# fails on a repository that no longer exists -- porter breaking apt for good on
# a machine nobody can ssh into.
trap 'rm -f "$LIST"' EXIT
echo "deb [trusted=yes] file:${{HERE}}/repo ./" > "$LIST"
"""

TRUST_SIGNED = """LIST=/etc/apt/sources.list.d/{app}.list
KEYRING=/etc/apt/keyrings/{app}.gpg
# Both are residue and both go. The keyring names the key of a stick this client
# will never see again; the list names a path that stops existing the moment the
# stick is unplugged, and every later `apt-get update` would fail on it.
trap 'rm -f "$LIST" "$KEYRING"' EXIT
mkdir -p /etc/apt/keyrings
# NO FALLBACK. A keyring that failed to copy must stop the install here, under
# `set -e`, rather than degrade this source to an unverified one: a bundle that
# verifies nothing while exiting 0 is the exact failure a signature exists to
# prevent, and it is invisible from the client.
#
# The words the unsigned template uses for that are deliberately NOT written
# anywhere in this one. A reader -- or a test -- grepping the emitted script for
# them must get an answer about the `deb` line below and not about a comment.
install -m 0644 "${{HERE}}/repo/{keyring_name}" "$KEYRING"
echo "deb [signed-by=$KEYRING] file:${{HERE}}/repo ./" > "$LIST"
"""

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

{trust_block}
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


def _gpg(args: list[str], *, home: Path | None,
         stdin: bytes | None = None) -> subprocess.CompletedProcess:
    """`gpg --batch --yes`, with an optional throwaway keyring home.

    `--batch` because everything porter runs is unattended; without it gpg will
    ask for a passphrase on a terminal that is not there, which on a build
    server is a hang and not an error.
    """
    env = dict(os.environ)
    if home is not None:
        env["GNUPGHOME"] = str(home)
    return subprocess.run(["gpg", "--batch", "--yes", *args], input=stdin,
                          capture_output=True, env=env)


def sign_release(repo_dir: Path, key: str, *, gpg_home: Path | None = None) -> Path:
    """Detach-sign `Release`, export the public key beside it. -> Release.gpg

    The client verifies with the key on the stick, so that exact pair --
    `Release.gpg` against the *exported* key, not against the signer's own
    keyring -- is what has to be proved here, and it is proved by doing it: the
    key is imported into a scratch GNUPGHOME and `gpgv` is run the way apt runs
    it.

    That check needs a control of its own, and gets one immediately below it: a
    byte of `Release` is flipped in a copy and `gpgv` must reject it. Without
    that, a `gpgv` that could not fail -- a missing binary read as a pass, a
    keyring argument silently ignored -- would make the verification above a
    line that always says yes.
    """
    repo_dir = Path(repo_dir)
    release = repo_dir / "Release"
    if not release.exists():
        raise ValueError(f"{release} does not exist: sign the index, not the "
                         "absence of one")

    # `gpg --export <pattern>` with a pattern that matches nothing exits 0 and
    # writes NOTHING. Exported blindly, that is a keyring file of zero bytes on
    # the stick and an install that dies at the client with `no valid OpenPGP
    # data found` -- so the emptiness is caught here, where there is still
    # somebody to tell.
    exported = _gpg(["--export", key], home=gpg_home)
    if exported.returncode != 0 or len(exported.stdout) < 100:
        raise RuntimeError(
            f"gpg --export {key!r} produced {len(exported.stdout)} bytes "
            f"(rc={exported.returncode}): there is no such key in this keyring, "
            f"or it carries no public material. {exported.stderr.decode(errors='replace').strip()}")
    keyring = repo_dir / KEYRING_NAME
    keyring.write_bytes(exported.stdout)

    signature = repo_dir / "Release.gpg"
    signed = _gpg(["--detach-sign", "--armor", "--local-user", key,
                   "--output", str(signature), str(release)], home=gpg_home)
    if signed.returncode != 0:
        raise RuntimeError(
            f"gpg --detach-sign with {key!r} rc={signed.returncode}: "
            f"{signed.stderr.decode(errors='replace').strip()}")
    if b"BEGIN PGP SIGNATURE" not in signature.read_bytes():
        raise RuntimeError(
            f"{signature} is not an armoured signature: apt reads this file "
            f"literally and would report `Splitting up ... failed`")

    _verify_with_the_shipped_key(release, signature, keyring)
    return signature


def _verify_with_the_shipped_key(release: Path, signature: Path,
                                 keyring: Path) -> None:
    """Do what the client will do, then prove the check can fail.

    `gpgv` and not `gpg --verify`: gpgv takes a plain keyring file and has no
    web of trust, which is exactly apt's model. It also means the verification
    cannot be satisfied by a key that happens to sit in the build host's own
    keyring -- the failure this function exists to catch.
    """
    import tempfile

    with tempfile.TemporaryDirectory() as scratch:
        ring = Path(scratch) / "ring.gpg"
        ring.write_bytes(keyring.read_bytes())
        ok = subprocess.run(["gpgv", "--keyring", str(ring), str(signature),
                             str(release)], capture_output=True)
        if ok.returncode != 0:
            raise RuntimeError(
                f"the exported key does not verify the signature just written "
                f"(gpgv rc={ok.returncode}): the client would refuse this repo. "
                f"{ok.stderr.decode(errors='replace').strip()}")

        # The control. A flipped byte must be rejected; if it is not, the pass
        # above was gpgv failing open and the signature on this stick means
        # nothing. Done on a copy -- the real Release is never touched.
        tampered = Path(scratch) / "Release"
        body = bytearray(release.read_bytes())
        body[0] ^= 0x20
        tampered.write_bytes(bytes(body))
        control = subprocess.run(["gpgv", "--keyring", str(ring), str(signature),
                                  str(tampered)], capture_output=True)
        if control.returncode == 0:
            raise RuntimeError(
                "gpgv accepted a Release with a flipped byte: this verification "
                "cannot fail, so the one above proves nothing about the "
                "signature that is about to ship")


def write_index(repo_dir: Path, sign_key: str | None = None, *,
                gpg_home: Path | None = None) -> Path:
    """Write `Packages`, `Packages.gz` and `Release` for a flat repo. -> Packages.

    With `sign_key`, also `Release.gpg` and the exported public key. Without it
    the repo is unsigned and `install.sh` keeps `[trusted=yes]` -- signing is
    optional by design, so a dev loop needs no key.
    """
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

    if sign_key is not None:
        sign_release(repo_dir, sign_key, gpg_home=gpg_home)
    else:
        # A signature left over from a previous signed run does not verify the
        # Release just written, and a repo directory that carries one is a
        # directory that LOOKS signed to anybody reading it. Unsigned means
        # unsigned, visibly.
        for residue in ("Release.gpg", "InRelease", KEYRING_NAME):
            (repo_dir / residue).unlink(missing_ok=True)
    return packages


def usb_tree(debs: list[Path], out: Path, app: str, readme: str,
             sign_key: str | None = None, *, gpg_home: Path | None = None) -> Path:
    """Lay out `out/` as a USB tree: repo/, install.sh, README.txt. -> out.

    `sign_key` is a gpg key id, fingerprint or uid in the build host's keyring
    (or in `gpg_home`). Given one, the index is signed and the emitted
    `install.sh` points apt at the shipped public key with `signed-by=`; without
    one it keeps `[trusted=yes]`. Nothing else about the tree changes.
    """
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
    body = write_index(repo, sign_key, gpg_home=gpg_home).read_text()

    # install.sh names exactly one package. A tree built for `app` out of
    # somebody else's .debs copies fine, boots fine, and tells the client
    # `Unable to locate package <app>` -- with no network to fix it from.
    if not re.search(rf"^Package: {re.escape(app)}$", body, re.M):
        raise ValueError(f"{app!r} is not in the index of {repo}: install.sh "
                         "would install a package this tree does not carry")

    # Baked in, not detected at run time. See the module docstring: a script
    # that could fall back to [trusted=yes] turns a lost key file into a silent
    # downgrade of the only guarantee signing provides.
    trust = (TRUST_SIGNED if sign_key is not None else TRUST_UNSIGNED).format(
        app=app, keyring_name=KEYRING_NAME)
    script = out / "install.sh"
    script.write_text(INSTALL_SH.format(app=app, trust_block=trust))
    script.chmod(0o755)
    # install.sh is generated shell and it is the only thing the client runs.
    # deb.py already paid for this one: a script that does not parse is written
    # at rc=0 and dies on the machine with no network to fix it from.
    proc = subprocess.run(["bash", "-n", str(script)], capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(f"generated install.sh does not parse: {proc.stderr.strip()}")

    (out / "README.txt").write_text(readme)
    return out
