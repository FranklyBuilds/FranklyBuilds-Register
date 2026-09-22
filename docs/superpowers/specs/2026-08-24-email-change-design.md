# Account Email Change Design

**Status:** Proposed

**Goal:** Add an isolated, replayable email-change workflow for an account already stored by FB-Register without changing the browser-registration or protocol-registration state machines.

## Findings From The Baseline

The source repository is the baseline. At commit
`7fb6ecb12ee4c2087887b3069593c65f110b65e7`, `v2026.08.22` contains a complete
`sms_tool/account_email_change.py` workflow. It calls the account-management endpoints
`/backend-api/accounts/change_email/eligibility`, `/begin`, and `/verify`, then performs a
target-mailbox relogin, an HTTP 200 liveness check, and `storage.migrate_account_email()`.

The older `v2026.08.05` installer extraction is not authoritative and its unused
`bind_email` parameter must not be used to infer current baseline behavior.

## Scope And Isolation

The new feature is a separate account-control workflow. It does not add `email_change` to the existing `RunKind` or `MongoRunStore`: that store has a singleton active-run key, so doing so would block protocol registration, browser registration, and mock runs.

The new workflow owns an `email_change_runs` collection and an independent store. At most one active change is allowed for a given `accountId`; changes for other accounts can be queued independently of registration runs.

The remote operation is an authenticated account-maintenance flow. FB should isolate it from
registration, preserve the source repository's eligibility and post-change liveness checks, and
keep the endpoint contract in a provider adapter so changes do not enter the registration state
machine.

## Data Model

`EmailChangeRun` stores:

```text
runId, kind="email_change", status
accountId
oldEmail, oldEmailNormalized, oldEmailAccessUrl
targetEmailId, targetEmail, targetEmailNormalized, targetEmailAccessUrl
reservationOwner
stage, attempts
errorCode, errorMessage
cancelRequested
createdAt, startedAt, updatedAt, finishedAt
```

The public response redacts mailbox credentials, cookies, Access Tokens, and OTP values. `AccountRecord` is not changed for a pending run; the target email remains a reserved email resource.

## State And Data Flow

```text
queued
  -> target_reserved
  -> remote_submitted
  -> remote_verified
  -> committing
  -> cleanup_pending
  -> completed
```

Failure before the local commit releases the target reservation and leaves the account unchanged. `remote_verified` means the authenticated session reports the target email after the official page flow. Only then is the local account updated.

The local commit is a compare-and-set update on one account document:

```text
filter: _id=accountId, emailNormalized=oldEmailNormalized,
        emailChangeRunId=runId
set:    email, emailNormalized, emailAccessUrl, sourceEmailId, updatedAt
unset:  emailChangeRunId, emailChangeTargetEmailId
```

The target mailbox is consumed after the account CAS succeeds. If consumption fails, the run becomes `cleanup_pending`; recovery retries consumption after checking that the account already contains the target email. It never blindly rolls the account back to the old email.

Target reservation uses a dedicated owner prefix (`email-change:<runId>`). Generic registration reservation recovery must not release or reconcile these records. The email-change store owns startup recovery and cancellation cleanup.

## Remote Adapter Contract

The adapter receives an account snapshot, target email, target mailbox client, and a transport
lease. It must mirror the source workflow:

1. Confirm that the authenticated session email equals `oldEmailNormalized`.
2. Call the eligibility endpoint and reject unsupported account types.
3. Call the begin endpoint with the target email.
4. Poll the target mailbox for the OTP and call the verify endpoint.
5. Relogin using the target mailbox and require a successful liveness check.
6. Return only non-sensitive diagnostics.

The adapter must not log cookies, tokens, OTPs, mailbox URLs, or full response bodies. Unsupported
account types, eligibility limits, provider errors, and failed target relogin are terminal errors
for the run and do not mutate the account record.

## API And UI

Add independent endpoints:

```text
POST /api/email-changes
GET  /api/email-changes/{runId}
POST /api/email-changes/{runId}/cancel
```

The create payload contains `accountId` and `targetEmailId`. The target must be an available, unregistered mailbox. The UI adds a single-account “更换邮箱” action, shows the old and target addresses, and polls the independent run status. Existing registration controls and run polling continue to use their current endpoints.

## Error And Recovery Rules

- Target already registered or reserved: reject before any browser work.
- Old account email changed by another operation: CAS fails; release target and fail.
- Remote submit failed or verification timed out: release target; keep old account data.
- Mongo failure after remote verification: retain the reservation and run as `cleanup_pending`; retry on recovery.
- Process exit: inspect each active run. If the account already equals the target, finish mailbox cleanup; otherwise release the target reservation.
- Cancel before commit releases the target reservation. Cancel after commit only performs cleanup and never reverts the account.

## Verification

Backend tests cover normalization, target reservation ownership, same-account concurrency, CAS success/conflict, cleanup-pending replay, cancellation, and legacy registration isolation. Browser adapter tests use a fake page and mailbox client; they do not contact a third-party service.

The existing protocol-registration and browser-probe suites must remain green. No existing registration endpoint, `RunState` field, or generic registration reservation recovery path is changed.
