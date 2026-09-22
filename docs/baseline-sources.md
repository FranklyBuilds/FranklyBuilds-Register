# Baseline Sources

## Baseline Register Tool 1

- Source of truth: <https://github.com/2951461586/GPT-Register-Tool>
- Local analysis clone: `E:\code\GPT-Register-Tool`
- Reviewed commit: `7fb6ecb12ee4c2087887b3069593c65f110b65e7`
- Priority: source repository first; release notes and source code determine behavior.
- Installer archives under `gpt/artifacts/reverse/` are historical comparison material only.

### Email-change implementation

The source repository contains the implementation introduced in `v2026.08.22`:

```text
WPF / CLI selection
  -> sms_tool.commands.email_change
  -> sms_tool.account_email_change
  -> /backend-api/accounts/change_email/eligibility
  -> /backend-api/accounts/change_email/begin
  -> target-mailbox OTP
  -> /backend-api/accounts/change_email/verify
  -> relogin with the target mailbox
  -> account liveness check
  -> storage.migrate_account_email
```

Primary files in the source repository:

- `sms_tool/account_email_change.py`
- `sms_tool/commands/email_change.py`
- `sms_tool/storage.py`
- `SmsWorkbench/ChangeEmailDialogService.cs`
- `SmsWorkbench/BackendCommandPlanner.cs`
- `docs/release-v2026.08.22.md`
- `tests/test_account_email_change.py`

The previous conclusion based on the `v2026.08.05` installer extraction is superseded for all future comparisons.
