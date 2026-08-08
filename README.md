# porter

Airgapped `.deb` installers for Debian-family clients. One command builds a
signed local apt repository onto a USB tree; the client runs one command to
install it, and the same command to update it.

```bash
porter build           # bake -> assemble -> lint -> package
porter gate            # install into a clean, networkless container; upgrade; verify
porter publish --out /media/usb/myapp-1.0
```

No Docker, no container runtime and no system Python on the client. The
interpreter is vendored and native binaries are built on a glibc floor, so one
artifact runs on Ubuntu 22.04 through Debian 13. Docker is a *build* dependency
only.

Start at [`AGENTS.md`](AGENTS.md). Design and measurements:
[`docs/design-spec.md`](docs/design-spec.md).

**Status:** scaffold — design settled, implementation not started.
