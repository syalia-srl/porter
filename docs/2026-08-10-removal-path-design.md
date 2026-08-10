---
date: 2026-08-10
type: design
status: approved in conversation 2026-08-10; implementation plan not yet written
extends: docs/design-spec.md
---

# The removal path: `prerm`, `postrm`, purge, and `uninstall.sh`

*Reach for this when changing anything about how a porter package comes off a
client. It extends `docs/design-spec.md`, which covers install, configure and
update and says nothing about removal.*

## The gap

porter emits **one** maintainer script. `assemble.py` sets `scripts["postinst"]`
for `service` and `oneshot` kinds and nothing else; `deb.py` accepts `prerm` and
`postrm` in its `scripts` dict but no caller ever passes them. `usb_tree()`
writes `install.sh`, `README.txt` and the repo. No test in the suite removes or
purges a package.

So the delivery promise — *one command installs, the same command updates* —
has no third verb, and an operator on an airgapped box has no supported way to
back a deployment out.

### What that costs today

Read off the code on 2026-08-10, **not measured** — no removal has been run.
Each becomes a gate assertion below, which is where they get measured.

1. **The service keeps running after `apt-get remove`.** Nothing stops it. dpkg
   deletes the payload out from under a live process.
2. **A dangling enable symlink is left behind.** `postinst` runs `systemctl
   enable`, which links the unit into `multi-user.target.wants/`. dpkg does not
   own that symlink — it never shipped it — so removal leaves it pointing at a
   unit file that no longer exists, with no `daemon-reload`. A `oneshot` leaves
   the same for its `.timer`.
3. **Purge then reinstall silently resurrects the old admin config.**
   `/etc/<pkg>/defaults` is a conffile, so dpkg clears it on purge.
   `/etc/<pkg>/env` is admin-owned and never shipped, so dpkg has never heard of
   it and it survives — and `postinst` only creates `env` *if absent*. The
   reinstall picks the old secrets back up, and `/etc/<pkg>/` is left holding a
   lone `env`.
4. **`/var/lib/<pkg>` survives purge.** Correct on `remove`; on `purge` the
   convention is to clear it.

The system user and group surviving is **correct**, not a defect — see *Known
limits*.

## What removal must guarantee

Five properties. Properties 1–4 are asserted in the **gate**, so an adopter
proves them about their own bundle and not just about porter's examples;
property 5 is asserted in the docker suite, which is where the `setsid` harness
and its control already live. *Testing* below says which file holds what.

1. After `remove`: the service is stopped, the unit file is gone, and **no
   dangling symlink remains anywhere under `/etc/systemd/system`**.
2. After `remove`: `/var/lib/<pkg>` and `/etc/<pkg>/env` are **intact**. Removal
   is not a data-loss operation.
3. After `purge`: both are **gone**, and `/etc/<pkg>/` is gone with them.
4. An **upgrade** stops nothing. The removal path must be unreachable from
   `$1 = upgrade`.
5. Removal reaches **no prompt**, under the same `setsid`-with-stdin-closed
   harness the install is held to. It runs on the same unattended airgapped box.

## The emitted scripts

`prerm` and `postrm` are generated in `config.py` beside `env_postinst`, and
wired in `assemble.py` at the point that sets `scripts["postinst"]` today.

**Only `service` and `oneshot` get them.** `command`, `meta` and the
`<app>-desktop` split ship no maintainer scripts at all: no unit, no system
user, no `/etc`, no state, nothing to undo. A package with nothing to clean up
should not carry a script that says so.

### `prerm` — stop and disable, while the unit file still exists

Guarded twice:

- **on `$1 = remove`** — never on `upgrade`. `postinst`'s `try-restart` owns the
  upgrade path; stopping here would turn every upgrade into an outage. This is
  property 4, and it is the single most consequential line in the script.
