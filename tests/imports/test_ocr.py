from __future__ import annotations

from finance_app.imports.ocr import extract_candidates


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

    def fake_image_to_data(image, *, lang, output_type):
        captured.update(lang=lang, output_type=output_type)
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
    assert preview.requires_confirmation is True
    assert preview.persisted_transactions == 0
    assert preview.values["asset_name"] == "示例基金"
    assert preview.values["quantity"] == "10.25"
    assert preview.values["cost"] == "100.01"
    assert preview.candidates[0].source_text
    assert preview.candidates[0].confidence <= 100


def test_ocr_rejects_non_plain_numbers_and_distant_label_matches(monkeypatch) -> None:
    def fake_image_to_data(image, *, lang, output_type):
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
    def fake_image_to_data(image, *, lang, output_type):
        return ocr_data([[("可用现金", "90")], [("12.34", "95")], [("999.99", "95")]])

    monkeypatch.setattr(
        "finance_app.imports.ocr.pytesseract.image_to_data", fake_image_to_data
    )

    preview = extract_candidates(b"image", image_loader=lambda _: object())

    assert preview.values["available_cash"] == "12.34"
