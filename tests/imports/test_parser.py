from __future__ import annotations

from io import BytesIO
from zipfile import ZIP_DEFLATED, ZipFile

import pytest
from openpyxl import Workbook

from finance_app.imports import parser
from finance_app.imports.parser import (
    MAX_UPLOAD_BYTES,
    ImportFileError,
    parse_portfolio_file,
)


def workbook_bytes(headers: list[str], row: list[object]) -> bytes:
    workbook = Workbook()
    sheet = workbook.active
    sheet.append(headers)
    sheet.append(row)
    output = BytesIO()
    workbook.save(output)
    return output.getvalue()


def workbook_rows_bytes(headers: list[str], rows: list[list[object]]) -> bytes:
    workbook = Workbook()
    sheet = workbook.active
    sheet.append(headers)
    for row in rows:
        sheet.append(row)
    output = BytesIO()
    workbook.save(output)
    return output.getvalue()


def zip_bytes(entries: list[tuple[str, bytes]]) -> bytes:
    output = BytesIO()
    with ZipFile(output, "w", ZIP_DEFLATED) as archive:
        for name, content in entries:
            archive.writestr(name, content)
    return output.getvalue()


def test_xlsx_normalizes_chinese_columns_and_exact_money() -> None:
    content = workbook_bytes(
        ["基金代码", "基金名称", "份额", "持仓成本", "当前市值", "日期"],
        ["000001", "示例基金", "10.125", "100.01", "123.45", "2026-09-23"],
    )

    rows = parse_portfolio_file(content, "portfolio.xlsx")

    assert len(rows) == 1
    assert rows[0].asset_code == "000001"
    assert rows[0].asset_name == "示例基金"
    assert str(rows[0].quantity) == "10.125"
    assert rows[0].cost_cents == 10_001
    assert rows[0].market_value_cents == 12_345
    assert rows[0].occurred_on.isoformat() == "2026-09-23"


def test_csv_accepts_aliases_and_preserves_source_text() -> None:
    content = (
        "代码,名称,份额,成本金额,持有金额,可用现金,日期\n"
        "161725,白酒基金,2.5,20.00,21.50,30.01,2026-09-22\n"
    ).encode()

    row = parse_portfolio_file(content, "positions.csv")[0]

    assert row.asset_code == "161725"
    assert row.asset_name == "白酒基金"
    assert row.available_cash_cents == 3_001
    assert "161725" in row.source_text


@pytest.mark.parametrize(
    "filename", ["positions.txt", "positions.xls", "positions.exe"]
)
def test_parser_rejects_unsupported_extensions(filename: str) -> None:
    with pytest.raises(ImportFileError, match="unsupported"):
        parse_portfolio_file(b"x", filename)


def test_parser_rejects_oversized_input_before_parsing() -> None:
    with pytest.raises(ImportFileError, match="10 MiB"):
        parse_portfolio_file(b"x" * (MAX_UPLOAD_BYTES + 1), "positions.csv")


def test_parser_reports_strict_numeric_validation_errors() -> None:
    content = "基金代码,基金名称,份额,持仓成本\n000001,示例,1e2,12.345\n".encode()

    row = parse_portfolio_file(content, "positions.csv")[0]

    assert any("份额" in error for error in row.errors)
    assert any("成本" in error for error in row.errors)


@pytest.mark.parametrize(
    ("content", "filename"),
    [
        (b"not-a-zip-file", "positions.xlsx"),
        (b"name\n" + b"x" * 200_000 + b"\n", "positions.csv"),
    ],
    ids=["bad-xlsx", "long-csv"],
)
def test_malformed_workbooks_raise_safe_import_errors(
    content: bytes, filename: str
) -> None:
    with pytest.raises(ImportFileError) as caught:
        parse_portfolio_file(content, filename)

    assert str(caught.value) in {
        "CSV could not be read",
        "XLSX workbook could not be read",
    }
    assert "positions" not in str(caught.value)


@pytest.mark.parametrize("failure", [MemoryError("oom"), KeyboardInterrupt()])
def test_unexpected_csv_failures_are_not_hidden(
    monkeypatch: pytest.MonkeyPatch, failure: BaseException
) -> None:
    def fail_reader(*args, **kwargs):
        raise failure

    monkeypatch.setattr(parser.csv, "reader", fail_reader)

    with pytest.raises(type(failure)):
        parse_portfolio_file(b"header\nvalue\n", "positions.csv")


def csv_rows(count: int, *, extra_headers: list[str] | None = None) -> bytes:
    headers = ["基金代码", "基金名称", "份额", "持仓成本", "日期"]
    headers.extend(extra_headers or [])
    lines = [",".join(headers)]
    values = ["000001", "示例基金", "1", "10.00", "2026-09-23"]
    values.extend("x" for _ in (extra_headers or []))
    lines.extend(",".join(values) for _ in range(count))
    return ("\n".join(lines) + "\n").encode()


def test_csv_row_limit_is_inclusive_and_rejects_the_next_row() -> None:
    assert len(parse_portfolio_file(csv_rows(2_000), "positions.csv")) == 2_000

    with pytest.raises(ImportFileError, match="row limit"):
        parse_portfolio_file(csv_rows(2_001), "positions.csv")


