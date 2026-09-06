"""Tests for resumable uploads + mkdir/rename/move/copy in file_explorer."""
import io
import json
import os
import time

import pytest

from app import file_explorer as fe


@pytest.fixture()
def home(tmp_path, monkeypatch):
    """Point $HOME at tmp and return (home_dir, sessions_dir)."""
    fake = str(tmp_path)

    def _expand(p):
        if p == "~":
            return fake
        if p.startswith("~/"):
            return os.path.join(fake, p[2:])
        return p

    monkeypatch.setattr("os.path.expanduser", _expand)
    sessions = os.path.join(fake, ".server-services-manager", "uploads")
    return fake, sessions


def _chunk(data: bytes):
    return io.BytesIO(data)


def test_round_trip_multi_chunk(home):
    _home, sessions = home
    s = fe.init_upload("big.bin", 10, ".", sessions=sessions)
    assert s["received"] == 0
    r = fe.append_chunk(s["id"], 0, _chunk(b"abc"), sessions=sessions)
    assert r["received"] == 3
    r = fe.append_chunk(s["id"], 3, _chunk(b"defghij"), sessions=sessions)
    assert r["received"] == 10
    out = fe.complete_upload(s["id"], sessions=sessions)
    assert out["path"] == "big.bin"
    assert open(os.path.join(_home, "big.bin"), "rb").read() == b"abcdefghij"


def test_resume_from_status(home):
    _home, sessions = home
    payload = b"x" * 100
    s = fe.init_upload("resume.bin", len(payload), ".", sessions=sessions)
    fe.append_chunk(s["id"], 0, _chunk(payload[:40]), sessions=sessions)
    st = fe.upload_status(s["id"], sessions=sessions)
    assert st["received"] == 40
    # Simulate a fresh client that lost its counter: wrong offset rejected
    # with the server's position attached.
    with pytest.raises(fe.FileExplorerError) as exc:
        fe.append_chunk(s["id"], 0, _chunk(payload[40:]), sessions=sessions)
    assert exc.value.code == "offset_mismatch"
    assert getattr(exc.value, "received", None) == 40
    fe.append_chunk(s["id"], 40, _chunk(payload[40:]), sessions=sessions)
    fe.complete_upload(s["id"], sessions=sessions)
    assert os.path.getsize(os.path.join(_home, "resume.bin")) == 100


def test_complete_incomplete_rejected(home):
    _home, sessions = home
    s = fe.init_upload("half.bin", 10, ".", sessions=sessions)
    fe.append_chunk(s["id"], 0, _chunk(b"abc"), sessions=sessions)
    with pytest.raises(fe.FileExplorerError) as exc:
        fe.complete_upload(s["id"], sessions=sessions)
    assert exc.value.code == "incomplete"
    # Partial must not be visible under the real name.
    assert not os.path.lexists(os.path.join(_home, "half.bin"))


def test_zero_size_file(home):
    _home, sessions = home
    s = fe.init_upload("empty.txt", 0, ".", sessions=sessions)
    out = fe.complete_upload(s["id"], sessions=sessions)
    assert out["path"] == "empty.txt"
    assert os.path.getsize(os.path.join(_home, "empty.txt")) == 0


def test_hash_ok_and_mismatch(home):
    import hashlib

    _home, sessions = home
    payload = b"verify me"
    digest = hashlib.sha256(payload).hexdigest()
    s = fe.init_upload("h.bin", len(payload), ".", sessions=sessions)
    fe.append_chunk(s["id"], 0, _chunk(payload), sessions=sessions)
    fe.complete_upload(s["id"], sha256=digest, sessions=sessions)
    assert open(os.path.join(_home, "h.bin"), "rb").read() == payload

    s2 = fe.init_upload("h2.bin", len(payload), ".", sessions=sessions)
    fe.append_chunk(s2["id"], 0, _chunk(payload), sessions=sessions)
    with pytest.raises(fe.FileExplorerError) as exc:
        fe.complete_upload(s2["id"], sha256="0" * 64, sessions=sessions)
    assert exc.value.code == "hash_mismatch"
    # Staging discarded, real name never appeared.
    assert not os.path.lexists(os.path.join(_home, "h2.bin"))


