# porter — design

How porter is built and the rules it holds to. AGENTS.md says what porter is and
what done means; `know-how/` says how to do a particular job.

This file changes when the architecture changes, and not otherwise. Adding an
example, a module or a test touches none of it. The measurements behind each rule
are in `docs/design-spec.md`; approved extensions to that spec are the
`docs/*-design.md` files whose frontmatter says `extends: docs/design-spec.md`, and
`TASKS.md` puts them in order.

## The asymmetry

Nothing is inherited from the client OS except glibc, systemd and, optionally, the
NVIDIA driver. No Docker, no container runtime, no `nvidia-container-toolkit`, no
system Python. The interpreter is vendored, native binaries are built on the glibc
floor, and everything else is package payload.

Docker is a build dependency and never a client one. The install also runs with
nobody watching: an airgapped client has no interactive fallback, so a prompt is a
hang, and every path is non-interactive by construction rather than by flag.

## Rules

Each rule was paid for with a measurement in `docs/design-spec.md`.

1. **No venv, ever.** `uv venv --relocatable` writes an absolute symlink to the
   build host's interpreter and dies at the client. Vendor a python-build-standalone
   tree and install into its own `site-packages`.
2. **Dereference the interpreter's root directory** when materialising it.
   `uv python find` resolves through a symlinked directory
   (`cpython-3.12-linux-x86_64-gnu` to `cpython-3.12.13-...`), and a copy that
   preserves that link vendors nothing while still working on any host that has uv.
   In a shell that means `cp -aL`, never `cp -a`. Python's `shutil.copytree` follows
   a symlinked source root regardless of its `symlinks=` argument, so the hazard is
   specific to `cp` and friends (verified 2026-08-07). Do not restate the two as
   equivalent.
3. **`python -m <module>`, never `bin/` console scripts.** Their shebangs are
   absolute build paths.
4. **Config is two files.** `/etc/<pkg>/defaults` is a conffile the package owns;
   `/etc/<pkg>/env` is admin-owned and never shipped. A single file either fails the
   unattended upgrade or silently withholds new keys forever.
5. **`postinst` never asks a question.** Interactive configuration lives in
   `<app>-setup`, a separate first-run wizard.
6. **The package never writes to `/var/lib/<pkg>/`.** That directory is the
   client's.
7. **Ubuntu 22.04 is the build floor.** Verified to run on glibc 2.35 through 2.41.
8. **Static system user, never `DynamicUser=yes`.** The latter redirects state to
   `/var/lib/private/<pkg>` at `700 root:root`, so a non-root operator can neither
   read nor list it, backups, monitoring and support all need root, and admin-dropped
   files change owner as the UID rotates.
9. **The install reaches no prompt.** `sudo -n` or an explicit refusal; never block
   on a password. `NEEDRESTART_SUSPEND` is not a real variable (it is absent from
   needrestart 3.6's code); only `NEEDRESTART_MODE` is.
10. **porter hardcodes no interpreter name or version.** Which Python, and whether it
    is bundled per component or emitted as its own package, is declared in each
    project's `porter.yaml`, and no vendor prefix belongs in porter. The same holds
    for the optional bundled browser: the project declares the URL and checksum. A
    shared interpreter package is versioned by the CPython version its tree reports,
    not by the project's, and components depend on it with `(= <that version>)`,
    because `>=` would let a client keep an older tree and run wheels compiled
    against a newer ABI.
11. **`Depends:` is derived from ELF headers, never hand-written.** Any bundled
    native binary gets its libraries mapped to target-distro packages at build time.
    A hand-kept list is how a package installs cleanly and then cannot open a window,
    and it goes stale silently on the next upstream build.
12. **A desktop dependency never enters the core package.** A GUI needs GTK, X11 and
    NSS from the client, which apt cannot fetch on an airgapped box, so it lives in a
    separate `<app>-desktop` package and can never block a headless install.

### Refuse, never repair

When a stage, a manifest or a binary is wrong, porter refuses with a message and
changes nothing. It does not fix the input and carry on. A silent repair is how a
caller's `triggers` or a conffile disappears from a package with the build still
exiting 0. The one exception is state porter created itself, such as the CLI's own
scratch stage, which it removes. The modules that apply this carry the specifics in
their docstrings.

### The gallery is the schema

The `porter.yaml` schema is defined by `examples/`. Each example is a manifest that
must parse and build, so a field that no example exercises does not exist. Writing
the schema in prose first is how the `build:` escape hatch quietly becomes the
default path.

### The gate rule

Every assertion in the gate carries a positive control or a magnitude check.

During the design, five probes reported passes that were false: a dangling symlink
resolving to the build host's own interpreter, a network probe using bash-only
`/dev/tcp` under dash, an interface count where the binary was absent, a
memory-limit check satisfied by "command not found", and a truncated 12 KB package
reported as built. Each was caught only because something downstream contradicted
it. The airgap failures that matter are the ones that look like passes on the build
host. So before asserting isolation, prove the probe detects the thing when
isolation is off; before trusting an artefact, assert its magnitude; and never pipe
a gate, because `cmd | tail` hands `&&` the exit code of `tail`.

### Language

English for everything, with no exceptions: code, comments, identifiers, error
strings, logs, commit messages, CLI text, tests, docs, and the files porter
generates (`install.sh`, `README.txt`, every banner and prompt). If a component
needs operator text in another language, it passes that text in as data
(`readme=`, a component `description`). A tool whose audience speaks Spanish is
still an English tool.

## What a machine checks, and what a reader judges

Checked by a command:

- `make test`: the suite, with the five `PORTER_REQUIRE_*` variables that turn a
  skip into a failure. CI sets the same five.
- `scripts/reverify-guards.sh`: every guard in the registry still bites when its use
  site is disabled.
- CI's `glibc-floor` job: one build runs on every target release.
- `make lint-docs`: the agent docs name only files that exist, and carry no status
  log or know-how list.

Judged by a reader:

- Whether a CHANGELOG claim carries a real measurement.
- Whether a new rule records the measurement that justifies it.
- Whether a guard's positive control proves the probe can see the failure.

## Non-goals

- A container runtime on the client, in any form.
- An interactive install path, including a flag that enables one.
- A schema written ahead of the example that exercises it.
