"""Guards on docker-compose.yml that are worth failing a build over.

Redpanda in dev-container mode has no authentication and the warehouse holds customer data, so a
published port that is not on loopback exposes both to anything that can reach this machine. That
is a one-character mistake to make and an easy one to miss in review, so it is a test.
"""

import re
from pathlib import Path

COMPOSE = (Path(__file__).resolve().parents[1] / "docker-compose.yml").read_text(encoding="utf-8")

PUBLISHED_PORT = re.compile(r'^\s+- "(?P<mapping>[^"]+)"\s*$', re.MULTILINE)
IMAGE = re.compile(r"^\s+image: (?P<reference>\S+)\s*$", re.MULTILINE)

# Built from airflow/Dockerfile in this repo, so there is no upstream digest to pin to.
LOCAL_IMAGES = ("megavolt/",)


def test_every_published_port_is_bound_to_loopback_only():
    mappings = [match["mapping"] for match in PUBLISHED_PORT.finditer(COMPOSE)]
    assert mappings, "no published ports found - did the compose file move or change shape?"
    for mapping in mappings:
        assert mapping.startswith("127.0.0.1:"), f"{mapping} is reachable beyond this machine"


def test_every_upstream_image_is_pinned_by_digest():
    references = [match["reference"] for match in IMAGE.finditer(COMPOSE)]
    assert references, "no images found - did the compose file move or change shape?"
    for reference in references:
        if reference.startswith(LOCAL_IMAGES):
            continue
        assert "@sha256:" in reference, f"{reference} is pinned by tag, which can be moved"
