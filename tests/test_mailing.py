"""Address labels, image/PDF labels and the upload store."""

import os
import re
import time

import pytest

from conftest import NOW, TZ
from fakes import carrier_label_png, letter_pdf_with_label
from zeprint.errors import InvalidRequest, LabelError, NotFoundError
from zeprint.labels import RenderContext
from zeprint.uploads import UploadStore, sniff
from zeprint.zpl import get_size, render_all, split_labels

TO = "Jane Q. Recipient\n1234 Harbor View Blvd Apt 5\nSeattle, WA 98101-2345"
FROM = "A. Sender|2 Elm St|Tacoma, WA 98402"


def ctx(size="4x6", dpi=300, files=None):
    def open_file(ref):
        if files is None or ref not in files:
            raise NotFoundError(ref)
        return files[ref], {"filename": ref, "content_type": sniff(files[ref])}
    return RenderContext(get_size(size), dpi=dpi, tz=TZ, now=NOW, open_file=open_file)


def label_count(zpl):
    return len(re.findall(r"\^XA", zpl))


def in_bounds(zpl, size, dpi):
    pw, ll = get_size(size).dots(dpi)
    for x, y in re.findall(r"\^FO(\d+),(\d+)", zpl):
        assert int(x) < pw and int(y) < ll
    for m in re.finditer(r"\^FO(\d+),(\d+)\^GFA,(\d+),\d+,(\d+),", zpl):
        x, y, total, rb = map(int, m.groups())
        assert x + rb * 8 <= pw + 8 and y + total // rb <= ll


# -------------------------------------------------------------------- address

@pytest.mark.parametrize("size,dpi", [("4x6", 300), ("4x6", 203), ("2x1", 300), ("2x1", 203)])
def test_address_both(size, dpi):
    from zeprint.labels.address import AddressLabel, AddressParams
    r = AddressLabel().render(AddressParams(to=TO, sender=FROM, note="fragile",
                                            reference="Order 1", barcode="10423"), ctx(size, dpi))
    assert label_count(r.zpl) == (1 if size == "4x6" else 2)     # 2x1: from + to labels
    assert r.data["to"][0] == "Jane Q. Recipient" and r.data["from"][2] == "Tacoma, WA 98402"
    in_bounds(r.zpl, size, dpi)
    if size == "4x6":
        assert "^FDSHIP TO:" in r.zpl and "^BCN" in r.zpl and "^FDOrder 1" in r.zpl


@pytest.mark.parametrize("dpi", [203, 300, 600])
def test_address_2x1_prints_banner_reference_and_barcode(dpi):
    """2x1 used to drop them silently; they go on the recipient's label."""
    from zeprint.labels.address import AddressLabel, AddressParams
    r = AddressLabel().render(AddressParams(to=TO, sender=FROM, note="fragile",
                                            reference="Order 1", barcode="1Z999AA10123456784"),
                              ctx("2x1", dpi))
    frm, to = split_labels(r.zpl)
    assert "^BC" not in frm and "^GB" not in frm                  # return address: plain
    assert "^BCN" in to and "^FD1Z999AA10123456784" in to and "^GB" in to
    in_bounds(r.zpl, "2x1", dpi)
    pw, _ = get_size("2x1").dots(dpi)
    module = int(re.search(r"\^BY(\d+)", to).group(1))
    x = int(re.search(r"\^FO(\d+),\d+\^BC", to).group(1))
    assert x + (11 * (18 + 3) + 2) * module <= pw                  # whole barcode on the label
    only = AddressLabel().render(AddressParams(sender=FROM, include="from", barcode="42"),
                                 ctx("2x1", dpi))
    assert "^FD42" in only.zpl                                     # the only label gets them


