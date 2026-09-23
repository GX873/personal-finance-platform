"""Conservative OCR extraction that always returns confirmation-only candidates."""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from io import BytesIO
from statistics import fmean

import pytesseract  # type: ignore[import-untyped]
from PIL import Image
from pytesseract import Output

_PLAIN_NUMBER = re.compile(r"(?:0|[1-9][0-9]*)(?:\.[0-9]{1,8})?")
_MONEY = re.compile(r"(?:0|[1-9][0-9]*)(?:\.[0-9]{1,2})?")
_DATE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")
_CODE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")

_LABELS = {
    "asset_code": ("基金代码", "代码"),
    "asset_name": ("基金名称", "名称"),
    "quantity": ("份额",),
    "cost": ("成本金额", "持仓成本"),
    "market_value": ("当前市值", "持有金额"),
    "available_cash": ("可用现金",),
    "date": ("日期",),
}


@dataclass(frozen=True)
class OcrCandidate:
    field: str
    value: str
    source_text: str
    confidence: float
    line_number: int


@dataclass(frozen=True)
class OcrPreview:
    values: dict[str, str]
    candidates: list[OcrCandidate]
    source_text: str
    requires_confirmation: bool = True
    persisted_transactions: int = 0


@dataclass(frozen=True)
class _Token:
    text: str
    confidence: float


def _default_image_loader(content: bytes):
    image = Image.open(BytesIO(content))
    image.load()
    return image


def _confidence(raw: object) -> float:
    try:
        return max(0.0, min(100.0, float(str(raw))))
    except ValueError:
        return 0.0


def _lines(data: dict[str, list[object]]) -> list[list[_Token]]:
    grouped: dict[tuple[object, object, object], list[_Token]] = {}
    count = len(data.get("text", []))
    for index in range(count):
        text = str(data["text"][index]).strip()
        if not text:
            continue
        key = (
            data.get("block_num", [0] * count)[index],
            data.get("par_num", [0] * count)[index],
            data.get("line_num", list(range(count)))[index],
        )
        grouped.setdefault(key, []).append(
            _Token(
                text=text, confidence=_confidence(data.get("conf", [0] * count)[index])
            )
        )
    return list(grouped.values())


def _label_index(tokens: list[_Token], aliases: tuple[str, ...]) -> int | None:
    for index, token in enumerate(tokens):
        if any(alias in token.text for alias in aliases):
            return index
    return None


def _matches(field: str, value: str) -> bool:
    if field in {"cost", "market_value", "available_cash"}:
        return _MONEY.fullmatch(value) is not None
    if field == "quantity":
        return _PLAIN_NUMBER.fullmatch(value) is not None
    if field == "date":
        return _DATE.fullmatch(value) is not None
    if field == "asset_code":
        return _CODE.fullmatch(value) is not None
    return bool(value) and len(value) <= 200


def _candidate(
    field: str,
    aliases: tuple[str, ...],
    lines: list[list[_Token]],
) -> OcrCandidate | None:
    for line_number, tokens in enumerate(lines):
        label_at = _label_index(tokens, aliases)
        if label_at is None:
            continue
        for candidate_line in range(line_number, min(line_number + 2, len(lines))):
            possible = lines[candidate_line]
            start = label_at + 1 if candidate_line == line_number else 0
            for token in possible[start:]:
                value = token.text.strip(":：,，")
                if _matches(field, value):
                    source = " ".join(item.text for item in possible)
                    confidence = fmean([tokens[label_at].confidence, token.confidence])
                    return OcrCandidate(
                        field=field,
                        value=value,
                        source_text=source,
                        confidence=confidence,
                        line_number=candidate_line + 1,
                    )
    return None


def extract_candidates(
    content: bytes,
    *,
    image_loader: Callable[[bytes], object] = _default_image_loader,
) -> OcrPreview:
    """Extract bounded label/value candidates without writing financial data."""
    image = image_loader(content)
    data = pytesseract.image_to_data(image, lang="chi_sim+eng", output_type=Output.DICT)
    lines = _lines(data)
    candidates = [
        candidate
        for field, aliases in _LABELS.items()
        if (candidate := _candidate(field, aliases, lines)) is not None
    ]
    return OcrPreview(
        values={candidate.field: candidate.value for candidate in candidates},
        candidates=candidates,
        source_text="\n".join(" ".join(token.text for token in line) for line in lines),
    )
