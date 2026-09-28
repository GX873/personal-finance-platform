from __future__ import annotations

from pathlib import Path

import pytest

from finance_app.imports import preview_store
from finance_app.imports.preview_store import PreviewMetadata, PreviewMetadataError


def metadata(digest: str = "a" * 64) -> PreviewMetadata:
    return {
        "sha256": digest,
        "filename": "持仓.csv",
        "source_text": "基金名称: 示例基金",
        "requires_confirmation": False,
        "rows": [{"source_text": "基金名称: 示例基金", "confidence": None}],
    }


def test_preview_metadata_round_trips_utf8_without_temporary_files(
    tmp_path: Path,
) -> None:
    preview_id = preview_store.save_preview_metadata(tmp_path, metadata())

    stored = tmp_path / f"{preview_id}.json"
    assert "示例基金".encode() in stored.read_bytes()
    assert preview_store.load_preview_metadata(tmp_path, preview_id) == metadata()
    assert list(tmp_path.glob("*.tmp")) == []


def test_preview_metadata_is_bounded_and_cleans_temporary_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(preview_store, "MAX_PREVIEW_METADATA_BYTES", 1)

    with pytest.raises(PreviewMetadataError, match="too large"):
        preview_store.save_preview_metadata(tmp_path, metadata())

    assert list(tmp_path.iterdir()) == []


def test_deeply_nested_corrupt_metadata_is_rejected_safely(tmp_path: Path) -> None:
    preview_id = "A" * 43
    nested_json = "[" * 2_000 + "0" + "]" * 2_000
    (tmp_path / f"{preview_id}.json").write_text(nested_json, encoding="utf-8")

    with pytest.raises(PreviewMetadataError, match="preview metadata"):
        preview_store.load_preview_metadata(tmp_path, preview_id)


def test_integer_limit_json_value_error_is_rejected_safely(tmp_path: Path) -> None:
    preview_id = "A" * 43
    payload = '{"oversized_integer":' + "9" * 5_000 + "}"
    (tmp_path / f"{preview_id}.json").write_text(payload, encoding="utf-8")

    with pytest.raises(PreviewMetadataError, match="unavailable"):
        preview_store.load_preview_metadata(tmp_path, preview_id)


def test_unexpected_memory_error_is_not_hidden(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    preview_id = "A" * 43
    (tmp_path / f"{preview_id}.json").write_text("{}", encoding="utf-8")
    monkeypatch.setattr(
        preview_store.json,
        "loads",
        lambda _: (_ for _ in ()).throw(MemoryError("out of memory")),
    )

    with pytest.raises(MemoryError, match="out of memory"):
        preview_store.load_preview_metadata(tmp_path, preview_id)


def test_preview_id_collision_never_overwrites_existing_metadata(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    first_id = preview_store.save_preview_metadata(tmp_path, metadata())
    first_payload = (tmp_path / f"{first_id}.json").read_bytes()
    second_id = "B" * 43 if first_id != "B" * 43 else "C" * 43
    generated_ids = iter((first_id, second_id))
    monkeypatch.setattr(
        preview_store.secrets, "token_urlsafe", lambda _: next(generated_ids)
    )

    saved_id = preview_store.save_preview_metadata(tmp_path, metadata("b" * 64))

    assert saved_id == second_id
    assert (tmp_path / f"{first_id}.json").read_bytes() == first_payload
    assert preview_store.load_preview_metadata(tmp_path, second_id)["sha256"] == "b" * 64


@pytest.mark.parametrize("preview_id", ["../outside", "A" * 42, "A" * 44])
def test_preview_identifier_rejects_path_traversal_and_invalid_lengths(
    tmp_path: Path, preview_id: str
) -> None:
    with pytest.raises(PreviewMetadataError, match="invalid preview identifier"):
        preview_store.load_preview_metadata(tmp_path, preview_id)
