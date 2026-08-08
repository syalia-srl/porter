"""The near-native desktop package: a chromeless window that is never a core dep.

Two properties carry this module. The first is the split -- GUI needs GTK, X11
and NSS from the client, apt cannot fetch them on an airgapped box, so a desktop
dependency in the core package is fatal on a headless server. The second is that
"near-native" is more than chromelessness: an app-menu entry, an isolated
profile, and a wait for the service to answer before the window opens.

Most of the launcher assertions below **run the launcher** rather than reading
it. A generated shell script is exactly where a substring assertion decays: the
sketch this was built from probed browsers with
`command -v X && BROWSER=X && break`, which contains every string a text
assertion would look for and, under `set -e`, exits 1 the moment the first
candidate is absent -- the launcher dies on every machine without Chrome.
"""
import os
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

from porter.deb import build_deb
from porter.desktop import (
    BROWSERS,
    Desktop,
    assemble_desktop,
    desktop_entry,
    launcher,
    refuse_desktop_dependencies_in_the_core_package,
)

EXAMPLE = Path(__file__).resolve().parents[1] / "examples" / "desktop-app"
PKG = "porter-example-desktop"
URL = "http://127.0.0.1:9200"


def _body() -> str:
    return launcher(PKG, URL, "/health", "PorterDemo")


# --- the launcher, read ----------------------------------------------------


def test_launcher_probes_browsers_in_order_and_uses_app_mode():
    body = _body()
    order = [body.index(b) for b in ("google-chrome", "chromium", "brave-browser")]
    assert order == sorted(order), "browser probe order is not deterministic"
    assert "--app=" in body
    assert "--class=" in body
    assert "--no-first-run" in body


def test_launcher_waits_for_health_before_opening():
    """Clicking the icon right after login must not show connection-refused
    while systemd is still bringing the stack up."""
    body = _body()
    assert "/health" in body
    assert body.index("/health") < body.index("--app="), "opens before waiting"


def test_launcher_uses_an_isolated_profile():
    body = _body()
    assert "--user-data-dir=" in body
    assert f".local/share/{PKG}" in body


def test_launcher_parses_as_the_shell_its_shebang_names():
    """Nothing else checks it. deb.py runs `sh -n` over the maintainer scripts
    it is handed; the launcher is payload under /usr/bin and never passes
    through that list, so an unbalanced quote here ships."""
    body = _body()
    assert body.startswith("#!/usr/bin/env bash"), body.splitlines()[0]
    probe = subprocess.run(["bash", "-n"], input=body, capture_output=True, text=True)
    assert probe.returncode == 0, probe.stderr


# --- the launcher, run -----------------------------------------------------


@pytest.fixture
def harness(tmp_path):
    """A PATH holding fakes for everything the launcher shells out to.

    `curl` fails the first two calls and then succeeds, which is the client's
    real timeline: systemd is still starting the unit when the user clicks.
    Every fake appends to `calls`, so the ORDER of the wait and the open is
    observable rather than inferred from where two substrings sit in a string.

    PATH is **only** this directory, with the launcher's two remaining real
    tools symlinked in. The fallback test asserts that no browser was found,
    and on a developer's laptop that has Chromium installed a PATH inheriting
    /usr/bin would find one -- the test would then pass or fail depending on
    whose machine ran it.
    """
    bindir = tmp_path / "bin"
    bindir.mkdir()
    calls = tmp_path / "calls"
    for tool in ("bash", "mkdir", "grep"):
        (bindir / tool).symlink_to(shutil.which(tool))

    def fake(name: str, body: str) -> None:
        p = bindir / name
        p.write_text(f'#!/bin/sh\necho "{name} $*" >> "{calls}"\n{body}\n')
        p.chmod(0o755)

    fake("curl", f'test "$(grep -c "^curl " "{calls}")" -ge 3')
    fake("sleep", "exit 0")
    fake("xdg-open", "exit 0")

    def run(body: str, *, browsers: tuple[str, ...] = ()) -> str:
        for b in browsers:
            fake(b, "exit 0")
        script = tmp_path / "launcher"
        script.write_text(body)
        script.chmod(0o755)
        env = {**os.environ, "PATH": str(bindir), "HOME": str(tmp_path)}
        proc = subprocess.run([str(script)], env=env, capture_output=True, text=True)
        assert proc.returncode == 0, f"rc={proc.returncode} {proc.stderr}"
        return calls.read_text() if calls.exists() else ""

    return run