def test_address_barcode_fits_or_errors():
    from zeprint.labels.address import AddressLabel, AddressParams
    long = "1234567890" * 4                                          # max_length
    for dpi in (203, 300, 600):                                      # 4x6: narrower bars
        r = AddressLabel().render(AddressParams(to=TO, barcode=long), ctx("4x6", dpi))
        module = int(re.search(r"\^BY(\d+)", r.zpl).group(1))
        x = int(re.search(r"\^FO(\d+),\d+\^BC", r.zpl).group(1))
        assert x + (11 * (40 + 3) + 2) * module <= get_size("4x6").dots(dpi)[0]
    with pytest.raises(LabelError, match="too long for a 2x1"):
        AddressLabel().render(AddressParams(to=TO, barcode=long), ctx("2x1", 300))


def test_address_include_and_missing():
    from zeprint.labels.address import AddressLabel, AddressParams
    lbl = AddressLabel()
    assert label_count(lbl.render(AddressParams(to=TO, sender=FROM, include="to"),
                                  ctx("2x1")).zpl) == 1
    r = lbl.render(AddressParams(sender=FROM, include="both"), ctx("2x1"))
    assert label_count(r.zpl) == 1 and r.data["to"] == []            # prints what it has
    with pytest.raises(LabelError, match="To address"):
        lbl.render(AddressParams(sender=FROM, include="to"), ctx())
    with pytest.raises(LabelError):
        lbl.render(AddressParams(), ctx())


def test_long_address_is_fitted_not_clipped():
    from zeprint.labels.address import AddressLabel, AddressParams
    long = ("Dr. Maximilian Alexander Featherstonehaugh III|Department of Extremely Long "
            "Street Names|98765 Southwest Boulevard of the Pacific Northwest|Unit 4B|"
            "Port Townsend, WA 98368")
    for size in ("2x1", "4x6"):
        r = AddressLabel().render(AddressParams(to=long, include="to"), ctx(size, 203))
        in_bounds(r.zpl, size, 203)


def test_text_block_fits_box():
    from zeprint.labels._text import split_lines, text_block
    assert split_lines("a|b| |c") == ["a", "b", "c"]
    assert split_lines("a|b\nc") == ["a|b", "c"]                       # newlines win
    img = text_block(["x" * 200, "short"], 300, 60, 80, 10)
    assert img.width <= 300 and img.height <= 60


# ---------------------------------------------------------------------- image

def test_image_png_fills_label():
    from zeprint.labels.image import ImageLabel, ImageParams
    files = {"lbl.png": carrier_label_png(812, 1218)}
    r = ImageLabel().render(ImageParams(image="lbl.png"), ctx("4x6", 300, files))
    assert label_count(r.zpl) == 1 and r.data["page_count"] == 1
    in_bounds(r.zpl, "4x6", 300)
    img = render_all(r.zpl)
    assert img.getpixel((600, 60)) == 255 and img.getpixel((100, 100)) == 0   # service block


def test_image_pdf_pages_crop_and_rotation():
    from zeprint.labels.image import ImageLabel, ImageParams
    files = {"usps.pdf": letter_pdf_with_label(3)}
    lbl = ImageLabel()
    r = lbl.render(ImageParams(image="usps.pdf", pages="all", crop="top"), ctx("4x6", 203, files))
    assert label_count(r.zpl) == 3 and r.data["page_count"] == 3
    in_bounds(r.zpl, "4x6", 203)
    first = render_all(split_labels(r.zpl)[0])
    assert first.getpixel((40, 40)) == 0          # rotated back upright: block top-left
    assert label_count(lbl.render(ImageParams(image="usps.pdf", pages="2-3"),
                                  ctx("4x6", 300, files)).zpl) == 2
    with pytest.raises(LabelError, match="page 7"):
        lbl.render(ImageParams(image="usps.pdf", pages="7"), ctx("4x6", 300, files))


def test_image_params_validation():
    from pydantic import ValidationError
    from zeprint.labels.image import ImageParams
    assert ImageParams(crop="bottom-left").crop == "0,0.5,0.5,1"
    for bad in ({"pages": "one"}, {"crop": "0,0,2,1"}, {"crop": "middle"}):
        with pytest.raises(ValidationError):
            ImageParams(**bad)


