# know-how: proving a guard actually bites

*When to reach for it:* you have added or changed a guard — a lint rule, a
validation, a refusal — and need to show it works. Also when reviewing someone
else's mutation evidence, or before a release.

**The rule this serves:** a check that cannot fail is worth less than no check,
because it licenses shipping. Every silent-success bug found in porter so far was
caught by mutation and none by the suite going red on its own.

## The procedure

```
mutate the USE SITE  ->  purge caches  ->  run  ->  restore  ->  purge  ->  run again
```

The trailing run is the control. If the suite is not green after restore, the
harness is dirty and no verdict above it means anything.

`scripts/reverify-guards.sh` does this for every guard in the codebase. **Add an
entry when you add a guard** — one with no entry is unverified by the only test
that matters.

## Trap 1 — a reverted mutation that keeps running

CPython invalidates bytecode on source **mtime and size**. An edit that preserves
the byte count and is reverted inside the same second leaves a `.pyc` that Python
considers current, so the *mutated* module keeps loading against a clean git tree.

Found 2026-08-08 in Task 3: swapping the unit's two `EnvironmentFile` lines is
byte-identical in length, and after `git checkout` the mutant was still running.
The implementer nearly read a green suite off a module that no longer existed on
disk.

```bash
find src tests -name __pycache__ -type d -exec rm -rf {} +
```

Purge after **every** mutate and after **every** restore. Not once at the end.

## Trap 2 — a mutation that mutates nothing

Mutate where the guard is *used*, never the constant it reads.

```python
ALLOWED_TOP_LEVEL = () or ("usr", "etc")   # this is ("usr", "etc"). () is falsy.
```

That "mutation" changes the file's bytes, passes a `cmp` check, and changes
nothing. The harness then reports a **working guard as broken** — a false alarm
inside the tool built to catch false passes. Measured 2026-08-08 while writing
`reverify-guards.sh`.

Mutate the branch instead: `if entry.name not in ALLOWED_TOP_LEVEL:` → `if False:`.

## Trap 3 — reproducing *a* failure instead of *the* failure

A red suite is not the goal; reproducing the original symptom is. porter's
characteristic bug is **silent success** — a wrong artefact produced at rc=0 —
so the mutation should show that, not merely an exception somewhere.

Worked examples from this repo:

| Guard | Mutation | What it showed |
|---|---|---|
| interpreter provenance | drop the check | `vendor()` returns **successfully** with the wrong tree, all 8 tests green |
| fresh `DEBIAN/` | `mkdir(exist_ok=True)` | build at rc=0 shipping `{'postinst','control','conffiles'}` nobody passed |
| FHS allowlist | `if False:` | build at rc=0 shipping `./home/apiad/secrets/id_rsa` |
| payload magnitude | stub the interpreter | 30,912-byte package; **every path assertion still passed** |

That last one is why magnitude checks exist: "the file is there" and "the file is
real" are different questions.

## Trap 4 — overlapping guards

Two guards can cover the same case, so removing one alone does not reproduce the
symptom. In Task 2, the `DEBIAN/` refusal was also caught by the new top-level
allowlist; only removing **both** reproduced the original rc=0 build.

Report that plainly. An implementer who glosses it leaves the next reader
believing a single guard is load-bearing when it is not.

## Trap 5 — testing the test harness with the test harness

A test whose *subject* is pytest's control flow cannot use pytest's control flow
as its assertion mechanism.

Measured 2026-08-08 in Task 3: a test asserting that `PORTER_REQUIRE_SYSTEMD`
turns a skip into a failure used `pytest.raises(pytest.fail.Exception)`. That
does not catch `Skipped` — so under mutation the skip propagated, pytest marked
*that test* skipped, and the run was green and silent. The arming test failed to
notice the variable had been disarmed, which is the exact failure the variable
exists to prevent.

Use an explicit outcome helper that inspects the result, not an exception matcher
that happens to share a base class with the thing you are measuring.

The general form: **when the thing under test is the mechanism you would normally
assert with, you need a second, independent mechanism.** Same reason a positive
control must not share a failure mode with the thing it controls.

## Reviewing someone else's evidence

Ask of each mutation:

1. Did it change behaviour, or only bytes? (Trap 2)
2. Were caches purged between mutate and restore? (Trap 1)
3. Does the failure it produced match the *original* symptom, or is it incidental? (Trap 3)
4. Does the positive control fail when the thing it guards is inverted — or does it pass no matter what?

A test that cannot go red is the finding. Say so even when the code is correct;
the docstring is usually what needs fixing, not the guard.
