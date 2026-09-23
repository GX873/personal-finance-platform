"""Authenticated, two-step upload/preview/confirmation workflow."""

from __future__ import annotations

import hashlib
import re
from datetime import datetime
from pathlib import Path
from statistics import fmean
from typing import Annotated, Any
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, File, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from finance_app.auth.models import User
from finance_app.auth.routes import templates
from finance_app.auth.service import (
    csrf_token,
    current_user,
    require_csrf,
    require_user,
)
from finance_app.config import get_settings
from finance_app.db import get_db
from finance_app.imports.ocr import extract_candidates
from finance_app.imports.parser import (
    ALLOWED_EXTENSIONS,
    MAX_UPLOAD_BYTES,
    ImportFileError,
    PortfolioRow,
    normalize_row,
    parse_portfolio_file,
)
from finance_app.ledger.models import Account, Asset
from finance_app.ledger.schemas import PostTransaction
from finance_app.ledger.service import post_transaction
from finance_app.notifications.models import ImportBatch
from finance_app.web.forms import SHANGHAI, audit_event

router = APIRouter()
_IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg"}


def _private_dir() -> Path:
    path = Path(get_settings().import_upload_dir).resolve()
    public_static = Path(__file__).resolve().parents[1] / "static"
    if path == public_static or public_static in path.parents:
        raise ImportFileError("import storage must be outside public static directory")
    path.mkdir(parents=True, exist_ok=True)
    return path


def _safe_filename(filename: str | None) -> str:
    name = Path(filename or "upload").name
    if Path(name).suffix.lower() not in ALLOWED_EXTENSIONS:
        raise ImportFileError("unsupported import file type")
    return name


def _base_context(request: Request, user: User, **extra: Any) -> dict[str, Any]:
    context: dict[str, Any] = {
        "request": request,
        "user": user,
        "csrf_token": csrf_token(request),
        "section": "imports",
        "demo": False,
        "vm": {"today": datetime.now(SHANGHAI).date().isoformat()},
    }
    context.update(extra)
    return context


@router.get("/imports", response_class=HTMLResponse)
def imports_page(request: Request, db: Annotated[Session, Depends(get_db)]):
    user = current_user(request, db)
    if user is None:
        return RedirectResponse("/login", status_code=303)
    return templates.TemplateResponse(
        request=request,
        name="import_preview.html",
        context=_base_context(
            request,
            user,
            preview=None,
            error=None,
            accounts=list(db.scalars(select(Account).order_by(Account.name))),
        ),
    )


async def _read_upload(upload: UploadFile) -> tuple[str, bytes]:
    filename = _safe_filename(upload.filename)
    content = await upload.read(MAX_UPLOAD_BYTES + 1)
    if len(content) > MAX_UPLOAD_BYTES:
        raise ImportFileError("upload exceeds the 10 MiB limit")
    return filename, content


def _preview_context(
    request: Request, user: User, preview: dict[str, Any], *, error=None
):
    from finance_app.db import get_session_factory

    with get_session_factory()() as db:
        accounts = list(db.scalars(select(Account).order_by(Account.name)))
    return templates.TemplateResponse(
        request=request,
        name="import_preview.html",
        status_code=422 if error else 200,
        context=_base_context(
            request, user, preview=preview, error=error, accounts=accounts
        ),
    )


@router.post("/imports/preview", dependencies=[Depends(require_csrf)])
async def preview_import(
    request: Request,
    file: Annotated[UploadFile, File(...)],
    db: Annotated[Session, Depends(get_db)],
    user: Annotated[User, Depends(require_user)],
):
    try:
        filename, content = await _read_upload(file)
        digest = hashlib.sha256(content).hexdigest()
        if (
            db.scalar(select(ImportBatch).where(ImportBatch.sha256 == digest))
            is not None
        ):
            raise HTTPException(status_code=409, detail="file already imported")
        extension = Path(filename).suffix.lower()
        if extension in _IMAGE_EXTENSIONS:
            ocr = extract_candidates(content)
            confidence = (
                fmean(candidate.confidence for candidate in ocr.candidates)
                if ocr.candidates
                else None
            )
            rows: list[PortfolioRow] = [
                normalize_row(
                    ocr.values,
                    source_text=ocr.source_text,
                    confidence=confidence,
                )
            ]
            preview: dict[str, Any] = {
                "sha256": digest,
                "filename": filename,
                "rows": rows,
                "source_text": ocr.source_text,
                "requires_confirmation": True,
            }
        else:
            rows = parse_portfolio_file(content, filename)
            preview = {
                "sha256": digest,
                "filename": filename,
                "rows": rows,
                "source_text": "\n".join(row.source_text for row in rows),
                "requires_confirmation": False,
            }
        (_private_dir() / f"{digest}{extension}").write_bytes(content)
        request.session["import_preview"] = {"sha256": digest}
        return _preview_context(request, user, preview)
    except HTTPException:
        raise
    except ImportFileError as exc:
        return _preview_context(
            request, user, {"rows": [], "filename": ""}, error=str(exc)
        )
    except (OSError, ValueError):
        return _preview_context(
            request,
            user,
            {"rows": [], "filename": ""},
            error="upload could not be processed",
        )


