# Protocol Payment Static Analysis

Archive: `GPT-Register-Tool-Setup-v2026.08.05`

- Installer SHA-256: `5C83FED290B5EB19749775209F1BD150D96C2B920C00F53277D2C1FC27559590`
- Embedded ZIP entries: `231`
- Extracted payload files: `228`
- Source root: `payload/services/protocol-payment/`
- Analysis mode: read-only source and AST inspection; no installer or payload entry point was executed.

## Protocol Modules

| Module | Source | Region / currency | Result | Registration app status |
| --- | --- | --- | --- | --- |
| BLIK | `blik/blik_qr_extract.py` | PL / PLN | confirmation or QR | integrated, disabled by default |
| Direct Card | `direct_card/direct_card_extract.py` | PH / PHP | Checkout short link | source present, not in current task API |
| iDEAL | `ideal/ideal_qr_extract.py` | NL / EUR | redirect or QR | integrated |
| Kakao Pay | `kakao/kakao_extract.py` | KR / KRW | redirect or QR | integrated |
| MoMo | `momo/ac_paylink_core.py`, `momo/momo_qr_extract.py`, `momo/run_momo.py` | VN / VND | QR or deep link | integrated |
| PIX | `pix/pix_core.py`, `pix/pix_extract.py`, `pix/run_pix.py` | BR / BRL | QR or deep link | integrated |
| TWINT | `twint/twint_extract.py` | CH / CHF | redirect or QR | integrated |

The runtime index records each module's file hashes, Python functions/classes,
imports, literal URL hosts, source size and extracted-file presence.

## Shared And Native Entries

- Shared wallet adapter: `sms_tool/wallet_provider.py`, `wallet_transport.py`,
  and `checkout_contract.py`; covers GoPay (ID/IDR), GCash (PH/PHP), and
  GrabPay (PH/PHP). GoPay and GCash are integrated; GrabPay remains source-only.
- Native manager entries: PayPal and UPI, backed by
  `sms_tool/payment_link_manager.py` and `sms_tool/gen_pp_link.py`; both are
  already integrated in the current registration app.

## MoMo Flow

`VN/VND Checkout -> Stripe init -> zero-amount promotion check -> MoMo payment
method -> confirm -> ChatGPT approve -> payment.momo.vn redirect -> QR/deep-link`

Indexed functions include `probe_token_blob`, `create_momo_payment_method`,
`confirm_momo_payment_page`, `follow_momo_redirect`, and `emit_momo_qr`.

## Runtime Boundary

The generated `MANIFEST.json` and this report are the persisted evidence beside
the archive. The optional local diagnostic endpoint can read the manifest, but
there is deliberately no installation-analysis page in the console. The
analysis does not execute the archived installer, load archived credentials, or
make network requests.
