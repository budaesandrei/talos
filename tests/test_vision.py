"""Tests for vision input (M42)."""

import base64

from talos import vision


def _png(tmp_path):
    # a 1x1 PNG
    raw = base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mـ"
        .replace("ـ", "") + "NkYAAAAAYAAjCB0C8AAAAASUVORK5CYII=")
    p = tmp_path / "shot.png"
    p.write_bytes(raw)
    return p


def test_no_images_returns_plain_text(monkeypatch):
    monkeypatch.setattr(vision, "model_supports_vision", lambda m: True)
    assert vision.build_content("just text here", "gpt-4o") == "just text here"


def test_text_only_model_never_builds_blocks(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    p = _png(tmp_path)
    monkeypatch.setattr(vision, "model_supports_vision", lambda m: False)
    out = vision.build_content(f"look at {p.name}", "text-only-model")
    assert isinstance(out, str)              # untouched


def test_image_path_becomes_multimodal_block(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    p = _png(tmp_path)
    monkeypatch.setattr(vision, "model_supports_vision", lambda m: True)
    out = vision.build_content(f"is this button right? {p.name}", "gpt-4o")
    assert isinstance(out, list)
    assert out[0]["type"] == "text"
    assert out[1]["type"] == "image_url"
    assert out[1]["image_url"]["url"].startswith("data:image/")


def test_data_url_detected(monkeypatch):
    monkeypatch.setattr(vision, "model_supports_vision", lambda m: True)
    durl = "data:image/png;base64,AAAA"
    out = vision.build_content(f"see {durl}", "gpt-4o")
    assert isinstance(out, list) and out[1]["image_url"]["url"] == durl
