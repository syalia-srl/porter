import os
import shutil
import subprocess
import pytest
import yaml
from pathlib import Path
from porter.config import env_postinst, split
from porter.deb import build_deb
from porter.interpreter import install, vendor
from porter.systemd import unit

EXAMPLE = Path(__file__).resolve().parents[1] / "examples" / "service-fastapi"


def pytest_configure(config):
    config.addinivalue_line("markers", "docker: needs a working docker daemon")
    config.addinivalue_line("markers", "nspawn: needs systemd-nspawn and root")


def _require_uv() -> None:
    """Absence of uv must not be able to report success.

    Most of this suite needs a real uv, and `pytest.skip` is the right local
    behaviour on a laptop that hasn't got one. On a gate it is the failure mode
    AGENTS.md exists to prevent: pytest exits 0 with essentially nothing
    verified. Set PORTER_REQUIRE_UV=1 wherever the run is meant to be evidence
    and the absence becomes loud instead.
    """
    if shutil.which("uv"):
        return
    if os.environ.get("PORTER_REQUIRE_UV", "") not in ("", "0"):
        pytest.fail(
            "uv is not on PATH and PORTER_REQUIRE_UV is set: this run would "
            "have skipped the tests it exists to perform.",
            pytrace=False,
        )
    pytest.skip("uv not on PATH")


@pytest.fixture
def require_uv() -> None:
    """For tests that shell out to uv without going through `vendored`."""
    _require_uv()


def _require_systemd_analyze() -> None:
    """Third of the same bargain, and the one that was missing.

    `systemd-analyze verify` is the only check in this suite that can say a unit
    directive is one systemd actually *knows*. A misspelled key is not an error
    to systemd -- it logs "Unknown key ... ignoring" and starts the service
    anyway, so `EnviromentFile` hands the service an environment with none of
    its config in it, at rc=0. A slim CI image is precisely where the binary is
    absent, so without an armed variable that check disappears exactly where it
    is needed and pytest still exits 0.

    Off by default, like the other two, so a contributor on a non-systemd host
    gets a skip rather than a wall of failures.
    """
    if shutil.which("systemd-analyze"):
        return
    if os.environ.get("PORTER_REQUIRE_SYSTEMD", "") not in ("", "0"):
        pytest.fail(
            "systemd-analyze is not on PATH and PORTER_REQUIRE_SYSTEMD is set: "
            "this run would have skipped the only check that every directive in "
            "the emitted unit is one systemd recognises.",
            pytrace=False,
        )
    pytest.skip("systemd-analyze not on PATH")


@pytest.fixture
def require_systemd_analyze() -> None:
    _require_systemd_analyze()


@pytest.fixture(scope="session")
def vendored(tmp_path_factory) -> Path:
    """One vendored interpreter for the whole session -- it is ~97 MB and a
    download, so building it per-test would make the suite unusable.

    Shared and mutable: `test_install_puts_packages_in_the_vendored_site_packages`
    installs into it. Anything that installs, or that alters the tree, should
    ask for `vendored_copy` instead.
    """
    _require_uv()
    dest = tmp_path_factory.mktemp("vendored")
    vendor(dest)
    return dest


@pytest.fixture
def vendored_copy(vendored, tmp_path) -> Path:
    """A throwaway copy of the session tree, for tests that mutate it."""
    shutil.copytree(vendored / "python", tmp_path / "python", symlinks=True)
    return tmp_path


# --- examples/service-fastapi, built as a real package -----------------------
#
# The gallery entry is the fixture. Nothing here restates the manifest: package
# name, interpreter version, requirements, ExecStart and the env template are
# all read out of examples/service-fastapi/porter.yaml, so an example that stops
# parsing or stops building takes the suite red. That is the whole reason the
# schema is defined by the gallery rather than in prose.

DOCKERFILE = """FROM debian:bookworm-slim
RUN apt-get update \\
 && apt-get install -y --no-install-recommends curl ca-certificates \\
 && rm -rf /var/lib/apt/lists/*
"""


def _require_docker() -> None:
    """Same bargain as `_require_uv`, for the same reason.

    The docker tests are the only place anything is *installed* rather than
    merely built, so if they skip quietly the suite still exits 0 having proved
    nothing about the client. PORTER_REQUIRE_DOCKER=1 turns the skip loud.
    """
    if shutil.which("docker") and subprocess.run(
            ["docker", "info"], capture_output=True).returncode == 0:
        return
    if os.environ.get("PORTER_REQUIRE_DOCKER", "") not in ("", "0"):
        pytest.fail(
            "no usable docker daemon and PORTER_REQUIRE_DOCKER is set: this run "
            "would have skipped the only tests that install the package.",
            pytrace=False,
        )
    pytest.skip("no usable docker daemon")


