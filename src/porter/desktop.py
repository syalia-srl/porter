"""The optional near-native desktop package: a chromeless window, in the app menu.

Rule 12 at the door, and it is what shapes this whole module: **a desktop
dependency never enters the core package.** A GUI needs GTK, X11 and NSS from
the client, and apt cannot fetch them on an airgapped box -- so a desktop
dependency in the core package is not a nuisance on a headless server, it is a
package that cannot be installed there at all. It lives in a separate
`<app>-desktop` package that `Depends:` on the core one, and
`refuse_desktop_dependencies_in_the_core_package` below is that rule written as
a check rather than left as a convention.

`browser: system` is the only shape 0.1.0 emits. The launcher probes what the
client already has and falls back to `xdg-open`; `chromium --app=URL` gives a
window with no tabs and no URL bar, and `--class=` gives it its own taskbar
identity. Bundling a pinned Chromium is deliberately out of scope: the artefact
used to validate the mechanism identified itself as *Google Chrome for Testing*,
not Chromium, and its terms are unverified. porter picks no vendor -- rule 10.

**Near-native is more than chromelessness.** A `.desktop` entry and an icon so
it appears in the app menu; an isolated profile under `~/.local/share/<pkg>/` so
it does not inherit the user's browsing session; `--no-first-run
--no-default-browser-check` so it does not greet them with a setup wizard; and a
wait for the service to answer before the window opens, because clicking the
icon right after login otherwise lands on connection-refused while systemd is
still starting the stack.

**Not verified:** whether the window genuinely *reads* as native. That needs a
display and a human, and Chromium ignores unknown flags silently, so "the flags
were accepted" is not evidence. The tests here prove the launcher picks the
right browser, waits, and passes the flags -- not what the result looks like.
"""
from __future__ import annotations

import re
import struct
from dataclasses import dataclass
from pathlib import Path

from porter.assemble import Staged
from porter.depends import derive_depends
from porter.types import Component

# Probed in this order, and the order is a preference: a client with both
# Chrome and Chromium gets Chrome. porter names candidates and chooses no
# vendor (rule 10) -- the client's own installation decides.
BROWSERS = ("google-chrome", "chromium", "chromium-browser",
            "brave-browser", "microsoft-edge")

# What the launcher shells out to, and the packages that provide it. These are
# the desktop package's dependencies that no ELF header can reveal: the
# launcher is a shell script, so `derive_depends` over its staged tree is
# correctly empty and this is the rest of the answer. Both are absent from a
# minimal Debian install, and undeclared the icon does nothing on first click
# with the only trace on the stderr of a process nobody is watching.
LAUNCHER_TOOLS = {"curl": "curl", "xdg-open": "xdg-utils"}

# The sizes freedesktop's hicolor theme defines a directory for. An icon staged
# under a directory that is not its own size is rendered blurred by some themes
# and not at all by others, and the package installs cleanly either way.
HICOLOR_SIZES = (16, 22, 24, 32, 48, 64, 128, 256, 512)

# X11's WM_CLASS is one token. `--class=Porter Demo` reaches the browser as two
# argv entries while `StartupWMClass=Porter Demo` is one string, so the window
# never groups under its own launcher icon -- and nothing anywhere errors.
WM_CLASS = re.compile(r"[A-Za-z0-9_.\-]+\Z")