def test_cancel_idempotent(home):
    _home, sessions = home
    s = fe.init_upload("tmp.bin", 10, ".", sessions=sessions)
    fe.append_chunk(s["id"], 0, _chunk(b"abc"), sessions=sessions)
    assert fe.cancel_upload(s["id"], sessions=sessions) == {"cancelled": True}
    assert fe.cancel_upload(s["id"], sessions=sessions) == {"cancelled": True}
    assert fe.cancel_upload("nope-not-real", sessions=sessions) == {"cancelled": True}
    with pytest.raises(fe.FileExplorerError):
        fe.upload_status(s["id"], sessions=sessions)


def test_name_and_path_guards(home):
    _home, sessions = home
    with pytest.raises(fe.FileExplorerError):
        fe.init_upload("a/b.txt", 5, ".", sessions=sessions)
    with pytest.raises(fe.FileExplorerError):
        fe.init_upload("..", 5, ".", sessions=sessions)
    with pytest.raises(fe.FileExplorerError):
        fe.init_upload("ok.txt", 5, "../", sessions=sessions)
    with pytest.raises(fe.FileExplorerError):
        fe.init_upload("ok.txt", 5, ".", subpath="../../evil", sessions=sessions)
    with pytest.raises(fe.FileExplorerError):
        fe.init_upload("ok.txt", -1, ".", sessions=sessions)
    with pytest.raises(fe.FileExplorerError):
        fe.init_upload("ok.txt", fe._RESUMABLE_MAX_BYTES + 1, ".", sessions=sessions)


def test_subpath_folder_upload(home):
    _home, sessions = home
    s = fe.init_upload("f.txt", 3, ".", subpath="a/b", sessions=sessions)
    fe.append_chunk(s["id"], 0, _chunk(b"xyz"), sessions=sessions)
    out = fe.complete_upload(s["id"], sessions=sessions)
    assert out["path"] == os.path.join("a", "b", "f.txt")
    assert open(os.path.join(_home, "a", "b", "f.txt"), "rb").read() == b"xyz"


def test_exists_and_space_guards(home, monkeypatch):
    _home, sessions = home
    open(os.path.join(_home, "taken.txt"), "w").write("x")
    with pytest.raises(fe.FileExplorerError) as exc:
        fe.init_upload("taken.txt", 5, ".", sessions=sessions)
    assert exc.value.code == "exists"

    class _Usage:
        free = 10

    monkeypatch.setattr("shutil.disk_usage", lambda p: _Usage())
    with pytest.raises(fe.FileExplorerError) as exc2:
        fe.init_upload("big2.txt", 100, ".", sessions=sessions)
    assert exc2.value.code == "not_enough_space"


def test_sweep_stale(home):
    _home, sessions = home
    s = fe.init_upload("old.bin", 5, ".", sessions=sessions)
    _, sidecar = fe._session_paths(s["id"], sessions)
    with open(sidecar, "r", encoding="utf-8") as f:
        data = json.load(f)
    data["updated"] = time.time() - 200000
    with open(sidecar, "w", encoding="utf-8") as f:
        json.dump(data, f)
    fresh = fe.init_upload("fresh.bin", 5, ".", sessions=sessions)
    # init sweeps: old gone, fresh kept.
    with pytest.raises(fe.FileExplorerError):
        fe.upload_status(s["id"], sessions=sessions)
    st = fe.upload_status(fresh["id"], sessions=sessions)
    assert st["received"] == 0


def test_mkdir_rename_guards(home):
    _home, _sessions = home
    out = fe.mkdir(".", "newdir")
    assert out["path"] == "newdir"
    assert os.path.isdir(os.path.join(_home, "newdir"))
    with pytest.raises(fe.FileExplorerError):
        fe.mkdir(".", "newdir")  # exists
    with pytest.raises(fe.FileExplorerError):
        fe.mkdir(".", "a/b")  # slashes rejected
    r = fe.rename("newdir", "renamed")
    assert r["path"] == "renamed"
    with pytest.raises(fe.FileExplorerError):
        fe.rename(".", "home2")  # refusing $HOME itself


def _route_client(tmp_path, monkeypatch):
    import server

    fake_home = str(tmp_path)

    def _expand(p):
        if p == "~":
            return fake_home
        if p.startswith("~/"):
            return os.path.join(fake_home, p[2:])
        return p

    monkeypatch.setattr("os.path.expanduser", _expand)
    app = server.app
    app.config["TESTING"] = True
    client = app.test_client()
    with client.session_transaction() as sess:
        sess["logged_in"] = True
    return client


