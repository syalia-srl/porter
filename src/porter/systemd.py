"""Units replace compose services.

depends_on/service_healthy has no systemd equivalent; ordering is After=/
Requires= plus Restart=on-failure convergence. That is a deliberate accepted
behavioural change, recorded in docs/design-spec.md.

**Measured 2026-08-08 on systemd 259**, because the convergence half of that
sentence was an assumption until it was run: with `B` declaring
`After=A.service` `Requires=A.service` and `A` failing its first two starts,
`B` is started anyway, fails, and is retried by its own `Restart=on-failure`
every `RestartSec` until `A` is up -- `A` active at t=6s, `B` active at t=9s.
So `Requires=` does *not* strand the dependent when the dependency's first
start job fails, which is what a reading of "the dependency failed, so the job
is cancelled" would predict. The pair emitted here is the pair that converges.

NOT DynamicUser=yes. Measured 2026-08-07: it redirects StateDirectory to
/var/lib/private/<pkg>, mode 700 root:root, which a non-root operator can
neither read nor list -- so backups, monitoring and support all need root, and
admin-dropped files silently change owner as the UID rotates. A static system
user created in postinst gives a real /var/lib/<pkg> at 750 root:<pkg>, which
passed every non-root access check the private layout failed.
"""
from __future__ import annotations

import shutil
import subprocess
from collections.abc import Sequence

# The kinds that get a unit. `command` gets a wrapper instead and never reaches
# here; see assemble.SUPPORTED_KINDS.
UNIT_KINDS = ("service", "oneshot")


def unit(pkg: str, description: str, exec_start: str, workdir: str,
         user: str | None = None, after: str = "network.target",
         depends_on: Sequence[str] = (), kind: str = "service") -> str:
    """The `<pkg>.service` file, for a long-running service or a timer's job.

    `depends_on` is a sequence of **unit names** -- `demo-alpha.service`, never
    the component's bare `alpha`. The component name is porter's; the unit name
    is the client's, and systemd resolves nothing else. `resolve_ordering`
    below is what performs that translation, and it is the only thing that
    should: a caller that spells the unit name itself is a caller that can
    spell it wrong, silently, because systemd treats a dependency on a unit
    that does not exist as... a dependency on a unit that does not exist.

    Both `After=` and `Requires=` are emitted for each entry, and neither
    alone is enough: `After=` orders the two without coupling their lifecycles
    (a dependent starts happily when its dependency was never started at all),
    `Requires=` couples them without ordering (systemd starts both at once and
    the dependent races). Emitted only when there is something to order -- a
    single-component project gets exactly today's `After=network.target` and no
    `Requires=` line, because a directive whose value is empty is ceremony a
    reader has to interpret.

    `kind="oneshot"` is a scheduled job, run by `<pkg>.timer`:

    - `Type=oneshot` -- the process runs to completion and systemd waits for
      it, rather than treating the fork as success.
    - **No `Restart=`.** For a service, restart-on-failure is the convergence
      mechanism. For a timer job it is a retry loop three seconds wide against
      whatever made the job fail, and the timer is already the retry: a job
      that fails at 03:00 should next run at 03:00 tomorrow, not 28,800 times
      before then.
    - `[Install]` carries `Also=<pkg>.timer` and **no `WantedBy=`**. That is
      what makes the postinst's existing `systemctl enable <pkg>.service`
      correct for a job: measured 2026-08-08, it creates
      `timers.target.wants/<pkg>.timer` and no `multi-user.target.wants` entry
      at all, leaving the service `is-enabled` = `indirect` (rc=0). Enabling
      the *service* of a oneshot into multi-user.target is the failure the
      Task 4 refusal existed to prevent -- a job that runs at boot, forever,
      instead of on its schedule.
    """
    if kind not in UNIT_KINDS:
        raise ValueError(f"unit kind {kind!r} is not one of {UNIT_KINDS}")
    user = user or pkg
    order = ""
    if depends_on:
        # One line each rather than appending to `after`, so the base target and
        # the project's own graph stay legible and separately assertable.
        # systemd accumulates repeated After=.
        order = (f"After={' '.join(depends_on)}\n"
                 f"Requires={' '.join(depends_on)}\n")
    if kind == "oneshot":
        type_line = "Type=oneshot\n"
        # See the docstring: the timer is the retry.
        restart = ""
        install = f"Also={pkg}.timer"
    else:
        type_line = ""
        restart = "Restart=on-failure\nRestartSec=3\n"
        install = "WantedBy=multi-user.target"
    # EnvironmentFile order is the whole split: defaults first, admin second,
    # so the admin's value is the one systemd keeps. The `-` prefix on env
    # makes it optional -- the unit must still start on a client whose admin
    # has removed it, and a missing *required* EnvironmentFile is a start
    # failure with no output beyond the unit's status.
    return f"""[Unit]
Description={description}
After={after}
{order}
[Service]
{type_line}EnvironmentFile=/etc/{pkg}/defaults
EnvironmentFile=-/etc/{pkg}/env
WorkingDirectory={workdir}
ExecStart={exec_start}
User={user}
Group={user}
StateDirectory={pkg}
ProtectSystem=strict
PrivateTmp=yes
NoNewPrivileges=yes
{restart}
[Install]
{install}
"""


