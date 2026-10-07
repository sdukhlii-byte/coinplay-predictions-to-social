"""Тесты kit_server.py: HTTP-приёмник zip-китов (без сети наружу, run() подменён)."""

import argparse
import json
import os
import sys
import threading
import zipfile

import pytest
import requests

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import kit_server   # noqa: E402
import state         # noqa: E402

TOKEN = "s3cret"


def _zip_bytes(tmp_path, title="CS2", match_id="m1", a="FURIA", b="Aurora Gaming"):
    p = tmp_path / f"{match_id}.zip"
    blank = {"schema": 2, "match_id": match_id, "team_a": a, "team_b": b,
             "event": "ESL", "title": title, "series_format": "Bo3",
             "labs": [{"name": "ChatGPT", "model": "x", "winner": a, "cells": ["2", "1"]}]}
    with zipfile.ZipFile(p, "w") as z:
        z.writestr("blank/blank.json", json.dumps(blank))
    return p.read_bytes()


@pytest.fixture
def srv(tmp_path):
    calls = []

    def run(match, args):
        calls.append(match["id"])
        return "x.zip"

    server, service = kit_server.serve(argparse.Namespace(), run, port=0, token=TOKEN,
                                       block=False)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    service.calls = calls
    service.url = f"http://127.0.0.1:{server.server_address[1]}"
    yield service
    server.shutdown()
    server.server_close()


def _post(srv, data, name="furia-vs-x-1791297000.zip", token=TOKEN):
    h = {"Authorization": f"Bearer {token}"} if token else {}
    return requests.post(f"{srv.url}/kit", params={"name": name}, data=data, headers=h, timeout=10)


def test_health(srv):
    r = requests.get(f"{srv.url}/health", timeout=5)
    assert r.status_code == 200 and r.json()["status"] == "ok"


def test_auth_required(srv, tmp_path):
    assert _post(srv, _zip_bytes(tmp_path), token=None).status_code == 401
    assert _post(srv, _zip_bytes(tmp_path), token="wrong").status_code == 401
    assert srv.calls == []


def test_queued_processed_once_and_duplicate_ignored(srv, tmp_path):
    data = _zip_bytes(tmp_path)
    r = _post(srv, data)
    assert r.status_code == 202 and r.json()["id"] == "coinplay-kit-m1"
    srv.q.join()
    assert srv.calls == ["coinplay-kit-m1"]
    assert state.already_posted({"coinplay-kit-m1"})
    r2 = _post(srv, data)                      # грабер переслал повторно
    assert r2.status_code == 200 and r2.json()["status"] == "duplicate"
    srv.q.join()
    assert srv.calls == ["coinplay-kit-m1"]


def test_bad_and_unsupported_kits_rejected(srv, tmp_path):
    assert _post(srv, b"not a zip").status_code == 422
    r = _post(srv, _zip_bytes(tmp_path, title="Tennis", match_id="t1"))
    assert r.status_code == 422 and "не поддерживается" in r.json()["error"]
    assert srv.calls == []


def test_empty_and_oversized_body(srv, monkeypatch):
    assert _post(srv, b"").status_code == 400
    monkeypatch.setattr(kit_server, "MAX_BYTES", 10)
    assert _post(srv, b"x" * 50).status_code == 413


def test_failures_retry_then_give_up(tmp_path):
    spent = []

    def boom(m, a):
        spent.append(m["id"])
        raise RuntimeError("video failed")
    svc = kit_server.KitService(argparse.Namespace(), boom, max_attempts=2,
                                inbox=str(tmp_path / "inbox"))
    svc.start()
    assert svc.submit(_zip_bytes(tmp_path), "a.zip")[0] == "queued"
    svc.q.join()
    assert spent == ["coinplay-kit-m1"] * 2          # ровно 2 платные попытки
    assert state.already_posted({"coinplay-kit-m1"})  # дальше не пытаемся


def test_past_date_is_rejected_without_retry(tmp_path):
    spent = []

    def past(m, a):
        spent.append(1)
        raise ValueError("дата уже в прошлом")
    svc = kit_server.KitService(argparse.Namespace(), past, max_attempts=3,
                                inbox=str(tmp_path / "inbox"))
    svc.start()
    svc.submit(_zip_bytes(tmp_path), "a.zip")
    svc.q.join()
    assert spent == [1]


def test_serve_requires_token():
    with pytest.raises(RuntimeError, match="KIT_API_TOKEN"):
        kit_server.serve(argparse.Namespace(), lambda m, a: None, port=0, token="", block=False)


def test_safe_name_strips_path_tricks():
    assert kit_server._safe_name("../../etc/passwd") == "passwd.zip"
    assert kit_server._safe_name("a b/c d.zip") == "c_d.zip"
