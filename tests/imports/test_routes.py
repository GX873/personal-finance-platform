from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import re
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace

import pytest
from argon2 import PasswordHasher
from fastapi.testclient import TestClient
from pytesseract.pytesseract import TesseractError
from sqlalchemy import select

from finance_app.app import create_app
from finance_app.auth.models import User
from finance_app.imports.parser import ImportFileError
from finance_app.ledger.models import Account, Asset, AuditEvent, Transaction
from finance_app.notifications.models import ImportBatch
from tests.test_database import db_session as database_session_fixture

db_session = database_session_fixture


@pytest.fixture
def client(
    db_session, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[TestClient]:
    monkeypatch.setenv("FINANCE_IMPORT_UPLOAD_DIR", str(tmp_path / "private-imports"))
    from finance_app.config import get_settings

    get_settings.cache_clear()
    db_session.add_all(
        [
            User(
                username="admin",
                password_hash=PasswordHasher().hash("long-password-123"),
            ),
            Account(name="导入账户", kind="brokerage", opening_balance_cents=100_000),
        ]
    )
    db_session.commit()
    with TestClient(create_app(), follow_redirects=False) as value:
        yield value


def csrf(client: TestClient, path: str) -> str:
    response = client.get(path)
    return re.search(r'name="csrf_token" value="([^"]+)"', response.text)[1]


def login(client: TestClient) -> None:
    response = client.post(
        "/login",
        data={
            "username": "admin",
            "password": "long-password-123",
            "csrf_token": csrf(client, "/login"),
        },
    )
    assert response.status_code == 303


def upload_csv(client: TestClient, content: bytes, filename: str = "positions.csv"):
    return client.post(
        "/imports/preview",
        data={"csrf_token": csrf(client, "/imports")},
        files={"file": (filename, content, "text/csv")},
    )


def confirmation_data(digest: str, account_id: int) -> dict[str, str]:
    return {
        "sha256": digest,
        "account_id": str(account_id),
        "rows-0-asset_code": "000001",
        "rows-0-asset_name": "示例基金",
        "rows-0-quantity": "1",
        "rows-0-cost": "10.00",
        "rows-0-market_value": "",
        "rows-0-available_cash": "",
        "rows-0-date": "2026-09-23",
    }


def test_private_upload_dir_rejects_the_public_static_tree(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from finance_app.config import get_settings
    from finance_app.imports.routes import _private_dir

    public_static = Path("finance_app/static").resolve()
    monkeypatch.setenv("FINANCE_IMPORT_UPLOAD_DIR", str(public_static))
    get_settings.cache_clear()

    with pytest.raises(ImportFileError, match="outside public static"):
        _private_dir()


def test_import_pages_require_login_and_upload_requires_csrf(
    client: TestClient,
) -> None:
    assert client.get("/imports").status_code == 303
    login(client)
    assert client.get("/imports").status_code == 200
    assert (
        client.post(
            "/imports/preview", files={"file": ("a.csv", b"x", "text/csv")}
        ).status_code
        == 403
    )


def test_preview_hashes_and_stores_privately_without_writing_ledger(
    client: TestClient, db_session, tmp_path: Path
) -> None:
    login(client)
    content = "基金代码,基金名称,份额,持仓成本,日期\n000001,示例基金,2,20.00,2026-09-23\n".encode()

    response = upload_csv(client, content, "../../positions.csv")

    assert response.status_code == 200
    digest = hashlib.sha256(content).hexdigest()
    assert digest in response.text
    assert "示例基金" in response.text
    assert 'name="rows-0-quantity"' in response.text
    stored = tmp_path / "private-imports" / f"{digest}.csv"
    assert stored.read_bytes() == content
    assert "static" not in str(stored)
    assert "../../positions.csv" not in response.text
    assert db_session.scalar(select(Transaction)) is None
    assert db_session.scalar(select(ImportBatch)) is None


def test_multiline_preview_keeps_cookie_small_and_metadata_server_side(
    client: TestClient, tmp_path: Path
) -> None:
    login(client)
    content = (
        "基金代码,基金名称,份额,持仓成本,日期\n"
        + "".join(
            f"{index:06d},第{index}只基金,1,10.00,2026-09-23\n"
            for index in range(1, 101)
        )
    ).encode()

    response = upload_csv(client, content)

    assert response.status_code == 200
    set_cookie = response.headers["set-cookie"]
    encoded_session = client.cookies["session"].split(".", maxsplit=1)[0]
    session_payload = json.loads(base64.b64decode(encoded_session))
    preview_session = session_payload["import_preview"]
    forbidden = {"source_text", "rows", "confidence"}
    violations = {
        "oversized": len(set_cookie.encode("latin-1")) > 4096,
        "metadata_in_session": bool(forbidden & set(preview_session)),
    }
    assert not any(violations.values()), violations
    assert set(preview_session) == {"id"}
    metadata_files = list((tmp_path / "private-imports").glob("*.json"))
    assert len(metadata_files) == 1
    assert metadata_files[0].stem == preview_session["id"]
    metadata = json.loads(metadata_files[0].read_text(encoding="utf-8"))
    assert metadata["source_text"]
    assert len(metadata["rows"]) == 100


def test_new_preview_removes_previous_server_metadata(
    client: TestClient, tmp_path: Path
) -> None:
    login(client)
    first = "基金代码,基金名称,份额,持仓成本,日期\n000001,第一只,1,10.00,2026-09-23\n".encode()
    second = "基金代码,基金名称,份额,持仓成本,日期\n000002,第二只,1,20.00,2026-09-23\n".encode()

    assert upload_csv(client, first).status_code == 200
    metadata_dir = tmp_path / "private-imports"
    first_metadata_files = list(metadata_dir.glob("*.json"))
    assert len(first_metadata_files) == 1
    first_metadata = first_metadata_files[0]
    assert upload_csv(client, second).status_code == 200

    metadata_files = list(metadata_dir.glob("*.json"))
    assert len(metadata_files) == 1
    assert not first_metadata.exists()


@pytest.mark.parametrize(
    "failure", ["missing", "corrupt", "digest-mismatch", "integer-limit"]
)
def test_confirm_rejects_untrusted_server_metadata_without_writes(
    client: TestClient, db_session, tmp_path: Path, failure: str
) -> None:
    login(client)
    content = "基金代码,基金名称,份额,持仓成本,日期\n000001,示例基金,1,10.00,2026-09-23\n".encode()
    digest = hashlib.sha256(content).hexdigest()
    assert upload_csv(client, content).status_code == 200
    metadata_files = list((tmp_path / "private-imports").glob("*.json"))
    assert len(metadata_files) == 1
    metadata_file = metadata_files[0]
    if failure == "missing":
        metadata_file.unlink()
    elif failure == "corrupt":
        metadata_file.write_text("{not-json", encoding="utf-8")
    elif failure == "integer-limit":
        metadata_file.write_text(
            '{"oversized_integer":' + "9" * 5_000 + "}", encoding="utf-8"
        )
    else:
        metadata = json.loads(metadata_file.read_text(encoding="utf-8"))
        metadata["sha256"] = "f" * 64
        metadata_file.write_text(json.dumps(metadata), encoding="utf-8")
    account_id = db_session.scalar(select(Account.id))
    data = confirmation_data(digest, account_id)
    data["csrf_token"] = csrf(client, "/imports")

    if failure == "integer-limit":
        with TestClient(
            client.app, follow_redirects=False, raise_server_exceptions=False
        ) as error_client:
            error_client.cookies.update(client.cookies)
            response = error_client.post("/imports/confirm", data=data)
    else:
        response = client.post("/imports/confirm", data=data)

    assert response.status_code == 422
    assert 'action="/imports/preview"' in response.text
    assert 'type="file"' in response.text
    assert 'action="/imports/confirm"' not in response.text
    assert str(metadata_file.parent) not in response.text
    db_session.expire_all()
    assert db_session.scalars(select(Transaction)).all() == []
    assert db_session.scalars(select(ImportBatch)).all() == []
    assert db_session.scalars(select(Asset)).all() == []


def test_upload_storage_error_does_not_disclose_private_path(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    login(client)
    private_path = r"C:\secret\portfolio-imports"

    def fail_private_dir() -> Path:
        raise OSError(f"cannot create {private_path}")

    monkeypatch.setattr("finance_app.imports.routes._private_dir", fail_private_dir)
    content = "基金代码,基金名称,份额,持仓成本,日期\n000001,示例基金,1,10.00,2026-09-23\n".encode()

    response = upload_csv(client, content)

    assert response.status_code == 422
    assert private_path not in response.text
    assert 'action="/imports/preview"' in response.text
    assert 'type="file"' in response.text
    assert 'action="/imports/confirm"' not in response.text


def test_confirm_requires_the_hash_from_the_latest_preview(
    client: TestClient, db_session
) -> None:
    login(client)
    content = "基金代码,基金名称,份额,持仓成本,日期\n000001,示例基金,1,10.00,2026-09-23\n".encode()
    assert upload_csv(client, content).status_code == 200
    account_id = db_session.scalar(select(Account.id))
    data = confirmation_data("f" * 64, account_id)
    data["csrf_token"] = csrf(client, "/imports")

    response = client.post("/imports/confirm", data=data)

    assert response.status_code == 422
    assert 'action="/imports/preview"' in response.text
    assert 'type="file"' in response.text
    assert 'action="/imports/confirm"' not in response.text
    db_session.expire_all()
    assert db_session.scalar(select(Transaction)) is None
    assert db_session.scalar(select(ImportBatch)) is None


def test_confirm_without_a_preview_writes_nothing(
    client: TestClient, db_session
) -> None:
    login(client)
    account_id = db_session.scalar(select(Account.id))
    data = confirmation_data("a" * 64, account_id)
    data["csrf_token"] = csrf(client, "/imports")

    response = client.post("/imports/confirm", data=data)

    assert response.status_code == 422
    assert 'action="/imports/preview"' in response.text
    assert 'type="file"' in response.text
    assert 'action="/imports/confirm"' not in response.text
    db_session.expire_all()
    assert db_session.scalar(select(Transaction)) is None
    assert db_session.scalar(select(ImportBatch)) is None


@pytest.mark.parametrize("digest", [None, "not-a-sha256"])
def test_confirm_invalid_or_missing_hash_shows_upload_form(
    client: TestClient, db_session, digest: str | None
) -> None:
    login(client)
    account_id = db_session.scalar(select(Account.id))
    data = confirmation_data("a" * 64, account_id)
    if digest is None:
        data.pop("sha256")
    else:
        data["sha256"] = digest
    data["csrf_token"] = csrf(client, "/imports")

    response = client.post("/imports/confirm", data=data)

    assert response.status_code == 422
    assert "invalid import hash" in response.text
    assert 'action="/imports/preview"' in response.text
    assert 'type="file"' in response.text
    assert 'action="/imports/confirm"' not in response.text
    db_session.expire_all()
    assert db_session.scalar(select(Transaction)) is None
    assert db_session.scalar(select(ImportBatch)) is None


def test_confirm_is_csrf_protected_uses_prg_audits_and_never_fabricates_price(
    client: TestClient, db_session, tmp_path: Path
) -> None:
    login(client)
    content = "基金代码,基金名称,份额,持仓成本,当前市值,日期\n000001,示例基金,2,20.00,25.00,2026-09-23\n".encode()
    preview = upload_csv(client, content)
    digest = hashlib.sha256(content).hexdigest()
    account_id = db_session.scalar(select(Account.id))
    data = {
        "sha256": digest,
        "account_id": str(account_id),
        "rows-0-asset_code": "000001",
        "rows-0-asset_name": "示例基金",
        "rows-0-quantity": "2",
        "rows-0-cost": "20.00",
        "rows-0-market_value": "25.00",
        "rows-0-available_cash": "",
        "rows-0-date": "2026-09-23",
    }
    assert preview.status_code == 200
    metadata_files = list((tmp_path / "private-imports").glob("*.json"))
    assert len(metadata_files) == 1
    metadata_file = metadata_files[0]
    assert client.post("/imports/confirm", data=data).status_code == 403
    data["csrf_token"] = csrf(client, "/imports")

    response = client.post("/imports/confirm", data=data)

    assert response.status_code == 303
    assert response.headers["location"] == "/transactions"
    db_session.expire_all()
    transaction = db_session.scalar(select(Transaction))
    assert transaction is not None
    assert transaction.source == f"import:{digest[:16]}"
    assert transaction.amount_cents == 2_000
    assert transaction.price is None
    assert transaction.quantity == 2
    assert db_session.scalar(select(Asset).where(Asset.code == "000001")) is not None
    batch = db_session.scalar(select(ImportBatch).where(ImportBatch.sha256 == digest))
    assert batch is not None
    assert batch.filename == digest
    events = list(db_session.scalars(select(AuditEvent)))
    assert "import.confirmed" in [event.event_type for event in events]
    assert all("positions.csv" not in event.details_json for event in events)
    assert not metadata_file.exists()
    assert (tmp_path / "private-imports" / f"{digest}.csv").read_bytes() == content


def test_same_confirmed_file_is_not_imported_twice(
    client: TestClient, db_session
) -> None:
    login(client)
    content = "基金代码,基金名称,份额,持仓成本,日期\n000001,示例基金,1,10.00,2026-09-23\n".encode()
    digest = hashlib.sha256(content).hexdigest()
    account_id = db_session.scalar(select(Account.id))
    assert upload_csv(client, content).status_code == 200
    data = {
        "csrf_token": csrf(client, "/imports"),
        "sha256": digest,
        "account_id": str(account_id),
        "rows-0-asset_code": "000001",
        "rows-0-asset_name": "示例基金",
        "rows-0-quantity": "1",
        "rows-0-cost": "10.00",
        "rows-0-market_value": "",
        "rows-0-available_cash": "",
        "rows-0-date": "2026-09-23",
    }
    assert client.post("/imports/confirm", data=data).status_code == 303

    duplicate = upload_csv(client, content)

    assert duplicate.status_code == 409
    assert "already imported" in duplicate.text.lower()
    db_session.expire_all()
    assert len(db_session.scalars(select(Transaction)).all()) == 1
    assert len(db_session.scalars(select(ImportBatch)).all()) == 1


@pytest.mark.parametrize("filename", ["bad.txt", "bad.exe", "bad.xls"])
def test_upload_rejects_disallowed_extensions(
    client: TestClient, filename: str
) -> None:
    login(client)
    response = upload_csv(client, b"x", filename)

    assert response.status_code == 422
    assert 'action="/imports/preview"' in response.text
    assert 'type="file"' in response.text
    assert 'action="/imports/confirm"' not in response.text


def test_parse_error_redisplays_the_upload_form(client: TestClient) -> None:
    login(client)

    response = upload_csv(client, b"\xff", "positions.csv")

    assert response.status_code == 422
    assert 'action="/imports/preview"' in response.text
    assert 'type="file"' in response.text
    assert 'action="/imports/confirm"' not in response.text


@pytest.mark.parametrize(
    "failure", ["bad-xlsx", "long-csv", "ocr-error", "ocr-timeout"]
)
def test_expected_import_failures_are_safe_422_without_writes(
    client: TestClient,
    db_session,
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
) -> None:
    login(client)
    private_detail = r"C:\private\tesseract command failed"
    if failure == "bad-xlsx":
        content, filename = b"not-a-zip-file", "positions.xlsx"
    elif failure == "long-csv":
        content, filename = b"name\n" + b"x" * 200_000 + b"\n", "positions.csv"
    else:
        content, filename = b"image", "positions.png"

        from finance_app.imports.ocr import extract_candidates

        ocr_failure: Exception = (
            TesseractError(1, private_detail)
            if failure == "ocr-error"
            else RuntimeError("Tesseract process timeout")
        )
        monkeypatch.setattr(
            "finance_app.imports.ocr.pytesseract.image_to_data",
            lambda *args, **kwargs: (_ for _ in ()).throw(ocr_failure),
        )
        monkeypatch.setattr(
            "finance_app.imports.routes.extract_candidates",
            lambda payload: extract_candidates(
                payload, image_loader=lambda _: object()
            ),
        )
    token = csrf(client, "/imports")
    with TestClient(
        client.app, follow_redirects=False, raise_server_exceptions=False
    ) as error_client:
        error_client.cookies.update(client.cookies)
        response = error_client.post(
            "/imports/preview",
            data={"csrf_token": token},
            files={"file": (filename, content, "application/octet-stream")},
        )

    assert response.status_code == 422
    assert 'action="/imports/preview"' in response.text
    assert 'action="/imports/confirm"' not in response.text
    assert private_detail not in response.text
    db_session.expire_all()
    assert db_session.scalar(select(Transaction)) is None
    assert db_session.scalar(select(ImportBatch)) is None


@pytest.mark.parametrize(
    "member_name", ["xl/workbook.xml", "xl/worksheets/sheet1.xml"]
)
def test_malformed_xlsx_xml_is_safe_422_without_disclosure(
    client: TestClient, db_session, member_name: str
) -> None:
    from tests.imports.test_parser import replace_zip_member, workbook_bytes

    login(client)
    content = workbook_bytes(
        ["基金代码", "基金名称", "份额", "持仓成本", "日期"],
        ["000001", "示例基金", "1", "10.00", "2026-09-23"],
    )
    corrupted = replace_zip_member(content, member_name, b"<broken")
    token = csrf(client, "/imports")
    with TestClient(
        client.app, follow_redirects=False, raise_server_exceptions=False
    ) as error_client:
        error_client.cookies.update(client.cookies)
        response = error_client.post(
            "/imports/preview",
            data={"csrf_token": token},
            files={
                "file": ("positions.xlsx", corrupted, "application/octet-stream")
            },
        )

    assert response.status_code == 422
    assert "XLSX workbook could not be read" in response.text
    assert "&lt;broken" not in response.text
    assert member_name not in response.text
    assert 'action="/imports/preview"' in response.text
    assert 'action="/imports/confirm"' not in response.text
    db_session.expire_all()
    assert db_session.scalar(select(Transaction)) is None
    assert db_session.scalar(select(ImportBatch)) is None


def test_preview_offloads_ocr_and_structured_parsing_from_event_loop(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    login(client)
    ran_in_worker: dict[str, bool] = {}

    def record_worker(label: str) -> None:
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            ran_in_worker[label] = True
        else:
            ran_in_worker[label] = False

    def fake_parse(content: bytes, filename: str):
        from finance_app.imports.parser import normalize_row

        record_worker("parser")
        return [
            normalize_row(
                {
                    "asset_code": "000001",
                    "asset_name": "示例基金",
                    "quantity": "1",
                    "cost": "10.00",
                    "date": "2026-09-23",
                },
                source_text="structured source",
            )
        ]

    def fake_ocr(content: bytes):
        record_worker("ocr")
        return SimpleNamespace(
            values={
                "asset_code": "000002",
                "asset_name": "OCR基金",
                "quantity": "1",
                "cost": "20.00",
                "date": "2026-09-23",
            },
            candidates=[],
            source_text="ocr source",
        )

    monkeypatch.setattr("finance_app.imports.routes.parse_portfolio_file", fake_parse)
    monkeypatch.setattr("finance_app.imports.routes.extract_candidates", fake_ocr)

    csv_response = upload_csv(client, b"csv", "positions.csv")
    image_response = upload_csv(client, b"image", "positions.png")

    assert csv_response.status_code == 200
    assert image_response.status_code == 200
    assert ran_in_worker == {"parser": True, "ocr": True}


def test_confirm_validation_error_writes_nothing_and_is_editable(
    client: TestClient, db_session
) -> None:
    login(client)
    content = "基金代码,基金名称,份额,持仓成本,日期\n000001,示例基金,1,10.00,2026-09-23\n".encode()
    digest = hashlib.sha256(content).hexdigest()
    assert upload_csv(client, content).status_code == 200
    response = client.post(
        "/imports/confirm",
        data={
            "csrf_token": csrf(client, "/imports"),
            "sha256": digest,
            "account_id": "999999",
            "rows-0-asset_code": "000001",
            "rows-0-asset_name": "保留名称",
            "rows-0-quantity": "1e2",
            "rows-0-cost": "10.00",
            "rows-0-market_value": "",
            "rows-0-available_cash": "",
            "rows-0-date": "2026-09-23",
        },
    )

    assert response.status_code == 422
    assert "保留名称" in response.text
    assert "1e2" in response.text
    assert "validation-errors" in response.text
    db_session.expire_all()
    assert db_session.scalar(select(Transaction)) is None
    assert db_session.scalar(select(ImportBatch)) is None


def test_confirm_giant_money_is_a_validation_error_without_writes(
    client: TestClient, db_session
) -> None:
    login(client)
    content = "基金代码,基金名称,份额,持仓成本,日期\n000001,示例基金,1,10.00,2026-09-23\n".encode()
    digest = hashlib.sha256(content).hexdigest()
    assert upload_csv(client, content).status_code == 200
    account_id = db_session.scalar(select(Account.id))
    giant = "9" * 5_000
    data = confirmation_data(digest, account_id)
    data["csrf_token"] = csrf(client, "/imports")
    data["rows-0-cost"] = giant
    with TestClient(
        client.app, follow_redirects=False, raise_server_exceptions=False
    ) as error_client:
        error_client.cookies.update(client.cookies)
        response = error_client.post("/imports/confirm", data=data)

    assert response.status_code == 422
    assert "成本金额超出可记录范围" in response.text
    assert f'name="rows-0-cost" value="{giant}"' in response.text
    db_session.expire_all()
    assert db_session.scalar(select(Transaction)) is None
    assert db_session.scalar(select(ImportBatch)) is None


def test_confirm_rejects_available_cash_without_writing_any_import_records(
    client: TestClient, db_session
) -> None:
    login(client)
    content = "基金代码,基金名称,份额,持仓成本,可用现金,日期\n000001,示例基金,1,10.00,25.00,2026-09-23\n".encode()
    digest = hashlib.sha256(content).hexdigest()
    assert upload_csv(client, content).status_code == 200
    account_id = db_session.scalar(select(Account.id))
    audit_count = len(db_session.scalars(select(AuditEvent)).all())
    data = confirmation_data(digest, account_id)
    data["csrf_token"] = csrf(client, "/imports")
    data["rows-0-available_cash"] = "25.00"

    response = client.post("/imports/confirm", data=data)

    assert response.status_code == 422
    assert "不能从持仓快照自动写入现金" in response.text
    assert "清空" in response.text
    assert "手工记录" in response.text
    db_session.expire_all()
    assert db_session.scalars(select(Transaction)).all() == []
    assert db_session.scalars(select(ImportBatch)).all() == []
    assert len(db_session.scalars(select(AuditEvent)).all()) == audit_count


def test_confirm_rolls_back_every_row_when_a_later_post_fails(
    client: TestClient, db_session, monkeypatch: pytest.MonkeyPatch
) -> None:
    login(client)
    content = (
        "基金代码,基金名称,份额,持仓成本,日期\n"
        "000001,第一只基金,1,10.00,2026-09-23\n"
        "000002,第二只基金,1,20.00,2026-09-23\n"
    ).encode()
    digest = hashlib.sha256(content).hexdigest()
    assert upload_csv(client, content).status_code == 200
    account_id = db_session.scalar(select(Account.id))

    from finance_app.imports import routes

    real_post_transaction = routes.post_transaction
    calls = 0

    def fail_second_post(session, command):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise ValueError("injected second-row failure")
        return real_post_transaction(session, command)

    monkeypatch.setattr(routes, "post_transaction", fail_second_post)
    data = {
        "csrf_token": csrf(client, "/imports"),
        "sha256": digest,
        "account_id": str(account_id),
    }
    for index, (code, name, cost) in enumerate(
        [("000001", "第一只基金", "10.00"), ("000002", "第二只基金", "20.00")]
    ):
        data.update(
            {
                f"rows-{index}-asset_code": code,
                f"rows-{index}-asset_name": name,
                f"rows-{index}-quantity": "1",
                f"rows-{index}-cost": cost,
                f"rows-{index}-market_value": "",
                f"rows-{index}-available_cash": "",
                f"rows-{index}-date": "2026-09-23",
            }
        )

    response = client.post("/imports/confirm", data=data)

    assert response.status_code == 422
    db_session.expire_all()
    assert db_session.scalars(select(Transaction)).all() == []
    assert db_session.scalars(select(ImportBatch)).all() == []
    assert db_session.scalars(select(Asset)).all() == []


def test_ocr_preview_is_candidate_only(
    client: TestClient, db_session, monkeypatch
) -> None:
    login(client)
    monkeypatch.setattr(
        "finance_app.imports.routes.extract_candidates",
        lambda _: type(
            "Preview",
            (),
            {
                "values": {
                    "asset_code": "000001",
                    "asset_name": "OCR基金",
                    "quantity": "1",
                },
                "candidates": [SimpleNamespace(confidence=87.5)],
                "source_text": "基金代码 000001\n基金名称 OCR基金\n份额 1",
                "requires_confirmation": True,
                "persisted_transactions": 0,
            },
        )(),
    )

    response = upload_csv(client, b"fake-image", "screenshot.png")

    assert response.status_code == 200
    assert "OCR基金" in response.text
    assert "基金代码 000001" in response.text
    assert "requires confirmation" in response.text.lower()
    assert response.text.count('role="status"') == 1
    assert "OCR 置信度 87.5%" in response.text
    db_session.expire_all()
    assert db_session.scalar(select(Transaction)) is None
    assert db_session.scalar(select(ImportBatch)) is None


def test_ocr_validation_error_preserves_trusted_preview_metadata_and_edits(
    client: TestClient, db_session, monkeypatch, tmp_path: Path
) -> None:
    login(client)
    source_text = "基金代码 000001\n基金名称 OCR基金\n份额 1"
    monkeypatch.setattr(
        "finance_app.imports.routes.extract_candidates",
        lambda _: type(
            "Preview",
            (),
            {
                "values": {
                    "asset_code": "000001",
                    "asset_name": "OCR基金",
                    "quantity": "1",
                },
                "candidates": [SimpleNamespace(confidence=87.5)],
                "source_text": source_text,
                "requires_confirmation": True,
                "persisted_transactions": 0,
            },
        )(),
    )
    content = b"fake-image"
    digest = hashlib.sha256(content).hexdigest()
    assert upload_csv(client, content, "screenshot.png").status_code == 200
    metadata_files = list((tmp_path / "private-imports").glob("*.json"))
    assert len(metadata_files) == 1
    metadata_file = metadata_files[0]
    account_id = db_session.scalar(select(Account.id))

    response = client.post(
        "/imports/confirm",
        data={
            "csrf_token": csrf(client, "/imports"),
            "sha256": digest,
            "account_id": str(account_id),
            "requires_confirmation": "false",
            "source_text": "forged overall source",
            "rows-0-asset_code": "000001",
            "rows-0-asset_name": "用户修正名称",
            "rows-0-quantity": "1e2",
            "rows-0-cost": "10.00",
            "rows-0-market_value": "",
            "rows-0-available_cash": "",
            "rows-0-date": "2026-09-23",
            "rows-0-source_text": "forged row source",
            "rows-0-confidence": "1.0",
            "rows-0-requires_confirmation": "false",
        },
    )

    assert response.status_code == 422
    assert "用户修正名称" in response.text
    assert 'name="rows-0-quantity" value="1e2"' in response.text
    assert "validation-errors" in response.text
    assert source_text in response.text
    assert "OCR 置信度 87.5%" in response.text
    assert "requires confirmation" in response.text.lower()
    assert "forged overall source" not in response.text
    assert "forged row source" not in response.text
    assert metadata_file.exists()
    db_session.expire_all()
    assert db_session.scalar(select(Transaction)) is None
    assert db_session.scalar(select(ImportBatch)) is None
