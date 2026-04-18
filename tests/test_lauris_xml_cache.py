import json
from datetime import datetime, timedelta

import sources.lauris_xml as lauris_xml


class _Response:
    def __init__(self, text):
        self.text = text

    def raise_for_status(self):
        return None


def test_fetch_xml_text_uses_fresh_disk_cache(monkeypatch, tmp_path):
    monkeypatch.setattr(lauris_xml, "LAURIS_CACHE_DIR", tmp_path)
    cache_file = tmp_path / "demo.json"
    cache_file.write_text(json.dumps({
        "fetched_at": datetime.now().isoformat(),
        "content": "<root><ok/></root>",
    }), encoding="utf-8")

    called = {"count": 0}

    def _unexpected_get(*args, **kwargs):
        called["count"] += 1
        raise AssertionError("network should not be used when cache is fresh")

    monkeypatch.setattr(lauris_xml.requests, "get", _unexpected_get)

    result = lauris_xml.fetch_xml_text("https://example.test/demo", cache_key="demo")

    assert result == "<root><ok/></root>"
    assert called["count"] == 0


def test_fetch_xml_text_refreshes_stale_cache(monkeypatch, tmp_path):
    monkeypatch.setattr(lauris_xml, "LAURIS_CACHE_DIR", tmp_path)
    cache_file = tmp_path / "demo.json"
    cache_file.write_text(json.dumps({
        "fetched_at": (datetime.now() - timedelta(hours=25)).isoformat(),
        "content": "<root><stale/></root>",
    }), encoding="utf-8")

    monkeypatch.setattr(
        lauris_xml.requests,
        "get",
        lambda *args, **kwargs: _Response("<root><fresh/></root>"),
    )

    result = lauris_xml.fetch_xml_text("https://example.test/demo", cache_key="demo")

    assert result == "<root><fresh/></root>"
    payload = json.loads(cache_file.read_text(encoding="utf-8"))
    assert payload["content"] == "<root><fresh/></root>"
