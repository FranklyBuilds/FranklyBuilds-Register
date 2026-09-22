# Account Export Formats Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add `account----password----mailbox-url----2fa` and `account----password----mailbox-url` as explicit account-pool export formats without changing existing formats.

**Architecture:** Extend the shared frontend and backend format unions with two stable identifiers. Keep the existing request flow unchanged: the dialog submits `format`, `scope`, and `ids`, and the backend remains authoritative for exported text. Mirror the backend behavior in the local frontend formatter so its contract stays complete and testable.

**Tech Stack:** Vue 3, TypeScript 6, Element Plus, Vitest, FastAPI, Pydantic, pytest

**Repository note:** This workspace snapshot has no `.git` metadata, so commit steps are omitted. Each green test run is the execution checkpoint.

---

### Task 1: Extend The Frontend Export Contract And Formatter

**Files:**
- Modify: `app/src/services/exporter.spec.ts`
- Modify: `app/src/types/index.ts:243`
- Modify: `app/src/services/exporter.ts:17`

- [ ] **Step 1: Write failing formatter tests for both new field orders**

Add these cases inside `describe('buildAccountExport', ...)` in `app/src/services/exporter.spec.ts`:

```ts
it('exports account password mailbox URL and 2FA in the requested order', () => {
  const result = buildAccountExport([account(1)], 'credentials-mail-links-totp', now)

  expect(result.content).toBe(
    'demo1@example.com----password-1----https://example.com/inbox/1----TOTP-1',
  )
  expect(result.filename).toBe(
    'accounts-1-credentials-mail-links-totp-20260808-153000.txt',
  )
})

it('exports account password and mailbox URL in the requested order', () => {
  const result = buildAccountExport([account(1)], 'credentials-mail-links', now)

  expect(result.content).toBe(
    'demo1@example.com----password-1----https://example.com/inbox/1',
  )
  expect(result.filename).toBe(
    'accounts-1-credentials-mail-links-20260808-153000.txt',
  )
})
```

- [ ] **Step 2: Run the formatter tests and verify RED**

Run:

```powershell
npm test -- src/services/exporter.spec.ts
```

Expected: both new cases fail because the current fallback exports only `email----emailAccessUrl`.

- [ ] **Step 3: Add the new frontend identifiers**

Change `ExportFormat` in `app/src/types/index.ts` to:

```ts
export type ExportFormat =
  | 'credentials'
  | 'mail-links'
  | 'mail-links-totp'
  | 'credentials-mail-links'
  | 'credentials-mail-links-totp'
  | 'access-tokens'
```

- [ ] **Step 4: Implement exhaustive local line formatting**

Replace the nested formatter in `buildAccountExport` with an explicit switch helper in `app/src/services/exporter.ts`:

```ts
type TextAccountExportFormat = Exclude<ExportFormat, 'access-tokens'>

function formatAccountLine(account: AccountRecord, format: TextAccountExportFormat) {
  switch (format) {
    case 'credentials':
      return `${account.email}----${account.chatgptPassword}----${account.totpSecret}`
    case 'mail-links':
      return `${account.email}----${account.emailAccessUrl}`
    case 'mail-links-totp':
      return `${account.email}----${account.emailAccessUrl}----${account.totpSecret}`
    case 'credentials-mail-links':
      return `${account.email}----${account.chatgptPassword}----${account.emailAccessUrl}`
    case 'credentials-mail-links-totp':
      return `${account.email}----${account.chatgptPassword}----${account.emailAccessUrl}----${account.totpSecret}`
  }
}
```

Use `accounts.map((account) => formatAccountLine(account, format)).join('\n')` and use `format` directly as the filename suffix because every text format identifier is already its suffix.

- [ ] **Step 5: Run the formatter tests and verify GREEN**

Run:

```powershell
npm test -- src/services/exporter.spec.ts
```

Expected: all `exporter.spec.ts` tests pass.

### Task 2: Expose Both Formats In The Account Export Dialog

**Files:**
- Modify: `app/src/components/ExportDialog.spec.ts`
- Modify: `app/src/components/ExportDialog.vue:92`

- [ ] **Step 1: Write a failing dialog option test**

Add a separate `describe('ExportDialog account formats', ...)` block:

```ts
describe('ExportDialog account formats', () => {
  it('offers both password and mailbox URL combinations', () => {
    const wrapper = mount(ExportDialog, {
      props: {
        modelValue: true,
        scope: 'all',
        ids: [],
        count: 2,
      },
      global: { plugins: [ElementPlus] },
    })

    const values = wrapper
      .findAllComponents({ name: 'ElRadio' })
      .map((radio) => radio.props('value'))

    expect(values).toContain('credentials-mail-links-totp')
    expect(values).toContain('credentials-mail-links')
    expect(wrapper.text()).toContain('账号----密码----接码 URL----2FA')
    expect(wrapper.text()).toContain('账号----密码----接码 URL')
  })
})
```

- [ ] **Step 2: Run the dialog test and verify RED**

Run:

```powershell
npm test -- src/components/ExportDialog.spec.ts
```

Expected: the new radio values and labels are absent.

- [ ] **Step 3: Add two fixed radio choices**

Insert these options before `access-tokens` in `app/src/components/ExportDialog.vue`:

```vue
<el-radio value="credentials-mail-links-totp" border>
  <strong>密码 + 接码地址 + 2FA</strong>
  <span>账号----密码----接码 URL----2FA</span>
</el-radio>
<el-radio value="credentials-mail-links" border>
  <strong>密码 + 接码地址</strong>
  <span>账号----密码----接码 URL</span>
</el-radio>
```

Keep the existing two-column desktop and one-column mobile grid unchanged; each radio already has stable width and minimum height.

- [ ] **Step 4: Run the dialog test and verify GREEN**