- **on `[ -d /run/systemd/system ]`**, with **no blanket `|| true`**. That
  directory is the only thing separating "no systemd here, skip it" from
  "systemd is real and this failed" — the distinction Task 3 paid for, where the
  old `|| true` reported success for both and left a service that was simply
  gone after the next reboot.

A `oneshot` stops and disables both its `.timer` and its `.service`.

**Why disable belongs here and not in `postrm`.** `postrm` runs *after* dpkg has
deleted the unit file, and `systemctl disable` against a unit file that no
longer exists cannot be relied on to clear the symlink. Debian's usual answer is
`deb-systemd-helper`, which tracks enable state independently — but it comes
from `init-system-helpers`, and porter depends on nothing the client did not
already have. Disabling while the file is still on disk needs no helper.

Property 1 is stated as *no dangling symlink under `/etc/systemd/system`*, not
as *`systemctl disable` was called*. If disable proves insufficient, the fix is
an explicit `rm -f` of the symlink porter's own `postinst` caused to exist —
`WantedBy=multi-user.target` makes that path deterministic. **The property is
specified; the mechanism is the implementer's to choose and to justify.**

### `postrm` — reload, and on purge delete

- `remove|purge` → `daemon-reload`, under the same `/run/systemd/system` guard.
- `purge` only → `rm -f /etc/<pkg>/env`; `rmdir /etc/<pkg>` if empty;
  `rm -rf /var/lib/<pkg>`.

Purge clears **exactly the two paths porter owns**, derived from the package
name. There is no manifest field for additional purge paths, and that is a
decision rather than an omission — see *Refused by design*.

## `uninstall.sh`

Written by `usb_tree()` next to `install.sh`.

**It is much smaller than its twin, because removal needs no repository.**
`apt-get remove` and `apt-get purge` operate on dpkg's database, so there is no
source list to write, no keyring to import, no `apt-get update` to scope around
a client's broken network source — and therefore no cleanup trap on the way out.
`install.sh` needs all of that and removes it again afterwards
(`test_signing.py::test_a_signed_install_leaves_the_clients_apt_and_keyrings_as_it_found_them`).
`uninstall.sh` never creates any of it.

What it keeps from `install.sh`: the `sudo -n`-or-refuse lift, and the same
non-interactive exports (`DEBIAN_FRONTEND`, `NEEDRESTART_MODE=a`,
`UCF_FORCE_CONFOLD`, `-o Dpkg::Use-Pty=0`). `NEEDRESTART_SUSPEND` is still not a
real variable and is still not to be reintroduced.

**Flags.** Bare invocation removes. `--purge` purges. `--yes`/`-y` is accepted
and implied, as in `install.sh`. An unknown flag exits 2.

**The default is `remove`, and the reason is specific to porter.** Rule 9 says
the install reaches no prompt; the same holds here, on the same unattended box.
So `uninstall.sh --purge` **cannot ask "are you sure?"** — there is no
confirmation to fall back on, and the flag itself is the entire safety margin.
On a client with no backup story, the irreversible operation is the one that
requires typing something extra.

## Reaping a shared interpreter

`examples/shared-interpreter` emits a 101.7 MB interpreter package that several
components `Depends:` on by exact version. `install.sh` pulls it in as a
dependency, so apt marks it **auto** — and `apt-get remove <component>` leaves
the tree on disk with nothing to ever reclaim it.

`usb_tree()` learns the interpreter package name, and `uninstall.sh` removes it
**only when `dpkg-query` shows no other installed package still depends on it**.
Removing one of three services leaves it; removing the last one takes it.

**Never `apt-get autoremove`.** apt's refcounting is correct, but `autoremove`
is not scoped to our packages — on a client's machine it reaps whatever else apt
considers orphaned, kernels included. porter does not get to run that on someone
else's box to reclaim its own 100 MB.

## The gate

