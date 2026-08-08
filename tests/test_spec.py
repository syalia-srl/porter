"""`porter.spec` -- the validated reader for a porter.yaml.

Two halves, and they are the same argument twice.

The first is that the gallery IS the schema. Every manifest under `examples/`
must load, so a field porter stops reading, or starts reading differently,
takes this file red before it reaches a client. Nothing here restates a
manifest's contents by hand.

The second is that a key porter does not read is a key that is silently
dropped, and porter's characteristic bug is the silently dropped input. Every
refusal below stands where the alternative was a package that builds at rc=0,
installs at rc=0, and is wrong at the client: `admin_key:` for `admin_keys:`
writes a secret into the package-owned conffile; `verson:` for `version:`
ships an interpreter one release off what the payload needs; a metapackage
naming a component the manifest does not build produces a role that fails
`apt install` with unmet dependencies on the one machine with no network.
"""
from pathlib import Path

import pytest
import yaml

from porter.spec import Component, Metapackage, Python, load

EXAMPLES = Path(__file__).resolve().parents[1] / "examples"
SUITE = EXAMPLES / "suite/porter.yaml"


def _manifest(tmp_path: Path, doc: dict, name: str = "porter.yaml") -> Path:
    path = tmp_path / name
    path.write_text(yaml.safe_dump(doc, sort_keys=False))
    return path


@pytest.fixture
def suite_doc() -> dict:
    """The gallery's suite manifest as a dict, for the refusals to damage.

    Read from the example rather than written here, so a refusal test cannot
    keep passing against a shape the gallery has moved on from -- which is the
    same failure as a stale reverify entry, one directory over.
    """
    return yaml.safe_load(SUITE.read_text())


# --- the gallery loads ------------------------------------------------------

def test_every_example_manifest_parses():
    """The gallery is the regression suite: a manifest that stops parsing is a
    porter bug, not an example bug. `porter*.yaml` and not `porter.yaml`, so
    that stateful-service's `porter-previous.yaml` -- the v1.9 half of the
    upgrade example, and a real manifest porter builds -- is covered too."""
    manifests = sorted(EXAMPLES.glob("*/porter*.yaml"))
    assert len(manifests) >= 8, f"the gallery shrank: {manifests}"
    for path in manifests:
        manifest = load(path)
        assert manifest.components, f"{path} declared no components"
        for component, python in manifest.components:
            assert isinstance(component, Component) and isinstance(python, Python)
            assert python.version, f"{path}: {component.package} has no interpreter"


def test_kinds_cover_every_shape_porter_emits():
    """`assemble.SUPPORTED_KINDS` is the claim; this is the evidence. A kind
    porter emits and no example exercises has no schema -- which is how the
    `command` branch went four tasks with a refusal list and no manifest."""
    kinds = {c.kind
             for path in EXAMPLES.glob("*/porter*.yaml")
             for c, _ in load(path).components}
    assert {"service", "command", "oneshot"} <= kinds, kinds


def test_the_command_example_is_a_binary_and_nothing_else():
    """Read off the manifest, because the refusals in `assemble` are what make
    the absences real: a command with config, ordering or a schedule is
    refused, so an example carrying one would not build."""
    (component, _), = load(EXAMPLES / "command/porter.yaml").components
    assert component.kind == "command"
    assert component.bin_name and component.bin_name != component.package
    assert not component.defaults and not component.admin_keys
    assert not component.after and not component.schedule
    assert not component.migrations


# --- the suite: several components, and one name per machine ----------------

def test_the_suite_declares_two_roles_over_one_shared_component_set():
    """The point of the example, asserted rather than described.

    One role would be indistinguishable from "install everything"; two roles
    that shared nothing would be two deployments that happen to live in one
    file. The overlap is what makes it a suite -- a component named by both
    roles is built once and carried once."""
    manifest = load(SUITE)
    roles = {m.package: set(m.depends) for m in manifest.metapackages}
    assert len(roles) == 2, roles
    built = {c.package for c, _ in manifest.components}
    shared = set.intersection(*roles.values())
    assert shared, f"the roles share no component: {roles}"
    assert shared <= built
    for role, depends in roles.items():
        assert depends - shared, f"{role} is nothing but the shared component"


def test_the_suite_mixes_every_kind_under_one_metapackage():
    """A metapackage that resolved only services would work in an example made
    of services and fail at a client. une-tools' technical machine is an API, a
    scheduled job and a CLI, which is three kinds behind one name."""
    manifest = load(SUITE)
    kind_of = {c.package: c.kind for c, _ in manifest.components}
    technical, = [m for m in manifest.metapackages if m.package.endswith("technical")]
    assert {kind_of[p] for p in technical.depends} == {"service", "oneshot", "command"}


def test_a_metapackage_is_architecture_independent_and_inherits_the_shared_keys():
    """`Architecture: all`, and deliberately NOT the manifest's `amd64`.

    A metapackage has no payload, so pinning it to an architecture makes the
    one package on the USB with no binaries in it the one an arm64 client
    cannot install. `version` and `maintainer` are inherited, because a role
    that drifts from the components it names is the drift porter exists to
    stop."""
    doc = yaml.safe_load(SUITE.read_text())
    assert doc["architecture"] == "amd64", "the example stopped making the point"
    for meta in load(SUITE).metapackages:
        assert isinstance(meta, Metapackage)
        assert meta.architecture == "all"
        assert meta.version == doc["version"]
        assert meta.maintainer == doc["maintainer"]


def test_a_manifest_with_no_metapackages_declares_none():
    """Absent, not empty-by-accident: every other example must keep loading
    with the block unwritten, or `metapackages:` would be a required key."""
    assert load(EXAMPLES / "service-fastapi/porter.yaml").metapackages == []
    assert load(EXAMPLES / "multi-service/porter.yaml").metapackages == []