@dataclass(frozen=True)
class Desktop:
    """The `desktop:` block of a `porter.yaml`.

    Read here rather than in `porter.types` on purpose: Task 7 owns validating
    the manifest and will absorb this, and a second half-validator wedged into
    the shared schema module is the drift that seam exists to prevent. The keys
    below are the ones `examples/desktop-app/porter.yaml` carries and no others
    -- a field no example exercises does not exist.
    """

    name: str
    icon: str
    url: str
    health: str = "/health"
    browser: str = "system"
    component: str | None = None
    categories: str = "Office;"

    @classmethod
    def from_manifest(cls, manifest: dict) -> Desktop | None:
        """The block, or None for the manifests that declare no desktop at all.

        None and not a default-constructed `Desktop`: the desktop package is
        optional, and a project that never asked for one must not have a second
        `.deb` appear in its output directory.
        """
        block = manifest.get("desktop")
        if block is None:
            return None
        spec = cls(
            name=block["name"],
            icon=block["icon"],
            url=block["url"],
            health=block.get("health", "/health"),
            browser=block.get("browser", "system"),
            component=block.get("component"),
            categories=block.get("categories", "Office;"),
        )
        if spec.browser != "system":
            raise ValueError(
                f"desktop.browser={spec.browser!r} is not implemented: 'system' "
                "is the only shape porter emits for 0.1.0. A bundled browser is "
                "a pinned third-party artefact whose licence terms porter has "
                "not verified, and accepting the key would emit a launcher that "
                "probes the client's browser under a manifest saying it pins one"
            )
        if not spec.health.startswith("/"):
            raise ValueError(
                f"desktop.health={spec.health!r} must be a path beginning with "
                f"'/': it is appended to {spec.url!r} to build the readiness "
                "probe, and a bare word produces a URL nothing answers -- the "
                "launcher would then wait its full timeout on every click"
            )
        if not spec.url.startswith(("http://", "https://")):
            raise ValueError(
                f"desktop.url={spec.url!r} must be an http(s) URL: it is what "
                "the browser is handed with --app=, and xdg-open in the "
                "fallback branch resolves anything else against the filesystem"
            )
        return spec

    def pick(self, components: list[Component]) -> Component:
        """Which component the window points at.

        Named explicitly when a manifest declares several: a suite's desktop
        entry belongs to exactly one of its services, and guessing would put
        the launcher's `Depends:` on a package chosen by list order.
        """
        if self.component is not None:
            for candidate in components:
                if candidate.name == self.component:
                    return candidate
            raise ValueError(
                f"desktop.component={self.component!r} names no component in "
                f"this manifest ({', '.join(c.name for c in components)}). The "
                "desktop package Depends: on it by name, so an unresolved "
                "reference is an uninstallable package"
            )
        if len(components) != 1:
            raise ValueError(
                f"this manifest declares {len(components)} components and the "
                "desktop: block names none. The launcher waits on one service's "
                "health endpoint and the package Depends: on one package -- "
                "add `component:` to say which"
            )
        return components[0]


def launcher(pkg: str, url: str, health: str, name: str) -> str:
    """`/usr/bin/<pkg>-desktop`: wait for the service, then open a bare window.

    Written as explicit `if` blocks, because the probe is *allowed* to fail and
    `set -e` must not act on it: a bare `command -v "$candidate"` in the loop
    body exits the launcher at rc=1 the moment the first candidate is absent --
    every client that has not installed Google Chrome, with no window and no
    message. An `if` condition is exempt from `set -e` by construction.

    The AND-list form `command -v X && BROWSER=X && break` is exempt too, and
    this docstring claimed the opposite until 2026-08-08. Both POSIX and bash
    exempt a command that fails in a non-final position of an `&&` list, and the
    enclosing `for` does not re-raise it; measured on bash 5.3, the AND-list
    loop survives a missing candidate at rc=0. The two forms are equivalent
    here. The `if` is preferred for legibility, not for safety.

    `tests/test_desktop.py` runs the script against fake browsers rather than
    grepping it, because a broken form contains every substring the working one
    does.

    No `seq`, no `[[ ]]`: the loop is a POSIX counter so the only external
    commands are `mkdir`, `curl` and `sleep`, and `LAUNCHER_TOOLS` can name what
    the package must therefore depend on.
    """
    if not WM_CLASS.match(name):
        raise ValueError(
            f"desktop.name={name!r} is not usable as an X11 WM_CLASS "
            f"([A-Za-z0-9_.-]). It is passed to the browser as --class= and "
            "written into the .desktop entry as StartupWMClass=, and the two "
            "must be one token or the running window never groups under its "
            "own launcher icon -- silently, with the app working perfectly"
        )
    candidates = " ".join(BROWSERS)
    return f"""#!/usr/bin/env bash
# Launcher for {name}, generated by porter. Do not edit -- reinstalling the
# {pkg}-desktop package replaces it.
#
# Opens a chromeless window against the local service, in a browser profile of
# its own so it inherits none of the user's browsing session.
set -euo pipefail

URL="{url}"
HEALTH="{url}{health}"
PROFILE="$HOME/.local/share/{pkg}/browser-profile"
mkdir -p "$PROFILE"

# Wait for the service before opening. A click right after login arrives while
# systemd is still bringing the stack up, and a browser that opens first shows
# connection-refused -- which reads as a broken install, not as a slow one.
tries=0
while [ "$tries" -lt 30 ]; do
  if curl -fsS -o /dev/null "$HEALTH"; then
    break
  fi
  tries=$((tries + 1))
  sleep 1
done

# Whatever the client already has, in porter's order of preference.
BROWSER=""
for candidate in {candidates}; do
  if command -v "$candidate" >/dev/null 2>&1; then
    BROWSER="$candidate"
    break
  fi
done

# No browser at all: hand it to the desktop's own default. Not a window with no
# tabs, but a working application beats a launcher that does nothing.
if [ -z "$BROWSER" ]; then
  exec xdg-open "$URL"
fi

exec "$BROWSER" --app="$URL" --class="{name}" \\
  --user-data-dir="$PROFILE" --no-first-run --no-default-browser-check
"""