def test_csv_rejects_column_and_field_limits() -> None:
    with pytest.raises(ImportFileError, match="column limit"):
        parse_portfolio_file(
            csv_rows(1, extra_headers=[f"extra-{index}" for index in range(60)]),
            "positions.csv",
        )

    field = "x" * 10_001
    content = f"基金代码,基金名称,份额,持仓成本,日期,备注\n000001,示例基金,1,10.00,2026-09-23,{field}\n".encode()
    with pytest.raises(ImportFileError, match="field limit"):
        parse_portfolio_file(content, "positions.csv")


def test_csv_rejects_cell_and_total_preview_budgets(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(parser, "MAX_IMPORT_CELLS", 10, raising=False)
    with pytest.raises(ImportFileError, match="cell limit"):
        parse_portfolio_file(csv_rows(2, extra_headers=["备注"]), "positions.csv")

    monkeypatch.setattr(parser, "MAX_IMPORT_CELLS", 50_000, raising=False)
    monkeypatch.setattr(parser, "MAX_PREVIEW_CHARACTERS", 10, raising=False)
    with pytest.raises(ImportFileError, match="preview size"):
        parse_portfolio_file(csv_rows(1, extra_headers=["备注"]), "positions.csv")


@pytest.mark.parametrize(
    ("setting", "limit", "entries", "message"),
    [
        (
            "MAX_XLSX_UNCOMPRESSED_BYTES",
            100,
            [("one", b"a" * 60), ("two", b"b" * 60)],
            "expanded size",
        ),
        ("MAX_XLSX_ENTRY_BYTES", 100, [("one", b"a" * 101)], "entry size"),
        (
            "MAX_XLSX_COMPRESSION_RATIO",
            5,
            [("one", b"a" * 5_000)],
            "compression ratio",
        ),
    ],
    ids=["total-expanded", "single-entry", "compression-ratio"],
)
def test_xlsx_zip_budget_rejects_suspicious_archives(
    monkeypatch: pytest.MonkeyPatch,
    setting: str,
    limit: int,
    entries: list[tuple[str, bytes]],
    message: str,
) -> None:
    monkeypatch.setattr(parser, "MAX_XLSX_UNCOMPRESSED_BYTES", 10_000, raising=False)
    monkeypatch.setattr(parser, "MAX_XLSX_ENTRY_BYTES", 10_000, raising=False)
    monkeypatch.setattr(parser, "MAX_XLSX_COMPRESSION_RATIO", 1_000, raising=False)
    monkeypatch.setattr(parser, setting, limit, raising=False)

    with pytest.raises(ImportFileError, match=message):
        parse_portfolio_file(zip_bytes(entries), "positions.xlsx")


@pytest.mark.parametrize(
    ("setting", "limit", "headers", "rows", "message"),
    [
        (
            "MAX_IMPORT_ROWS",
            1,
            ["基金代码", "基金名称", "份额", "持仓成本", "日期"],
            [
                ["000001", "一号", "1", "10.00", "2026-09-23"],
                ["000002", "二号", "1", "20.00", "2026-09-23"],
            ],
            "row limit",
        ),
        (
            "MAX_IMPORT_COLUMNS",
            5,
            ["基金代码", "基金名称", "份额", "持仓成本", "日期", "备注"],
            [["000001", "一号", "1", "10.00", "2026-09-23", "x"]],
            "column limit",
        ),
        (
            "MAX_IMPORT_CELLS",
            10,
            ["基金代码", "基金名称", "份额", "持仓成本", "日期", "备注"],
            [["000001", "一号", "1", "10.00", "2026-09-23", "x"]],
            "cell limit",
        ),
    ],
    ids=["rows", "columns", "cells"],
)
def test_xlsx_rejects_declared_worksheet_dimensions_over_budget(
    monkeypatch: pytest.MonkeyPatch,
    setting: str,
    limit: int,
    headers: list[str],
    rows: list[list[object]],
    message: str,
) -> None:
    monkeypatch.setattr(parser, setting, limit, raising=False)

    with pytest.raises(ImportFileError, match=message):
        parse_portfolio_file(workbook_rows_bytes(headers, rows), "positions.xlsx")


def test_xlsx_iteration_enforces_row_limit_when_dimensions_underreport(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Sheet:
        max_row = 1
        max_column = 5

        def iter_rows(self, *, values_only: bool):
            assert values_only is True
            yield ("基金代码", "基金名称", "份额", "持仓成本", "日期")
            yield ("000001", "一号", "1", "10.00", "2026-09-23")
            yield ("000002", "二号", "1", "20.00", "2026-09-23")

    class WorkbookStub:
        active = Sheet()
        closed = False

        def close(self) -> None:
            self.closed = True

    workbook = WorkbookStub()
    monkeypatch.setattr(parser, "MAX_IMPORT_ROWS", 1, raising=False)
    monkeypatch.setattr(parser, "_validate_xlsx_archive", lambda _: None, raising=False)
    monkeypatch.setattr(parser, "load_workbook", lambda *args, **kwargs: workbook)

    with pytest.raises(ImportFileError, match="row limit"):
        parse_portfolio_file(b"xlsx", "positions.xlsx")

    assert workbook.closed is True
