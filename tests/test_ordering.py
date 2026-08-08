"""Multi-service ordering: the directives, the refusals, and convergence.

`depends_on` with `condition: service_healthy` has no systemd equivalent. What
porter emits instead is `After=` + `Requires=` + the dependent's own
`Restart=on-failure`, and the last of those is the part that cannot be verified
by reading a file: it is a claim about what systemd *does* over several
seconds. So the interesting test in here boots one.

Two failure classes this file is written against, both of them silent:

- **A directive that was never emitted.** `systemd-analyze verify` reports keys
  it does not recognise and never keys that are missing -- measured here on
  systemd 259, deleting `ProtectSystem=strict` leaves it perfectly clean. So
  every assertion on a directive is direct and exact-line, never delegated.
- **A graph porter accepted and systemd will not.** A misspelled `after:` and a
  cycle both produce a package that builds, lints and installs at rc=0; the
  first fails its start job on the client, the second is silently reordered by
  systemd ("Job ... deleted to break ordering cycle") and boots in whatever
  order systemd picked.
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest
import yaml
import nspawn_support as nspawn
from conftest import _require_uv

from porter.assemble import assemble
from porter.systemd import resolve_ordering, unit
from porter.types import Component, Python

EXAMPLE = Path(__file__).resolve().parents[1] / "examples" / "multi-service"
PY = Python(version="3.12")


def _component(name: str, **over) -> Component:
    """A minimal service component. Ordering needs several of these and none of
    them needs a payload: every assertion below is on the emitted unit or on the
    refusal, and vendoring an interpreter to reach either would be 87 MB spent
    on nothing."""
    base = dict(name=name, package=f"demo-{name}", description=f"demo {name}",
                kind="service", module=name)
    base.update(over)
    return Component(**base)


def _sections(body: str) -> dict[str, list[str]]:
    """Unit file -> {section: [lines]}. Placement is half of each directive's
    meaning: `Requires=` in `[Service]` is a key systemd does not know there,
    and it would be logged and ignored while the unit started perfectly."""
    out: dict[str, list[str]] = {}
    current = None
    for line in body.splitlines():
        if line.startswith("[") and line.endswith("]"):
            current = line[1:-1]
            out[current] = []
        elif line and current is not None:
            out[current].append(line)
    return out


# --- the directives ----------------------------------------------------------

def test_a_dependent_orders_against_the_packages_unit_not_the_component_name():
    """`after: [alpha]` must become `demo-alpha.service`, never `alpha`.

    The manifest is written in the author's vocabulary and systemd understands
    only its own. A unit emitting `Requires=alpha` is not a build error and not
    a lint error: systemd accepts a dependency on a unit that does not exist and
    fails the start *job*, on the client, at boot, with the package having
    installed at rc=0. So the translation is asserted in both directions --
    the unit names are present, and the bare component names are absent.
    """
    components = [_component("alpha"), _component("beta"),
                  _component("gateway", after=["alpha", "beta"])]
    order = resolve_ordering(components)
    assert order["gateway"] == ["demo-alpha.service", "demo-beta.service"]

    body = unit("demo-gateway", "gw", "/bin/true", "/tmp",
                depends_on=order["gateway"])
    section = _sections(body)["Unit"]
    assert "After=demo-alpha.service demo-beta.service" in section, body
    assert "Requires=demo-alpha.service demo-beta.service" in section, body
    # Both, and in [Unit]. Neither directive alone is the behaviour porter
    # promises: After= orders without coupling (the dependent starts happily
    # when the dependency was never started at all), Requires= couples without
    # ordering (systemd starts both at once and the dependent races).
    for stray in ("After=alpha", "Requires=alpha", "After=beta", "Requires=beta"):
        assert stray + "\n" not in body + "\n", (
            f"{stray!r} names a component, not a unit; systemd would accept it "
            f"and fail the start job on the client:\n{body}")


def test_the_base_target_survives_the_projects_own_ordering():
    """`After=network.target` is not replaced by the graph, it is joined by it.

    A gateway that no longer waits for the network is a gateway that binds
    before an address exists -- and it would still pass every assertion about
    demo-alpha.service.
    """
    body = unit("demo-gateway", "gw", "/bin/true", "/tmp",
                depends_on=["demo-alpha.service"])
    assert "After=network.target" in _sections(body)["Unit"], body


def test_a_single_component_project_emits_no_ordering_directives():
    """No ceremony where none is needed.

    An empty `Requires=` is not inert -- systemd parses it, and a reader has to
    work out whether it means "nothing" or "someone deleted the value". A
    project with one component gets exactly the unit it got before this task.
    """
    body = unit("demo-app", "demo", "/bin/true", "/tmp")
    assert "Requires=" not in body, body
    assert [line for line in _sections(body)["Unit"] if line.startswith("After=")] \
        == ["After=network.target"], body


def test_ordering_survives_the_round_trip_through_a_staged_unit(tmp_path, src_tree):
    """The assertion that matters is on the file in the stage, not on a string.

    `unit()` returning the right text and `assemble` writing the right file are
    two facts, and porter's characteristic bug lives in the gap: a component
    whose ordering is resolved, formatted, and then never passed to the emitter
    stages a package that installs at rc=0 with its dependencies gone.
    """
    # `src/hello.py`, whose imports are all stdlib: this test is about the unit
    # file, and the gallery's FastAPI payload would spend a `uv pip install` on
    # proving nothing extra.
    alpha = _component("alpha", module="hello", source_paths=["src/hello.py"])
    gateway = _component("gateway", module="hello", source_paths=["src/hello.py"],
                         after=["alpha"])
    staged = assemble(gateway, PY, src_tree, tmp_path / "stage",
                      siblings=[alpha, gateway])
    body = (staged.stage / "usr/lib/systemd/system/demo-gateway.service").read_text()
    assert "Requires=demo-alpha.service" in _sections(body)["Unit"], body


# --- the refusals ------------------------------------------------------------

def test_an_after_naming_an_undeclared_component_is_refused():
    with pytest.raises(ValueError, match="alpah") as exc:
        resolve_ordering([_component("alpha"),
                          _component("gateway", after=["alpah"])])
    # The declared names are in the message: a typo is found by seeing the two
    # spellings next to each other, and the author is the only one who can.
    assert "alpha" in str(exc.value) and "gateway" in str(exc.value)


def test_a_cycle_is_refused_with_both_units_named():
    """systemd's own handling is the reason this is a refusal.

    It detects the cycle at load time and breaks it by deleting one of the jobs
    -- "Found ordering cycle... Job demo-beta.service/start deleted to break
    ordering cycle" -- and then boots. So the client gets an order, chosen by
    systemd, with nothing failing anywhere. The author is the only one who can
    say which edge was wrong, and build time is the only place they are present.
    """
    with pytest.raises(ValueError, match="cycle") as exc:
        resolve_ordering([_component("alpha", after=["beta"]),
                          _component("beta", after=["alpha"])])
    message = str(exc.value)
    assert "demo-alpha.service" in message and "demo-beta.service" in message, message


def test_a_component_that_depends_on_itself_is_refused():
    """A cycle of one. systemd drops the self-edge and says nothing at all."""
    with pytest.raises(ValueError, match="cycle") as exc:
        resolve_ordering([_component("alpha", after=["alpha"])])
    assert "demo-alpha.service" in str(exc.value)


def test_two_components_with_one_name_are_refused():
    """`after:` names a component, so a duplicated name makes every reference to
    it ambiguous -- and the dict-based resolution would silently pick the last
    one, which is a package chosen by file order."""
    with pytest.raises(ValueError, match="more than once"):
        resolve_ordering([_component("alpha"),
                          _component("alpha", package="demo-other")])


def test_assemble_refuses_ordering_it_was_given_no_siblings_to_resolve(
        tmp_path, src_tree):
    """The caller cannot drop the graph on the floor.

    `assemble` takes the sibling list rather than a pre-resolved list of unit
    names precisely so that omitting it is loud: with no siblings, a component
    declaring `after:` names something its (one-entry) manifest does not
    provide, which is the refusal above. Passing resolved names instead would
    make the same mistake produce a package with no ordering, at rc=0.
    """
    gateway = _component("gateway", module="app", source_paths=["src/app.py"],
                         after=["alpha"])
    with pytest.raises(ValueError, match="alpha"):
        assemble(gateway, PY, src_tree, tmp_path / "stage")


def test_a_command_may_not_declare_after(tmp_path, src_tree):
    """A wrapper in /usr/bin is not a unit. There is nothing for systemd to
    order, so the declaration would be read and dropped -- and the manifest
    would go on saying otherwise."""
    cmd = Component(name="tool", package="demo-tool", description="t",
                    kind="command", module="hello", bin_name="hello",
                    source_paths=["src/hello.py"], after=["alpha"])
    with pytest.raises(ValueError, match="after"):
        assemble(cmd, PY, src_tree, tmp_path / "stage",
                 siblings=[cmd, _component("alpha")])


# --- systemd's own opinion of the emitted unit -------------------------------

def _verify(tmp_path: Path, body: str) -> str:
    path = tmp_path / "porter-ordered.service"
    path.write_text(body)
    proc = subprocess.run(["systemd-analyze", "verify", str(path)],
                          capture_output=True, text=True, cwd=tmp_path)
    return proc.stdout + proc.stderr


def test_the_ordered_unit_carries_no_directive_systemd_would_ignore(
        tmp_path, require_systemd_analyze):
    """`Requires=` and `After=` spelled the way systemd spells them.

    The positive control is the point, and it is aimed at this test's own
    subject: prove the probe reports a misspelled *ordering* key before
    trusting it to report a clean one. A unit with `Require=` is not an error
    to systemd -- it logs the unknown key and starts the service with no
    dependency at all.
    """
    good = unit("demo-gateway", "gw", "/bin/true", "/tmp",
                depends_on=["demo-alpha.service", "demo-beta.service"])
    assert "Unknown key" not in _verify(tmp_path, good), _verify(tmp_path, good)

    bad = good.replace("Requires=", "Require=")
    out = _verify(tmp_path, bad)
    assert "Unknown key" in out and "Require" in out, (
        f"systemd-analyze did not report an ordering key it does not know: this "
        f"probe cannot detect the failure it exists to detect. Got: {out!r}")


# --- the gallery entry, built ------------------------------------------------

def _dpkg_contents(deb: Path) -> str:
    proc = subprocess.run(["dpkg-deb", "--contents", str(deb)],
                          capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr
    return proc.stdout


def _unit_from_deb(deb: Path, dest: Path, name: str) -> str:
    """Read a unit out of the BUILT PACKAGE, not out of the stage.

    The stage is what porter wrote; the .deb is what the client gets, and the
    two have been different before -- deb.py's lint can refuse a path, and a
    file that never made it into the archive is exactly the kind of absence
    that reads as success everywhere upstream of it.
    """
    dest.mkdir(parents=True, exist_ok=True)
    proc = subprocess.run(["dpkg-deb", "-x", str(deb), str(dest)],
                          capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr
    return (dest / "usr/lib/systemd/system" / name).read_text()


@pytest.fixture(scope="module")
def built_example(tmp_path_factory) -> tuple[Path, dict]:
    """`porter build examples/multi-service/porter.yaml`, run as a user would.

    Through the CLI and not through `assemble`, because the multi-component
    path IS the CLI's: a manifest that only a test can build is not a gallery
    entry. Run from a scratch cwd with relative paths, which is the shape
    tests/test_cli.py established after `porter build` turned out never to have
    been run on its own defaults.
    """
    _require_uv()
    work = tmp_path_factory.mktemp("multi-service")
    proc = subprocess.run(
        ["uv", "run", "porter", "build", str(EXAMPLE / "porter.yaml"),
         "--out", "dist", "--stage", "build"],
        cwd=work, capture_output=True, text=True,
        env={**os.environ, "UV_PROJECT": str(Path(__file__).resolve().parents[1])})
    assert proc.returncode == 0, f"{proc.stdout}\n{proc.stderr}"
    return work / "dist", yaml.safe_load((EXAMPLE / "porter.yaml").read_text())


def test_the_multi_service_example_builds_one_package_per_component(built_example):
    dist, manifest = built_example
    built = sorted(p.name for p in dist.glob("*.deb"))
    expected = sorted(f"{c['package']}_{manifest['version']}_"
                      f"{manifest['architecture']}.deb"
                      for c in manifest["components"])
    assert built == expected, f"built {built}, manifest declares {expected}"
    # Magnitude, not existence: a stubbed interpreter produced a 30,912-byte
    # package here once with every path assertion still passing.
    for deb in dist.glob("*.deb"):
        assert deb.stat().st_size > 20 * 1024 * 1024, (
            f"{deb.name} is {deb.stat().st_size} bytes: too small to contain a "
            "vendored interpreter, so the payload assertions below would pass "
            "against a package that cannot run")


def test_the_built_gateway_package_carries_both_dependencies(built_example, tmp_path):
    """The artefact assertion for the whole task: the ordering is in the .deb.

    Read out of the built package, and exact-line, so a `Requires=` that lost
    one of its two entries fails here. Truncation to the first element is the
    likeliest wrong version of this code and it is invisible to any test that
    only checks `"demo-alpha.service" in body`.
    """
    dist, _ = built_example
    body = _unit_from_deb(dist / "demo-gateway_1.0_amd64.deb", tmp_path / "x",
                          "demo-gateway.service")
    section = _sections(body)["Unit"]
    assert "After=demo-alpha.service demo-beta.service" in section, body
    assert "Requires=demo-alpha.service demo-beta.service" in section, body
    assert "Restart=on-failure" in _sections(body)["Service"], (
        "without it the gateway's first failed start against a warming upstream "
        f"is its last, and the suite never converges:\n{body}")


def test_the_upstream_packages_carry_no_ordering_at_all(built_example, tmp_path):
    """The control for the test above: `alpha` declares no `after:`, so its unit
    must be exactly the unit a single-component project gets. Without this, an
    emitter that put every component's dependencies on every component's unit
    would pass every assertion above."""
    dist, _ = built_example
    for pkg in ("demo-alpha", "demo-beta"):
        body = _unit_from_deb(dist / f"{pkg}_1.0_amd64.deb", tmp_path / pkg,
                              f"{pkg}.service")
        assert "Requires=" not in body, body
        assert [line for line in _sections(body)["Unit"]
                if line.startswith("After=")] == ["After=network.target"], body


# --- convergence, under real systemd -----------------------------------------
#
# Everything above reads files. This boots one.

CONVERGENCE_MANIFEST = {
    "version": "1.0",
    "maintainer": "porter <porter@example.com>",
    "architecture": "amd64",
    "python": {"version": "3.12", "package": "bundled"},
    "components": [
        {"name": "dep", "package": "porter-dep", "kind": "service",
         "description": "a dependency that fails its first two starts",
         "source": ["src/dep.py"], "exec": {"module": "dep"},
         "env": {"PORT": "9301", "FAIL_FIRST_STARTS": "2"}},
        {"name": "dependent", "package": "porter-dependent", "kind": "service",
         "description": "a service that needs the dependency",
         "after": ["dep"],
         "source": ["src/dependent.py"], "exec": {"module": "dependent"},
         "env": {"DEP_URL": "http://127.0.0.1:9301/"}},
    ],
}

# Counted in the unit's StateDirectory, which systemd creates and exports, so
# the count survives the restarts -- a counter in the process would reset with
# it and the service would fail forever.
DEP_PY = '''\
import os, pathlib, sys
from http.server import BaseHTTPRequestHandler, HTTPServer

state = pathlib.Path(os.environ["STATE_DIRECTORY"])
counter = state / "starts"
n = int(counter.read_text()) + 1 if counter.exists() else 1
counter.write_text(str(n))
if n <= int(os.environ["FAIL_FIRST_STARTS"]):
    print(f"dep: start {n} failing on purpose", file=sys.stderr, flush=True)
    raise SystemExit(1)


class H(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200); self.end_headers(); self.wfile.write(b"dep\\n")

    def log_message(self, *a):
        pass


print(f"dep: start {n} serving", file=sys.stderr, flush=True)
HTTPServer(("127.0.0.1", int(os.environ["PORT"])), H).serve_forever()
'''

# The marker is the whole reason this payload writes a file. `systemctl
# is-active` cannot answer "has it converged?": on Type=simple systemd reports
# a unit active the moment it forks, so the FIRST measured run of this test saw
# `dep=active dependent=active` at t=0 with the dependency's own log already
# saying "start 1 failing on purpose" -- both units nominally up, nothing
# converged, and only the NRestarts control caught it. A file on disk is
# written after the upstream actually answered.
DEPENDENT_PY = '''\
import os, pathlib, sys, time, urllib.request

url = os.environ["DEP_URL"]
try:
    with urllib.request.urlopen(url, timeout=2) as r:
        assert r.status == 200
except Exception as exc:
    print(f"dependent: {url} not ready ({exc}); exiting to retry",
          file=sys.stderr, flush=True)
    raise SystemExit(1)
marker = pathlib.Path(os.environ["STATE_DIRECTORY"]) / "converged"
marker.write_text(f"reached {url}\\n")
print("dependent: converged", file=sys.stderr, flush=True)
while True:
    time.sleep(3600)
'''

# Runs at multi-user.target inside the container, installs the two packages,
# starts the dependent and records what systemd does. `exec >` first so a
# failure anywhere below is readable from outside; the poweroff at the end is
# what returns control to the test.
PROBE_SH = '''\
#!/bin/sh
exec >/out/probe.log 2>&1
set -x
dpkg -i /debs/*.deb; echo "DPKG_RC=$?" > /out/dpkg-rc
# NOTHING STARTS ANYTHING HERE. This used to run
# `systemctl start --no-block porter-dependent.service`, which meant the
# convergence below was the test's doing and the probe could not tell an
# installed suite from an inert one. The postinst arms a fresh install now
# (porter/config.py), so `dpkg -i` is the whole of the operator's work and
# everything after this line is systemd's behaviour.
# Waits for the DEPENDENT'S OWN MARKER, not for `is-active`. On Type=simple
# systemd reports a unit active as soon as it has forked, so both units read
# `active` while the dependency's process was already exiting -- measured here,
# and it made the first version of this test pass at t=0 having converged
# nothing.
i=0
while [ $i -lt 60 ]; do
  a=$(systemctl is-active porter-dep.service)
  b=$(systemctl is-active porter-dependent.service)
  echo "t=${i}s dep=$a dependent=$b converged=$(test -f /var/lib/porter-dependent/converged && echo yes || echo no)" >> /out/timeline
  if [ -f /var/lib/porter-dependent/converged ]; then break; fi
  sleep 1
  i=$((i+1))
done
cp /var/lib/porter-dependent/converged /out/converged 2>/dev/null
cat /var/lib/porter-dep/starts > /out/dep-starts 2>/dev/null
systemctl is-active porter-dep.service > /out/dep-active
systemctl is-active porter-dependent.service > /out/dependent-active
systemctl show -p NRestarts --value porter-dep.service > /out/dep-restarts
systemctl show -p NRestarts --value porter-dependent.service > /out/dependent-restarts
systemctl show -p After --value porter-dependent.service > /out/dependent-after
systemctl show -p Requires --value porter-dependent.service > /out/dependent-requires
journalctl --no-pager -u porter-dep -u porter-dependent > /out/journal 2>&1
systemctl poweroff
'''

@pytest.fixture(scope="module")
def nspawn_root(tmp_path_factory) -> Path:
    root = nspawn.build_root(tmp_path_factory.mktemp("nspawn"))
    yield root
    nspawn.sudo("rm", "-rf", str(root))


@pytest.fixture(scope="module")
def convergence_debs(tmp_path_factory) -> Path:
    """The two packages of the convergence scenario, built by `porter build`.

    Written here rather than added to examples/multi-service because a gallery
    entry an adopter copies should not ship a knob whose only purpose is to make
    the service fail. The example proves the shape; this proves the behaviour.
    """
    _require_uv()
    work = tmp_path_factory.mktemp("convergence")
    (work / "src").mkdir()
    (work / "src/dep.py").write_text(DEP_PY)
    (work / "src/dependent.py").write_text(DEPENDENT_PY)
    (work / "porter.yaml").write_text(yaml.safe_dump(CONVERGENCE_MANIFEST))
    proc = subprocess.run(
        ["uv", "run", "porter", "build", str(work / "porter.yaml"),
         "--out", str(work / "debs"), "--stage", str(work / "build")],
        cwd=work, capture_output=True, text=True,
        env={**os.environ, "UV_PROJECT": str(Path(__file__).resolve().parents[1])})
    assert proc.returncode == 0, f"{proc.stdout}\n{proc.stderr}"
    assert len(list((work / "debs").glob("*.deb"))) == 2
    return work / "debs"


@pytest.mark.nspawn
def test_the_dependent_converges_though_its_dependency_fails_its_first_starts(
        nspawn_root, convergence_debs, tmp_path):
    """The behavioural claim, run rather than read.

    porter's answer to `depends_on: condition: service_healthy` is
    `After=` + `Requires=` + `Restart=on-failure`, and the argument for it is
    that the system *converges*: a dependency that is not ready yet costs the
    dependent a few restarts, not a failed boot. That argument had never been
    executed. Measured here, in a booted container, with the dependency exiting
    non-zero on its first two starts -- and with `dpkg -i` as the only thing the
    operator does, since the probe no longer starts anything itself.

    Four assertions, and the middle two are controls -- both of which have
    already fired. The first version of this test waited for
    `systemctl is-active` to report both units active and got it at t=0, with
    the dependency's own journal line reading "start 1 failing on purpose" on
    the same second: `Type=simple` means active-once-forked, so "active" is a
    proxy for having converged and not a measurement of it. What the test waits
    for now is a file the dependent writes only after its upstream answered,
    and what it asserts is that the failures it was supposed to survive
    happened.
    """
    out = tmp_path / "out"
    booted = nspawn.boot_with_probe(nspawn_root, PROBE_SH, out, convergence_debs)
    context = nspawn.context(booted, out)

    assert (out / "dpkg-rc").read_text().strip() == "DPKG_RC=0", context

    # 1. IT CONVERGED. The dependent reached its upstream and said so by
    #    writing to its StateDirectory -- a file on the container's disk, not
    #    systemd's opinion of a process it forked.
    assert (out / "converged").exists(), (
        "the dependent never reached its dependency within 60s: with "
        "Restart=on-failure the suite is supposed to converge, and it did "
        f"not\n{context}")
    assert (out / "dep-active").read_text().strip() == "active", context
    assert (out / "dependent-active").read_text().strip() == "active", context

    # 2. FIRST CONTROL. The dependency really did fail: three starts, two of
    #    them deliberate failures. Without this the test passes just as happily
    #    against a scenario where nothing ever went wrong, which is what it
    #    becomes the day the counter stops surviving the restart.
    assert int((out / "dep-starts").read_text().strip()) == 3, (
        f"the dependency was started {(out / 'dep-starts').read_text().strip()} "
        f"times; two failures and one success is the scenario\n{context}")
    restarts = int((out / "dep-restarts").read_text().strip())
    assert restarts >= 2, (
        f"systemd restarted the dependency {restarts} times, so Restart= is not "
        f"what brought it up and convergence was not what was measured\n{context}")

    # 3. SECOND CONTROL. The dependent had to retry too. If it had come up
    #    first time, the dependency's failures would have been irrelevant to it
    #    and the ordering would be proving nothing.
    assert int((out / "dependent-restarts").read_text().strip()) >= 1, context

    # 4. The ordering systemd actually loaded, read back from the running
    #    system rather than from the file porter wrote.
    assert "porter-dep.service" in (out / "dependent-requires").read_text(), context
    assert "porter-dep.service" in (out / "dependent-after").read_text(), context
