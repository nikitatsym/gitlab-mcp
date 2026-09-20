"""The integration services must not float.

A rolling tag makes CI a function of the calendar: GitLab 19.4.0 landed on Docker
Hub and the next push went red on a docs-only change. Pins are bumped in their
own commit, never by a `docker pull`.
"""

from __future__ import annotations

import re
from pathlib import Path

_COMPOSE = Path(__file__).resolve().parents[1] / "tests" / "docker-compose.yml"
_IMAGE = re.compile(r"^\s*image:\s*(\S+)", re.MULTILINE)
_VAR_DEFAULT = re.compile(r"\$\{[A-Z_]+:-([^}]+)\}")


def test_compose_images_carry_a_fixed_tag():
    images = [_VAR_DEFAULT.sub(r"\1", ref) for ref in _IMAGE.findall(_COMPOSE.read_text())]
    assert images, "no image: entries parsed - the compose file or this pattern moved"
    floating = [
        ref for ref in images
        if ":" not in ref.rsplit("/", 1)[-1] or ref.endswith(":latest")
    ]
    assert not floating, f"floating image tags: {floating}"
