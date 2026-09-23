from __future__ import annotations

from io import BytesIO

import pytest
from openpyxl import Workbook

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
