#!/bin/sh
# Install the LaTeX fontawesome5 package (the contact icons in the CV header)
# into TEXMFLOCAL, from the vendored archive in docker/vendor/ — see
# docker/vendor/README.md for what it is and where it came from. No network
# access is needed for this step; the Dockerfile COPYs the archive in before
# running this script.
#
# Debian ships fontawesome5 only inside texlive-fonts-extra — 1.7 GB for about
# 1 MB of font — so this installs the TeX Live archive instead: it is laid
# out as a texmf tree already, so it unpacks straight into TEXMFLOCAL.
set -eu

ARCHIVE="${1:?usage: install-fontawesome5.sh <path to fontawesome5.tar.xz>}"
TEXMFLOCAL=/usr/local/share/texmf

python - "$ARCHIVE" "$TEXMFLOCAL" <<'PY'
import sys
import tarfile

archive_path, target = sys.argv[1], sys.argv[2]
with tarfile.open(archive_path, mode="r:xz") as archive:
    # tlpkg/ is TeX Live's own package metadata: not part of the texmf tree.
    members = [m for m in archive.getmembers() if not m.name.startswith("tlpkg/")]
    archive.extractall(target, members=members, filter="data")
print(f"fontawesome5: {len(members)} files -> {target}")
PY

# Index the new tree, then register the font map so that pdflatex embeds the
# Type 1 fonts instead of reporting them missing.
mktexlsr
updmap-sys --enable Map=fontawesome5.map

# Fail the build here rather than on the first CV.
kpsewhich fontawesome5.sty > /dev/null || {
    echo "✗ fontawesome5.sty is still not on the TeX search path" >&2
    exit 1
}
