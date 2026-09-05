"""Tests for chunked upload + editable-types (borrowed from Laranode)."""
import io
import os


def _client(tmp_path, monkeypatch):
    import server

    fake_home = str(tmp_path)
    monkeypatch.setattr("os.path.expanduser", lambda p: fake_home if p == "~" else p)
    app = server.app
    app.config["TESTING"] = True
    client = app.test_client()
    with client.session_transaction() as sess:
        sess["logged_in"] = True
    return client


def test_single_shot_upload_ok(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    data = {"file": (io.BytesIO(b"hello"), "hi.txt"), "path": "."}
    resp = client.post("/api/files/upload", data=data, content_type="multipart/form-data")
    assert resp.status_code == 200
    assert resp.get_json()["filename"] == "hi.txt"
    assert (tmp_path / "hi.txt").read_bytes() == b"hello"


def test_chunked_upload_assembles(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    for i, chunk in enumerate([b"ab", b"cd", b"ef"]):
        data = {
            "file": (io.BytesIO(chunk), "blob"),
            "path": ".",
            "chunkIndex": str(i),
            "totalChunks": "3",
            "originalName": "big.bin",
        }
        resp = client.post("/api/files/upload", data=data, content_type="multipart/form-data")
        assert resp.status_code == 200, resp.get_json()
        body = resp.get_json()
        assert body["files"][0]["filename"] == "big.bin"
    assert (tmp_path / "big.bin").read_bytes() == b"abcdef"


def test_chunked_bad_index_rejected(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    data = {
        "file": (io.BytesIO(b"x"), "blob"),
        "path": ".",
        "chunkIndex": "5",
        "totalChunks": "3",
        "originalName": "bad.bin",
    }
    resp = client.post("/api/files/upload", data=data, content_type="multipart/form-data")
    assert resp.status_code in (200, 207, 500)
    body = resp.get_json()
    files = body.get("files", [])
    assert files and files[0].get("ok") is False


def test_traversal_blocked(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    data = {"file": (io.BytesIO(b"x"), "evil.txt"), "path": "../"}
    resp = client.post("/api/files/upload", data=data, content_type="multipart/form-data")
    assert resp.status_code == 403


def test_editable_types_endpoint(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    resp = client.get("/api/files/editable-types")
    assert resp.status_code == 200
    assert "text/plain" in resp.get_json()["editable_mime_types"]
    (tmp_path / "notes.txt").write_text("hi")
    resp2 = client.get("/api/files/editable-types?path=notes.txt")
    assert resp2.get_json()["editable"] is True


def test_editable_mime_helper():
    from app import file_explorer as fe

    assert fe.is_editable_mime("text/plain") is True
    assert fe.is_editable_mime("image/png") is False
    assert fe.is_editable_path("notes.txt") is True
    assert fe.is_editable_path("photo.png") is False