def test_image_errors():
    from PIL import Image
    import io
    from zeprint.labels.image import ImageLabel, ImageParams
    buf = io.BytesIO()
    Image.new("L", (300, 300), 255).save(buf, format="PNG")
    files = {"blank.png": buf.getvalue(), "junk.png": b"\x89PNG not really"}
    with pytest.raises(LabelError, match="blank"):
        ImageLabel().render(ImageParams(image="blank.png"), ctx(files=files))
    with pytest.raises(LabelError, match="readable"):
        ImageLabel().render(ImageParams(image="junk.png"), ctx(files=files))
    with pytest.raises(LabelError, match="choose"):
        ImageLabel().render(ImageParams(), ctx(files=files))


# -------------------------------------------------------------------- uploads

def test_upload_store(tmp_path):
    store = UploadStore(tmp_path, max_bytes=1 << 20)
    meta = store.put(letter_pdf_with_label(2), "usps.pdf")
    assert meta["content_type"] == "application/pdf" and meta["pages"] == 2
    data, again = store.get(meta["id"])
    assert data.startswith(b"%PDF") and again == meta
    png = store.put(carrier_label_png(100, 150), "../../etc/x.png")
    assert png["filename"] == "x.png"                                  # no paths kept
    assert [m["id"] for m in store.list()] == [png["id"], meta["id"]]
    with pytest.raises(InvalidRequest, match="unsupported"):
        store.put(b"hello world")
    with pytest.raises(InvalidRequest, match="MiB"):
        store.put(b"%PDF" + b"0" * (2 << 20))
    with pytest.raises(NotFoundError):
        store.get("../config")
    store.delete(png["id"])
    with pytest.raises(NotFoundError):
        store.meta(png["id"])
    old = time.time() - 30 * 86400
    os.utime(tmp_path / f"{meta['id']}.bin", (old, old))
    assert store.prune() == 1 and store.list() == []


def test_api_upload_preview_and_print_file(client, printer):
    pdf = letter_pdf_with_label(2)
    r = client.post("/api/uploads?filename=usps.pdf", content=pdf)
    assert r.status_code == 201
    meta = r.json()
    assert meta["pages"] == 2
    assert client.get(f"/api/uploads/{meta['id']}/file").content == pdf
    r = client.post("/api/labels/image/preview", json={"image": meta["id"], "pages": "all"})
    assert r.status_code == 200 and r.content[:4] == b"\x89PNG"
    r = client.post("/api/print-file?pages=all&crop=top&wait=30", content=pdf)
    assert r.status_code == 200 and r.json()["status"] == "done", r.json()
    assert printer.wait_for(1)[0].count(b"^XA") == 2
    assert client.post("/api/uploads", content=b"not a file").status_code == 422
    assert client.post("/api/print-file?copies=500", content=pdf).status_code == 422
    assert client.delete(f"/api/uploads/{meta['id']}").status_code == 204


def test_image_from_url(svc, fake_net, monkeypatch):
    png = carrier_label_png(400, 600)
    real = fake_net.__call__
    monkeypatch.setattr("zeprint.net.fetch",
                        lambda url, **kw: png if url.endswith("label.png") else real(url, **kw))
    r = svc.render("image", {"image": "https://example.com/ship/label.png"}, size="4x6")
    assert r.data["source"] == "label.png"
    from zeprint.errors import LabelError as LE
    with pytest.raises(LE, match="upload id"):
        svc.render("image", {"image": "/etc/passwd"}, size="4x6")


def test_cli_at_file(tmp_path):
    from zeprint.__main__ import main
    src = tmp_path / "label.png"
    src.write_bytes(carrier_label_png(400, 600))
    out = tmp_path / "out.zpl"
    rc = main(["--data-dir", str(tmp_path / "data"), "render", "image", "-p",
               f"image=@{src}", "--size", "2x1", "--zpl", str(out)])
    assert rc == 0 and out.read_text().startswith("~SD")
