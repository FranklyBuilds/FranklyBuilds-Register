# Account Export Format Additions

## Goal

Add two account-pool export options while preserving every existing export format:

- `account----password----mailbox-url----2fa`
- `account----password----mailbox-url`

In stored account records, `account` maps to `email`, `password` maps to
`chatgptPassword`, `mailbox-url` maps to `emailAccessUrl`, and `2fa` maps to
`totpSecret`.

## Contract

Introduce two format identifiers shared by the frontend and backend:

| Format identifier | Output fields | Filename suffix |
| --- | --- | --- |
| `credentials-mail-links-totp` | `email----chatgptPassword----emailAccessUrl----totpSecret` | `credentials-mail-links-totp` |
| `credentials-mail-links` | `email----chatgptPassword----emailAccessUrl` | `credentials-mail-links` |

Each account produces exactly one UTF-8 line with no header. Existing selected,
single-account, and all-account scopes continue to work. Empty stored values are
emitted as empty fields so the number and order of columns remain stable; rows are
not skipped.

## Components And Data Flow

The account export dialog adds two explicit radio options using the requested
field order. Its selected format is sent unchanged through `dataGateway` to
`POST /api/accounts/export` with the existing `scope` and `ids` fields.

The backend request model accepts the two identifiers. `ResourceService` loads
the same account records used by existing exports, joins the required fields with
`----`, and returns the existing `TextExport` response shape. No database or API
response schema migration is required.

The local `buildAccountExport` utility is kept consistent with the server even
though production account exports currently use the backend response.

## Compatibility And Errors

All four existing formats remain unchanged. Unknown format identifiers continue
to fail request validation. Export download, clipboard delivery, filenames,
scope selection, and Access Token filtering retain their current behavior.

The new formats contain secrets and remain behind the existing manual export
dialog. Exported content is not added to logs or browser storage.

## Tests

- Frontend formatter tests assert exact content, order, delimiter count, and
  filename suffix for both new formats.
- Dialog coverage asserts both new choices are visible and selectable.
- Backend service tests assert exact server-generated text and returned format
  for both selected identifiers, including empty-field column preservation.
- Existing frontend tests, backend tests, type checking, and the production
  frontend build must continue to pass.

## Out Of Scope

This change does not add arbitrary field selection, reorder existing formats,
change email-pool exports, or modify paid-account export formats.