def _form_rows(form: Any) -> list[dict[str, str]]:
    rows: dict[int, dict[str, str]] = {}
    for key, value in form.multi_items():
        match = re.fullmatch(r"rows-(\d+)-([a-z_]+)", str(key))
        if match and isinstance(value, str):
            rows.setdefault(int(match.group(1)), {})[match.group(2)] = value
    return [rows[index] for index in sorted(rows)]


@router.post("/imports/confirm", dependencies=[Depends(require_csrf)])
async def confirm_import(
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    user: Annotated[User, Depends(require_user)],
):
    form = await request.form()
    digest = form.get("sha256")
    account_value = form.get("account_id")
    if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
        return _preview_context(
            request, user, {"rows": []}, error="invalid import hash"
        )
    preview_state = request.session.get("import_preview")
    if not isinstance(preview_state, dict) or preview_state.get("sha256") != digest:
        return _preview_context(
            request,
            user,
            {"rows": []},
            error="import confirmation does not match the latest preview",
        )
    if db.scalar(select(ImportBatch).where(ImportBatch.sha256 == digest)) is not None:
        raise HTTPException(status_code=409, detail="file already imported")
    try:
        rows = [normalize_row(values) for values in _form_rows(form)]
        if not rows:
            raise ValueError("at least one row is required")
        errors = [error for row in rows for error in row.errors]
        if errors:
            return _preview_context(
                request,
                user,
                {"sha256": digest, "rows": rows, "source_text": ""},
                error="; ".join(errors),
            )
        account_id = int(str(account_value))
        if db.get(Account, account_id) is None:
            raise ValueError("account not found")
        source = f"import:{digest[:16]}"
        for index, row in enumerate(rows):
            if not row.asset_code:
                continue
            quantity = row.quantity
            occurred_on = row.occurred_on
            assert quantity is not None
            assert occurred_on is not None
            asset = db.scalar(
                select(Asset).where(Asset.code == row.asset_code, Asset.market == "CN")
            )
            if asset is None:
                asset = Asset(code=row.asset_code, market="CN", name=row.asset_name)
                db.add(asset)
                db.flush()
            post_transaction(
                db,
                PostTransaction(
                    source=source,
                    external_id=f"{digest}:{index}",
                    kind="BUY",
                    account_id=account_id,
                    asset_id=asset.id,
                    amount_cents=row.cost_cents or 0,
                    quantity=quantity,
                    price=None,
                    occurred_at=datetime.combine(
                        occurred_on,
                        datetime.min.time(),
                        tzinfo=ZoneInfo("Asia/Shanghai"),
                    ),
                    note="confirmed portfolio import",
                ),
            )
        batch = ImportBatch(sha256=digest, filename=digest, source=source)
        db.add(batch)
        db.flush()
        db.add(
            audit_event(
                user=user,
                event_type="import.confirmed",
                action="import.confirm",
                entity_type="import_batch",
                entity_id=batch.id,
                summary={"sha256": digest, "rows": len(rows)},
            )
        )
        db.commit()
        request.session.pop("import_preview", None)
    except (ValueError, IntegrityError) as exc:
        db.rollback()
        rows = [normalize_row(values) for values in _form_rows(form)]
        return _preview_context(
            request,
            user,
            {"sha256": digest, "rows": rows, "source_text": ""},
            error=str(exc),
        )
    return RedirectResponse("/transactions", status_code=303)