def test_route_resumable_round_trip(tmp_path, monkeypatch):
    client = _route_client(tmp_path, monkeypatch)
    r = client.post("/api/files/uploads", json={"name": "r.bin", "size": 10, "path": "."})
    assert r.status_code == 200, r.get_json()
    sid = r.get_json()["id"]
    r = client.put(f"/api/files/uploads/{sid}?offset=0", data=b"abcde")
    assert r.get_json()["received"] == 5
    # Wrong offset → 409 with the server's position for re-sync.
    r = client.put(f"/api/files/uploads/{sid}?offset=0", data=b"zzzzz")
    assert r.status_code == 409
    assert r.get_json()["received"] == 5
    r = client.put(f"/api/files/uploads/{sid}?offset=5", data=b"fghij")
    assert r.get_json()["received"] == 10
    r = client.get(f"/api/files/uploads/{sid}")
    assert r.get_json()["received"] == 10
    r = client.post(f"/api/files/uploads/{sid}/complete", json={})
    assert r.status_code == 200
    assert (tmp_path / "r.bin").read_bytes() == b"abcdefghij"


def test_route_resumable_guards(tmp_path, monkeypatch):
    client = _route_client(tmp_path, monkeypatch)
    # Traversal in path.
    r = client.post("/api/files/uploads", json={"name": "x", "size": 1, "path": "../"})
    assert r.status_code == 400
    # Unknown session.
    assert client.get("/api/files/uploads/nope").status_code == 404
    assert client.put("/api/files/uploads/nope?offset=0", data=b"x").status_code == 404
    # Cancel then status → gone.
    r = client.post("/api/files/uploads", json={"name": "c.bin", "size": 4, "path": "."})
    sid = r.get_json()["id"]
    assert client.delete(f"/api/files/uploads/{sid}").status_code == 200
    assert client.get(f"/api/files/uploads/{sid}").status_code == 404


def test_route_file_ops(tmp_path, monkeypatch):
    client = _route_client(tmp_path, monkeypatch)
    assert client.post("/api/files/mkdir", json={"path": ".", "name": "d1"}).status_code == 200
    assert client.post("/api/files/mkdir", json={"path": ".", "name": "d1"}).status_code == 400
    assert client.post("/api/files/mkdir", json={"path": ".", "name": "a/b"}).status_code == 400
    (tmp_path / "f.txt").write_text("hi")
    r = client.post("/api/files/rename", json={"path": "f.txt", "name": "g.txt"})
    assert r.status_code == 200
    assert (tmp_path / "g.txt").exists()
    r = client.post("/api/files/copy", json={"paths": ["g.txt"], "dest": "d1"})
    assert r.get_json()["copied"] == 1
    assert client.post("/api/files/mkdir", json={"path": ".", "name": "d2"}).status_code == 200
    r = client.post("/api/files/move", json={"paths": ["d1/g.txt"], "dest": "d2"})
    assert r.get_json()["moved"] == 1
    assert (tmp_path / "d2" / "g.txt").exists()
    assert not (tmp_path / "d1" / "g.txt").exists()
    # Move dir into itself → per-path failure, still HTTP 200.
    r = client.post("/api/files/move", json={"paths": ["d1"], "dest": "d1"})
    assert r.status_code == 200
    assert r.get_json()["failed"] == 1


def test_move_copy(home):
    _home, _sessions = home
    os.mkdir(os.path.join(_home, "src"))
    open(os.path.join(_home, "src", "f.txt"), "w").write("data")
    os.mkdir(os.path.join(_home, "dst"))
    m = fe.move(["src/f.txt"], "dst")
    assert m["moved"] == 1 and m["failed"] == 0
    assert open(os.path.join(_home, "dst", "f.txt")).read() == "data"
    c = fe.copy(["dst/f.txt"], "src")
    assert c["copied"] == 1
    assert open(os.path.join(_home, "src", "f.txt")).read() == "data"
    # Dir into itself refused for both.
    with pytest.raises(fe.FileExplorerError):
        fe._move_one(os.path.join(_home, "src"), os.path.join(_home, "src"))
    bad = fe.copy(["src"], "src")
    assert bad["failed"] == 1
    # Missing source reported per-path, not raised.
    res = fe.move(["nope.txt"], "dst")
    assert res["moved"] == 0 and res["failed"] == 1
