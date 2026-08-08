"""Migrations, and the `<pkg>-setup` wizard, without a container.

What is proved here is the *decision*: given the version dpkg hands a postinst
as `$2`, does the migration run or not. The block is generated and then
executed, so what is asserted is a file appearing on disk rather than a
substring appearing in a string -- a source-grep would survive the day someone
inverts the comparison.

What is NOT proved here is that dpkg passes `$2` at all, that a failing
migration leaves the package unconfigured, or that the wizard writes
`/etc/<pkg>/env` at 600 root:root. Those are claims about dpkg and about a real
root filesystem, and they live in tests/test_migrate_e2e.py where there is one.
"""
import subprocess
from dataclasses import replace
from pathlib import Path

import pytest
import yaml

from porter.config import env_postinst, setup_script
from porter.migrate import Migration, migration_postinst
from porter.types import Component

EXAMPLE = Path(__file__).resolve().parents[1] / "examples" / "stateful-service"

# The migration body used by the matrix below: it records every run, so "did it
# run" and "how many times" are the same question asked of the same artefact.
RECORD = 'echo ran >> "$WITNESS"'


def _configure(tmp_path, migrations, previous_version: str, script_body=RECORD):
    """Generate the block, wrap it as dpkg wraps it, run it, return (rc, runs).

    The wrapper is `if [ "$1" = configure ]` and nothing else, which is exactly
    the context `env_postinst` splices the block into -- minus the parts (the
    static user, /etc, systemctl) that need root and say nothing about when a
    migration fires.
    """
    witness = tmp_path / "witness"
    # Removed per call, not per test: a test that runs this twice is counting
    # the runs of the second invocation, and a witness carried over from the
    # first reports a migration firing that never did.
    witness.unlink(missing_ok=True)
    block = migration_postinst("demo-app", [
        Migration(before_version=v, script=script_body) for v in migrations])
    postinst = tmp_path / "postinst"
    postinst.write_text(
        '#!/bin/sh\nset -e\nif [ "$1" = configure ]; then\n' + block + 'fi\nexit 0\n')
    postinst.chmod(0o755)
    proc = subprocess.run([str(postinst), "configure", previous_version],
                          capture_output=True, text=True,
                          env={"PATH": "/usr/bin:/bin", "WITNESS": str(witness)})
    runs = witness.read_text().count("ran") if witness.exists() else 0
    return proc, runs


def test_a_fresh_install_runs_no_migration(tmp_path):
    """dpkg leaves `$2` empty when there is no previously-configured version.

    This is the whole reason the module exists. ainbox's installer runs its
    beaver migration `ALWAYS, not just on update` because a hand-invoked shell
    script cannot tell the two apart; a postinst can, and a migration that fires
    here runs against a database that does not exist yet.
    """
    proc, runs = _configure(tmp_path, ["1.10"], "")
    assert proc.returncode == 0, proc.stderr
    assert runs == 0, "the migration ran on a fresh install"


def test_an_upgrade_from_below_runs_the_migration_exactly_once(tmp_path):
    proc, runs = _configure(tmp_path, ["1.10"], "1.9")
    assert proc.returncode == 0, proc.stderr
    assert runs == 1, f"expected exactly one run, got {runs}"


def test_an_upgrade_from_at_or_above_the_before_version_does_not_run_it(tmp_path):
    """1.10 -> 1.11 with a migration for < 1.10: the client already has it."""
    proc, runs = _configure(tmp_path, ["1.10"], "1.10")
    assert (proc.returncode, runs) == (0, 0), (proc.stderr, runs)
    proc, runs = _configure(tmp_path, ["1.10"], "1.11")
    assert (proc.returncode, runs) == (0, 0), (proc.stderr, runs)


def test_a_reinstall_of_the_same_version_does_not_run_it(tmp_path):
    """dpkg passes the same version as `$2` when a package is re-installed over
    itself, which is the ordinary way an operator retries a failed install."""
    proc, runs = _configure(tmp_path, ["1.10"], "1.10")
    assert (proc.returncode, runs) == (0, 0), (proc.stderr, runs)


def test_the_shell_would_have_ordered_this_pair_wrong(tmp_path):
    """The positive control for every version number in this file.

    `dpkg --compare-versions` is not ceremony over `[ "$2" \\< "X" ]`: for the
    pair the suite uses, the shell is *wrong*. '1.9' sorts after '1.10'
    lexicographically, so a string comparison would decide a real 1.9 -> 1.10
    upgrade needs no migration and exit 0. Without this assertion, the tests
    above would pass just as happily on a string comparison and would be
    measuring nothing.
    """
    shell = subprocess.run(["sh", "-c", '[ "1.9" \\< "1.10" ]'])
    assert shell.returncode != 0, (
        "the shell now orders 1.9 before 1.10, so this suite's version numbers no "
        "longer discriminate between a string comparison and dpkg's")
    dpkg = subprocess.run(["dpkg", "--compare-versions", "1.9", "lt", "1.10"])
    assert dpkg.returncode == 0, "dpkg does not order 1.9 below 1.10"


def test_a_failing_migration_fails_the_script(tmp_path):
    """No `|| true` anywhere in the block: the postinst's `set -e` must see it.

    The e2e asserts the consequence -- dpkg leaves the package half-configured.
    This asserts the mechanism, which is that a non-zero migration produces a
    non-zero script.
    """
    proc, _ = _configure(tmp_path, ["1.10"], "1.9", script_body="exit 3")
    assert proc.returncode != 0, (
        f"a migration exiting 3 left the postinst at rc=0:\n{proc.stdout}")


