"""Prove a bundle before it ships.

`gate()` takes a USB tree built by `porter.repo.usb_tree`, installs the older
version inside a container that has no network and no system Python, lets a
client seed its own state, upgrades with **the same command**, and reports what
survived.

**Every assertion here carries a positive control or a magnitude check.** That
is not style. During the design five probes reported passes that were false: a
dangling symlink resolving to the build host's own interpreter; a network probe
using bash-only `/dev/tcp` under dash; an interface count where the binary was
absent; a memory-limit check satisfied by *command not found*; and a truncated
12 KB package reported as built. Each was caught only because something
downstream contradicted it. The airgap failures that matter are exactly the ones
that look like passes on the build host, so before asserting isolation this
module proves the probe *detects* the thing when isolation is off, and before
trusting an artefact it asserts the artefact's magnitude.

Never pipe a gate: `cmd | tail` hands the caller `tail`'s exit code. Every
verdict below is a marker line printed by the container and judged in Python;
the pipes that remain extract *data* (`du`, `grep -c`) and their exit codes are
read by nothing.

**What each gate proves, and what it does not.** `gate()` -- the docker one --
proves payload, upgrade, state survival and that the shipped `ExecStart=`
answers HTTP from the shipped `WorkingDirectory=`. It does **not** prove that
systemd starts the unit: there is no PID 1 systemd in a container, so it runs
the command line itself, as root, with no namespace and no supervision.

`nspawn_gate()` is the other half and it is where systemd finally does it.
`User=`, `StateDirectory=`, `ProtectSystem=strict` and `Restart=on-failure`
were, until it existed, assertions about a *file* plus `systemd-analyze verify`
-- and `verify` reports keys it does not recognise and **never keys that are
missing**, so deleting `ProtectSystem=strict` leaves it perfectly clean
(measured 2026-08-08). A directive nothing enforces reads exactly like a
directive that works.

Read them as a pair. The docker gate is fast and covers the upgrade path; the
nspawn gate is slower, needs root, and is the only one that can say the service
came up.
"""
from __future__ import annotations

import base64
import functools
import hashlib
import os
import re
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from porter.repo import PACKAGE_NAME

# `health_url` is interpolated into a shell command inside the gate's own
# container. A URL carrying `;` or `&&` would not merely be untidy -- it could
# make the probe *report success* without anything having answered, which is the
# precise failure class this module exists to detect. Loopback only, because
# `--network none` means nothing else is reachable and a URL naming a host is a
# gate that silently tests the build machine.
HEALTH_URL = re.compile(r"^https?://127\.0\.0\.1:\d{1,5}(/[A-Za-z0-9._~/-]*)?$")
# Same reasoning for a seeded path. Absolute, no quoting metacharacters, no
# `..` -- a seed that escapes /var/lib is a gate writing outside the state
# directory it claims to be testing.
SEED_PATH = re.compile(r"^(/[A-Za-z0-9._-]+)+$")


@dataclass
class GateResult:
    """`ok` is the verdict; `failures` says which assertions produced it.

    `log` is the container's whole transcript. It is carried rather than
    summarised because every failure here is a statement about a machine the
    caller cannot see, and a bare "service did not answer" with no traceback is
    the kind of verdict that gets re-run rather than read.
    """

    ok: bool = True
    failures: list[str] = field(default_factory=list)
    log: str = ""

    def check(self, condition: bool, message: str) -> None:
        if not condition:
            self.ok = False
            self.failures.append(message)


def _dpkg_cmp(a: str, b: str) -> int:
    """Order two Debian versions the way dpkg does, never the way sort does.

    `1.9` and `1.10`: lexically the first is larger, and to dpkg it is smaller.
    `examples/stateful-service` is numbered 1.9 -> 1.10 -> 1.11 for exactly this
    reason. A gate that picked its v_prev lexically would install the *newer*
    package, "upgrade" to the older one, and find every seeded file intact --
    reporting a pass for an upgrade that never happened.
    """
    if a == b:
        return 0
    return -1 if subprocess.run(
        ["dpkg", "--compare-versions", a, "lt", b]).returncode == 0 else 1


def versions_in(usb: Path, app: str) -> list[str]:
    """Every version of `app` in the tree's flat index, oldest first.

    Stanzas are filtered by `Package:`. A multi-component USB carries several
    packages, and taking every `Version:` line in the file would mix a
    component's version into another's upgrade path.
    """
    body = (Path(usb) / "repo/Packages").read_text()
    found = []
    for stanza in body.split("\n\n"):
        fields = dict(
            line.split(": ", 1) for line in stanza.splitlines() if ": " in line)
        if fields.get("Package") == app and "Version" in fields:
            found.append(fields["Version"])
    return sorted(set(found), key=functools.cmp_to_key(_dpkg_cmp))


def _run(image: str, usb: Path, script: str,
         timeout: int) -> subprocess.CompletedProcess:
    """One container: no network, the USB tree mounted read-only.

    `--network none` is the airgap. Loopback still exists inside the namespace,
    which is what lets the service be probed at all. The rc is returned, never
    piped -- the caller reads it directly.
    """
    return subprocess.run(
        ["docker", "run", "--rm", "--network", "none",
         "-v", f"{usb}:/media/usb:ro", image, "bash", "-c", script],
        capture_output=True, text=True, timeout=timeout)


def _marker(out: str, key: str) -> str | None:
    """First `KEY=value` line, or None. Diagnostics are printed with a `| `
    prefix precisely so that a line inside an install log can never be read as
    a marker -- a package whose output happened to contain `HEALTH_RC=0` would
    otherwise hand itself a pass."""
    prefix = key + "="
    for line in out.splitlines():
        if line.startswith(prefix):
            return line[len(prefix):].strip()
    return None


def _int(out: str, key: str) -> int:
    value = (_marker(out, key) or "").strip()
    return int(value) if value.isdigit() else -1