def test_launcher_survives_its_first_candidate_being_absent(harness):
    """The bug the sketch shipped, reproduced as a test.

    `command -v google-chrome >/dev/null && BROWSER=... && break` is an AND-list
    whose failure is the last command in the loop body, so `set -e` exits the
    script -- rc=1, no window, no message. Every client without Google Chrome.
    """
    log = harness(_body(), browsers=("chromium",))
    assert "chromium --app=" in log, log
    assert "google-chrome" not in log, log


def test_launcher_picks_the_first_browser_present_not_merely_any(harness):
    """The probe order is a preference, not decoration."""
    log = harness(_body(), browsers=("chromium", "brave-browser"))
    assert "chromium --app=" in log, log
    assert "brave-browser" not in log, log


def test_launcher_falls_back_to_xdg_open_with_no_browser_at_all(harness):
    log = harness(_body())
    assert "xdg-open " in log, log
    assert "--app=" not in log, log


def test_launcher_does_not_open_until_the_service_answers(harness):
    """Asserted on the call log, not on where two substrings sit in the text.

    The fake curl fails twice and succeeds on the third call, so a launcher that
    opened first would show `chromium` before three `curl` lines.
    """
    log = harness(_body(), browsers=("chromium",)).splitlines()
    opened = next(i for i, line in enumerate(log) if line.startswith("chromium "))
    assert sum(1 for line in log[:opened] if line.startswith("curl ")) >= 3, log


def test_launcher_creates_the_isolated_profile_directory(harness, tmp_path):
    harness(_body(), browsers=("chromium",))
    assert (tmp_path / ".local/share" / PKG / "browser-profile").is_dir()


def test_launcher_passes_the_profile_and_the_first_run_flags_through(harness):
    log = harness(_body(), browsers=("chromium",))
    line = next(ln for ln in log.splitlines() if ln.startswith("chromium "))
    for flag in ("--app=", "--class=", "--user-data-dir=",
                 "--no-first-run", "--no-default-browser-check"):
        assert flag in line, f"{flag} missing from {line!r}"


# --- the .desktop entry ----------------------------------------------------


def test_desktop_entry_is_valid(tmp_path):
    entry = tmp_path / f"{PKG}.desktop"
    entry.write_text(desktop_entry(PKG, "PorterDemo", PKG))
    if shutil.which("desktop-file-validate"):
        proc = subprocess.run(["desktop-file-validate", str(entry)],
                              capture_output=True, text=True)
        assert proc.returncode == 0, proc.stdout + proc.stderr


def test_the_window_groups_under_its_own_icon():
    """`--class=` and `StartupWMClass=` are one identity written in two files.

    They are what makes the running window attach to the launcher's icon in the
    taskbar instead of appearing as a second, generic entry. Neither file can
    catch a disagreement alone, and a disagreement looks like nothing at all --
    the app runs, the icon is just wrong.
    """
    body = launcher(PKG, URL, "/health", "PorterDemo")
    entry = desktop_entry(PKG, "PorterDemo", PKG)
    from_launcher = body.split("--class=")[1].split()[0].strip('"')
    from_entry = next(ln.split("=", 1)[1] for ln in entry.splitlines()
                      if ln.startswith("StartupWMClass="))
    assert from_launcher == from_entry, (from_launcher, from_entry)


def test_a_name_that_is_not_a_wm_class_is_refused():
    """A space in the name reaches `--class=Porter Demo` as two argv entries and
    `StartupWMClass=Porter Demo` as one string. The window then never groups,
    and nothing errors."""
    with pytest.raises(ValueError, match="WM_CLASS"):
        launcher(PKG, URL, "/health", "Porter Demo")


# --- the package -----------------------------------------------------------


