"""Read the real resolution out of an H.264 stream.

Why this exists: every source site in this ecosystem advertises "4K", "蓝光", "高清". The
measurement that settles it is not in the catalogue and not in the page title -- it is the
sequence parameter set the encoder wrote into the stream. If the SPS says 1920x1080, the
"4K" label is marketing, no matter how it is phrased.

Scope and limits (deliberate):

* Annex-B byte streams only -- that covers HLS (``.ts`` segments) and most ``.flv``.
  ``.mp4`` stores the SPS in an ``avcC`` box with a different framing; not handled, and the
  caller gets ``None`` rather than a guess.
* Only H.264. HEVC (``hvc1``) has a different parameter-set layout; also ``None``.
* The scan is a byte-level start-code search, not a full MPEG-TS demux. An SPS split across
  two 188-byte packets would be missed -- in practice the first segment of an HLS stream
  carries it whole at the head, which is the only place this reads.
"""

from __future__ import annotations

from dataclasses import dataclass

#: Profiles whose SPS carries the chroma_format_idc block.
_HIGH_PROFILES = frozenset({100, 110, 122, 244, 44, 83, 86, 118, 128, 138, 139, 134, 135})

_NAL_SPS = 7


@dataclass(frozen=True)
class SpsInfo:
    width: int
    height: int
    profile: int
    level: int

    @property
    def label(self) -> str:
        return f"{self.width}x{self.height}"


def _unescape_rbsp(data: bytes) -> bytes:
    """Strip emulation-prevention bytes (00 00 03 -> 00 00)."""
    out = bytearray()
    i = 0
    n = len(data)
    while i < n:
        if i + 2 < n and data[i] == 0 and data[i + 1] == 0 and data[i + 2] == 3:
            out += data[i : i + 2]
            i += 3
        else:
            out.append(data[i])
            i += 1
    return bytes(out)


class _Bits:
    def __init__(self, data: bytes) -> None:
        self._data = data
        self._pos = 0

    def u(self, count: int) -> int:
        value = 0
        for _ in range(count):
            byte_index = self._pos >> 3
            if byte_index >= len(self._data):
                raise ValueError("truncated SPS")
            bit = (self._data[byte_index] >> (7 - (self._pos & 7))) & 1
            value = (value << 1) | bit
            self._pos += 1
        return value

    def ue(self) -> int:
        zeros = 0
        while self.u(1) == 0:
            zeros += 1
            if zeros > 32:
                raise ValueError("bad exp-golomb code")
        if zeros == 0:
            return 0
        return (1 << zeros) - 1 + self.u(zeros)

    def se(self) -> int:
        code = self.ue()
        return (code + 1) // 2 if code % 2 else -(code // 2)


def parse_sps(payload: bytes) -> SpsInfo | None:
    """Parse an SPS NAL unit *without* its start code / NAL header byte."""
    try:
        bits = _Bits(_unescape_rbsp(payload))
        profile = bits.u(8)
        bits.u(8)  # constraint flags
        level = bits.u(8)
        bits.ue()  # seq_parameter_set_id

        chroma_format_idc = 1
        separate_colour_plane = 0
        if profile in _HIGH_PROFILES:
            chroma_format_idc = bits.ue()
            if chroma_format_idc == 3:
                separate_colour_plane = bits.u(1)
            bits.ue()  # bit_depth_luma_minus8
            bits.ue()  # bit_depth_chroma_minus8
            bits.u(1)  # qpprime_y_zero_transform_bypass_flag
            if bits.u(1):  # seq_scaling_matrix_present_flag
                for index in range(8 if chroma_format_idc != 3 else 12):
                    if not bits.u(1):
                        continue
                    # Skip the scaling list: the delta walk matters only for the lengths.
                    last_scale, next_scale = 8, 8
                    for _ in range(16 if index < 6 else 64):
                        if next_scale:
                            next_scale = (last_scale + bits.se() + 256) % 256
                        last_scale = next_scale if next_scale else last_scale

        bits.ue()  # log2_max_frame_num_minus4
        poc_type = bits.ue()
        if poc_type == 0:
            bits.ue()  # log2_max_pic_order_cnt_lsb_minus4
        elif poc_type == 1:
            bits.u(1)
            bits.se()
            bits.se()
            for _ in range(bits.ue()):
                bits.se()

        bits.ue()  # max_num_ref_frames
        bits.u(1)  # gaps_in_frame_num_value_allowed_flag
        width_mbs = bits.ue() + 1
        height_map_units = bits.ue() + 1
        frame_mbs_only = bits.u(1)
        if not frame_mbs_only:
            bits.u(1)  # mb_adaptive_frame_field_flag
        bits.u(1)  # direct_8x8_inference_flag

        crop_left = crop_right = crop_top = crop_bottom = 0
        if bits.u(1):  # frame_cropping_flag
            crop_left = bits.ue()
            crop_right = bits.ue()
            crop_top = bits.ue()
            crop_bottom = bits.ue()

        if chroma_format_idc == 0 or separate_colour_plane:
            crop_unit_x, crop_unit_y = 1, 2 - frame_mbs_only
        elif chroma_format_idc in (1, 2):
            crop_unit_x, crop_unit_y = 2, 2 * (2 - frame_mbs_only)
        else:
            crop_unit_x, crop_unit_y = 1, 2 - frame_mbs_only

        width = width_mbs * 16 - (crop_left + crop_right) * crop_unit_x
        height = height_map_units * 16 * (2 - frame_mbs_only) - (crop_top + crop_bottom) * crop_unit_y
        if width <= 0 or height <= 0:
            return None
        return SpsInfo(width=width, height=height, profile=profile, level=level)
    except (ValueError, IndexError):
        return None


def find_sps(stream: bytes, *, scan_limit: int = 600_000) -> bytes | None:
    """Return the first SPS NAL payload in an Annex-B byte stream."""
    window = stream[:scan_limit]
    index = 0
    while True:
        start = window.find(b"\x00\x00\x01", index)
        if start < 0:
            return None
        header = start + 3
        if header >= len(window):
            return None
        if window[header] & 0x1F == _NAL_SPS:
            end = window.find(b"\x00\x00\x01", header + 1)
            return window[header + 1 : end if end > 0 else len(window)]
        index = header


def resolution_from_ts(stream: bytes) -> SpsInfo | None:
    """Real resolution of an MPEG-TS / Annex-B segment, or ``None`` if undeterminable."""
    payload = find_sps(stream)
    if payload is None:
        return None
    return parse_sps(payload)


def is_4k(info: SpsInfo | None) -> bool | None:
    """``None`` when unmeasured -- never ``False`` as a stand-in for "don't know"."""
    if info is None:
        return None
    return info.width >= 3800 or info.height >= 2100