def desktop_entry(pkg: str, name: str, icon: str, comment: str | None = None) -> str:
    """The app-menu entry. `icon` is a theme name, never a path.

    A theme name is what lets the client's icon theme pick the right size out
    of hicolor; a path pins one file and ignores the theme entirely.

    `StartupWMClass` is the other half of the launcher's `--class=`: it is what
    attaches the running window to this entry in the taskbar. The two are
    asserted equal in the tests, because a disagreement produces a second
    generic taskbar entry and no error anywhere.
    """
    return f"""[Desktop Entry]
Type=Application
Name={name}
Comment={comment or name}
Exec=/usr/bin/{pkg}-desktop
Icon={icon}
Terminal=false
Categories=Office;
StartupWMClass={name}
"""


def _icon_size(icon: Path) -> int:
    """The PNG's own declared width, read from its IHDR.

    Read rather than declared so the hicolor directory the icon is staged under
    cannot disagree with the icon. A magnitude check as much as a format one:
    "there is a file at assets/icon.png" and "there is an icon there" are
    different questions, and a `.desktop` entry whose Icon= resolves to nothing
    shows a blank tile in the app menu with dpkg perfectly satisfied.
    """
    if not icon.is_file():
        raise ValueError(
            f"desktop.icon points at {icon}, which does not exist. The .desktop "
            "entry would ship an Icon= that resolves to nothing and the app menu "
            "would show a blank tile -- at rc=0, with the package installed"
        )
    head = icon.read_bytes()[:24]
    if head[:8] != b"\x89PNG\r\n\x1a\n":
        raise ValueError(
            f"desktop.icon {icon} is not a PNG (magic {head[:8]!r}). hicolor "
            "themes read PNG; a JPEG or an SVG renamed to .png is not shown and "
            "not reported"
        )
    width, height = struct.unpack(">II", head[16:24])
    if width != height:
        raise ValueError(
            f"desktop.icon {icon} is {width}x{height}: hicolor directories are "
            "square, so a non-square icon has no directory it belongs in"
        )
    if width not in HICOLOR_SIZES:
        raise ValueError(
            f"desktop.icon {icon} is {width}x{width}, which is not a hicolor "
            f"size ({', '.join(str(s) for s in HICOLOR_SIZES)}). An icon staged "
            "outside the theme's directories is not found by the theme at all"
        )
    return width


def _refuse_a_url_pointing_at_a_port_the_service_does_not_use(
        component: Component, spec: Desktop) -> None:
    """`desktop.url` and `env.PORT` are one number written twice.

    The drift that actually happens is an author bumping the port in `env:` and
    leaving the desktop block behind. The launcher then waits its full timeout
    on a health check nothing answers and opens a window on connection-refused
    -- and the package builds, lints and installs at rc=0 throughout.

    An admin who edits PORT in `/etc/<pkg>/env` **after** installation is not
    covered by this and cannot be: the URL is baked at build time. That is a
    real limitation of `browser: system`, not an oversight.
    """
    declared = component.defaults.get("PORT")
    if not declared:
        return
    in_url = spec.url.rsplit(":", 1)[-1].split("/")[0]
    if in_url.isdigit() and in_url != str(declared):
        raise ValueError(
            f"desktop.url points at port {in_url} and the component's env.PORT "
            f"is {declared}. The launcher would wait its whole timeout on a "
            "health check nothing answers, then open a window on "
            "connection-refused -- with the package installed at rc=0"
        )


