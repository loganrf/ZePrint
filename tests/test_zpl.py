import re

import numpy as np
import pytest
from PIL import Image

from zeprint.zpl import (ZPL, calibrate_zpl, compress_hex, decompress_hex, encode_image,
                         field_data, get_size, render_preview)


def _img(arr):
    return Image.fromarray(np.where(arr, 0, 255).astype(np.uint8))


@pytest.mark.parametrize("w,h", [(8, 1), (16, 3), (803, 17), (1200, 40), (3208, 5)])
def test_raster_round_trip(w, h):
    rng = np.random.default_rng(w * h)
    arr = rng.random((h, w)) < 0.5
    if h > 3:
        arr[1:3] = arr[0]           # repeated rows -> ':'
    arr[-1] = True                  # all-black row -> '!'
    packed, rb, rows = encode_image(_img(arr))
    enc = compress_hex(packed, rb)
    assert decompress_hex(enc, rb, len(packed)) == packed
    assert decompress_hex(enc.replace("\n", ""), rb, len(packed)) == packed


@pytest.mark.parametrize("n", [1, 2, 19, 20, 21, 39, 40, 399, 400, 401, 420, 802, 1600])
def test_run_length_boundaries(n):
    arr = np.zeros((1, 1608), bool)
    arr[0, :n] = True
    packed, rb, _ = encode_image(_img(arr))
    assert decompress_hex(compress_hex(packed, rb), rb, len(packed)) == packed


def test_encode_crops_to_whole_bytes():
    packed, rb, rows = encode_image(Image.new("L", (803, 2), 0))
    assert (rb, rows) == (100, 2)


def test_sizes_and_aliases():
    assert get_size("1x2").id == "2x1"
    assert get_size("6x4").id == "4x6"
    assert get_size("4X6").dots(203) == (812, 1218)
    with pytest.raises(ValueError):
        get_size("3x5")


@pytest.mark.parametrize("dpi,pw,ll", [(203, 812, 1218), (300, 1200, 1800), (600, 2400, 3600)])
def test_builder_scales_to_dpi(dpi, pw, ll):
    z = ZPL("4x6", dpi, darkness=40, speed=3, media="ribbon")
    z.text(40, 40, 60, "hello")
    z.hline(40, 100, 1120, 3)
    zpl = z.build()
    assert f"^PW{pw}" in zpl and f"^LL{ll}" in zpl
    assert "~SD30" in zpl                     # darkness clamped
    assert "^MTT" in zpl and "^PR3" in zpl
    k = dpi / 300
    assert f"^FO{round(40 * k)},{round(40 * k)}^A0N,{round(60 * k)},{round(60 * k)}^FDhello" in zpl
    assert zpl.rstrip().endswith("^PQ1\n^XZ")


def test_builder_image_reports_design_units():
    z = ZPL("4x6", 203)
    img = Image.new("L", (z.dots(400), z.dots(100)), 0)
    w, h = z.image(None, 500, img)
    assert abs(h - 100) < 1 and abs(w - 400) < 12      # width cropped to whole bytes
    m = re.search(r"\^FO(\d+),(\d+)\^GFA,(\d+),\d+,(\d+),", z.build())
    assert int(m.group(2)) == z.dots(500)
    assert int(m.group(1)) == z.dots((300 * 4 - w) / 2)


def test_qr_magnification_is_clamped():
    assert ZPL("4x6", 600).qr_mag(8) == 10
    assert ZPL("2x1", 203).qr_mag(1) == 1


def test_field_data_escapes_control_characters():
    assert field_data("plain_text") == "^FDplain_text"
    assert field_data("a^b~c_d") == "^FH^FDa_5Eb_7Ec_5Fd"


def test_calibrate_zpl_uses_device_dots():
    zpl = calibrate_zpl("2x1", 203)
    assert "^PW406" in zpl and "^LL203" in zpl and "^ML365" in zpl
    assert zpl.index("^JUS") < zpl.index("~JC")


def test_preview_renders_text_boxes_and_rasters():
    z = ZPL("2x1", 300)
    z.box(10, 10, 60, 60, 60)
    arr = np.zeros((40, 80), bool)
    arr[:, :40] = True
    z.image(300, 100, _img(arr))
    z.text(10, 200, 30, "Label ^ text")
    z.qr(450, 150, "https://example.com", 3)
    img = render_preview(z.build())
    assert img.size == (600, 300)
    assert img.getpixel((40, 40)) == 0          # solid box
    assert img.getpixel((310, 110)) == 0        # left half of the raster is black
    assert img.getpixel((370, 110)) == 255      # right half white
