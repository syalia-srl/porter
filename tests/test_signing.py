"""A signed flat repo, and what the signature is actually worth.

Two claims are easy to conflate here, and only one of them is about signing.

**apt verifies a package against the `SHA256:` in `Packages` whether or not the
repo is signed.** So "a flipped byte in a .deb is rejected" is true of an
unsigned porter repo too, and a test that showed only that would be reported as
evidence for signing while proving nothing about it. It is parametrized over
both modes below, precisely so the reader can see it does not discriminate.

**What the signature buys is the index.** Anyone who can write to the stick can
replace a package *and* recompute the hash the index records -- at which point
an unsigned repo installs the replacement without a murmur. The discriminating
test is `test_a_rewritten_index_...`: the same tamper, rejected when signed and
**accepted when unsigned**, with the acceptance asserted on the file that lands
in the payload rather than on anything apt printed.

Every key here is generated into a throwaway GNUPGHOME. Nothing touches the
build host's own keyring, and `gpgv` -- which has no web of trust -- is what
verifies, so a pass cannot be borrowed from a key that happens to be installed
on this machine.
"""
import gzip
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

from porter.deb import build_deb
from porter.repo import (
    KEYRING_NAME,
    _verify_with_the_shipped_key,
    usb_tree,
    write_index,
)

KEY_UID = "porter test key <porter@example.invalid>"

# Same discipline as tests/test_repo.py: every source that could rescue a broken
# local repo is removed inside the container first. `--network none` alone is
# not enough, because a cached index in /var/lib/apt/lists would still resolve.
NO_NETWORK_SOURCES = (
    "rm -f /etc/apt/sources.list /etc/apt/sources.list.d/*.sources "
    "/etc/apt/sources.list.d/*.list; rm -rf /var/lib/apt/lists/*;"
)


@pytest.fixture(scope="module")
def gpg_home():
    """A keyring of exactly one throwaway key, in a SHORT path.

    Short deliberately: gpg-agent's socket lives inside GNUPGHOME when
    /run/user/<uid> is unavailable, and a unix socket path is capped at ~108
    bytes. pytest's own tmp_path is long enough to blow that on a nested test
    name, and the failure is an opaque "can't connect to the agent" rather than
    anything naming a path.
    """
    home = Path(tempfile.mkdtemp(prefix="pg"))
    home.chmod(0o700)
    env = {**os.environ, "GNUPGHOME": str(home)}
    made = subprocess.run(
        ["gpg", "--batch", "--yes", "--pinentry-mode", "loopback",
         "--passphrase", "", "--quick-generate-key", KEY_UID,
         "default", "default", "never"],
        capture_output=True, text=True, env=env)
    assert made.returncode == 0, made.stderr
    # The control on the fixture itself: a keyring that silently generated
    # nothing would make every "the signature verifies" below a statement about
    # an empty file, and `gpg --export` on a missing key exits 0 with no output.
    listed = subprocess.run(["gpg", "--batch", "--list-secret-keys"],
                            capture_output=True, text=True, env=env)
    assert KEY_UID in listed.stdout, (
        f"no secret key in the throwaway keyring: {listed.stdout!r} "
        f"{listed.stderr!r}")
    yield home
    subprocess.run(["gpgconf", "--kill", "gpg-agent"], capture_output=True,
                   env=env)
    shutil.rmtree(home, ignore_errors=True)


def _deb(tmp: Path, out: Path, *, version: str, payload: str) -> Path:
    """A minimal but real demo-app .deb, built by porter's own build_deb.

    The payload filename is what makes a swapped package visible on the client:
    dpkg-query reports a version, and a version is exactly what an attacker
    who rewrites the index keeps identical.
    """
    stage = tmp / f"stage-{version}-{payload}"
    (stage / "usr/lib/demo-app").mkdir(parents=True)
    (stage / "usr/lib/demo-app" / payload).write_text(f"payload {payload}\n")
    return build_deb(stage, {
        "Package": "demo-app",
        "Version": version,
        "Architecture": "amd64",
        "Maintainer": "porter <porter@example.com>",
        "Description": "porter's example package",
    }, out)


@pytest.fixture
def one_deb(tmp_path) -> list[Path]:
    return [_deb(tmp_path, tmp_path / "debs", version="1.0", payload="v1.txt")]


