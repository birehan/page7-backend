from __future__ import annotations

import io
from decimal import Decimal

import pytest
from PIL import Image

from app.core.errors import ApiError
from app.features.media.service import (
    probe_image_dimensions,
    source_hash,
    validate_declared,
)


def test_validate_declared_rejects_pdf() -> None:
    with pytest.raises(ApiError) as exc:
        validate_declared(content_type="application/pdf", size=100)
    assert exc.value.code == "UNSUPPORTED_MEDIA_TYPE"
    assert exc.value.status_code == 415


def test_validate_declared_rejects_oversized_image() -> None:
    with pytest.raises(ApiError) as exc:
        validate_declared(content_type="image/png", size=11 * 1024 * 1024)
    assert exc.value.code == "PAYLOAD_TOO_LARGE"


def test_svg_bytes_fail_pillow_probe() -> None:
    svg = b'<svg xmlns="http://www.w3.org/2000/svg"></svg>'
    with pytest.raises(ApiError) as exc:
        probe_image_dimensions(svg)
    assert exc.value.code == "UNSUPPORTED_MEDIA_TYPE"


def test_source_hash_changes_with_focal_point() -> None:
    a = source_hash(
        checksum_sha256="abc",
        focal_x=Decimal("0.5"),
        focal_y=Decimal("0.5"),
        template_params=None,
        aspect="square",
    )
    b = source_hash(
        checksum_sha256="abc",
        focal_x=Decimal("0.2"),
        focal_y=Decimal("0.8"),
        template_params=None,
        aspect="square",
    )
    assert a != b


def test_probe_real_png() -> None:
    img = Image.new("RGB", (100, 50), color=1)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    width, height = probe_image_dimensions(buf.getvalue())
    assert (width, height) == (100, 50)
