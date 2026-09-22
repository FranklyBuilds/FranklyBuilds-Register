# FranklyBuilds Register Branding Implementation Plan

> **For agentic workers:** This plan is executed inline in the current workspace. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Record the baseline project URL and change the application-facing product name to `FranklyBuilds-Register (FB注册机)` without breaking existing runtime configuration names.

**Architecture:** Keep internal `AUTOREGISTER_*` environment variables, storage names, and API paths stable for backward compatibility. Update only user-facing HTML, Vue shell branding, npm package metadata, and operator documentation; add a focused UI assertion for the visible product name.

**Tech Stack:** Vue 3, TypeScript, Vitest, Vite, Markdown, npm metadata

---

### Task 1: Lock The Visible Brand Contract

**Files:**
- Modify: `app/src/App.spec.ts`

- [x] **Step 1: Add an assertion for the new product name**

Assert that the mounted application shell exposes `FranklyBuilds-Register` and `FB注册机`.

- [x] **Step 2: Run the focused test and verify it fails**

Run `npm.cmd test -- src/App.spec.ts` from `app/`. Expected: the current shell still renders `AutoRegister`.

### Task 2: Apply The Branding And Baseline Metadata

**Files:**
- Modify: `app/src/App.vue`
- Modify: `app/index.html`
- Modify: `app/package.json`
- Modify: `app/package-lock.json`
- Modify: `README.md`
- Modify: `THIRD_PARTY_NOTICES.md`

- [x] **Step 1: Replace visible product labels**

Use `FranklyBuilds-Register` as the product identifier and `FB注册机` as the Chinese display name in the shell and document title.

- [x] **Step 2: Record the baseline URL**

Add `https://github.com/2951461586/GPT-Register-Tool` to the project provenance section without copying its live account-control workflow.

- [x] **Step 3: Keep internal compatibility identifiers unchanged**

Do not rename `AUTOREGISTER_*` environment variables, MongoDB database defaults, or local storage keys in this branding-only change.

### Task 3: Verify

**Files:**
- Verify only; no additional source changes planned

- [x] **Step 1: Run the focused UI test**

Run `npm.cmd test -- src/App.spec.ts` from `app/`.

- [x] **Step 2: Run type checking and production build**

Run `npm.cmd run type-check` and `npm.cmd run build` from `app/`; both must exit successfully.

Verification evidence:

- `npm.cmd test`: 15 files and 109 tests passed.
- `npm.cmd run type-check`: exit code 0.
- `npm.cmd run build`: exit code 0; Vite generated `app/dist/`.
- `& ..\\register_env\\Scripts\\python.exe -m pytest -q tests\\backend`: 648 passed, 13 skipped, 1 pre-existing failure in `tests/backend/test_chatgpt_plan.py::test_parse_accounts_check_plan_and_trial_rules[account0-True-free-False]`.
- The local dev server was smoke-tested at `http://127.0.0.1:5174/launch`; the document title and visible shell brand were both `FranklyBuilds-Register · FB注册机`.

Scope note: the source baseline is maintained at `https://github.com/2951461586/GPT-Register-Tool`;
the installer extraction under `artifacts/reverse/` is historical only. The source repository's
email-change workflow is tracked separately from this branding plan and was not part of the
branding change.