def _script(app: str, old: str, health_url: str, seed: dict[str, str]) -> str:
    """The whole of what runs on the client, as one bash program.

    Deliberately **not** `set -e`. A gate that aborts on its first surprise
    reports one failure and leaves every other question unanswered, so the
    operator learns one fact per container run. Every step emits a marker
    instead and Python judges the set; a step that dies simply leaves its marker
    missing, which is a failure by construction.
    """
    seed_items = list(seed.items())
    seed_block = "".join(
        f'mkdir -p "$(dirname \'{path}\')"\n'
        f"base64 -d <<< '{base64.b64encode(value.encode()).decode()}' > '{path}'\n"
        f'echo "SEED_BEFORE_{i}=$(sha256sum \'{path}\')"\n'
        for i, (path, value) in enumerate(seed_items))
    verify_block = "".join(
        f'echo "SEED_AFTER_{i}=$(sha256sum \'{path}\' 2>/dev/null)"\n'
        for i, (path, _) in enumerate(seed_items))

    return f"""
exec 2>&1
APP={app}
USB=/media/usb

# --- the airgap, and the control that the probe can see a source at all ------
#
# Removing every network apt source is not hygiene: measured on this project, a
# working Debian mirror will satisfy the install and the gate then passes with
# the local repo completely broken. The count BEFORE the wipe is the positive
# control -- the base image ships sources, so a probe reporting 0 both times is
# blind, not clean. A cached index in /var/lib/apt/lists would rescue a broken
# repo the same way, so that goes too.
echo "NETSRC_BEFORE=$(cat /etc/apt/sources.list /etc/apt/sources.list.d/* 2>/dev/null | grep -cE 'https?://')"
rm -f /etc/apt/sources.list /etc/apt/sources.list.d/*.list /etc/apt/sources.list.d/*.sources
rm -rf /var/lib/apt/lists/*
echo "NETSRC_AFTER=$(cat /etc/apt/sources.list /etc/apt/sources.list.d/* 2>/dev/null | grep -cE 'https?://')"

# Read the kernel's own list rather than counting `ip link` output: the burn
# here was an interface count taken on a host where the binary was absent, which
# reported zero interfaces and passed. A directory listing cannot be satisfied
# by a missing binary, and requiring it to be exactly `lo` is self-controlling:
# a blind read yields the empty string, which is not `lo` either.
echo "IFACES=$(ls /sys/class/net 2>/dev/null | tr '\\n' ' ')"

# The resolver probe and its control, in that order. `localhost` comes out of
# /etc/hosts and must resolve -- if it does not, `getent` is broken and the
# verdict below means nothing. `deb.debian.org` needs a nameserver, which is
# what --network none removes.
getent hosts localhost >/dev/null 2>&1 && echo "RESOLVER=ok" || echo "RESOLVER=blind"
getent hosts deb.debian.org >/dev/null 2>&1 && echo "DNS=reachable" || echo "DNS=blocked"

# --- the unattended harness, and its two controls ----------------------------
#
# The install below is bounded by `timeout`, and a hang is the failure that
# matters most on an airgapped client: an installer that blocks at 3am is
# indistinguishable from one still working. So the timeout has to be shown to
# fire. rc=124 is `timeout` doing its job.
timeout 1 sleep 5; echo "TIMEOUTCTL=$?"
command -v setsid >/dev/null 2>&1 && echo "SETSID=ok" || echo "SETSID=absent"
# ...and stdin really is closed under this harness, so a `read` cannot succeed.
# Without this, "the install reached no prompt" would be satisfied by a harness
# incapable of noticing one.
setsid -w bash -c 'read -r _' < /dev/null 2>/dev/null && echo "TTYCTL=blind" || echo "TTYCTL=ok"

# --- install v_prev ----------------------------------------------------------
#
# No controlling terminal (setsid), stdin closed, bounded. `setsid -w` and not
# a bare `setsid`: without --wait it forks and returns at once, so the rc read
# below would be setsid's and not the installer's.
setsid -w timeout 600 bash "$USB/install.sh" --version {old} < /dev/null > /tmp/inst1.log 2>&1
echo "INSTALL_RC=$?"
echo "INSTALLED=$(dpkg-query -W -f='${{Version}}' "$APP" 2>/dev/null)"
grep -qiE 'what would you like|whiptail|EOF on stdin|Which services|\\[Y/n\\]' /tmp/inst1.log \
  && echo "PROMPTED=yes" || echo "PROMPTED=no"

# --- the artefact names its own paths ----------------------------------------
#
# WorkingDirectory= and ExecStart= are read out of the INSTALLED unit, never
# written here. A hand-copied command line is how a gate comes to supply the
# very thing the directive under test exists to supply, and porter hardcodes no
# interpreter name or version (rule 10) -- so the interpreter is simply the
# first word of the shipped ExecStart.
UNIT=/usr/lib/systemd/system/$APP.service
WD=$(sed -n 's/^WorkingDirectory=//p' "$UNIT" 2>/dev/null)
ESLINE=$(sed -n 's/^ExecStart=//p' "$UNIT" 2>/dev/null)
echo "WD=$WD"
echo "ES=$ESLINE"
set -- $ESLINE
echo "PY=$1"
PY=$1

# --- magnitude, and the interpreter's own account of itself ------------------
#
# "The file is there" and "the file is real" are different questions. A stubbed
# interpreter once produced a 30,912-byte package where every path assertion
# still passed, so the tree is measured. And the interpreter is asked where it
# lives: a dangling symlink resolving to the build host's own python answers
# every other probe correctly and reports an executable outside the payload.
echo "PAYLOAD_PREV=$(du -sk "$WD" 2>/dev/null | cut -f1)"
"$PY" -c 'import sys; print("PYEXE=" + sys.executable)' 2>/dev/null

# --- the client seeds its own state ------------------------------------------
{seed_block}
# --- upgrade, with the SAME command and no flags -----------------------------
setsid -w timeout 600 bash "$USB/install.sh" < /dev/null > /tmp/inst2.log 2>&1
echo "UPGRADE_RC=$?"
echo "UPGRADED=$(dpkg-query -W -f='${{Version}}' "$APP" 2>/dev/null)"
echo "PAYLOAD_NEXT=$(du -sk "$WD" 2>/dev/null | cut -f1)"
{verify_block}
# --- the service answers -----------------------------------------------------
#
# The negative control comes first and is the whole reason the positive one
# means anything: on the SAME url, with nothing started, curl must fail. The
# pair discriminates -- a curl that always succeeded fails here, a curl that
# always failed fails below -- and it shares no machinery with the subject, so
# neither half can be satisfied by the other's failure mode.
curl -fsS -o /dev/null --max-time 3 '{health_url}'; echo "PREHEALTH_RC=$?"

set -a
. "/etc/$APP/defaults" 2>/dev/null
[ -f "/etc/$APP/env" ] && . "/etc/$APP/env"
set +a
( cd "$WD" && eval "$ESLINE" ) > /tmp/svc.log 2>&1 &
i=0
while [ $i -lt 30 ]; do
  curl -fsS -o /dev/null --max-time 2 '{health_url}' && break
  i=$((i + 1)); sleep 1
done
curl -fsS -o /dev/null --max-time 3 '{health_url}'; echo "HEALTH_RC=$?"

# Diagnostics, every line prefixed so that nothing in a package's own output can
# be mistaken for one of the markers above.
echo "--- install v_prev"; sed 's/^/| /' /tmp/inst1.log 2>/dev/null
echo "--- upgrade";        sed 's/^/| /' /tmp/inst2.log 2>/dev/null
echo "--- service";        sed 's/^/| /' /tmp/svc.log  2>/dev/null
echo "DONE=yes"
"""


