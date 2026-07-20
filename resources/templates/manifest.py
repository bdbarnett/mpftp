# Frozen Python from workspace user-module repos, plus the MicroPython upstream
# freeze for the active port/board/variant.
#
# mpftp sets ``FROZEN_MANIFEST_UPSTREAM`` to the same manifest file MicroPython
# would have selected (most-specific variant/board/port file). This static file
# includes that path so no generated wrapper is needed.
#
# Optional local overrides: ``my-manifest.py`` (gitignored). Use ``package()`` to
# freeze a tree; paths are relative to the current (workspace) directory. The
# first argument is the import name; that name must be a folder under
# ``base_path``. Example::
#
#     package("pdwidgets", base_path="../pdwidgets/src", opt=3)
#
# freezes ``../pdwidgets/src/pdwidgets/`` as importable ``pdwidgets`` (not
# ``src``).

import os

_SKIP = frozenset()

# Optional personal overrides. Missing file is fine; errors inside the file
# must surface (a broad except was silently dropping bad paths).
try:
    include("my-manifest.py")
except OSError:
    pass

for _name in sorted(os.listdir(".")):
    if _name in _SKIP or _name.startswith("."):
        continue
    _path = os.path.join(_name, "manifest.py")
    if os.path.isfile(_path):
        try:
            include(_path)
        except Exception:
            pass

_upstream = os.environ.get("FROZEN_MANIFEST_UPSTREAM", "").strip()
if not _upstream:
    raise Exception(
        "FROZEN_MANIFEST_UPSTREAM is not set. "
        "Build via mpftp Firmware, or export FROZEN_MANIFEST_UPSTREAM to the "
        "MicroPython port/board/variant manifest.py for this build."
    )
include(_upstream)