@pytest.fixture(scope="session")
def desktop_manifest() -> dict:
    return yaml.safe_load((EXAMPLE / "porter.yaml").read_text())


@pytest.fixture
def staged_desktop(tmp_path, desktop_manifest):
    from porter.types import Component
    component, _ = Component.from_manifest(desktop_manifest)
    spec = Desktop.from_manifest(desktop_manifest)
    return assemble_desktop(component, spec, EXAMPLE, tmp_path / "stage")


def test_the_desktop_package_is_a_separate_package_that_needs_the_core_one(
        staged_desktop, desktop_manifest):
    core = desktop_manifest["package"]
    assert staged_desktop.control["Package"] == f"{core}-desktop"
    version = str(desktop_manifest["version"])
    assert f"{core} (= {version})" in staged_desktop.control["Depends"]


def test_the_desktop_package_declares_the_tools_its_launcher_runs(staged_desktop):
    """`curl` and `xdg-open` are invoked by the launcher and are absent from a
    minimal Debian install. Undeclared, the icon does nothing on first click and
    the only trace is on stderr of a process nobody is watching."""
    depends = staged_desktop.control["Depends"]
    assert "curl" in depends, depends
    assert "xdg-utils" in depends, depends


def test_the_staged_tree_is_what_the_desktop_entry_points_at(staged_desktop):
    """Exec= is a path on the client, and nothing else checks it resolves."""
    stage = staged_desktop.stage
    entry = (stage / "usr/share/applications").glob("*.desktop")
    text = next(entry).read_text()
    exec_path = next(ln.split("=", 1)[1] for ln in text.splitlines()
                     if ln.startswith("Exec="))
    staged = stage / exec_path.lstrip("/")
    assert staged.is_file(), f"{exec_path} is not staged"
    assert staged.stat().st_mode & 0o111, f"{exec_path} is staged non-executable"


def test_the_icon_is_staged_under_the_size_it_actually_is(staged_desktop):
    """The hicolor directory is derived from the PNG's IHDR, never written by
    hand: a 48x48 file under 256x256/apps/ is rendered by some themes as a
    blurred smear and by others not at all, and the package installs cleanly
    either way."""
    icons = list((staged_desktop.stage / "usr/share/icons/hicolor").iterdir())
    assert len(icons) == 1, icons
    png = next((icons[0] / "apps").iterdir())
    width = int.from_bytes(png.read_bytes()[16:20], "big")
    assert icons[0].name == f"{width}x{width}", (icons[0].name, width)


def test_the_desktop_package_builds_into_a_real_deb(staged_desktop, tmp_path):
    """Through the same lint every other porter package goes through."""
    deb = build_deb(staged_desktop.stage, staged_desktop.control, tmp_path / "out",
                    conffiles=staged_desktop.conffiles, scripts=staged_desktop.scripts)
    assert deb.exists()
    info = subprocess.run(["dpkg-deb", "--field", str(deb), "Depends"],
                          capture_output=True, text=True)
    assert "xdg-utils" in info.stdout, info.stdout


def test_a_browser_porter_cannot_ship_is_refused(desktop_manifest):
    """`browser: bundled` is out of scope for 0.1.0 -- the artefact's terms are
    unverified and the tree used to validate the mechanism identified itself as
    Google Chrome for Testing, not Chromium. Accepted-and-ignored would emit a
    launcher that probes the system browser under a manifest that says it pins
    one."""
    with pytest.raises(ValueError, match="browser"):
        Desktop.from_manifest({**desktop_manifest,
                               "desktop": {**desktop_manifest["desktop"],
                                           "browser": "bundled"}})


def test_a_stage_that_is_not_empty_is_refused(tmp_path, desktop_manifest):
    """Leftovers ship, and here they ship *into the desktop package*.

    `build_deb`'s lint allows `usr/`, so a core payload left in this directory
    would be packaged into the launcher -- and its libraries would then land in
    the desktop package's derived Depends:, which is rule 12 running backwards.
    """
    from porter.types import Component
    component, _ = Component.from_manifest(desktop_manifest)
    spec = Desktop.from_manifest(desktop_manifest)
    stage = tmp_path / "stage"
    (stage / "usr/lib").mkdir(parents=True)
    (stage / "usr/lib/leftover").write_text("someone else's payload")
    with pytest.raises(ValueError, match="not empty"):
        assemble_desktop(component, spec, EXAMPLE, stage)