def gate(usb: Path, app: str, image: str, health_url: str,
         seed: dict[str, str], *, min_payload_kb: int = 1024,
         timeout: int = 1800) -> GateResult:
    """Install v_prev, seed client state, upgrade, and report what held.

    `min_payload_kb` is the magnitude floor for `/usr/lib/<app>`. The default
    is a megabyte: any package carrying a vendored interpreter is ~90 MB, and
    the stub that started this rule was 30 KB.
    """
    usb = Path(usb)
    # Refusals, before a container is started. Each of these is a value
    # interpolated into shell that runs the gate's own assertions, so a bad one
    # does not produce a wrong answer -- it produces an answer to a different
    # question, which is worse.
    if not PACKAGE_NAME.match(app):
        raise ValueError(f"{app!r} is not a Debian package name")
    if not HEALTH_URL.match(health_url):
        raise ValueError(
            f"{health_url!r} is not a loopback http(s) URL. The gate runs this "
            "inside a shell, so a URL carrying shell metacharacters could make "
            "the health probe report success with nothing listening")
    for path in seed:
        if not SEED_PATH.match(path) or ".." in path.split("/"):
            raise ValueError(f"{path!r} is not an absolute, quoting-safe path")

    r = GateResult()
    versions = versions_in(usb, app)
    if len(versions) < 2:
        r.check(False, f"need >=2 versions of {app} to test an upgrade, "
                       f"the index carries {versions}")
        return r
    old, new = versions[0], versions[-1]

    try:
        proc = _run(image, usb, _script(app, old, health_url, seed), timeout)
    except subprocess.TimeoutExpired as exc:
        r.check(False, f"the gate container did not finish within {timeout}s")
        r.log = (exc.stdout or b"").decode(errors="replace") if isinstance(
            exc.stdout, bytes) else (exc.stdout or "")
        return r
    out = proc.stdout + proc.stderr
    r.log = out

    # The transcript reached the end. Without this, every marker check below
    # would be reading a truncated log, and "the marker is absent" would mean
    # "the container died early" rather than "the assertion failed".
    r.check("DONE=yes" in out, "the gate script did not run to completion")

    # --- the harness itself, before anything it measures ---------------------
    r.check(_int(out, "NETSRC_BEFORE") >= 1,
            "the apt-source probe found no network source even before the wipe, "
            "so it is blind and 'no network source remained' proves nothing")
    r.check(_int(out, "NETSRC_AFTER") == 0,
            "a network apt source survived into the install: a working mirror "
            "can satisfy it and the local repo would never be exercised")
    r.check(_marker(out, "IFACES") == "lo",
            f"the container is not isolated: interfaces are "
            f"{_marker(out, 'IFACES')!r}, expected exactly 'lo'")
    r.check(_marker(out, "RESOLVER") == "ok",
            "getent could not resolve localhost, so the DNS verdict below is "
            "the probe failing rather than the network being absent")
    r.check(_marker(out, "DNS") == "blocked",
            "DNS resolved outside the container: this bundle was not proved "
            "offline")
    r.check(_marker(out, "TIMEOUTCTL") == "124",
            "`timeout` did not fire on a command that overruns it, so the "
            "bounded install below could hang and still report a pass")
    r.check(_marker(out, "SETSID") == "ok",
            "setsid is absent, so the install ran with whatever terminal the "
            "harness had -- the unattended claim is untested")
    r.check(_marker(out, "TTYCTL") == "ok",
            "an interactive `read` succeeded under this harness, so "
            "'the install reached no prompt' would prove nothing")

    # --- v_prev --------------------------------------------------------------
    r.check(_marker(out, "INSTALL_RC") == "0",
            f"install of {old} exited {_marker(out, 'INSTALL_RC')} "
            f"(124 means it hung and was killed)")
    r.check(_marker(out, "INSTALLED") == old,
            f"install of {old} left version {_marker(out, 'INSTALLED')!r}")
    r.check(_marker(out, "PROMPTED") == "no",
            "the install log carries prompt text: something asked a question on "
            "a client with nobody to answer it")

    workdir = _marker(out, "WD") or ""
    r.check(workdir == f"/usr/lib/{app}",
            f"the installed unit declares WorkingDirectory={workdir!r}, not "
            f"/usr/lib/{app}: the payload root and the import root disagree")
    payload_prev = _int(out, "PAYLOAD_PREV")
    r.check(payload_prev >= min_payload_kb,
            f"payload of {old} is {payload_prev} KB against a floor of "
            f"{min_payload_kb} KB -- truncated package, and every path in it "
            f"would still exist")
    interpreter = _marker(out, "PY") or ""
    r.check(_marker(out, "PYEXE") == interpreter and interpreter != "",
            f"the shipped interpreter {interpreter!r} did not report itself as "
            f"sys.executable (it said {_marker(out, 'PYEXE')!r}): it is a stub, "
            f"or a link out to an interpreter this package does not carry")

    # --- the upgrade ---------------------------------------------------------
    r.check(_marker(out, "UPGRADE_RC") == "0",
            f"the upgrade exited {_marker(out, 'UPGRADE_RC')} "
            f"(124 means it hung and was killed)")
    r.check(_marker(out, "UPGRADED") == new,
            f"the upgrade left version {_marker(out, 'UPGRADED')!r}, not {new}")
    payload_next = _int(out, "PAYLOAD_NEXT")
    r.check(payload_next >= min_payload_kb,
            f"payload of {new} is {payload_next} KB against a floor of "
            f"{min_payload_kb} KB -- truncated package")

    # --- the client's own files ---------------------------------------------
    for i, (path, value) in enumerate(seed.items()):
        want = hashlib.sha256(value.encode()).hexdigest()
        before = (_marker(out, f"SEED_BEFORE_{i}") or "").split(" ")[0]
        after = (_marker(out, f"SEED_AFTER_{i}") or "").split(" ")[0]
        # The control, and it is the half that matters: if seeding silently did
        # nothing, `before` and `after` would both be empty and a bare equality
        # check would report the state as intact.
        r.check(before == want,
                f"the gate could not seed {path}: it hashed to {before!r}, not "
                f"{want!r}, so nothing below says anything about client state")
        r.check(after == want,
                f"the upgrade destroyed client state at {path}: it hashed to "
                f"{before!r} before and {after!r} after "
                f"({'the file is gone' if not after else 'the bytes changed'})")

    # --- the service ---------------------------------------------------------
    r.check(_marker(out, "PREHEALTH_RC") not in (None, "0"),
            "the health URL answered before the service was started, so a pass "
            "below would not mean the shipped ExecStart did anything")
    r.check(_marker(out, "HEALTH_RC") == "0",
            f"the service did not answer {health_url} after the upgrade "
            f"(curl rc={_marker(out, 'HEALTH_RC')})")
    return r