`nspawn_gate()` gains a removal phase **after** its existing assertions, reusing
the boot it has already paid for (33 s end to end on `ubuntu-latest`). Both
failures that matter are systemd failures — a process that outlives its package,
a symlink left dangling in a target's `wants/` — and neither reproduces in
Docker, where there is no PID 1 systemd to leave one.

Putting it in the **gate** rather than only in the suite is the point: an
adopter running the gate against their own bundle proves their package comes off
cleanly, not merely that porter's examples do.

Every assertion carries a positive control or a magnitude check, per the gate
rule. Three registry mutations, each of which must turn the gate red:

| Mutation | What stays green without it |
|---|---|
| Drop the `prerm` stop | A process still serving after its package is gone |
| Drop the disable | A dangling symlink and a `daemon-reload` warning on every later boot |
| Drop the purge `rm` | Purge that leaves credentials and client data on disk |

A guard with no entry in `scripts/reverify-guards.sh` is unverified by the only
test that matters; these are not optional.

## Testing

- **`tests/test_removal.py`** — properties of the generated script text:
  the `$1 = upgrade` unreachability, the systemd guard, the absence of a blanket
  `|| true`, kind-by-kind emission (and non-emission for `command`, `meta`,
  `desktop`), `sh -n` parse.
- **`tests/test_removal_e2e.py`** (`docker`) — file-level outcomes: what
  survives `remove`, what is gone after `purge`, and the purge-then-reinstall
  case that today resurrects the old `env`.
- **`nspawn_gate`** — properties 1 and 4, which need real systemd.
- The `setsid` no-prompt check reuses the harness from
  `test_migrate_e2e.py::test_the_upgrade_reaches_no_prompt_under_setsid_with_stdin_closed`,
  **including its control** — a bare `read` must fail under the same harness, or
  "no prompt reached" proves nothing.

## Refused by design

**There is no `purge_paths:` manifest field.** A component that scattered state
outside `/var/lib/<pkg>` is already outside porter's model — `deb.py` refuses
any stage that writes to `/var/lib` or `/var/log`, and `StateDirectory` is the
sanctioned location.

The alternative is a manifest field that expands into `rm -rf` on a client's
filesystem. A typo'd `purge_paths: [/var]` is unrecoverable on a box with no
backups, and porter would have shipped the mechanism. `spec.py` already refuses
unknown component keys, so a manifest declaring one fails at parse.

By the gallery rule this also means **no new example**: removal is not an opt-in
field, it is behaviour every `service` and `oneshot` package gets. The existing
eleven examples are its coverage.

## Known limits

- **Per-user browser profiles survive purge.** The desktop launcher creates
  `$HOME/.local/share/<pkg>/browser-profile` at runtime, per user. `postrm` runs
  as root with no list of users, and walking `/home` guessing at them is worse
  than leaving them. Named, not omitted.
- **The system user and group survive purge**, deliberately. Debian policy: files
  outside `/var/lib/<pkg>` may still be owned by that uid, and a purge that frees
  the uid turns them into someone else's files the next time one is allocated.
- **Removal is not gated on the client's own dependents.** If a client
  hand-installed something that depends on a porter package, `apt-get remove`
  will report that and refuse; porter adds nothing on top.
- **The costs in *What that costs today* are reasoned from the code, not
  measured.** They are written as gate assertions precisely so the implementation
  measures them; a claim here that the gate then contradicts is the gate being
  right.

## Out of scope

- **`porter uninstall` as a CLI verb.** `build` remains the only verb, for the
  reason `CHANGELOG.md` gives about `gate` and `publish`: the inputs are not
  manifest fields, and a verb with no example defining its shape does not exist.
  `uninstall.sh` is an artifact of the bundle, not of the CLI.
- **Rollback to a previous version.** `install.sh --version` already downgrades
  (`--allow-downgrades`); a distinct rollback story is a different feature.
- **Uninstalling a metapackage's members selectively.** apt already does this,
  and porter has no better opinion than dpkg's dependency graph.