@pytest.fixture(scope="session")
def docker_image() -> str:
    """A client image with no Python in it.

    The positive control matters more than the image does. `--network none`
    plus a base that cannot run `python3` is what makes "the service answered"
    mean "the vendored interpreter ran": on a base that happened to ship
    python3, the same test would pass with the vendored tree broken. Assert the
    control, do not assume the base.
    """
    _require_docker()
    tag = "porter-test-client:bookworm"
    build = subprocess.run(["docker", "build", "-q", "-t", tag, "-"],
                           input=DOCKERFILE, capture_output=True, text=True)
    assert build.returncode == 0, build.stderr
    probe = subprocess.run(["docker", "run", "--rm", "--network", "none", tag,
                            "sh", "-c", "command -v python3"],
                           capture_output=True, text=True)
    assert probe.returncode != 0, (
        f"{tag} ships a system python3 at {probe.stdout.strip()!r}: the e2e test "
        "would then pass without the vendored interpreter being exercised at all"
    )
    return tag


@pytest.fixture(scope="session")
def demo_manifest() -> dict:
    return yaml.safe_load((EXAMPLE / "porter.yaml").read_text())


@pytest.fixture(scope="session")
def demo_stage(tmp_path_factory, demo_manifest) -> Path:
    """Everything in the example's package except the version-specific config.

    Session-scoped because vendoring plus `uv pip install` is a 97 MB tree
    (measured on zion 2026-08-08);
    the two .debs are built from this one stage, in sequence, which is also how
    a real v_prev/v_next pair is produced.
    """
    _require_uv()
    m = demo_manifest
    pkg, pyver = m["package"], m["python"]["version"]
    stage = tmp_path_factory.mktemp("demo") / "stage"

    libdir = stage / "usr/lib" / pkg
    libdir.mkdir(parents=True)
    python_bin = vendor(libdir, pyver)
    install(python_bin, m["requirements"])
    shutil.copy(EXAMPLE / "src/app.py", libdir / "app.py")

    # The path as it will be ON THE CLIENT, not the staging path: an ExecStart
    # naming a tmp_path is the exact class of bug rule 1 is about.
    installed = f"/usr/lib/{pkg}/python/bin/python{pyver}"
    exec_start = " ".join([installed, "-m", m["exec"]["module"], *m["exec"]["args"]])
    units = stage / "usr/lib/systemd/system"
    units.mkdir(parents=True)
    (units / f"{pkg}.service").write_text(
        unit(pkg, m["description"], exec_start, f"/usr/lib/{pkg}"))

    (stage / "etc" / pkg).mkdir(parents=True)
    (stage / "usr/share" / pkg).mkdir(parents=True)
    return stage


def _package(stage: Path, m: dict, out_dir: Path, *, version: str, env: dict) -> Path:
    """Write the split config into the stage and build the .deb.

    `env` is passed in rather than read from the manifest so the upgrade test
    can build a v2 whose package-owned values differ -- which is the only way
    to exercise "a new key reaches a client that has configured itself".
    """
    pkg = m["package"]
    defaults, env_example = split(env, m["admin_keys"])
    (stage / "etc" / pkg / "defaults").write_text(defaults)
    # env_postinst copies this into /etc/<pkg>/env on first configure. It is
    # payload under /usr/share, never under /etc: /etc/<pkg>/env in the .deb is
    # refused by deb.py's lint, and rightly.
    (stage / "usr/share" / pkg / "env.example").write_text(env_example)
    control = {
        "Package": pkg,
        "Version": version,
        "Architecture": m["architecture"],
        "Maintainer": m["maintainer"],
        "Description": m["description"],
    }
    return build_deb(stage, control, out_dir,
                     conffiles=[f"/etc/{pkg}/defaults"],
                     scripts={"postinst": env_postinst(pkg)})


@pytest.fixture(scope="session")
def built_demo_deb(demo_stage, demo_manifest) -> Path:
    """v1 of the example, alone in its own directory -- the tests mount that
    directory and install `/debs/*.deb`, so a second package next to it would
    be installed too."""
    out = demo_stage.parent / "debs-v1"
    return _package(demo_stage, demo_manifest, out,
                    version=demo_manifest["version"], env=demo_manifest["env"])


@pytest.fixture(scope="session")
def built_demo_deb_v2(built_demo_deb, demo_stage, demo_manifest) -> Path:
    """v2: same payload, corrected package-owned config.

    TUNING changes and DB_TDS_VERSION appears -- the case the design is built
    around, a value we get wrong and fix later. Depends on `built_demo_deb`
    because both write config into the same stage, and v1 must be a .deb before
    v2's values overwrite it.
    """
    env = {**demo_manifest["env"], "TUNING": "from-defaults-v2", "DB_TDS_VERSION": "7.0"}
    out = demo_stage.parent / "debs-v2"
    return _package(demo_stage, demo_manifest, out, version="1.1", env=env)
