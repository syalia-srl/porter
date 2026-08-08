#!/usr/bin/env bash
# The escape hatch, exercised: this script IS the assemble stage for
# examples/custom-build. porter creates an empty stage, hands it over in
# $PORTER_STAGE, and packages whatever is here when the script exits 0.
#
# porter runs it as `bash -e -o pipefail build.sh` from the manifest's own
# directory. The shebang is therefore decoration for anyone running it by hand;
# `set -u` is not, so it is here.
set -u

: "${PORTER_STAGE:?porter did not pass a stage -- run this through porter build}"
: "${PORTER_PACKAGE:?}"
: "${PORTER_VERSION:?}"

# Every path is built from $PORTER_PACKAGE and never hardcoded. The package
# name is one string in porter.yaml and it lands in the .deb's `Package:` field
# whatever this script does, so a tree written from anything else is a tree
# that can drift out of its own package -- two owners for /usr/lib, and dpkg
# reporting a file conflict on the client.
bin="$PORTER_STAGE/usr/bin"
share="$PORTER_STAGE/usr/share/$PORTER_PACKAGE"
conf="$PORTER_STAGE/etc/$PORTER_PACKAGE"
mkdir -p "$bin" "$share" "$conf"

# The provenance block bake computed: package, version, build time and the
# commit the source tree was at. porter deliberately does NOT write this file
# itself -- /usr/share/<pkg>/VERSION is inside the hook's tree, and porter
# writing there after the hook ran would overwrite an adopter's own file at
# rc=0. So the hook is handed the text and decides.
printf '%s' "$PORTER_STAMP" > "$share/VERSION"

# A generated asset, to make the point that the payload is produced here rather
# than copied from the repo: nothing in examples/custom-build/ contains this
# text.
{
  printf '%s %s\n' "$PORTER_PACKAGE" "$PORTER_VERSION"
  printf '%s\n' "$PORTER_DESCRIPTION"
  printf -- '----\n'
  printf 'built by the porter build hook, not by porter\n'
} > "$share/summary.txt"

# The shipped half of rule 4: package-owned config, registered as a conffile.
# porter derives `conffiles` from the tree, so this file is declared to dpkg
# without the manifest listing it -- and deb.py refuses any /etc path that is
# NOT declared, so the derivation and the lint are two readings of one
# directory that cannot disagree.
#
# There is no /etc/<pkg>/env here and there could not be: shipping one is
# refused by the lint whoever staged it.
#
# $PORTER_DESCRIPTION is NOT interpolated here, and that is a bug this example
# shipped once. porter-report `.`-sources this file, so an apostrophe in the
# description ("porter's example of...") becomes an unterminated quoted string
# and the tool dies with rc=2 -- on the client, at first run, having built,
# linted and installed at rc=0. porter's `sh -n` pass reads /usr/bin and cannot
# see it: the file that does not parse is under /etc. Config a script sources
# is code, and only values a hook controls the shape of belong in it. A package
# name is [a-z0-9.+-] by dpkg's own rule; free text is not.
cat > "$conf/report.conf" <<EOF
# Package-owned defaults for $PORTER_PACKAGE. dpkg will not replace an edited
# copy of this file without saying so.
REPORT_WIDTH=72
REPORT_TITLE="$PORTER_PACKAGE report"
EOF

# The tool itself: POSIX sh, no interpreter to vendor. build_deb runs `sh -n`
# over everything porter finds in /usr/bin, so a syntax error here fails the
# build rather than the client -- a guarantee this package gets for free by
# going through the hatch instead of around porter.
cat > "$bin/porter-report" <<EOF
#!/bin/sh
set -eu
conf=/etc/$PORTER_PACKAGE/report.conf
[ -r "\$conf" ] && . "\$conf"
width=\${REPORT_WIDTH:-72}
printf '%s\n' "\${REPORT_TITLE:-$PORTER_PACKAGE}"
awk -v w="\$width" '{ print substr(\$0, 1, w) }' /usr/share/$PORTER_PACKAGE/summary.txt
EOF
chmod 755 "$bin/porter-report"