def _deb_line(usb: Path) -> str:
    """The `deb ...` line install.sh writes into sources.list.d.

    Read out of the generated script rather than asserted against the whole
    file: the substrate here is the source apt ends up with, and a substring
    search over the script answers questions about its comments too. That is
    not hypothetical -- the signed template deliberately does not spell the
    unsigned template's option, for exactly this reason.
    """
    lines = [ln for ln in (usb / "install.sh").read_text().splitlines()
             if ln.startswith('echo "deb ')]
    assert len(lines) == 1, lines
    return lines[0]


# --- what each mode emits ----------------------------------------------------

def test_an_unsigned_repo_carries_no_signature_and_trusts_its_own_source(
        one_deb, tmp_path):
    """Property 5, the build half: no key, no ceremony, a tree that works."""
    usb = usb_tree(one_deb, tmp_path / "usb", app="demo-app", readme="x")
    repo = usb / "repo"
    assert not (repo / "Release.gpg").exists()
    assert not (repo / KEYRING_NAME).exists()
    assert "[trusted=yes]" in _deb_line(usb)


def test_a_signed_repo_ships_a_detached_signature_and_the_key_that_checks_it(
        one_deb, tmp_path, gpg_home):
    usb = usb_tree(one_deb, tmp_path / "usb", app="demo-app", readme="x",
                   sign_key=KEY_UID, gpg_home=gpg_home)
    repo = usb / "repo"
    signature = repo / "Release.gpg"
    assert signature.exists(), sorted(p.name for p in repo.iterdir())
    assert "BEGIN PGP SIGNATURE" in signature.read_text()
    # Magnitude, not presence. `gpg --export` of a pattern that matches nothing
    # exits 0 and writes zero bytes, and a zero-byte keyring passes every
    # existence check while making the client's install fail with `no valid
    # OpenPGP data found` -- on a machine with no network to fix it from.
    assert (repo / KEYRING_NAME).stat().st_size > 100, (
        f"{KEYRING_NAME} is {(repo / KEYRING_NAME).stat().st_size} bytes")


def test_the_shipped_key_verifies_the_shipped_signature_and_can_still_say_no(
        one_deb, tmp_path, gpg_home):
    """gpgv, the way apt runs it -- with a keyring holding only the stick's key.

    And with its own control on the line below, because a `gpgv` that failed
    open would make the assertion above a line that always says yes. This is
    the same pair `repo._verify_with_the_shipped_key` runs at build time; it is
    repeated here so that a regression which removed it from the build path is
    still visible from the suite.
    """
    usb = usb_tree(one_deb, tmp_path / "usb", app="demo-app", readme="x",
                   sign_key=KEY_UID, gpg_home=gpg_home)
    repo = usb / "repo"
    ring, sig, release = repo / KEYRING_NAME, repo / "Release.gpg", repo / "Release"

    good = subprocess.run(["gpgv", "--keyring", str(ring), str(sig), str(release)],
                          capture_output=True, text=True)
    assert good.returncode == 0, good.stderr

    tampered = tmp_path / "Release"
    body = bytearray(release.read_bytes())
    body[0] ^= 0x20
    tampered.write_bytes(bytes(body))
    bad = subprocess.run(["gpgv", "--keyring", str(ring), str(sig), str(tampered)],
                         capture_output=True, text=True)
    assert bad.returncode != 0, (
        "gpgv accepted a Release with a flipped byte: this check cannot fail, "
        "so the one above proves nothing")


def test_a_signed_install_script_names_the_key_and_never_trusts_the_source(
        one_deb, tmp_path, gpg_home):
    """Property 3's second half. The `deb` line is the whole artefact here."""
    usb = usb_tree(one_deb, tmp_path / "usb", app="demo-app", readme="x",
                   sign_key=KEY_UID, gpg_home=gpg_home)
    line = _deb_line(usb)
    script = (usb / "install.sh").read_text()
    # The option, and separately the variable it expands to. Asserting the
    # expanded path against this line would be asserting something the script
    # does not say -- `KEYRING` is set once, above, and used here.
    assert "signed-by=$KEYRING" in line, line
    assert "trusted=yes" not in line, line
    assert "KEYRING=/etc/apt/keyrings/demo-app.gpg" in script, script
    # No run-time fallback anywhere in the script: a signed tree whose key file
    # did not copy must die, not quietly install an unverified source.
    assert "trusted=yes" not in script
    assert KEYRING_NAME in script
    # ...and the client-side proof that the pair actually resolves is
    # `test_a_signed_repo_installs_offline_through_the_shipped_key`, which runs
    # this script against a real apt. A grep over generated shell pins the text,
    # not the behaviour.


