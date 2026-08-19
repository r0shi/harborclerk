"""Every image that installs apt packages must also upgrade the base.

`python:3.12-slim` is rebuilt on Debian's schedule, not ours, so an image that
only ever runs `apt-get install` ships whatever CVEs the base layer carried on
the day it was tagged.

This is not hypothetical. `docker/reranker.Dockerfile` was the one image of
three without `apt-get upgrade`, and it shipped **nine HIGH** findings from a
single util-linux advisory (CVE-2026-53615, integer overflow in libblkid) —
bsdutils, libblkid1, liblastlog2-2, libmount1, libsmartcols1, libuuid1, login,
mount, util-linux — while app and embedder were clean. The asymmetry is the
whole bug: two of three sites had it, so nothing looked wrong.

`container-scan` catches this eventually, but only after a build, and only for
images the workflow happens to scan. This is the cheap version that names the
file.
"""

from __future__ import annotations

import re
from pathlib import Path

DOCKER_DIR = Path(__file__).resolve().parents[1] / "docker"


def _dockerfiles() -> list[Path]:
    return sorted(DOCKER_DIR.rglob("*.Dockerfile"))


def test_every_apt_installing_image_also_upgrades():
    files = _dockerfiles()
    assert files, "found no Dockerfiles — the glob is broken, not the repo"

    missing = []
    for path in files:
        # Strip comments first. The first version of this test did not, and the
        # explanatory comment added directly above the fix contained the words
        # "apt-get upgrade" — so removing the actual command still passed. A
        # guard that its own documentation satisfies is worse than none.
        text = "\n".join(re.sub(r"#.*$", "", line) for line in path.read_text().splitlines())

        # Only images that touch apt at all are in scope; a pure COPY-from image
        # has no base packages of its own to upgrade.
        if "apt-get install" not in text:
            continue
        if not re.search(r"apt-get\s+upgrade", text):
            missing.append(str(path.relative_to(DOCKER_DIR.parent)))

    assert not missing, (
        f"these images install apt packages but never upgrade the base: {missing}. "
        "They ship whatever CVEs the base layer carried when it was tagged. Add "
        "`apt-get upgrade -y` to the same RUN as the update."
    )