# =============================================================================
# The nspawn gate: the same bundle, installed by a real systemd.
# =============================================================================

# systemd-sysv provides /sbin/init, without which `--boot` has nothing to start.
# dbus is what lets `systemctl` inside the container reach PID 1. curl is the
# health probe; its presence is asserted from inside rather than assumed, so a
# root built without it fails loudly instead of reporting a service that did
# not answer.
NSPAWN_DOCKERFILE = """FROM debian:bookworm-slim
RUN apt-get update \\
 && apt-get install -y --no-install-recommends systemd systemd-sysv dbus curl \\
 && rm -rf /var/lib/apt/lists/*
"""

PROBE_UNIT = """[Unit]
Description=porter nspawn gate probe
After=multi-user.target

[Service]
Type=oneshot
ExecStart=/porter-probe.sh

[Install]
WantedBy=multi-user.target
"""


def _sudo(*args: str, **kw) -> subprocess.CompletedProcess:
    """Root, without ever prompting for it.

    `sudo -n`: a gate that can block on a password is a gate that hangs in a
    cron run, which is the same failure porter refuses in its own install.sh.
    """
    prefix = [] if os.geteuid() == 0 else ["sudo", "-n"]
    return subprocess.run([*prefix, *args], **kw)


def nspawn_available() -> str | None:
    """None if this host can run the nspawn gate, else why it cannot."""
    if not shutil.which("systemd-nspawn"):
        return "systemd-nspawn is not on PATH (apt install systemd-container)"
    if os.geteuid() != 0 and subprocess.run(
            ["sudo", "-n", "true"], capture_output=True).returncode != 0:
        return "not root and `sudo -n` does not work"
    if not (shutil.which("docker") and subprocess.run(
            ["docker", "info"], capture_output=True).returncode == 0):
        return "no usable docker daemon to build the container root from"
    return None


def nspawn_root(parent: Path) -> Path:
    """A bootable Debian tree, built by exporting a docker image. -> the root.

    `debootstrap` is deliberately not used: it is absent on this build host, and
    `docker export` of an image that has systemd in it is the same filesystem it
    would have produced. Docker is a **build** dependency here and never a
    client one -- nothing under test runs in docker, and the subject is systemd's
    own behaviour inside nspawn.

    Extracted as root, so the caller frees it with `_sudo("rm", "-rf", root)`;
    pytest's own tmp_path cleanup cannot delete it.
    """
    reason = nspawn_available()
    if reason:
        raise RuntimeError(f"cannot build an nspawn root: {reason}")
    tag = "porter-nspawn-gate:bookworm"
    build = subprocess.run(["docker", "build", "-q", "-t", tag, "-"],
                           input=NSPAWN_DOCKERFILE, capture_output=True, text=True)
    if build.returncode != 0:
        raise RuntimeError(f"docker build rc={build.returncode}: {build.stderr}")
    created = subprocess.run(["docker", "create", tag],
                             capture_output=True, text=True)
    if created.returncode != 0:
        raise RuntimeError(f"docker create rc={created.returncode}: {created.stderr}")
    cid = created.stdout.strip()
    root = Path(parent) / "root"
    root.mkdir(parents=True)
    try:
        export = subprocess.Popen(["docker", "export", cid], stdout=subprocess.PIPE)
        extract = _sudo("tar", "-x", "-C", str(root), stdin=export.stdout)
        export.stdout.close()
        if export.wait() != 0 or extract.returncode != 0:
            raise RuntimeError("docker export | tar -x did not complete")
    finally:
        subprocess.run(["docker", "rm", "-f", cid], capture_output=True)
    # The positive control on the root itself: with no init, `--boot` exits at
    # once and every verdict downstream would describe a container that never
    # ran. Checked here rather than after a failed boot, where it would read as
    # "the service did not come up".
    if not ((root / "sbin/init").exists() or (root / "usr/sbin/init").exists()):
        raise RuntimeError(f"{root} has no init: systemd-nspawn --boot has "
                           "nothing to start")
    return root