def test_a_key_that_is_not_in_the_keyring_is_refused_rather_than_exported_empty(
        one_deb, tmp_path, gpg_home):
    """The magnitude guard, at the one place it is cheap to hold.

    `gpg --export nobody@nowhere` exits **0** and prints nothing. Without this
    refusal the stick ships a zero-byte keyring, `usb_tree` returns happily, and
    the failure surfaces as an apt error on an airgapped client.
    """
    with pytest.raises(RuntimeError, match="no such key|0 bytes"):
        usb_tree(one_deb, tmp_path / "usb", app="demo-app", readme="x",
                 sign_key="nobody@nowhere.invalid", gpg_home=gpg_home)


def test_re_indexing_without_a_key_removes_the_stale_signature(
        one_deb, tmp_path, gpg_home):
    """A signature over a Release that no longer exists is worse than none: the
    directory looks signed to anybody who lists it, and verifies for nobody."""
    usb = usb_tree(one_deb, tmp_path / "usb", app="demo-app", readme="x",
                   sign_key=KEY_UID, gpg_home=gpg_home)
    repo = usb / "repo"
    assert (repo / "Release.gpg").exists()  # control: there was one to remove
    write_index(repo)
    assert not (repo / "Release.gpg").exists()
    assert not (repo / KEYRING_NAME).exists()


# --- the client, with a real apt ---------------------------------------------

def _run(image: str, usb: Path, script: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["docker", "run", "--rm", "--network", "none",
         "-v", f"{usb}:/media/usb:ro", image, "bash", "-c", script],
        capture_output=True, text=True)


INSTALL = ("set -e;" + NO_NETWORK_SOURCES +
           "bash /media/usb/install.sh >/dev/null;"
           "dpkg-query -W -f='${Version}' demo-app")


@pytest.mark.docker
def test_an_unsigned_repo_installs_offline(one_deb, tmp_path, docker_image):
    """Property 5, the client half. A dev loop needs no key."""
    usb = usb_tree(one_deb, tmp_path / "usb", app="demo-app", readme="x")
    proc = _run(docker_image, usb, INSTALL)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert proc.stdout.strip() == "1.0", proc.stdout


@pytest.mark.docker
def test_a_signed_repo_installs_offline_through_the_shipped_key(
        one_deb, tmp_path, docker_image, gpg_home):
    """Property 3, end to end: apt verified the index against a key that arrived
    on the stick, with no network and nothing pre-trusted on the client.

    The negative control is `test_a_rewritten_index_...` below -- the same apt,
    the same keyring, and a repo it must refuse. Without that pair, an apt that
    ignored `signed-by=` entirely would pass this test.
    """
    usb = usb_tree(one_deb, tmp_path / "usb", app="demo-app", readme="x",
                   sign_key=KEY_UID, gpg_home=gpg_home)
    proc = _run(docker_image, usb, INSTALL)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert proc.stdout.strip() == "1.0", proc.stdout