# --- refusals ---------------------------------------------------------------

def test_refuses_an_unknown_top_level_key(tmp_path, suite_doc):
    """`metapackage:` for `metapackages:`. Read as nothing: the build emits the
    four components at rc=0 and no role at all, and the sysadmin's runbook
    names a package that is not on the USB."""
    suite_doc["metapackage"] = suite_doc.pop("metapackages")
    with pytest.raises(ValueError, match="unknown key.*metapackage"):
        load(_manifest(tmp_path, suite_doc))


def test_refuses_an_unknown_key_in_a_component_entry(tmp_path, suite_doc):
    """`admin_key:` for `admin_keys:` is the expensive one. The key stays in
    `env:`, so `split()` puts it in /etc/<pkg>/defaults -- package-owned, a
    conffile, shipped inside the .deb and replaced on every upgrade. The
    client's secret is then in the package."""
    entry, = [e for e in suite_doc["components"] if "admin_keys" in e]
    entry["admin_key"] = entry.pop("admin_keys")
    with pytest.raises(ValueError, match="unknown key.*admin_key"):
        load(_manifest(tmp_path, suite_doc))


def test_refuses_an_unknown_key_in_a_metapackage(tmp_path, suite_doc):
    """`depends_on:` for `depends:` builds a role with an empty `Depends:`:
    `apt install porter-example-suite-technical` succeeds, installs nothing,
    and the machine is bare."""
    suite_doc["metapackages"][0]["depends_on"] = suite_doc["metapackages"][0].pop("depends")
    with pytest.raises(ValueError, match="unknown key.*depends_on"):
        load(_manifest(tmp_path, suite_doc))


def test_refuses_an_unknown_key_in_the_python_block(tmp_path, suite_doc):
    """`verson: "3.13"` falls back to the 3.12 default, so the .deb ships an
    interpreter one release below what the manifest asked for and the payload
    dies at its first 3.13-only import -- on the client, at rc=0 here."""
    suite_doc["python"]["verson"] = suite_doc["python"].pop("version")
    with pytest.raises(ValueError, match="unknown key.*verson"):
        load(_manifest(tmp_path, suite_doc))


def test_refuses_an_unknown_key_in_the_exec_block(tmp_path, suite_doc):
    """`arg:` for `args:` drops the module's own arguments. The gallery's
    `-m uvicorn app:app --host ... --port ...` becomes a bare `-m uvicorn`,
    which exits on its usage message and is restarted forever."""
    suite_doc["components"][0]["exec"]["arg"] = ["--verbose"]
    with pytest.raises(ValueError, match="unknown key.*arg"):
        load(_manifest(tmp_path, suite_doc))


@pytest.mark.parametrize("missing", ["package", "description", "kind", "exec"])
def test_refuses_a_component_missing_a_required_key(tmp_path, suite_doc, missing):
    """Without this, `from_manifest` fails deep in assembly -- a traceback
    whose entire message is the word the adopter did not type, with no manifest
    path and no component name in it."""
    suite_doc["components"][0].pop(missing)
    with pytest.raises(ValueError, match=f"missing.*{missing}"):
        load(_manifest(tmp_path, suite_doc))


@pytest.mark.parametrize("missing", ["package", "description", "depends"])
def test_refuses_a_metapackage_missing_a_required_key(tmp_path, suite_doc, missing):
    suite_doc["metapackages"][0].pop(missing)
    with pytest.raises(ValueError, match=f"missing.*{missing}"):
        load(_manifest(tmp_path, suite_doc))


def test_refuses_a_role_naming_a_package_the_manifest_does_not_build(tmp_path, suite_doc):
    """The headline refusal.

    A one-character typo in `depends:` builds a metapackage whose `Depends:`
    names a package that is not on the USB. dpkg-deb is content -- it never
    resolves anything -- so the build exits 0, the USB looks complete, and the
    failure surfaces as `apt install` reporting unmet dependencies on an
    airgapped client with no network to fetch the missing name from."""
    suite_doc["metapackages"][0]["depends"][0] += "s"
    with pytest.raises(ValueError, match="depends on .* no component"):
        load(_manifest(tmp_path, suite_doc))


def test_refuses_a_role_that_depends_on_nothing(tmp_path, suite_doc):
    """`depends: []` is what a half-finished edit leaves behind. It builds, it
    installs, and it pulls in not one component: the sysadmin's one command
    reports success on a machine where nothing was delivered."""
    suite_doc["metapackages"][0]["depends"] = []
    with pytest.raises(ValueError, match="depends on nothing"):
        load(_manifest(tmp_path, suite_doc))


def test_refuses_two_packages_sharing_one_name(tmp_path, suite_doc):
    """`build_deb` writes `<package>_<version>_<arch>.deb`, so two components
    with one name are one file: the second build overwrites the first and the
    USB ships three packages where the manifest declared four. Nothing reports
    it -- both builds exit 0."""
    suite_doc["components"][1]["package"] = suite_doc["components"][0]["package"]
    with pytest.raises(ValueError, match="declared twice"):
        load(_manifest(tmp_path, suite_doc))


def test_refuses_a_role_sharing_a_name_with_a_component(tmp_path, suite_doc):
    """The same collision across the two halves, which the components-only
    check cannot see: a role named after one of its own components would
    depend on itself and overwrite its 91 MB .deb with a 20 KB stub."""
    suite_doc["metapackages"][0]["package"] = suite_doc["components"][0]["package"]
    with pytest.raises(ValueError, match="declared twice"):
        load(_manifest(tmp_path, suite_doc))
