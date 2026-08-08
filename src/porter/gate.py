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

**What this proves, and what it does not.** The container proves payload,
upgrade, state survival and that the shipped `ExecStart=` answers HTTP from the
shipped `WorkingDirectory=`. It does **not** prove that systemd starts the unit:
there is no PID 1 systemd in a container, and no unit has ever been started by
systemd in this project. `User=`, `StateDirectory=`, `ProtectSystem=strict`,
`Restart=on-failure` and systemd's root-then-drop read of `EnvironmentFile` are
Task 14's nspawn work. Do not read a green gate as a claim about any of them.
"""
from __future__ import annotations

import base64
import functools
import hashlib
import re
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
