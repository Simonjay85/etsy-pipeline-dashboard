from __future__ import annotations

import json
import os
from pathlib import Path
from zipfile import ZipFile

import creativeca_etsy_bot as bot


def test_large_source_creates_bounded_evidence_bundle(tmp_path, monkeypatch):
    monkeypatch.setattr(bot, "CHATGPT_MAX_ATTACHMENT_BYTES", 1024)
    stage = tmp_path / "stage"
    source_assets = stage / "source_assets"
    source_assets.mkdir(parents=True)
    source = source_assets / "source.zip"
    with ZipFile(source, "w") as archive:
        archive.writestr("vectors/design.svg", os.urandom(4096))
        archive.writestr("previews/design.png", b"preview")
    reference = source_assets / "reference_01.png"
    reference.write_bytes(b"reference")

    evidence = bot.prepare_chatgpt_source(source, stage, [reference])

    assert evidence != source
    assert evidence.is_file()
    assert evidence.stat().st_size <= bot.CHATGPT_MAX_ATTACHMENT_BYTES
    with ZipFile(evidence) as archive:
        manifest = json.loads(archive.read("SOURCE_EVIDENCE.json"))
        assert manifest["canonical_source_sha256"] == bot.sha256_file(source)
        assert manifest["canonical_source_bytes"] == source.stat().st_size
        assert "references/reference_01.png" in archive.namelist()
    saved = json.loads((stage / "chatgpt-source-evidence.json").read_text())
    assert saved["canonical_source_sha256"] == manifest["canonical_source_sha256"]


def test_small_source_is_uploaded_without_repackaging(tmp_path):
    stage = tmp_path / "stage"
    source = stage / "source.zip"
    source.parent.mkdir(parents=True)
    with ZipFile(source, "w") as archive:
        archive.writestr("README.txt", "ok")

    assert bot.prepare_chatgpt_source(source, stage, []) == source


def test_chatgpt_duplicate_filename_suffix_is_normalized():
    assert bot._canonical_upload_name("creative-fabrica-source(3).zip") == "creative-fabrica-source.zip"
    assert bot._canonical_upload_name("reference_01(12).png") == "reference_01.png"
    assert bot._canonical_upload_name("reference_01.png") == "reference_01.png"


def test_chat_images_route_is_not_work_project():
    assert bot.is_chat_images_route("https://chatgpt.com/images/")
    assert bot.is_chat_images_route("https://chatgpt.com/images")
    assert not bot.is_chat_images_route("https://chatgpt.com/g/g-p-example/project")