def refuse_desktop_dependencies_in_the_core_package(
        core_package: str, core_depends: list[str],
        desktop_depends: list[str]) -> None:
    """Rule 12, as a check.

    The core package may not depend on anything the desktop package introduced.
    apt cannot fetch GTK, X11 or `xdg-utils` on an airgapped box, so such a
    dependency does not degrade a headless install -- it makes it impossible,
    and the failure arrives at the client during the one install nobody is
    watching.

    The desktop package's dependency on the *core* package is excluded: that is
    the split working, not a leak.
    """
    def name(dep: str) -> str:
        return dep.split("(")[0].strip()

    introduced = {name(d) for d in desktop_depends} - {core_package}
    leaked = sorted(introduced & {name(d) for d in core_depends})
    if leaked:
        raise ValueError(
            f"the core package {core_package} depends on {', '.join(leaked)}, "
            "which the desktop package introduced. A desktop dependency in the "
            "core package cannot be satisfied on an airgapped headless client "
            "-- apt has no network and the GUI libraries are not installed -- so "
            f"the server install of {core_package} would fail outright. Move it "
            f"to {core_package}-desktop"
        )


def assemble_desktop(component: Component, spec: Desktop, src_root: Path,
                     stage_root: Path) -> Staged:
    """Stage `<pkg>-desktop`: the launcher, the app-menu entry, the icon.

    Its own stage root, never the core package's. That is the structural half of
    rule 12 -- the two trees are never the same tree, so the core package's
    derived `Depends:` cannot see the desktop payload even by accident -- and
    the refusal below mirrors `assemble`'s for the same reason: leftovers in a
    reused stage ship.
    """
    src_root, stage = Path(src_root), Path(stage_root).resolve()
    pkg = component.package
    desktop_pkg = f"{pkg}-desktop"

    if stage.exists() and any(stage.iterdir()):
        raise ValueError(
            f"stage root is not empty: {stage}. porter will not build the "
            "desktop package on top of an existing tree -- the leftovers would "
            "ship, and a core payload left there would put the core package's "
            "libraries into the desktop package's derived Depends:"
        )
    _refuse_a_url_pointing_at_a_port_the_service_does_not_use(component, spec)
    size = _icon_size(src_root / spec.icon)
    stage.mkdir(parents=True, exist_ok=True)

    bindir = stage / "usr/bin"
    bindir.mkdir(parents=True)
    entry_point = bindir / f"{pkg}-desktop"
    entry_point.write_text(launcher(pkg, spec.url, spec.health, spec.name))
    entry_point.chmod(0o755)  # dpkg preserves the mode; without it, not runnable

    applications = stage / "usr/share/applications"
    applications.mkdir(parents=True)
    (applications / f"{desktop_pkg}.desktop").write_text(
        desktop_entry(pkg, spec.name, desktop_pkg, component.description))

    icons = stage / f"usr/share/icons/hicolor/{size}x{size}/apps"
    icons.mkdir(parents=True)
    (icons / f"{desktop_pkg}.png").write_bytes((src_root / spec.icon).read_bytes())

    # Pinned to the exact version: the launcher's URL and health path are baked
    # from this component's manifest, so a desktop package paired with a core
    # package of another version is a window pointing at a port that moved.
    depends = [f"{pkg} (= {component.version})"]
    depends += sorted(set(LAUNCHER_TOOLS.values()) | set(derive_depends(stage)))

    control = {
        "Package": desktop_pkg,
        "Version": component.version,
        "Architecture": component.architecture,
        "Maintainer": component.maintainer,
        "Depends": ", ".join(depends),
        "Description": f"{component.description} -- desktop launcher",
    }
    # No conffiles and no maintainer scripts. Nothing here is under /etc, and
    # the launcher needs no user, no unit and no state: the whole package is
    # three files the desktop environment picks up on its own.
    return Staged(stage=stage, conffiles=[], control=control, scripts={})
