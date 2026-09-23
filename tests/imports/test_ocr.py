from __future__ import annotations

import pytest
from pytesseract.pytesseract import TesseractError, TesseractNotFoundError

from finance_app.imports.ocr import extract_candidates
from finance_app.imports.parser import ImportFileError


def ocr_data(lines: list[list[tuple[str, str]]]) -> dict[str, list[object]]:
    result: dict[str, list[object]] = {
        "text": [],
        "conf": [],
        "block_num": [],
        "par_num": [],
        "line_num": [],
    }
    for line_number, tokens in enumerate(lines, start=1):
        for text, confidence in tokens:
            result["text"].append(text)
            result["conf"].append(confidence)
            result["block_num"].append(1)
            result["par_num"].append(1)
            result["line_num"].append(line_number)
    return result


def test_ocr_result_is_candidate_only_and_groups_tokens_by_line(monkeypatch) -> None:
    captured = {}

    def fake_image_to_data(image, *, lang, output_type, timeout=None):
        captured.update(lang=lang, output_type=output_type, timeout=timeout)
        return ocr_data(
            [
                [("基金名称", "91"), ("示例基金", "88")],
                [("份额", "94"), ("10.25", "97")],
                [("持仓成本", "89"), ("100.01", "96")],
            ]
        )

    monkeypatch.setattr(
        "finance_app.imports.ocr.pytesseract.image_to_data", fake_image_to_data
    )

    preview = extract_candidates(b"not-a-real-image", image_loader=lambda _: object())

    assert captured["lang"] == "chi_sim+eng"
    assert captured["timeout"] == 30
    assert preview.requires_confirmation is True
    assert preview.persisted_transactions == 0
    assert preview.values["asset_name"] == "示例基金"
    assert preview.values["quantity"] == "10.25"
    assert preview.values["cost"] == "100.01"
    assert preview.candidates[0].source_text
    assert preview.candidates[0].confidence <= 100


def test_ocr_rejects_non_plain_numbers_and_distant_label_matches(monkeypatch) -> None:
    def fake_image_to_data(image, *, lang, output_type, timeout=None):
        return ocr_data(
            [
                [("持仓成本", "90")],
                [("说明", "90")],
                [("1e2", "99"), ("100.00", "99")],
                [("份额", "90"), ("1,000", "99")],
            ]
        )

    monkeypatch.setattr(
        "finance_app.imports.ocr.pytesseract.image_to_data", fake_image_to_data
    )

    preview = extract_candidates(b"image", image_loader=lambda _: object())

    assert "cost" not in preview.values
    assert "quantity" not in preview.values


def test_ocr_associates_a_label_with_the_adjacent_line_only(monkeypatch) -> None:
    def fake_image_to_data(image, *, lang, output_type, timeout=None):
        return ocr_data([[("可用现金", "90")], [("12.34", "95")], [("999.99", "95")]])

    monkeypatch.setattr(
        "finance_app.imports.ocr.pytesseract.image_to_data", fake_image_to_data
    )

    preview = extract_candidates(b"image", image_loader=lambda _: object())

    assert preview.values["available_cash"] == "12.34"


def test_ocr_does_not_associate_adjacent_lines_across_blocks(monkeypatch) -> None:
    def fake_image_to_data(image, *, lang, output_type, timeout=None):
        return {
            "text": ["可用现金", "12.34"],
            "conf": ["90", "95"],
            "block_num": [1, 2],
            "par_num": [1, 1],
            "line_num": [1, 1],
        }

    monkeypatch.setattr(
        "finance_app.imports.ocr.pytesseract.image_to_data", fake_image_to_data
    )

    preview = extract_candidates(b"image", image_loader=lambda _: object())

    assert "available_cash" not in preview.values


def test_ocr_does_not_associate_adjacent_lines_across_paragraphs(monkeypatch) -> None:
    def fake_image_to_data(image, *, lang, output_type, timeout=None):
        return {
            "text": ["可用现金", "12.34"],
            "conf": ["90", "95"],
            "block_num": [1, 1],
            "par_num": [1, 2],
            "line_num": [1, 1],
        }

    monkeypatch.setattr(
        "finance_app.imports.ocr.pytesseract.image_to_data", fake_image_to_data
    )

    preview = extract_candidates(b"image", image_loader=lambda _: object())

    assert "available_cash" not in preview.values


@pytest.mark.parametrize(
    "failure",
    [
        TesseractError(1, r"C:\private\tesseract command failed"),
        TesseractNotFoundError(),
        RuntimeError("Tesseract process timeout"),
    ],
    ids=["engine-error", "not-found", "timeout"],
)
def test_expected_tesseract_failures_are_safe_import_errors(
    monkeypatch: pytest.MonkeyPatch, failure: Exception
) -> None:
    def fail_ocr(*args, **kwargs):
        raise failure

    monkeypatch.setattr(
        "finance_app.imports.ocr.pytesseract.image_to_data", fail_ocr
    )

    with pytest.raises(ImportFileError) as caught:
        extract_candidates(b"image", image_loader=lambda _: object())

    assert str(caught.value) in {"OCR could not be completed", "OCR timed out"}
    assert "private" not in str(caught.value)


@pytest.mark.parametrize("failure", [MemoryError("oom"), KeyboardInterrupt()])
def test_unexpected_ocr_failures_are_not_hidden(
    monkeypatch: pytest.MonkeyPatch, failure: BaseException
) -> None:
    def fail_ocr(*args, **kwargs):
        raise failure

    monkeypatch.setattr(
        "finance_app.imports.ocr.pytesseract.image_to_data", fail_ocr
    )

    with pytest.raises(type(failure)):
        extract_candidates(b"image", image_loader=lambda _: object())
