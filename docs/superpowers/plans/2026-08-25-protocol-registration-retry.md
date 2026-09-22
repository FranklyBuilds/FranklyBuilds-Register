# Protocol Registration Retry Plan

## Scope

- Retry transient protocol registration failures for a single reserved mailbox.
- Reacquire a proxy for every retry and release each lease promptly.
- Preserve deterministic failures and partial-success persistence semantics.
- Expose the retry count in worker diagnostics and protocol logs.

## Implementation

1. Add bounded error classification for Sentinel/provider, transport, timeout,
   and proxy failures; keep mailbox, OTP, account-state, and token-validation
   errors non-retryable.
2. Wrap the protocol service invocation in a per-worker loop with at most two
   retries, reacquiring a proxy and rebuilding request/service state per try.
3. Add incremental backoff that is cancellation-aware and record
   `errorRetryCount` on the terminal worker result.
4. Add focused executor tests for transient recovery, lease rotation,
   deterministic no-retry, and terminal retry diagnostics.

## Verification

- Run the protocol run-manager and protocol registration tests.
- Run the complete backend test suite and inspect the final diff.