Run:

```powershell
npm test -- src/components/ExportDialog.spec.ts
```

Expected: all dialog tests pass.

### Task 3: Extend Backend Validation And Server-Generated Export Text

**Files:**
- Modify: `app/tests/backend/test_mongodb_api.py`
- Modify: `app/backend/resource_models.py:14`
- Modify: `app/backend/resource_service.py:2168`

- [ ] **Step 1: Write failing backend tests for both formats**

Import `pytest` and `AccountRecord`, then add this fixture store and parameterized test near the existing Access Token export test:

```python
class CredentialExportStore:
    async def accounts_for_export(self, ids):
        assert ids == ["account-fixture"]
        return [
            AccountRecord(
                id="account-fixture",
                email="person@example.test",
                chatgptPassword="PASSWORD_FIXTURE",
                totpSecret="TOTP_FIXTURE",
                emailAccessUrl="https://mail.example.test/inbox/person",
                createdAt=datetime(2026, 8, 24, tzinfo=timezone.utc),
                accountType="free",
            )
        ]


@pytest.mark.parametrize(
    ("export_format", "expected", "suffix"),
    [
        (
            "credentials-mail-links-totp",
            "person@example.test----PASSWORD_FIXTURE----"
            "https://mail.example.test/inbox/person----TOTP_FIXTURE",
            "credentials-mail-links-totp",
        ),
        (
            "credentials-mail-links",
            "person@example.test----PASSWORD_FIXTURE----"
            "https://mail.example.test/inbox/person",
            "credentials-mail-links",
        ),
    ],
)
def test_combined_credential_exports_preserve_requested_field_order(
    export_format: str,
    expected: str,
    suffix: str,
) -> None:
    service = ResourceService(CredentialExportStore())  # type: ignore[arg-type]

    result = asyncio.run(
        service.export_accounts(
            AccountExportInput(
                format=export_format,
                scope="selected",
                ids=["account-fixture"],
            )
        )
    )

    assert result.content == expected
    assert result.format == export_format
    assert result.filename.startswith(f"accounts-1-{suffix}-")
```

Add a separate empty-field stability test:

```python
def test_combined_totp_export_preserves_empty_field_positions() -> None:
    class EmptyCredentialExportStore:
        async def accounts_for_export(self, ids):
            _ = ids
            return [
                AccountRecord(
                    id="empty-fixture",
                    email="empty@example.test",
                    chatgptPassword="",
                    totpSecret="",
                    emailAccessUrl="https://mail.example.test/inbox/empty",
                    createdAt=datetime(2026, 8, 24, tzinfo=timezone.utc),
                    accountType="free",
                )
            ]

    result = asyncio.run(
        ResourceService(EmptyCredentialExportStore()).export_accounts(  # type: ignore[arg-type]
            AccountExportInput(
                format="credentials-mail-links-totp",
                scope="all",
                ids=[],
            )
        )
    )

    assert result.content.split("----") == [
        "empty@example.test",
        "",
        "https://mail.example.test/inbox/empty",
        "",
    ]
```

- [ ] **Step 2: Run the backend tests and verify RED**

Run:

```powershell
.\register_env\Scripts\python.exe -m pytest app/tests/backend/test_mongodb_api.py -q
```

Expected: Pydantic rejects the two new format identifiers before service formatting runs.

- [ ] **Step 3: Extend the backend request and response format union**

Change `AccountExportFormat` in `app/backend/resource_models.py` to:

```python
AccountExportFormat = Literal[
    "credentials",
    "mail-links",
    "mail-links-totp",
    "credentials-mail-links",
    "credentials-mail-links-totp",
    "access-tokens",
]
```

- [ ] **Step 4: Add explicit backend formatting branches**

Add these branches before the existing `else` in `ResourceService.export_accounts`:

```python
elif incoming.format == "credentials-mail-links-totp":
    content = "\n".join(
        f"{item.email}----{item.chatgptPassword}----"
        f"{item.emailAccessUrl}----{item.totpSecret}"
        for item in records
    )
    suffix = "credentials-mail-links-totp"
elif incoming.format == "credentials-mail-links":
    content = "\n".join(
        f"{item.email}----{item.chatgptPassword}----{item.emailAccessUrl}"
        for item in records
    )
    suffix = "credentials-mail-links"
```

- [ ] **Step 5: Run the backend tests and verify GREEN**

Run:

```powershell
.\register_env\Scripts\python.exe -m pytest app/tests/backend/test_mongodb_api.py -q
```

Expected: all tests in `test_mongodb_api.py` pass.

### Task 4: Full Regression And UI Verification

**Files:**
- Verify only; no planned source changes

- [ ] **Step 1: Run all frontend tests**

Run:

```powershell
npm test
```

Expected: all Vitest files pass with zero failed tests.

- [ ] **Step 2: Run frontend type checking**

Run:

```powershell
npm run type-check
```

Expected: `vue-tsc --build` exits with code 0.

- [ ] **Step 3: Run the focused backend regression suite**

Run:

```powershell
.\register_env\Scripts\python.exe -m pytest app/tests/backend/test_mongodb_api.py app/tests/backend/test_mongodb_integration.py -q
```

Expected: all collected tests pass; MongoDB-dependent tests may be skipped only under their existing fixture conditions.

- [ ] **Step 4: Build the production frontend**

Run:

```powershell
npm run build
```

Expected: type checking and Vite production build both exit with code 0.

- [ ] **Step 5: Inspect the export dialog at desktop and mobile widths**

Start the Vite server on an available local port, open the account export dialog,
and confirm all six format choices are visible, the requested strings wrap inside
their radio controls, and the desktop two-column grid collapses to one column below
520px. Confirm selecting either new option sends its exact format identifier to
`POST /api/accounts/export`.