def test_a_migration_that_exits_zero_does_not_skip_the_rest_of_the_postinst(tmp_path):
    """Each script runs in a subshell, so `exit 0` inside one is contained.

    Without the subshell an `exit 0` -- an entirely reasonable last line for a
    standalone script someone pasted in -- would end the postinst there, before
    `systemctl enable`. The install exits 0 and the service is not wired to any
    target: silent success, on the path an unattended client depends on.
    """
    witness = tmp_path / "witness"
    block = migration_postinst("demo-app", [Migration("1.10", "exit 0")])
    postinst = tmp_path / "postinst"
    postinst.write_text(
        '#!/bin/sh\nset -e\nif [ "$1" = configure ]; then\n' + block +
        '  echo reached >> "$WITNESS"\nfi\nexit 0\n')
    postinst.chmod(0o755)
    proc = subprocess.run([str(postinst), "configure", "1.9"],
                          capture_output=True, text=True,
                          env={"PATH": "/usr/bin:/bin", "WITNESS": str(witness)})
    assert proc.returncode == 0, proc.stderr
    assert witness.exists(), (
        "a migration ending in `exit 0` ended the postinst with it: everything "
        "after the migration block -- systemctl enable included -- was skipped")


def test_two_migrations_each_fire_on_their_own_transition(tmp_path):
    """A client that skipped a release gets both, in declaration order."""
    proc, runs = _configure(tmp_path, ["1.9", "1.10"], "1.8")
    assert (proc.returncode, runs) == (0, 2), (proc.stderr, runs)
    proc, runs = _configure(tmp_path, ["1.9", "1.10"], "1.9")
    assert (proc.returncode, runs) == (0, 1), (proc.stderr, runs)


def test_a_before_version_dpkg_cannot_parse_is_refused_at_build_time(tmp_path):
    """`dpkg --compare-versions` exits 1 for false and 1 for malformed alike.

    So a `v2.0` in the manifest cannot be detected at run time at all: every
    client installs at rc=0 and migrates nothing, forever, with the only
    complaint on a stderr nobody reads. Refusing at build time is the only place
    the mistake is visible.
    """
    with pytest.raises(ValueError, match="dpkg can parse"):
        migration_postinst("demo-app", [Migration("v2.0", "true")])


def test_a_multi_line_migration_script_still_parses_as_a_postinst():
    """The example's own migration is a YAML block scalar with a continuation.

    Splicing arbitrary text into a generated script is how an unrunnable
    maintainer script ships: dpkg reports `subprocess installed post-installation
    script returned error exit status 2` on the client and nothing on the build
    host ever said a word.
    """
    manifest = yaml.safe_load((EXAMPLE / "porter.yaml").read_text())
    component, _ = Component.from_manifest(manifest)
    text = env_postinst(component.package, migrations=component.migrations,
                        has_setup=True)
    assert "migrate_v2.py" in text
    parsed = subprocess.run(["sh", "-n", "-"], input=text,
                            capture_output=True, text=True)
    assert parsed.returncode == 0, f"the emitted postinst does not parse:\n{parsed.stderr}"


def test_a_postinst_without_migrations_is_what_it_always_was():
    """The block is empty when there is nothing to run, so every package built
    before this module existed emits the same bytes it did then."""
    assert env_postinst("demo-app") == env_postinst("demo-app", migrations=[])
    assert "compare-versions" not in env_postinst("demo-app")


# --- <pkg>-setup -------------------------------------------------------------


def _example_component() -> Component:
    manifest = yaml.safe_load((EXAMPLE / "porter.yaml").read_text())
    return Component.from_manifest(manifest)[0]


def test_the_setup_script_parses_and_asks_about_every_admin_key():
    component = _example_component()
    text = setup_script(component)
    parsed = subprocess.run(["sh", "-n", "-"], input=text,
                            capture_output=True, text=True)
    assert parsed.returncode == 0, f"{parsed.stderr}\n{text}"
    assert "read new" in text
    for key in component.admin_keys:
        assert f"{key} [%s]" in text, (
            f"{key} is admin-owned and the wizard never asks about it")


def test_a_component_with_no_admin_keys_gets_no_wizard():
    """A wizard that prompts for nothing would still rewrite the admin's file.

    Refused rather than emitted-and-empty: `assemble` stages the script only
    when there are admin keys, and the two predicates are deliberately the same
    one written twice -- the failure they stop is a package shipping a
    `<pkg>-setup` that asks nothing and truncates /etc/<pkg>/env on its way out.
    """
    component = _example_component()
    with pytest.raises(ValueError, match="nothing to"):
        setup_script(replace(component, admin_keys=[]))


def test_the_postinst_points_at_the_wizard_only_when_one_is_shipped():
    """An airgapped operator has no documentation but the install's own output.

    And a hint naming a command that is not installed is worse than none, so the
    line is tied to the same condition that stages the script.
    """
    assert "stateful-demo-setup" in env_postinst("stateful-demo", has_setup=True)
    assert "-setup" not in env_postinst("stateful-demo")


def test_both_example_manifests_parse_and_differ_only_where_the_upgrade_does():
    """The gallery defines the schema, so an example that stops parsing is a
    schema that stopped existing. The pair is also the fixture the e2e builds:
    if these two stop differing in version, payload or migrations, the upgrade
    test would be measuring an upgrade to an identical package.
    """
    current, _ = Component.from_manifest(
        yaml.safe_load((EXAMPLE / "porter.yaml").read_text()))
    previous, _ = Component.from_manifest(
        yaml.safe_load((EXAMPLE / "porter-previous.yaml").read_text()))

    assert (previous.version, current.version) == ("1.9", "1.10")
    assert previous.migrations == []
    assert [m.before_version for m in current.migrations] == ["1.10"]
    assert previous.module != current.module
    assert previous.package == current.package  # or dpkg would not call it an upgrade
