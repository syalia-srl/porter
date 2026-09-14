# porter's local gates. `make` lists them.

.PHONY: help test lint-docs know-how

help:
	@echo "make test       the suite, with all five PORTER_REQUIRE_* gates armed (ARGS=... to narrow)"
	@echo "make lint-docs  rift over AGENTS.md, DESIGN.md and know-how/"
	@echo "make know-how   the know-how menu: one when: line per procedure doc"

# Each PORTER_REQUIRE_* variable turns a skip into a failure. Without them, a host
# missing uv, docker, systemd-analyze, a C compiler or systemd-nspawn skips whole
# groups of tests and pytest still exits 0. CI sets the same five; the reason for
# each is in the comment above `env:` in .github/workflows/ci.yml.
#
# A green run here is evidence about this host only. zion runs Ubuntu 26.04 and
# `ubuntu-latest` is 24.04, and `Depends:` derivation once passed locally while
# every package failed on the runner (CHANGELOG 0.1.0, "Fixed in this release").
PORTER_REQUIRE := PORTER_REQUIRE_UV=1 PORTER_REQUIRE_DOCKER=1 PORTER_REQUIRE_SYSTEMD=1 PORTER_REQUIRE_CC=1 PORTER_REQUIRE_NSPAWN=1

test:
	$(PORTER_REQUIRE) uv run --extra dev pytest $(ARGS)

lint-docs:
	rift check

know-how:
	@grep -m1 -H '^when:' know-how/*.md
