# Third-Party Notices

The top-level MIT license applies to original Codex Auto Register code only.
Third-party packages retain their respective copyrights and license terms.

## Baseline Project Reference

- Project: <https://github.com/2951461586/GPT-Register-Tool>
- Purpose: baseline project address supplied for feature and provenance comparison.
- Source of truth: the checked-out source repository and its reviewed commit; the
  installer archives below are historical comparison material only.

## PayPal Agreement Protocol

- Source: <https://github.com/1537271403/paypal-agreement-protocol>
- Integrated revision: `4719066ec6fd56b57a5bd9599758366836c9dc0a`
- Location: `app/backend/paypal_agreement_protocol/`

The upstream revision does not contain a license file. It is therefore not
relicensed by this repository. Obtain permission from the upstream author
before redistributing or modifying that directory outside the rights granted
by the hosting platform.

## OAI Payment Link Extractor

- Location: `app/backend/oai_payment_extractor/`
- Provenance record: `app/backend/oai_payment_extractor/SOURCE.md`

This migrated package is excluded from the top-level MIT grant unless its
copyright holder separately confirms compatible licensing.

## Protocol registration adapter

- Historical installer reference: `artifacts/reverse/GPT-Register-Tool-Setup-v2026.08.05/payload/sms_tool/`
- Adapted package: `app/backend/protocol_registration/`
- QuickJS adapter source: <https://github.com/zc-zhangchen/any-auto-register>
- Upstream revision: `0af4276871a719f6ef8489d9a2f147b347b72cf5`
- Upstream path: `platforms/chatgpt/protocol/openai_sentinel_quickjs.js`

The Python adapter is an independent transport-injected rewrite. The QuickJS
file retains upstream attribution and is treated under its upstream license;
see the package `SOURCE.md` for the exact reference modules and handling
requirements.

## Package dependencies

Python and npm dependencies are installed from their package registries and
retain the license shipped by each dependency. Generated `node_modules`, Python
virtual environments and MongoDB binaries are not part of this repository.
