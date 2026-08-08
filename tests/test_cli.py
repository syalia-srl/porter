"""`porter build` — the entry point, exercised the way a user reaches it.

The only surface a user touches, and until this file the only surface with no
test. It did not run: `uv run porter build examples/service-fastapi/porter.yaml`
raised `FileNotFoundError` on porter's own example, left 97 MB in `build/`, and
then failed the second time with a *different* error.

Every test in `test_assemble.py` hands `assemble` an absolute
`tmp_path`, and that is precisely why the suite was blind: `porter build`'s own
`--out`/`--stage` defaults are the **relative** `dist` and `build`, and a
relative interpreter path handed to a subprocess with `cwd=` is resolved against
the new directory. So these tests run the installed console script, in a
scratch cwd, with every path they pass relative to it -- and run it **twice**,
because a command that works exactly once is not a working command.

The .deb is asserted on for magnitude, not presence: a 30 KB stub passed every
path assertion in this repo once.
"""
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml
from conftest import EXAMPLE, _require_uv


@pytest.fixture(scope="session")
def porter_cli() -> str:
    """The installed console script, not an in-process call into `cli.build`.

    `pyproject.toml` maps `porter = "porter.cli:main"`, and that mapping, the
    argument parser microcli builds from the signature, and the defaults in it
    are all part of what broke. Loud rather than skipped, for the reason
    `_require_uv` is loud: a run that quietly declined to test the entry point
    is the failure this file exists to end.
    """
    exe = shutil.which("porter")
    if exe is None:
        pytest.fail(
            "the `porter` console script is not on PATH, so this run would have "
            "verified the entry point by not invoking it. Use "
            "`uv run --extra dev pytest`.",
            pytrace=False,
        )
    return exe


def _example_in(work: Path) -> Path:
    """A copy of the gallery entry, so a build never writes into the repo.

    Copied rather than restated: package name, version, interpreter,
    requirements and exec line stay the example's, so an example that stops
    building takes these tests red too.
    """
    shutil.copytree(EXAMPLE, work / "example")
    return work / "example/porter.yaml"


def _build(exe: str, work: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run([exe, "build", *args], cwd=work,
                          capture_output=True, text=True)


def test_porter_build_runs_twice_from_a_cwd_where_its_defaults_are_relative(
        porter_cli, demo_manifest, tmp_path):
    """The default invocation, and then the same one again.

    Every argument is relative to the cwd, which is the whole point: with
    `stage_root` left relative, `assemble`'s import probe execs
    `build/<pkg>/usr/lib/<pkg>/python/bin/python3.12` with `cwd=` set to a
    directory below it, the child chdir's before exec, and the interpreter it
    just vendored cannot be found.

    The second run is the other half. `assemble` refuses a non-empty stage --
    correctly, for a tree a caller pre-staged -- so a CLI that leaves its own
    stage behind succeeds exactly once and then reports a refusal about a
    directory the user never created.
    """
    _require_uv()
    pkg = demo_manifest["package"]
    _example_in(tmp_path)

    first = _build(porter_cli, tmp_path, "example/porter.yaml")
    assert first.returncode == 0, first.stderr

    debs = sorted((tmp_path / "dist").glob("*.deb"))
    assert [d.name for d in debs] == [f"{pkg}_{demo_manifest['version']}_"
                                      f"{demo_manifest['architecture']}.deb"]
    assert debs[0].stat().st_size > 20_000_000, (
        f"{debs[0]} is only {debs[0].stat().st_size} bytes: a vendored "
        "interpreter under -Znone cannot be small, so this is a truncated build")

    # Scratch, and porter's own: it created this tree and it removes it. The
    # 97 MB left here is what turned the obvious retry into a second, unrelated
    # error message.
    assert not (tmp_path / "build" / pkg).exists()

    second = _build(porter_cli, tmp_path, "example/porter.yaml")
    assert second.returncode == 0, second.stderr
    debs = sorted((tmp_path / "dist").glob("*.deb"))
    assert len(debs) == 1 and debs[0].stat().st_size > 20_000_000


def test_a_failed_build_fails_the_same_way_the_second_time(
        porter_cli, demo_manifest, tmp_path):
    """A failure must be repeatable, or the message stops being diagnostic.

    `requirements` dropped from the manifest is the airgap mistake the import
    probe exists for, and it is refused *after* the interpreter is vendored --
    so the failing run is the one that leaves 97 MB behind. The user's next
    move is to run it again; before this, that second run met the non-empty
    stage refusal and said nothing about the missing requirement at all.
    """
    _require_uv()
    pkg = demo_manifest["package"]
    manifest = _example_in(tmp_path)
    manifest.write_text(yaml.safe_dump(
        {k: v for k, v in demo_manifest.items() if k != "requirements"}))

    first = _build(porter_cli, tmp_path, "example/porter.yaml")
    assert first.returncode != 0
    assert "cannot import" in first.stderr, first.stderr
    assert not (tmp_path / "build" / pkg).exists()
    assert list((tmp_path / "build").iterdir()) == []

    second = _build(porter_cli, tmp_path, "example/porter.yaml")
    assert second.returncode != 0
    assert "cannot import" in second.stderr, second.stderr
    assert "not empty" not in second.stderr, (
        "the retry met a refusal about the previous run's leftovers instead of "
        "the error it was going to report: " + second.stderr)


def test_build_help_documents_the_flags_it_accepts(porter_cli):
    """`--out` and `--stage` work and were absent from `--help` entirely.

    microcli reads a flag's help from `Annotated[str, "..."]` and registers an
    optional with `help=argparse.SUPPRESS` when that is empty, so documenting
    them in docstring prose -- which is what porter did -- produced a usage line
    reading `porter build [-h] manifest` with a description beneath it
    explaining two flags nothing in the usage mentions.

    The prose is gone, so these substrings can only come from the annotations.
    """
    proc = subprocess.run([porter_cli, "build", "--help"],
                          capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr
    # argparse hard-wraps the help column, so a phrase can arrive with a
    # newline and eighteen spaces through the middle of it.
    helptext = " ".join(proc.stdout.split())
    assert "--out" in helptext and "--stage" in helptext
    assert "directory the .deb is written to" in helptext
    assert "porter removes the tree it creates there" in helptext