def test_a_url_pointing_at_a_port_the_service_does_not_use_is_refused(
        tmp_path, desktop_manifest):
    """`desktop.url` and `env.PORT` are one number written twice.

    The drift that happens is an author bumping the port in `env:` and leaving
    the desktop block behind. The launcher then waits its full timeout on a
    health check nothing answers and opens a window on connection-refused --
    with the package built, linted and installed at rc=0 throughout.
    """
    from porter.types import Component
    component, _ = Component.from_manifest(
        {**desktop_manifest, "env": {**desktop_manifest["env"], "PORT": "9300"}})
    spec = Desktop.from_manifest(desktop_manifest)
    with pytest.raises(ValueError, match="9300"):
        assemble_desktop(component, spec, EXAMPLE, tmp_path / "stage")


def test_a_missing_icon_is_refused(tmp_path, desktop_manifest):
    """A .desktop entry whose Icon= resolves to nothing shows a blank tile in
    the app menu. dpkg has no opinion about it."""
    from porter.types import Component
    component, _ = Component.from_manifest(desktop_manifest)
    spec = Desktop.from_manifest(
        {**desktop_manifest,
         "desktop": {**desktop_manifest["desktop"], "icon": "assets/nope.png"}})
    with pytest.raises(ValueError, match="icon"):
        assemble_desktop(component, spec, EXAMPLE, tmp_path / "stage")


def test_an_icon_that_is_not_a_png_is_refused(tmp_path, desktop_manifest):
    from porter.types import Component
    (tmp_path / "assets").mkdir()
    (tmp_path / "assets/icon.png").write_bytes(b"GIF89a" + b"\x00" * 200)
    (tmp_path / "src").mkdir()
    shutil.copy(EXAMPLE / "src/app.py", tmp_path / "src/app.py")
    component, _ = Component.from_manifest(desktop_manifest)
    spec = Desktop.from_manifest(desktop_manifest)
    with pytest.raises(ValueError, match="PNG"):
        assemble_desktop(component, spec, tmp_path, tmp_path / "stage")


# --- the rule that governs the split ---------------------------------------


def test_a_desktop_dependency_in_the_core_package_is_refused():
    """The entire point of the split, as a check rather than a convention.

    apt cannot fetch GTK on an airgapped box, so a core package that depends on
    anything the desktop package introduced cannot be installed on a headless
    server at all -- and the failure arrives at the client, during the one
    install nobody is watching.
    """
    with pytest.raises(ValueError, match="xdg-utils"):
        refuse_desktop_dependencies_in_the_core_package(
            core_package=PKG,
            core_depends=["libc6", "libcrypt1", "xdg-utils"],
            desktop_depends=[f"{PKG} (= 1.0)", "curl", "xdg-utils"],
        )


def test_the_split_check_passes_the_shape_porter_actually_emits():
    """Positive control. A refusal that fires on everything is not a check."""
    refuse_desktop_dependencies_in_the_core_package(
        core_package=PKG,
        core_depends=["libc6", "libcrypt1"],
        desktop_depends=[f"{PKG} (= 1.0)", "curl", "xdg-utils"],
    )


def test_the_split_check_ignores_the_dependency_on_the_core_package_itself():
    """`<pkg>-desktop` depends on `<pkg>` by construction; reading that as a
    leak would refuse every correct build."""
    refuse_desktop_dependencies_in_the_core_package(
        core_package=PKG,
        core_depends=["libc6"],
        desktop_depends=[f"{PKG} (= 1.0)"],
    )


def test_browsers_are_probed_by_name_and_porter_picks_no_vendor():
    """Rule 10's shape: porter carries a probe list, not a chosen browser."""
    assert BROWSERS[0] == "google-chrome"
    assert "chromium" in BROWSERS