@pytest.mark.docker
@pytest.mark.parametrize("signed", [False, True])
def test_a_package_with_a_flipped_byte_is_rejected_in_either_mode(
        one_deb, tmp_path, docker_image, gpg_home, signed):
    """Property 4, and the honest framing of it.

    The index records the package's SHA256 and apt checks it regardless of
    signing, so this rejection is NOT evidence about the signature -- which is
    why it runs in both modes and asserts the same outcome in both. What it does
    establish is that the recorded hash is real: an index whose SHA256 was
    decorative would let the flipped package install here.
    """
    usb = usb_tree(one_deb, tmp_path / "usb", app="demo-app", readme="x",
                   sign_key=KEY_UID if signed else None, gpg_home=gpg_home)
    deb = next((usb / "repo").glob("*.deb"))
    body = bytearray(deb.read_bytes())
    # Well inside the data member, not the ar header: a corrupt header is
    # rejected by dpkg's own parser and would prove nothing about apt's hash.
    body[len(body) // 2] ^= 0xFF
    deb.write_bytes(bytes(body))

    proc = _run(docker_image, usb, INSTALL)
    out = proc.stdout + proc.stderr
    assert proc.returncode != 0, (
        f"apt installed a package whose bytes do not match the index: the "
        f"recorded SHA256 is decorative\n{out}")
    assert "Hash Sum mismatch" in out or "mismatch" in out.lower(), out


@pytest.mark.docker
@pytest.mark.parametrize("signed", [True, False])
def test_a_rewritten_index_is_refused_only_when_the_repo_is_signed(
        tmp_path, docker_image, gpg_home, signed):
    """THE test that says what signing is for.

    The attacker has write access to the stick, so they do the obvious thing:
    replace the package and recompute the index so the hash matches. The
    previous test's protection is gone -- there is no mismatch to find.

    - **signed**: `Release.gpg` still signs the *old* Release, so apt refuses
      the repo and the payload never lands.
    - **unsigned**: apt installs the replacement without a word.

    The unsigned half is a positive control and it is the half that matters: it
    is what shows the signed half is the signature working rather than the
    tamper being broken. Both are asserted on the file in the payload, never on
    what apt printed -- a message assertion survives the bug it was written for.
    """
    good = _deb(tmp_path, tmp_path / "debs", version="1.0", payload="v1.txt")
    evil = _deb(tmp_path, tmp_path / "evil", version="1.0", payload="evil.txt")
    usb = usb_tree([good], tmp_path / "usb", app="demo-app", readme="x",
                   sign_key=KEY_UID if signed else None, gpg_home=gpg_home)
    repo = usb / "repo"

    # The attacker cannot sign, so the old signature and key are kept verbatim
    # while everything they CAN rewrite is rewritten. `write_index` unsigned
    # would delete both -- an attacker would not.
    keep = {name: (repo / name).read_bytes()
            for name in ("Release.gpg", KEYRING_NAME) if (repo / name).exists()}
    shutil.copy2(evil, repo / good.name)
    write_index(repo)
    for name, body in keep.items():
        (repo / name).write_bytes(body)

    # stdout of install.sh is dropped so `ls` below owns it; stderr is NOT.
    # Suppressing both made `"evil.txt" not in out` an assertion over an empty
    # string -- true no matter what apt did. Caught here 2026-08-08 only because
    # the BADSIG assertion was added afterwards and had nothing to match.
    script = ("set -e;" + NO_NETWORK_SOURCES +
              "bash /media/usb/install.sh >/dev/null;"
              "ls /usr/lib/demo-app")
    proc = _run(docker_image, usb, script)
    out = proc.stdout + proc.stderr

    if signed:
        assert proc.returncode != 0, (
            f"apt accepted a repo whose index was rewritten under a signature "
            f"that does not cover it\n{out}")
        assert "evil.txt" not in out, out
        # The substrate assertion above is the claim; this one pins the REASON,
        # so a rejection caused by a broken tamper rather than by the signature
        # cannot be read as evidence. Measured 2026-08-08:
        #   W: GPG error: file:/media/usb/repo ./ Release: The following
        #      signatures were invalid: BADSIG <keyid> porter test key ...
        #   E: The repository '...' is not signed.
        assert "BADSIG" in out, out
    else:
        # The control. If this half ever goes red the tamper stopped working,
        # and the signed half above is asserting nothing.
        assert proc.returncode == 0, (
            f"the unsigned tamper did not install, so the signed rejection "
            f"above is not evidence that the signature did anything\n{out}")
        assert "evil.txt" in out, (
            f"the swapped payload is not on the client: this control did not "
            f"reproduce the attack it exists to model\n{out}")


def test_a_keyring_that_does_not_match_the_signature_is_refused(
        one_deb, tmp_path, gpg_home):
    """The build-time verification, exercised directly.

    `sign_release` verifies its own output with the key it just exported, the
    way apt will. Nothing else in this file can make that check FAIL -- the
    exported key always matches, which is the point -- so it is called here with
    a second key's export instead. Without this the check is a line that has
    never been observed saying no, and a `sign_release` that exported the wrong
    key would ship a stick nobody can install from.
    """
    other = "porter second key <other@example.invalid>"
    env = {**os.environ, "GNUPGHOME": str(gpg_home)}
    made = subprocess.run(
        ["gpg", "--batch", "--yes", "--pinentry-mode", "loopback",
         "--passphrase", "", "--quick-generate-key", other,
         "default", "default", "never"],
        capture_output=True, text=True, env=env)
    assert made.returncode == 0, made.stderr

    usb = usb_tree(one_deb, tmp_path / "usb", app="demo-app", readme="x",
                   sign_key=KEY_UID, gpg_home=gpg_home)
    repo = usb / "repo"
    wrong = subprocess.run(["gpg", "--batch", "--export", other],
                           capture_output=True, env=env)
    assert wrong.returncode == 0 and len(wrong.stdout) > 100, wrong.stderr
    wrong_ring = tmp_path / "wrong.gpg"
    wrong_ring.write_bytes(wrong.stdout)

    with pytest.raises(RuntimeError, match="does not verify"):
        _verify_with_the_shipped_key(repo / "Release", repo / "Release.gpg",
                                     wrong_ring)
