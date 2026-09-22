# Protocol registration provenance

This package adapts the endpoint sequencing observed in the source baseline
repository at <https://github.com/2951461586/GPT-Register-Tool>.
The checked-out source repository and its reviewed commit are the source of
truth for baseline behavior. The installer extraction at
`artifacts/reverse/GPT-Register-Tool-Setup-v2026.08.05/payload/sms_tool/` is
historical comparison material only and is not used to override the source.
The implementation here is an independent, transport-injected rewrite for
AutoRegister; it does not import the artifact at runtime.  Relevant baseline
modules are `registration.py`, `auth_flow.py`, `auth_headers.py`,
`account_creation.py`, `otp_strategy.py`, and `sentinel_tokens.py`.

The QuickJS Sentinel adapter (`openai_sentinel_quickjs.js`) is adapted from
[`zc-zhangchen/any-auto-register`](https://github.com/zc-zhangchen/any-auto-register),
upstream commit `0af4276871a719f6ef8489d9a2f147b347b72cf5`,
`platforms/chatgpt/protocol/openai_sentinel_quickjs.js`. The upstream project
identifies that component as MIT-licensed in its README; this repository keeps
the adapter attribution and does not fold it into the top-level MIT grant.

Keep this notice with the package when moving or redistributing the protocol
registration implementation.  Do not place bearer tokens, mailbox
credentials, Sentinel values, or cookie material in source control.
