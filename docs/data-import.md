# Data Import Guide

Imports are private, bounded, and confirmation-gated. Uploads are stored
outside the public static tree and are protected by the authenticated import
page. A preview is generated first; no transaction or cash balance is written
until the operator reviews every row and submits the confirmation form.

## CSV and XLSX column mapping

Use UTF-8 CSV with a header row, or an XLSX workbook whose first sheet contains
the header row. Header matching ignores spaces and accepts these canonical
fields and common aliases:

| Canonical field | Meaning | Required rules |
| --- | --- | --- |
| `asset_code` | Fund/security code | Alphanumeric code; required for a holding row |
| `asset_name` | Display name | Non-empty for a holding row |
| `quantity` | Units/shares | Non-negative decimal, up to 8 places; greater than zero for holdings |
| `cost` | Cost basis in yuan | Non-negative money with at most 2 decimals; positive for holdings |
| `market_value` | Current market value in yuan | Optional; do not invent it when the source is stale |
| `available_cash` | Confirmed available cash in yuan | Optional; only enter a dated, confirmed amount |
| `date` | Source/transaction date | ISO `YYYY-MM-DD`; required on every row |

The Chinese aliases shown in the upload form map to the same fields: code/name,
quantity, cost or holding cost, market value or holding value, available cash,
and date. Keep one logical holding per row. A cash-only row must leave holding
fields empty and provide `available_cash` plus `date`.

Limits are deliberately conservative: 10 MiB upload size, 2,000 data rows,
64 columns, 50,000 cells, bounded field/preview lengths, and XLSX archive
expansion checks. Malformed or over-sized files are rejected without partial
writes. Duplicate content is detected by SHA-256 and cannot be confirmed a
second time accidentally.

## Preview and confirmation

After upload, inspect the row values, validation errors, source text, and the
as-of date. Correct values in the preview form rather than editing a file
silently. Submit only after verifying code, name, units, cost, market value,
cash, and date. The server revalidates the preview identifier, CSRF token,
digest, and row values before persisting. A confirmation is a normal ledger
operation and appears in the audit trail.

Never use an import to turn unknown cash into zero. If the available cash is not
confirmed, omit the field and leave it unknown; the daily check will then avoid
new-buy advice. Do not import a broker screenshot as if it were a structured
ledger export.

## OCR screenshots

PNG, JPEG, and JPG uploads produce OCR candidates only. OCR may misread a code,
decimal, or date, so the preview always has `requires_confirmation=true` and
`persisted_transactions=0`. Review every candidate against the original
screenshot, correct it manually, and then use the same explicit confirmation
step as CSV/XLSX. A low-confidence or missing candidate should be entered via
the manual form instead of guessed.

OCR is bounded by the same upload limits and a timeout. It does not make a
broker request, refresh a NAV, or infer a cash balance. Delete screenshots from
the private upload directory after successful confirmation when retention is
not required.

## Troubleshooting

- `unsupported import file type`: use CSV, XLSX, PNG, JPG, or JPEG.
- `preview expired` or duplicate digest: upload again and perform a fresh
  review; do not reuse an old browser tab.
- `date` or numeric validation errors: use ISO dates and plain decimal numbers
  without currency symbols or comma separators.
- A row that contains neither a holding nor confirmed cash is rejected.

If an import was confirmed incorrectly, record a reversal through the audited
transaction form. Do not edit SQLite directly while the service is running.