def _boot(root: Path, probe_sh: str, out: Path, usb: Path,
          timeout: int) -> subprocess.CompletedProcess:
    """Boot `root` ephemerally, run `probe_sh` at multi-user.target, come back.

    `--ephemeral` is what makes one root serve several gate runs: the container
    writes to a throwaway copy and the source tree is untouched, so a second
    bundle is never gated against a client the first one already installed into.
    Verified 2026-08-08 on ext4 -- a file created inside the boot is absent from
    the source root afterwards.

    `--private-network` is not optional. Without it the container shares the
    host's network namespace, so a service binding 127.0.0.1:9000 binds the
    HOST's loopback: it collides with whatever is already listening, and the
    health probe could be answered by the build machine. There is nothing to
    fetch -- porter's whole premise is that a client needs no network.

    The probe ends by powering off, which is what returns control here.
    """
    out.mkdir(parents=True, exist_ok=True)
    _sudo("chmod", "0777", str(out))
    _sudo("cp", "/dev/stdin", str(root / "porter-probe.sh"),
          input=probe_sh, text=True)
    _sudo("chmod", "0755", str(root / "porter-probe.sh"))
    _sudo("mkdir", "-p", str(root / "etc/systemd/system"))
    _sudo("cp", "/dev/stdin",
          str(root / "etc/systemd/system/porter-probe.service"),
          input=PROBE_UNIT, text=True)
    enable = _sudo("systemctl", f"--root={root}", "enable", "porter-probe.service",
                   capture_output=True, text=True)
    if enable.returncode != 0:
        raise RuntimeError(f"could not enable the probe unit: {enable.stderr}")

    # An explicit, unique --machine=. Without it nspawn names the machine after
    # the directory and registers a scope with the HOST's systemd; a second boot
    # then dies on "Unit <name>.scope was already loaded", and a container that
    # was killed rather than powered off leaves its scope behind to block every
    # later run. Measured here.
    machine = f"porter-gate-{os.getpid()}-{abs(hash(str(out))) % 100000}"
    try:
        booted = _sudo("systemd-nspawn", "-q", f"--directory={root}",
                       f"--machine={machine}", f"--bind-ro={usb}:/media/usb",
                       f"--bind={out}:/out", "--private-network",
                       "--ephemeral", "--boot",
                       capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        _sudo("machinectl", "terminate", machine, capture_output=True)
        raise
    finally:
        # For the paths where poweroff did not happen. Left behind, the scope
        # blocks the next boot on this host -- including a peer's.
        _sudo("machinectl", "terminate", machine, capture_output=True)
        # Written by root inside the container. Handed back before any verdict,
        # deliberately: a failing gate must not be what decides whether the
        # caller can clean up after itself.
        _sudo("chown", "-R", f"{os.getuid()}:{os.getgid()}", str(out))
    return booted


def _nspawn_script(app: str, health_url: str) -> str:
    """What runs inside the booted container, as one sh program.

    Not `set -e`, for the same reason `_script` is not: a gate that aborts on
    its first surprise answers one question per boot, and a boot here costs
    minutes. Every step appends a marker and Python judges the set; a step that
    dies simply leaves its marker missing, which is a failure by construction.

    **Nothing here starts the service.** The postinst's
    `systemctl start --no-block <pkg>.service` is the subject, and a probe that
    started the unit itself would be structurally unable to notice a package
    that installs and leaves nothing running until the next reboot -- which is
    precisely what porter shipped for four commits, and what the oneshot probe
    had to have that line removed to catch.
    """
    return f"""#!/bin/sh
exec >/out/probe.log 2>&1
set -x
APP={app}
M=/out/markers
: > $M

# --- the harness, before anything it measures --------------------------------
#
# If PID 1 is not systemd, nothing below is a statement about systemd; if we are
# not inside nspawn, it is a statement about the BUILD HOST, which would be a
# gate reporting on the wrong machine entirely.
echo "PID1=$(readlink -f /proc/1/exe)" >> $M
echo "CONTAINER=$(systemd-detect-virt --container 2>&1)" >> $M
# The kernel's own list, not `ip link`: an interface count taken where the
# binary was absent once reported zero interfaces and passed. A directory
# listing cannot be satisfied by a missing binary, and requiring exactly `lo` is
# self-controlling -- a blind read yields the empty string, which is not `lo`.
echo "IFACES=$(ls /sys/class/net 2>/dev/null | tr '\\n' ' ')" >> $M
command -v curl >/dev/null 2>&1 && echo "CURL=ok" >> $M || echo "CURL=absent" >> $M
# `timeout` is what bounds the install below, and a hang is the failure that
# matters most on an airgapped client. rc=124 is timeout doing its job.
timeout 1 sleep 5; echo "TIMEOUTCTL=$?" >> $M

# The airgap, with the control that the probe can see a source at all: the base
# image ships sources, so a probe reporting 0 both times is blind, not clean.
echo "NETSRC_BEFORE=$(cat /etc/apt/sources.list /etc/apt/sources.list.d/* 2>/dev/null | grep -cE 'https?://')" >> $M
rm -f /etc/apt/sources.list /etc/apt/sources.list.d/*.list /etc/apt/sources.list.d/*.sources
rm -rf /var/lib/apt/lists/*
echo "NETSRC_AFTER=$(cat /etc/apt/sources.list /etc/apt/sources.list.d/* 2>/dev/null | grep -cE 'https?://')" >> $M

# --- the three negative controls, taken BEFORE the install -------------------
#
# Each is the reason its counterpart after the install means anything: nothing
# answers the URL yet, the state directory does not exist yet, and the service
# user does not exist yet. Without these, a health check satisfied by something
# else on loopback, a /var/lib/$APP that shipped in the image, or a user the
# base already had would each read as the package working.
curl -fsS -o /dev/null --max-time 3 '{health_url}'; echo "PREHEALTH_RC=$?" >> $M
test -e "/var/lib/$APP" && echo "PRESTATE=present" >> $M || echo "PRESTATE=absent" >> $M
getent passwd "$APP" >/dev/null && echo "PREUSER=present" >> $M || echo "PREUSER=absent" >> $M

# --- install, with the client's own command and nothing else -----------------
setsid -w timeout 900 bash /media/usb/install.sh < /dev/null > /out/install.log 2>&1
echo "INSTALL_RC=$?" >> $M
echo "INSTALLED=$(dpkg-query -W -f='${{Version}}' "$APP" 2>/dev/null)" >> $M

# --- did systemd bring it up? ------------------------------------------------
#
# The postinst starts it with --no-block, so the start job is still running when
# dpkg returns. Waiting is not the same as starting: this loop cannot make a
# dead unit active, it can only avoid reading `activating` as a failure.
i=0
while [ $i -lt 60 ]; do
  [ "$(systemctl is-active "$APP.service" 2>/dev/null)" = active ] \
    && [ "$(systemctl show -p SubState --value "$APP.service")" = running ] && break
  i=$((i + 1)); sleep 1
done

# THEN SETTLE, AND ASK AGAIN. A single `is-active` sample cannot distinguish a
# service that came up from one in a restart loop: with no `Type=`, systemd
# treats the unit as Type=simple and calls it `active (running)` the instant the
# fork succeeds -- even when the process exits 25 ms later. Measured here
# 2026-08-08 against a deliberately broken ExecStart: the wait loop above broke
# on exactly that transient, and had the read below caught the same window the
# gate would have PASSED a package whose service never runs.
#
# Five seconds is longer than the unit's RestartSec=3, so a flapping unit must
# have accumulated a restart by now. NRESTARTS_PRE is what makes the reading
# safe: `active` with restarts already behind it is a moment in a loop, not a
# service. It is also the baseline the deliberate kill below is measured against.
sleep 5
echo "ACTIVE=$(systemctl is-active "$APP.service" 2>/dev/null)" >> $M
echo "ENABLED=$(systemctl is-enabled "$APP.service" 2>/dev/null)" >> $M
echo "SUBSTATE=$(systemctl show -p SubState --value "$APP.service")" >> $M
echo "NRESTARTS_PRE=$(systemctl show -p NRestarts --value "$APP.service")" >> $M

# --- systemd's own parse of the directives, not the file's text --------------
#
# `systemctl show` reports what systemd LOADED. That is the whole difference
# from grepping the unit: a key systemd does not recognise is a log line and a
# service that starts anyway, and `systemd-analyze verify` never reports a key
# that is missing. ProtectSystem defaults to `no` and StateDirectory to empty,
# so a value here can only have come from the shipped unit.
echo "SHOW_USER=$(systemctl show -p User --value "$APP.service")" >> $M
echo "SHOW_PROTECT=$(systemctl show -p ProtectSystem --value "$APP.service")" >> $M
echo "SHOW_RESTART=$(systemctl show -p Restart --value "$APP.service")" >> $M
echo "SHOW_STATEDIR=$(systemctl show -p StateDirectory --value "$APP.service")" >> $M
echo "SHOW_WD=$(systemctl show -p WorkingDirectory --value "$APP.service")" >> $M
# The control for all five: a property systemd does not know comes back EMPTY.
# Without it, "SHOW_PROTECT=strict" could be `show` echoing its argument, or
# defaulting, rather than reporting a directive it read.
echo "SHOW_CONTROL=$(systemctl show -p ProtectSystemNoSuchKey --value "$APP.service" 2>/dev/null)" >> $M

# --- User= actually took effect ----------------------------------------------
#
# Read off /proc/<mainpid>, which the kernel owns by the process's real user.
# `stat` and not `ps`: procps is not guaranteed in a slim base, and a probe
# satisfied by `command not found` is the memory-limit burn all over again.
MAINPID=$(systemctl show -p MainPID --value "$APP.service")
echo "MAINPID=$MAINPID" >> $M
echo "MAINUSER=$(stat -c %U "/proc/$MAINPID" 2>/dev/null)" >> $M

# --- StateDirectory= was created, by systemd, for this unit ------------------
echo "STATEDIR=$(stat -c '%U %G %a' "/var/lib/$APP" 2>/dev/null)" >> $M

# --- payload magnitude, at the path the UNIT names ---------------------------
#
# "The file is there" and "the file is real" are different questions: a stubbed
# interpreter once produced a 30,912-byte package where every path assertion
# still passed.
WD=$(systemctl show -p WorkingDirectory --value "$APP.service")
echo "PAYLOAD_KB=$(du -sk "$WD" 2>/dev/null | cut -f1)" >> $M

# --- does ProtectSystem=strict have teeth on THIS kernel? --------------------
#
# `systemctl show` proves systemd parsed the directive for this unit; this pair
# proves the directive blocks writes here. Neither alone is the claim. The
# second line is the positive control and it is the half that matters: a
# transient unit WITHOUT the property must be able to write, or the first line
# is measuring a read-only /usr rather than the sandbox.
systemd-run --wait --quiet --property=ProtectSystem=strict \
    /bin/sh -c 'touch /usr/porter-protect-probe' >/dev/null 2>&1
echo "PROTECT_BLOCKS_RC=$?" >> $M
systemd-run --wait --quiet \
    /bin/sh -c 'touch /usr/porter-protect-probe && rm -f /usr/porter-protect-probe' >/dev/null 2>&1
echo "PROTECT_CONTROL_RC=$?" >> $M

# --- everything below needs a LIVE unit --------------------------------------
#
# Two reasons for the guard, and the second is not caution but safety. A unit
# that failed to start has MainPID=0, and `kill -9 0` in sh signals the caller's
# own process GROUP -- the probe would kill itself, the container would hang
# until the host timeout, and the gate's verdict on a broken package would be
# "the container did not finish" instead of "the service never came up".
#
# The markers below simply do not appear when the unit is dead. That is a
# failure by construction: every check that reads them fires on the absence, and
# the ACTIVE failure above says why.
if [ "$(systemctl is-active "$APP.service" 2>/dev/null)" = active ] \
   && [ "${{MAINPID:-0}}" -gt 0 ] 2>/dev/null; then

# --- the service answers ------------------------------------------------------
i=0
while [ $i -lt 45 ]; do
  curl -fsS -o /dev/null --max-time 2 '{health_url}' && break
  i=$((i + 1)); sleep 1
done
curl -fsS -o /dev/null --max-time 3 '{health_url}'; echo "HEALTH_RC=$?" >> $M

# --- Restart=on-failure, demonstrated rather than declared -------------------
#
# SIGKILL the main process and watch systemd put it back. This is the one
# directive that cannot be established any other way: a unit file saying
# `Restart=on-failure` and a unit systemd actually restarts look identical on
# disk. The new PID must DIFFER from the old one -- "it is active" would be
# satisfied by systemd never having noticed the kill.
kill -9 "$MAINPID" 2>/dev/null; echo "KILL_RC=$?" >> $M
i=0
while [ $i -lt 45 ]; do
  NEW=$(systemctl show -p MainPID --value "$APP.service")
  [ -n "$NEW" ] && [ "$NEW" != "0" ] && [ "$NEW" != "$MAINPID" ] && break
  i=$((i + 1)); sleep 1
done
echo "NEWPID=$(systemctl show -p MainPID --value "$APP.service")" >> $M
echo "NRESTARTS=$(systemctl show -p NRestarts --value "$APP.service")" >> $M
echo "AFTER_KILL_ACTIVE=$(systemctl is-active "$APP.service" 2>/dev/null)" >> $M
i=0
while [ $i -lt 45 ]; do
  curl -fsS -o /dev/null --max-time 2 '{health_url}' && break
  i=$((i + 1)); sleep 1
done
curl -fsS -o /dev/null --max-time 3 '{health_url}'; echo "POSTKILL_HEALTH_RC=$?" >> $M

else
  echo "LIVE_CHECKS=skipped" >> $M
fi

# Diagnostics last, and into their own files -- nothing a package printed can
# be mistaken for a marker, because markers are only ever in $M.
systemctl status --no-pager -l "$APP.service" > /out/status 2>&1
journalctl --no-pager -u "$APP.service" > /out/journal 2>&1
echo "DONE=yes" >> $M
systemctl poweroff
"""


def nspawn_gate(root: Path, usb: Path, app: str, health_url: str, *,
                min_payload_kb: int = 1024, timeout: int = 1800) -> GateResult:
    """Install the bundle inside a booted systemd and report what it did.

    `root` is a tree from `nspawn_root()`; it is booted `--ephemeral`, so one
    root can gate several bundles without either seeing the other's install.

    This is the gate that can say the *service came up*. Everything it asserts
    about `User=`, `StateDirectory=`, `ProtectSystem=strict` and
    `Restart=on-failure` comes from systemd -- from `systemctl show`, from
    `/proc`, from a process systemd replaced after it was killed -- and not from
    reading the unit file back.
    """
    usb = Path(usb)
    root = Path(root)
    if not PACKAGE_NAME.match(app):
        raise ValueError(f"{app!r} is not a Debian package name")
    if not HEALTH_URL.match(health_url):
        raise ValueError(
            f"{health_url!r} is not a loopback http(s) URL. The gate runs this "
            "inside a shell, so a URL carrying shell metacharacters could make "
            "the health probe report success with nothing listening")

    r = GateResult()
    out = usb.parent / f"{usb.name}-nspawn-out"
    try:
        booted = _boot(root, _nspawn_script(app, health_url), out, usb, timeout)
    except subprocess.TimeoutExpired:
        r.check(False, f"the nspawn container did not finish within {timeout}s")
        r.log = _context(None, out)
        return r
    markers = (out / "markers").read_text() if (out / "markers").exists() else ""
    r.log = _context(booted, out)
    o = markers

    # The transcript reached the end. Without this every check below would be
    # reading a truncated marker file, and "the marker is absent" would mean
    # "the container died early" rather than "the assertion failed".
    r.check("DONE=yes" in o, "the probe did not run to completion")

    # --- the harness, before anything it measured ----------------------------
    r.check((_marker(o, "PID1") or "").endswith("systemd"),
            f"PID 1 is {_marker(o, 'PID1')!r} and not systemd: nothing below is "
            f"a statement about systemd starting anything")
    r.check(_marker(o, "CONTAINER") == "systemd-nspawn",
            f"systemd-detect-virt says {_marker(o, 'CONTAINER')!r}: this probe "
            f"did not run inside nspawn, so it may be describing the build host")
    r.check(_marker(o, "IFACES") == "lo",
            f"the container is not isolated: interfaces are "
            f"{_marker(o, 'IFACES')!r}, expected exactly 'lo'")
    r.check(_marker(o, "CURL") == "ok",
            "curl is absent from the root, so every health verdict below is the "
            "probe failing rather than the service not answering")
    r.check(_marker(o, "TIMEOUTCTL") == "124",
            "`timeout` did not fire on a command that overruns it, so the "
            "bounded install could hang and still report a pass")
    r.check(_int(o, "NETSRC_BEFORE") >= 1,
            "the apt-source probe found no network source even before the wipe, "
            "so it is blind and 'no network source remained' proves nothing")
    r.check(_int(o, "NETSRC_AFTER") == 0,
            "a network apt source survived into the install: a working mirror "
            "can satisfy it and the local repo would never be exercised")

    # --- the three before-controls -------------------------------------------
    r.check(_marker(o, "PREHEALTH_RC") not in (None, "0"),
            "the health URL answered before anything was installed, so a pass "
            "below would not mean the shipped unit did anything")
    r.check(_marker(o, "PRESTATE") == "absent",
            f"/var/lib/{app} existed before the install, so its presence "
            f"afterwards says nothing about StateDirectory=")
    r.check(_marker(o, "PREUSER") == "absent",
            f"the {app} user existed before the install, so User= resolving to "
            f"it says nothing about the postinst having created it")

    # --- the install ---------------------------------------------------------
    r.check(_marker(o, "INSTALL_RC") == "0",
            f"install.sh exited {_marker(o, 'INSTALL_RC')} "
            f"(124 means it hung and was killed)")
    r.check((_marker(o, "INSTALLED") or "") != "",
            f"dpkg does not report {app} as installed after install.sh exited "
            f"{_marker(o, 'INSTALL_RC')}")

    # --- THE claim: systemd brought the unit up ------------------------------
    r.check(_marker(o, "ACTIVE") == "active",
            f"the unit is {_marker(o, 'ACTIVE')!r} "
            f"(SubState={_marker(o, 'SUBSTATE')!r}) after install.sh: the "
            f"package installed and the service did not come up")
    r.check(_marker(o, "SUBSTATE") == "running",
            f"the unit's SubState is {_marker(o, 'SUBSTATE')!r}, not 'running': "
            f"'auto-restart' is what a service that keeps dying looks like, and "
            f"ActiveState is briefly 'active' throughout it")
    # A unit that flapped its way here is not a unit that came up. Without this,
    # "active" is a sample, and a sample taken inside a Type=simple restart cycle
    # is indistinguishable from a healthy service.
    r.check(_int(o, "NRESTARTS_PRE") == 0,
            f"systemd had already restarted the unit "
            f"{_marker(o, 'NRESTARTS_PRE')!r} times before anything killed it: "
            f"it is in a restart loop, and 'active' is a moment inside one")
    r.check(_marker(o, "ENABLED") == "enabled",
            f"the unit is {_marker(o, 'ENABLED')!r}, not enabled: it is running "
            f"now and absent after the next reboot")

    # --- systemd's own parse -------------------------------------------------
    r.check(_marker(o, "SHOW_CONTROL") in (None, ""),
            f"`systemctl show` returned {_marker(o, 'SHOW_CONTROL')!r} for a "
            f"property that does not exist, so the values below may be the tool "
            f"echoing its argument rather than reporting a loaded directive")
    r.check(_marker(o, "SHOW_USER") == app,
            f"systemd loaded User={_marker(o, 'SHOW_USER')!r}, not {app!r}")
    r.check(_marker(o, "SHOW_PROTECT") == "strict",
            f"systemd loaded ProtectSystem={_marker(o, 'SHOW_PROTECT')!r}, not "
            f"'strict' (its default is 'no', so this value can only come from "
            f"the shipped unit)")
    r.check(_marker(o, "SHOW_RESTART") == "on-failure",
            f"systemd loaded Restart={_marker(o, 'SHOW_RESTART')!r}")
    r.check(_marker(o, "SHOW_STATEDIR") == app,
            f"systemd loaded StateDirectory={_marker(o, 'SHOW_STATEDIR')!r}")
    r.check(_marker(o, "SHOW_WD") == f"/usr/lib/{app}",
            f"systemd loaded WorkingDirectory={_marker(o, 'SHOW_WD')!r}, not "
            f"/usr/lib/{app}: the payload root and the import root disagree")

    # --- and the directives had effect, not merely a value -------------------
    mainpid = _int(o, "MAINPID")
    r.check(mainpid > 0,
            f"the unit has MainPID={_marker(o, 'MAINPID')!r}: there is no "
            f"process to ask anything about")
    r.check(_marker(o, "MAINUSER") == app,
            f"the running process belongs to {_marker(o, 'MAINUSER')!r}, not "
            f"{app!r}: User= is in the file and did not take effect")
    state = _marker(o, "STATEDIR") or ""
    r.check(app in state,
            f"/var/lib/{app} is {state!r} after the unit started: "
            f"StateDirectory= did not create it, or created it for someone else")
    payload = _int(o, "PAYLOAD_KB")
    r.check(payload >= min_payload_kb,
            f"the payload at WorkingDirectory= is {payload} KB against a floor "
            f"of {min_payload_kb} KB -- truncated package, and every path in it "
            f"would still exist")
    r.check(_marker(o, "PROTECT_CONTROL_RC") == "0",
            "a transient unit with no sandboxing could not write to /usr "
            "either, so the refusal below is a read-only filesystem and not "
            "ProtectSystem=strict")
    r.check(_marker(o, "PROTECT_BLOCKS_RC") not in (None, "0"),
            "ProtectSystem=strict did NOT stop a write to /usr on this kernel: "
            "the directive is loaded and unenforced, which is indistinguishable "
            "on disk from one that works")

    # --- the service answers -------------------------------------------------
    r.check(_marker(o, "HEALTH_RC") == "0",
            f"the service did not answer {health_url} once systemd had started "
            f"it (curl rc={_marker(o, 'HEALTH_RC')})")

    # --- Restart=on-failure, demonstrated ------------------------------------
    newpid = _int(o, "NEWPID")
    r.check(_marker(o, "KILL_RC") == "0",
            f"the probe could not signal the main process "
            f"(kill rc={_marker(o, 'KILL_RC')}), so nothing below tests Restart=")
    r.check(newpid > 0 and newpid != mainpid,
            f"after SIGKILL the main PID is {newpid} and was {mainpid}: systemd "
            f"did not replace the process, so Restart=on-failure is a line in a "
            f"file and nothing more")
    # A delta, not an absolute: the count is only evidence about THIS kill if it
    # moved because of it, and `NRESTARTS_PRE` above is asserted to be 0 so the
    # two readings cannot both be explained by a unit that was already flapping.
    r.check(_int(o, "NRESTARTS") > _int(o, "NRESTARTS_PRE"),
            f"systemd counted {_marker(o, 'NRESTARTS_PRE')!r} restarts before "
            f"the kill and {_marker(o, 'NRESTARTS')!r} after: it did not record "
            f"having restarted anything")
    r.check(_marker(o, "AFTER_KILL_ACTIVE") == "active",
            f"the unit is {_marker(o, 'AFTER_KILL_ACTIVE')!r} after its process "
            f"was killed")
    r.check(_marker(o, "POSTKILL_HEALTH_RC") == "0",
            f"the restarted service does not answer {health_url} "
            f"(curl rc={_marker(o, 'POSTKILL_HEALTH_RC')}): systemd put a "
            f"process back that cannot serve")
    return r


def _context(booted: subprocess.CompletedProcess | None, out: Path) -> str:
    """Everything a failure needs in one string: the console, the probe's trace,
    and whatever the probe managed to write. Carried rather than summarised --
    every failure here is a statement about a machine the caller cannot see."""
    parts = []
    if booted is not None:
        parts.append(f"nspawn rc={booted.returncode}\n"
                     f"{booted.stdout[-2000:]}\n{booted.stderr[-2000:]}")
    else:
        parts.append("nspawn did not return (timed out)")
    if out.exists():
        for name in ("markers", "install.log", "status", "journal", "probe.log"):
            path = out / name
            parts.append(f"--- {name} ---\n"
                         + (path.read_text(errors="replace")[-3000:]
                            if path.exists() else "(absent)"))
    return "\n".join(parts)
