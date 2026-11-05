import io

import pytest
from PIL import ExifTags, Image

from retouch import export, geometry, ingest


def iou(a, b):
    a, b = a.point(lambda v: 1 if v else 0).tobytes(), b.point(lambda v: 1 if v else 0).tobytes()
    inter = sum(x & y for x, y in zip(a, b))
    union = sum(x | y for x, y in zip(a, b))
    return inter / union


@pytest.mark.parametrize("shape,geom", [
    ("ellipse", {"x": 0.2, "y": 0.3, "w": 0.4, "h": 0.3}),
    ("lasso", {"points": [[0.1, 0.1], [0.8, 0.2], [0.5, 0.9]]}),
    ("brush", {"points": [[0.1, 0.5], [0.9, 0.5]], "width": 0.06}),
])
def test_geometry_is_resolution_independent(shape, geom):
    small = geometry.build_mask(shape, geom, (100, 80))
    big = geometry.build_mask(shape, geom, (300, 240)).resize((100, 80), Image.Resampling.NEAREST)
    assert iou(small, big) > 0.9


def test_pixel_geometry_normalizes():
    norm = geometry.normalize("rect", {"x": 50, "y": 20, "w": 100, "h": 40}, (200, 100))
    assert norm == {"x": 0.25, "y": 0.2, "w": 0.5, "h": 0.4}


@pytest.mark.parametrize("shape,geom", [
    ("ellipse", {"x": 2, "y": 2, "w": 0.1, "h": 0.1}),  # entirely off the image
    ("rect", {"x": 0.1, "y": 0.1, "w": -1, "h": 0.2}),
    ("lasso", {"points": [[0, 0], [1, 1]]}),
    ("star", {}),
    ("ellipse", {"x": "nan", "y": 0, "w": 1, "h": 1}),
])
def test_bad_geometry_is_refused(shape, geom):
    with pytest.raises(geometry.GeometryError):
        geometry.build_mask(shape, geom, (100, 100))


def _jpeg_with_exif(orientation=6, gps=True):
    img = Image.new("RGB", (40, 20), (200, 10, 10))
    img.paste((10, 200, 10), (0, 0, 10, 20))  # green strip on the left
    exif = Image.Exif()
    exif[ExifTags.Base.Orientation] = orientation
    exif[ExifTags.Base.Make] = "TestCam"
    if gps:
        exif[ExifTags.IFD.GPSInfo] = {ExifTags.GPS.GPSLatitudeRef: "N", ExifTags.GPS.GPSLatitude: (37.0, 46.0, 30.0)}
    buf = io.BytesIO()
    img.save(buf, "JPEG", exif=exif.tobytes(), quality=95)
    return buf.getvalue()


def test_exif_orientation_applied_on_ingest():
    got = ingest.ingest(_jpeg_with_exif(orientation=6))
    assert got.image.size == (20, 40)  # rotated upright
    assert got.image.mode == "RGB"


def test_alpha_preserved_on_ingest(rgba_photo):
    buf = io.BytesIO()
    rgba_photo.save(buf, "PNG")
    got = ingest.ingest(buf.getvalue())
    assert got.image.mode == "RGBA" and got.image.tobytes() == rgba_photo.tobytes()


def test_a_file_that_is_not_an_image_is_refused_with_the_formats_that_work():
    with pytest.raises(ingest.IngestError) as refused:
        ingest.ingest(b"not an image at all")
    assert "PNG, JPEG" in str(refused.value) and "Error" not in str(refused.value)


def test_a_damaged_image_is_refused_as_damaged():
    buf = io.BytesIO()
    Image.effect_noise((128, 128), 60).convert("RGB").save(buf, "PNG")  # noise: a long pixel stream to cut
    data = buf.getvalue()
    with pytest.raises(ingest.IngestError, match="damaged or incomplete"):
        ingest.ingest(data[: len(data) // 2])


def test_an_image_too_big_for_pillow_is_refused_as_too_large(monkeypatch):
    monkeypatch.setattr(Image, "MAX_IMAGE_PIXELS", 100)  # 64x64 is then over Pillow's 2x bomb limit
    buf = io.BytesIO()
    Image.new("RGB", (64, 64)).save(buf, "PNG")
    with pytest.raises(ingest.IngestError, match="larger than"):
        ingest.ingest(buf.getvalue())


def test_export_strips_gps_by_default_and_keeps_other_exif():
    got = ingest.ingest(_jpeg_with_exif())
    data, ext = export.encode(got.image, "jpeg", exif=got.exif)
    exif = Image.open(io.BytesIO(data)).getexif()
    assert ext == ".jpg"
    assert exif.get(ExifTags.Base.Make) == "TestCam"
    assert ExifTags.IFD.GPSInfo not in exif
    assert ExifTags.Base.Orientation not in exif


def test_export_can_keep_gps_when_asked():
    got = ingest.ingest(_jpeg_with_exif())
    data, _ = export.encode(got.image, "jpeg", exif=got.exif, strip_gps=False)
    assert ExifTags.IFD.GPSInfo in Image.open(io.BytesIO(data)).getexif()


def test_export_keeps_icc_profile(photo):
    icc = b"fake-icc-profile-bytes" * 10
    data, _ = export.encode(photo, "png", icc_profile=icc)
    assert Image.open(io.BytesIO(data)).info.get("icc_profile") == icc


def test_export_never_overwrites(tmp_path, photo):
    data, ext = export.encode(photo, "png")
    first = export.write_new(tmp_path, "trip", data, ext)
    second = export.write_new(tmp_path, "trip", b"other", ext)
    assert first.name == "trip.png" and second.name == "trip-2.png"
    assert first.read_bytes() == data


def test_export_flattens_alpha_for_jpeg(rgba_photo):
    data, _ = export.encode(rgba_photo, "jpeg")
    assert Image.open(io.BytesIO(data)).mode == "RGB"


def test_export_max_side(photo):
    data, _ = export.encode(photo, "webp", max_side=100)
    assert max(Image.open(io.BytesIO(data)).size) == 100