def timer(pkg: str, description: str, schedule: str) -> str:
    """The `<pkg>.timer` that runs `<pkg>.service` on a calendar.

    `Persistent=true` is not decoration on an appliance. Without it a machine
    that was powered off at 03:00 simply misses that day's run and says
    nothing; with it, the run happens once at the next boot. An airgapped
    client is exactly the machine that gets switched off, and a crawler that
    silently skipped a day is indistinguishable from one that found nothing.

    No `Unit=`: a timer runs the service of its own name by default, and
    spelling it out invites the two to drift apart.
    """
    if not schedule:
        raise ValueError(
            f"component {pkg!r} is kind 'oneshot' and declares no schedule: a "
            "timer with no OnCalendar= is a unit that loads, enables and never "
            "fires. Add `schedule:` to the manifest"
        )
    _refuse_a_calendar_systemd_cannot_parse(pkg, schedule)
    return f"""[Unit]
Description={description}

[Timer]
OnCalendar={schedule}
Persistent=true

[Install]
WantedBy=timers.target
"""


def _refuse_a_calendar_systemd_cannot_parse(pkg: str, schedule: str) -> None:
    """A misspelled `OnCalendar=` is a job that installs cleanly and never runs.

    Measured 2026-08-08: `OnCalendar=every tuesday-ish` in a shipped timer is
    accepted all the way through `systemctl enable`, which exits 0 and creates
    the `timers.target.wants` symlink. systemd rejects the expression only when
    it loads the unit, in the journal, on the client. Nothing else in porter
    reads this string -- deb.py sees a file of bytes.

    `systemd-analyze calendar` is the parser itself, so this asks the authority
    rather than reimplementing its grammar. It is a **build-host** check and it
    is skipped where the binary is absent, which is a real limit and is stated
    rather than hidden: on such a host porter emits the expression unchecked,
    exactly as it did before this function existed. Ubuntu 22.04, porter's
    build floor, has it.
    """
    if not shutil.which("systemd-analyze"):
        return
    probe = subprocess.run(["systemd-analyze", "calendar", schedule],
                           capture_output=True, text=True)
    if probe.returncode != 0:
        raise ValueError(
            f"component {pkg!r} declares schedule {schedule!r}, which systemd "
            f"cannot parse: the timer would install at rc=0 and never fire. "
            f"rc={probe.returncode} {(probe.stderr or probe.stdout).strip()}"
        )


def unit_name(package: str) -> str:
    """The name systemd knows a component by. Always `<package>.service`.

    A oneshot's dependents order against its `.service` and not its `.timer`:
    the timer is a schedule, and waiting for a schedule is waiting forever.
    """
    return f"{package}.service"


def resolve_ordering(components: Sequence) -> dict[str, list[str]]:
    """Component name -> the unit names its unit must declare a dependency on.

    Takes the whole component list because both refusals below need it: the
    graph is a property of the manifest, not of any one component, and porter
    packages each component into a .deb of its own.

    Two refusals, and each is a silent success without it:

    - **An `after:` naming something undeclared.** systemd has no error for a
      dependency on a unit that does not exist -- `Requires=` on an absent unit
      fails the *start job*, on the client, at boot, with the package having
      installed at rc=0. A typo (`after: [alpah]`) is the whole failure mode,
      and the manifest is the only place it can be caught.
    - **A cycle.** systemd detects one at load time and breaks it by deleting
      an edge it chooses -- "Found ordering cycle... Job <x> deleted to break
      ordering cycle". The result is a boot where one of the two starts first,
      which one being systemd's decision and not the author's, and no failure
      anywhere. Refusing here is the only place the author still exists.

    Returned as a mapping rather than mutated onto the components: `Component`
    is frozen, and a resolution step that writes back would make the manifest
    and the emitted units two sources for one fact.
    """
    by_name = {c.name: c for c in components}
    duplicates = sorted({c.name for c in components if
                         sum(1 for o in components if o.name == c.name) > 1})
    if duplicates:
        raise ValueError(
            f"the manifest declares {', '.join(duplicates)} more than once: "
            "`after:` names a component, so two components with one name make "
            "every reference to it ambiguous"
        )

    for c in components:
        for dep in c.after:
            if dep not in by_name:
                raise ValueError(
                    f"component {c.name!r} declares after: [{dep!r}], which no "
                    f"component in this manifest provides. Declared: "
                    f"{', '.join(sorted(by_name)) or '(none)'}. systemd would "
                    f"accept the resulting Requires= and fail the start job on "
                    f"the client instead"
                )

    _refuse_a_cycle(by_name)
    return {c.name: [unit_name(by_name[d].package) for d in c.after]
            for c in components}


def _refuse_a_cycle(by_name: dict) -> None:
    """Depth-first, reporting the cycle as the path that closes it.

    The message names **unit** names and not component names, because the unit
    names are what an operator reading `systemctl list-dependencies` on the
    client will see, and porter's job is to make the two the same conversation.
    """
    WHITE, GREY, BLACK = 0, 1, 2
    colour = dict.fromkeys(by_name, WHITE)

    def walk(name: str, path: list[str]) -> None:
        colour[name] = GREY
        for dep in by_name[name].after:
            if colour[dep] == GREY:
                cycle = path[path.index(dep):] + [dep]
                raise ValueError(
                    "ordering cycle in the manifest: "
                    + " -> ".join(unit_name(by_name[n].package) for n in cycle)
                    + ". systemd breaks a cycle by deleting one of its jobs and "
                    "booting anyway, so the order would be systemd's choice and "
                    "no failure would be reported"
                )
            if colour[dep] == WHITE:
                walk(dep, path + [dep])
        colour[name] = BLACK

    for name in by_name:
        if colour[name] == WHITE:
            walk(name, [name])
