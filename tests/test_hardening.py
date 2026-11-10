"""What a steered agent, a hostile image provider or an oversized request must not be able to do."""
import base64
import io
import socket

import pytest
from PIL import Image

from conftest import noise
from retouch import export, geometry, service, store


@pytest.fixture
def ws(tmp_path):
    return service.Workspace(tmp_path / "ws")


@pytest.fixture
def pid(ws):
    buf = io.BytesIO()
    noise((64, 48), 9).save(buf, "PNG")
    project = ws.create_project("p.png", buf.getvalue())["id"]
    ws.add_pin(project, shape="rect", geom={"x": 4, "y": 4, "w": 30, "h": 30}, units="px")
    return project


def png_bytes(size=(64, 48), color=(255, 0, 0)):
    buf = io.BytesIO()
    Image.new("RGB", size, color).save(buf, "PNG")
    return buf.getvalue()


# -- from_file reads only the project's work folder -------------------------------------------------
def test_from_file_refuses_a_file_outside_the_work_folder(ws, pid, tmp_path):
    elsewhere = tmp_path / "private.png"
    elsewhere.write_bytes(png_bytes())
    with pytest.raises(service.RetouchError, match="work folder"):
        ws.edit(pid, 1, method="from_file", file=str(elsewhere))
    assert len(ws.project(pid)["versions"]) == 1


def test_from_file_refuses_a_symlink_that_leaves_the_work_folder(ws, pid, tmp_path):
    elsewhere = tmp_path / "private.png"
    elsewhere.write_bytes(png_bytes())
    link = ws.work_dir(pid) / "looks-local.png"
    link.symlink_to(elsewhere)
    with pytest.raises(service.RetouchError, match="work folder"):
        ws.edit(pid, 1, method="from_file", file=str(link))


def test_from_file_reports_a_non_image_without_crashing(ws, pid):
    bad = ws.work_dir(pid) / "notes.png"
    bad.write_text("not an image")
    with pytest.raises(service.RetouchError, match="could not be used"):
        ws.edit(pid, 1, method="from_file", file=str(bad))


# -- the image provider's answer is untrusted ---------------------------------------------------------
@pytest.mark.parametrize("ref", [
    "http://example.com/a.png",          # not https
    "file:///etc/hosts",
    "ftp://example.com/a.png",
    "https://127.0.0.1/a.png",
    "https://localhost/a.png",
    "https://2130706433/a.png",          # decimal spelling of 127.0.0.1
    "https://[::1]/a.png",
    "https://10.0.0.5/a.png",
    "https://192.168.1.1/a.png",
    "https://169.254.169.254/latest/meta-data",
])
def test_generated_image_links_to_private_or_non_https_places_are_refused(ref):
    with pytest.raises(service.RetouchError) as err:
        service.load_image_ref(ref)
    assert ref not in str(err.value)


def test_a_redirect_to_a_private_address_is_refused(monkeypatch):
    public = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443))]
    private = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 443))]
    monkeypatch.setattr(service.socket, "getaddrinfo", lambda host, *a, **k: private if host == "inside.test" else public)
    handler = service._CheckedRedirects()
    with pytest.raises(service.RetouchError, match="private address"):
        handler.redirect_request(None, None, 302, "Found", {}, "https://inside.test/secret.png")


def test_the_download_connects_to_the_address_that_was_checked(monkeypatch):
    """A host that answers the check with a public address and a later lookup with a private one
    gains nothing: there is one lookup, and the connection goes to what it returned."""
    answers = iter([[(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443))],
                    [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 443))]])
    monkeypatch.setattr(service.socket, "getaddrinfo", lambda *a, **k: next(answers))
    dialled = []

    def refuse(address, *a, **k):
        dialled.append(address)
        raise OSError("stop here")

    monkeypatch.setattr(service.socket, "create_connection", refuse)
    with pytest.raises(OSError, match="stop here"):
        service._PublicHTTPSConnection("rebind.test").connect()
    assert dialled == [("93.184.216.34", 443)]


