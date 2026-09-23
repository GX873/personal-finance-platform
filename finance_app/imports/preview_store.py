"""Bounded, private server-side storage for import preview metadata."""

from __future__ import annotations

import json
import math
import os
import re
import secrets
import tempfile
from pathlib import Path
from typing import TypedDict

MAX_PREVIEW_METADATA_BYTES = 32 * 1024 * 1024
_PREVIEW_ID = re.compile(r"[A-Za-z0-9_-]{43}")
_DIGEST = re.compile(r"[0-9a-f]{64}")
_CREATE_ATTEMPTS = 5


class PreviewMetadataError(ValueError):
    """A preview metadata file is invalid or cannot be trusted."""


class PreviewRowMetadata(TypedDict):
    source_text: str
    confidence: float | None


class PreviewMetadata(TypedDict):
    sha256: str
    filename: str
    source_text: str
    requires_confirmation: bool
    rows: list[PreviewRowMetadata]


def _metadata_path(directory: Path, preview_id: str) -> Path:
    if _PREVIEW_ID.fullmatch(preview_id) is None:
        raise PreviewMetadataError("invalid preview identifier")
    root = directory.resolve()
    path = (root / f"{preview_id}.json").resolve()
    if path.parent != root:
        raise PreviewMetadataError("invalid preview identifier")
    return path


def _validate_metadata(value: object) -> PreviewMetadata:
    if not isinstance(value, dict) or set(value) != {
        "sha256",
        "filename",
        "source_text",
        "requires_confirmation",
        "rows",
    }:
        raise PreviewMetadataError("invalid preview metadata")
    digest = value.get("sha256")
    filename = value.get("filename")
    source_text = value.get("source_text")
    requires_confirmation = value.get("requires_confirmation")
    raw_rows = value.get("rows")
    if not isinstance(digest, str) or _DIGEST.fullmatch(digest) is None:
        raise PreviewMetadataError("invalid preview metadata")
    if (
        not isinstance(filename, str)
        or not filename
        or len(filename) > 255
        or Path(filename).name != filename
    ):
        raise PreviewMetadataError("invalid preview metadata")
    if not isinstance(source_text, str) or not isinstance(requires_confirmation, bool):
        raise PreviewMetadataError("invalid preview metadata")
    if not isinstance(raw_rows, list) or not raw_rows:
        raise PreviewMetadataError("invalid preview metadata")
    rows: list[PreviewRowMetadata] = []
    for raw_row in raw_rows:
        if not isinstance(raw_row, dict) or set(raw_row) != {
            "source_text",
            "confidence",
        }:
            raise PreviewMetadataError("invalid preview metadata")
        row_source = raw_row.get("source_text")
        confidence = raw_row.get("confidence")
        if not isinstance(row_source, str):
            raise PreviewMetadataError("invalid preview metadata")
        if confidence is not None and (
            isinstance(confidence, bool)
            or not isinstance(confidence, (int, float))
            or not math.isfinite(confidence)
            or not 0 <= confidence <= 100
        ):
            raise PreviewMetadataError("invalid preview metadata")
        rows.append(
            {
                "source_text": row_source,
                "confidence": None if confidence is None else float(confidence),
            }
        )
    return {
        "sha256": digest,
        "filename": filename,
        "source_text": source_text,
        "requires_confirmation": requires_confirmation,
        "rows": rows,
    }


def save_preview_metadata(directory: Path, metadata: PreviewMetadata) -> str:
    """Atomically publish metadata under a collision-resistant opaque id."""
    validated = _validate_metadata(metadata)
    payload = json.dumps(
        validated,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
    ).encode("utf-8")
    if len(payload) > MAX_PREVIEW_METADATA_BYTES:
        raise PreviewMetadataError("preview metadata is too large")

    directory.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        dir=directory, prefix=".preview-", suffix=".tmp"
    )
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        for _ in range(_CREATE_ATTEMPTS):
            preview_id = secrets.token_urlsafe(32)
            destination = _metadata_path(directory, preview_id)
            try:
                os.link(temporary_path, destination)
            except FileExistsError:
                continue
            return preview_id
        raise PreviewMetadataError("could not allocate preview identifier")
    finally:
        temporary_path.unlink(missing_ok=True)


def load_preview_metadata(directory: Path, preview_id: str) -> PreviewMetadata:
    """Load and validate bounded metadata without trusting its JSON shape."""
    path = _metadata_path(directory, preview_id)
    try:
        with path.open("rb") as stream:
            payload = stream.read(MAX_PREVIEW_METADATA_BYTES + 1)
    except OSError as exc:
        raise PreviewMetadataError("preview metadata is unavailable") from exc
    if len(payload) > MAX_PREVIEW_METADATA_BYTES:
        raise PreviewMetadataError("preview metadata is too large")
    try:
        decoded = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise PreviewMetadataError("preview metadata is unavailable") from exc
    return _validate_metadata(decoded)


def delete_preview_metadata(directory: Path, preview_id: str) -> None:
    """Delete one validated preview path while leaving the original upload intact."""
    _metadata_path(directory, preview_id).unlink(missing_ok=True)
