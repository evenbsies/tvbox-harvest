"""The SPS is the only claim about picture size that cannot be marketing.

Two ways to get this wrong, both of which this file pins:

1. Trusting the label. Sites say "4K"; the encoder says 1920x1080.
2. Trusting "not measured" as "low quality". A stream whose SPS cannot be read is *unknown*,
   and the scorecard must say so rather than print 0 or False.
"""

from __future__ import annotations

import pytest

from harvest.probe.h264 import SpsInfo, find_sps, is_4k, parse_sps, resolution_from_ts

#: Captured from a real HLS segment (zhuiying3.cc, 2026-10-17). profile=100, level=40,
#: 1920x1080 after cropping 1088 -> 1080. Kept verbatim: a synthetic SPS would only prove
#: the writer and the reader agree, not that either matches the standard.
REAL_SPS_1080P = bytes.fromhex("640028acd940780227e5c05a808080a0000003002000000641e30632c000")


def _annexb(payload: bytes, nal_type: int = 7) -> bytes:
    return b"\x00\x00\x01" + bytes([0x60 | nal_type]) + payload


def test_a_real_1080p_sps_is_read_as_1080_not_1088() -> None:
    """The crop units are the easy bug: 1088 is the macroblock-aligned height, 1080 is real."""
    info = resolution_from_ts(_annexb(REAL_SPS_1080P))
    assert info is not None
    assert (info.width, info.height) == (1920, 1080)
    assert info.profile == 100
    assert info.level == 40


def test_crop_units_follow_the_chroma_format() -> None:
    """A 4:2:0 stream crops in units of 2, not 4 -- using 4 here silently loses 8 pixels."""
    info = parse_sps(REAL_SPS_1080P)
    assert info is not None
    assert info.height % 8 == 0
    assert info.height == 1080


def test_non_sps_nals_are_skipped() -> None:
    stream = _annexb(b"\x11\x22\x33", nal_type=6) + _annexb(REAL_SPS_1080P) + _annexb(b"\x44", nal_type=8)
    assert find_sps(stream) == REAL_SPS_1080P


def test_garbage_returns_none_instead_of_raising() -> None:
    """Probing a CDN that answers with an HTML error page must not crash the run."""
    assert resolution_from_ts(b"<html>404</html>") is None
    assert parse_sps(b"") is None
    assert parse_sps(b"\xff\xff\xff\xff") is None


def test_no_start_code_means_no_measurement() -> None:
    assert find_sps(REAL_SPS_1080P) is None  # payload without a start code is not a stream


@pytest.mark.parametrize(
    ("width", "height", "expected"),
    [
        (3840, 2160, True),
        (4096, 2160, True),
        (1920, 1080, False),
        (1280, 720, False),
    ],
)
def test_is_4k_is_a_threshold_not_a_label(width: int, height: int, expected: bool) -> None:
    assert is_4k(SpsInfo(width=width, height=height, profile=100, level=51)) is expected


def test_unmeasured_is_none_never_false() -> None:
    """`False` would read as "measured, and it is not 4K" -- a different claim."""
    assert is_4k(None) is None