def test_the_download_refuses_to_connect_to_a_private_address(monkeypatch):
    monkeypatch.setattr(service.socket, "getaddrinfo",
                        lambda *a, **k: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.0.0.5", 443))])
    monkeypatch.setattr(service.socket, "create_connection", lambda *a, **k: pytest.fail("connected"))
    with pytest.raises(service.RetouchError, match="private address"):
        service._PublicHTTPSConnection("inside.test").connect()


def test_a_provider_file_outside_the_allowed_folder_is_refused(tmp_path):
    cache = tmp_path / "cache"
    cache.mkdir()
    inside, outside = cache / "gen.png", tmp_path / "private.png"
    inside.write_bytes(png_bytes())
    outside.write_bytes(png_bytes())
    assert service.load_image_ref(str(inside), [cache]).size == (64, 48)
    with pytest.raises(service.RetouchError):
        service.load_image_ref(str(outside), [cache])
    with pytest.raises(service.RetouchError):
        service.load_image_ref(str(inside))  # no folder allowed: no local file is read


@pytest.mark.parametrize("ref", ["data:image/png;base64", "data:image/png;base64,@@@not-base64@@@", "",
                                 "data:text/plain;base64," + base64.b64encode(b"hello").decode()])
def test_a_malformed_generated_image_is_an_error_the_agent_can_read(ref):
    with pytest.raises(service.RetouchError):
        service.load_image_ref(ref)


# -- exports ------------------------------------------------------------------------------------------
def exif_with_private_fields():
    exif = Image.Exif()
    exif[270] = "/Users/someone/Photos/trip.jpg"   # ImageDescription
    exif[269] = "trip.jpg"                          # DocumentName
    exif[316] = "someones-macbook"                  # HostComputer
    exif[271] = "CameraCo"                          # Make: kept
    exif.get_ifd(0x8769)[37510] = b"ASCII\x00\x00\x00shot at home"  # UserComment
    exif.get_ifd(0x8825)[2] = (37.0, 46.0, 30.0)   # GPSLatitude
    return exif.tobytes()


@pytest.mark.parametrize("fmt", ["jpeg", "png", "webp"])
def test_an_export_carries_no_location_file_path_or_host_name(fmt):
    data, _ = export.encode(Image.new("RGB", (32, 32), (10, 20, 30)), fmt, exif=exif_with_private_fields())
    for secret in (b"/Users/someone", b"trip.jpg", b"someones-macbook", b"shot at home"):
        assert secret not in data
    out = Image.open(io.BytesIO(data)).getexif()
    assert not out.get_ifd(0x8825)
    assert out.get(271) == "CameraCo"


@pytest.mark.parametrize("bad", [-5, 0, "big"])
def test_a_bad_export_size_is_a_user_error(ws, pid, bad):
    with pytest.raises(service.RetouchError):
        ws.export(pid, max_side=bad)


# -- one project cannot be made to hold unbounded data ---------------------------------------------------
def test_a_mark_with_too_many_points_is_refused():
    points = [[i / 10000, 0.5] for i in range(geometry.MAX_POINTS + 1)]
    with pytest.raises(geometry.GeometryError, match="at most"):
        geometry.validate("lasso", {"points": points})


def test_an_oversized_message_or_note_is_refused(ws, pid):
    long = "x" * (store.MAX_TEXT + 1)
    with pytest.raises(store.StoreError, match="too long"):
        ws.reply(pid, 1, long, "needs_review")
    with pytest.raises(store.StoreError, match="too long"):
        ws.add_pin(pid, shape="rect", geom={"x": 1, "y": 1, "w": 5, "h": 5}, units="px", note=long)


def test_a_project_stops_taking_marks_at_the_limit(ws, pid, monkeypatch):
    monkeypatch.setattr(store, "MAX_PINS", 2)
    ws.add_pin(pid, shape="rect", geom={"x": 1, "y": 1, "w": 5, "h": 5}, units="px")
    with pytest.raises(store.StoreError, match="at most 2 marks"):
        ws.add_pin(pid, shape="rect", geom={"x": 1, "y": 1, "w": 5, "h": 5}, units="px")


def test_a_project_stops_taking_versions_at_the_limit(ws, pid, monkeypatch):
    monkeypatch.setattr(store, "MAX_VERSIONS", 2)
    ws.edit(pid, 1, method="adjust", ops=[{"op": "exposure", "value": 1.0}])
    with pytest.raises(store.StoreError, match="versions"):
        ws.edit(pid, 1, method="adjust", ops=[{"op": "exposure", "value": -1.0}])
    assert len(list((ws.project_dir(pid) / "versions").iterdir())) == 2  # the refused edit left no file


# -- a mark's Revert undoes that mark's edit, or nothing -------------------------------------------------
def test_reverting_a_mark_whose_edit_is_not_the_latest_is_refused(ws, pid):
    ws.add_pin(pid, shape="rect", geom={"x": 40, "y": 10, "w": 15, "h": 15}, units="px")
    first = ws.edit(pid, 1, method="adjust", ops=[{"op": "exposure", "value": 1.0}]).version_id
    second = ws.edit(pid, 2, method="adjust", ops=[{"op": "exposure", "value": -1.0}]).version_id
    with pytest.raises(store.StoreError, match="not the latest"):
        ws.revert(pid, pin=1)
    after = ws.project(pid)
    assert after["current_version"] == second
    assert {p["number"]: p["result_version"] for p in after["pins"]} == {1: first, 2: second}
    assert ws.revert(pid, pin=2)["current_version"] == first
