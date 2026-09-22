"""Read-only compatibility inventory for the archived registration-tool installer.

The installer is kept outside the application runtime.  This module only reads
the generated reverse-engineering manifest and derives a small, stable summary
for the local console.  It deliberately does not execute the installer or any
payload entry point.
"""

from __future__ import annotations

import json
import ast
import re
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit


ARCHIVE_NAME = "GPT-Register-Tool-Setup-v2026.08.05"
_REPO_ROOT = Path(__file__).resolve().parents[2]
ARCHIVE_ROOT = _REPO_ROOT / "artifacts" / "reverse" / ARCHIVE_NAME
MANIFEST_PATH = ARCHIVE_ROOT / "MANIFEST.json"
ANALYSIS_REPORT_PATH = ARCHIVE_ROOT / "PROTOCOL-PAYMENT-ANALYSIS.md"
_PROTOCOL_ROOT = "services/protocol-payment"

_MOMO_FILES = (
    "services/protocol-payment/momo/ac_paylink_core.py",
    "services/protocol-payment/momo/momo_qr_extract.py",
    "services/protocol-payment/momo/run_momo.py",
)
_MOMO_FUNCTIONS = (
    "probe_token_blob",
    "create_momo_payment_method",
    "confirm_momo_payment_page",
    "follow_momo_redirect",
    "emit_momo_qr",
)

_PROTOCOL_MODULES: tuple[dict[str, Any], ...] = (
    {
        "key": "blik",
        "directory": "blik",
        "name": "BLIK",
        "payment_method": "blik",
        "country": "PL",
        "currency": "PLN",
        "result_kind": "confirmation_or_qr",
        "adapter": "vendored_protocol",
        "status": "integrated_disabled",
        "notes": "当前注册机保留适配器，但因需要一次性 BLIK 验证码而默认关闭自动提交。",
    },
    {
        "key": "direct_card",
        "directory": "direct_card",
        "name": "Direct Card",
        "payment_method": "direct_card",
        "country": "PH",
        "currency": "PHP",
        "result_kind": "checkout_link",
        "adapter": "vendored_protocol",
        "status": "source_present_not_integrated",
        "notes": "独立直卡 Checkout 短链提炼器，使用 US Checkout、TR 优惠更新和 0 元验证。",
    },
    {
        "key": "ideal",
        "directory": "ideal",
        "name": "iDEAL",
        "payment_method": "ideal",
        "country": "NL",
        "currency": "EUR",
        "result_kind": "redirect_or_qr",
        "adapter": "vendored_protocol",
        "status": "already_integrated",
        "notes": "荷兰银行选择和 Stripe iDEAL 确认流程。",
    },
    {
        "key": "kakao_pay",
        "directory": "kakao",
        "name": "Kakao Pay",
        "payment_method": "kakao_pay",
        "country": "KR",
        "currency": "KRW",
        "result_kind": "redirect_or_qr",
        "adapter": "vendored_protocol",
        "status": "already_integrated",
        "notes": "韩国 KakaoPay/NicePay 跳转和预确认流程。",
    },
    {
        "key": "momo",
        "directory": "momo",
        "name": "MoMo",
        "payment_method": "momo",
        "country": "VN",
        "currency": "VND",
        "result_kind": "qr_or_deep_link",
        "adapter": "vendored_protocol",
        "status": "already_integrated",
        "notes": "越南 Checkout → Stripe init → 0 元优惠 → MoMo QR/deep-link。",
    },
    {
        "key": "pix",
        "directory": "pix",
        "name": "PIX",
        "payment_method": "pix",
        "country": "BR",
        "currency": "BRL",
        "result_kind": "qr_or_deep_link",
        "adapter": "vendored_protocol",
        "status": "already_integrated",
        "notes": "巴西 PIX QR/支付链接提炼和重定向解析。",
    },
    {
        "key": "twint",
        "directory": "twint",
        "name": "TWINT",
        "payment_method": "twint",
        "country": "CH",
        "currency": "CHF",
        "result_kind": "redirect_or_qr",
        "adapter": "vendored_protocol",
        "status": "already_integrated",
        "notes": "瑞士 TWINT QR/跳转确认流程。",
    },
)

_WALLET_ADAPTERS: tuple[dict[str, Any], ...] = (
    {
        "key": "gopay",
        "name": "GoPay",
        "payment_method": "gopay",
        "country": "ID",
        "currency": "IDR",
        "result_kind": "redirect",
        "status": "already_integrated",
    },
    {
        "key": "gcash",
        "name": "GCash",
        "payment_method": "gcash",
        "country": "PH",
        "currency": "PHP",
        "result_kind": "redirect",
        "status": "already_integrated",
    },
    {
        "key": "grabpay",
        "name": "GrabPay",
        "payment_method": "grabpay",
        "country": "PH",
        "currency": "PHP",
        "result_kind": "redirect",
        "status": "source_present_not_integrated",
    },
)

_WALLET_SOURCE_PATHS = (
    "sms_tool/wallet_provider.py",
    "sms_tool/wallet_transport.py",
    "sms_tool/checkout_contract.py",
)
_NATIVE_METHODS: tuple[dict[str, Any], ...] = (
    {
        "key": "paypal",
        "name": "PayPal",
        "payment_method": "paypal",
        "country": "MULTI",
        "currency": "MULTI",
        "result_kind": "redirect",
        "status": "already_integrated",
        "source_paths": ("sms_tool/payment_link_manager.py", "sms_tool/gen_pp_link.py"),
    },
    {
        "key": "upi",
        "name": "UPI",
        "payment_method": "upi",
        "country": "IN",
        "currency": "INR",
        "result_kind": "qr_or_deep_link",
        "status": "already_integrated",
        "source_paths": ("sms_tool/payment_link_manager.py", "sms_tool/gen_pp_link.py"),
    },
)


class InstallationAnalysisError(RuntimeError):
    """Raised when the archived analysis evidence is missing or malformed."""


def _read_manifest() -> dict[str, Any]:
    if not MANIFEST_PATH.is_file():
        raise InstallationAnalysisError(
            f"安装包解析清单不存在：{MANIFEST_PATH}"
        )
    try:
        value = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise InstallationAnalysisError("安装包解析清单损坏") from exc
    if not isinstance(value, dict) or not isinstance(value.get("installer"), dict):
        raise InstallationAnalysisError("安装包解析清单格式不受支持")
    return value


def _relative_path(path: Path) -> str:
    try:
        return path.relative_to(_REPO_ROOT).as_posix()
    except ValueError:
        return path.as_posix()


def _manifest_path(value: Any) -> str:
    """Convert an optional manifest path without turning an empty value into ``.``."""

    raw = str(value or "").strip()
    return _relative_path(Path(raw)) if raw else ""


def _file_record(manifest: dict[str, Any], relative: str) -> dict[str, Any] | None:
    for item in manifest.get("entries", []):
        if isinstance(item, dict) and item.get("path") == relative:
            return {
                "path": relative,
                "size": int(item.get("size") or 0),
                "sha256": str(item.get("sha256") or ""),
                "present": (ARCHIVE_ROOT / "payload" / Path(relative)).is_file(),
            }
    return {
        "path": relative,
        "size": 0,
        "sha256": "",
        "present": (ARCHIVE_ROOT / "payload" / Path(relative)).is_file(),
    }


def _manifest_paths(manifest: dict[str, Any], prefix: str) -> list[str]:
    """Return payload files below ``prefix`` in deterministic order."""

    normalized = prefix.strip("/") + "/"
    paths = {
        str(item.get("path"))
        for item in manifest.get("entries", [])
        if isinstance(item, dict)
        and str(item.get("path") or "").startswith(normalized)
        and not str(item.get("path") or "").endswith("/")
    }
    return sorted(paths)


def _source_text(relative: str) -> str:
    path = ARCHIVE_ROOT / "payload" / Path(relative)
    if not path.is_file():
        return ""
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def _python_index(paths: list[str]) -> dict[str, Any]:
    """Extract syntax-only symbols and literal hosts; never imports source code."""

    functions: list[str] = []
    classes: list[str] = []
    imports: set[str] = set()
    hosts: set[str] = set()
    source_chars = 0
    parsed_files = 0
    for relative in paths:
        if not relative.casefold().endswith(".py"):
            continue
        source = _source_text(relative)
        source_chars += len(source)
        try:
            tree = ast.parse(source, filename=relative)
        except SyntaxError:
            continue
        parsed_files += 1
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                functions.append(node.name)
            elif isinstance(node, ast.ClassDef):
                classes.append(node.name)
            elif isinstance(node, ast.Import):
                imports.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imports.add(node.module)
        for match in re.finditer(r"https?://[^\s\"'<>]+", source, flags=re.IGNORECASE):
            try:
                host = urlsplit(match.group(0)).hostname
            except ValueError:
                host = None
            if host:
                hosts.add(host.casefold())
    ordered_functions = sorted(set(functions))
    ordered_classes = sorted(set(classes))
    return {
        "functions": ordered_functions,
        "key_functions": [name for name in ordered_functions if not name.startswith("_")][:20],
        "function_count": len(ordered_functions),
        "classes": ordered_classes,
        "class_count": len(ordered_classes),
        "imports": sorted(imports),
        "hosts": sorted(hosts),
        "source_chars": source_chars,
        "parsed_files": parsed_files,
    }


def _payload_stats(manifest: dict[str, Any]) -> dict[str, Any]:
    entries = [item for item in manifest.get("entries", []) if isinstance(item, dict)]
    files = [item for item in entries if str(item.get("path") or "") and not str(item["path"]).endswith("/")]
    total_size = sum(int(item.get("size") or 0) for item in files)
    extensions: dict[str, int] = {}
    for item in files:
        suffix = Path(str(item.get("path") or "")).suffix.lower() or "[no extension]"
        extensions[suffix] = extensions.get(suffix, 0) + 1
    return {
        "entry_count": len(entries),
        "file_count": len(files),
        "directory_count": len(entries) - len(files),
        "total_size": total_size,
        "extensions": dict(sorted(extensions.items())),
    }


def _protocol_module_summary(manifest: dict[str, Any], metadata: dict[str, Any]) -> dict[str, Any]:
    prefix = f"{_PROTOCOL_ROOT}/{metadata['directory']}"
    paths = _manifest_paths(manifest, prefix)
    files = [_file_record(manifest, relative) for relative in paths]
    files = [item for item in files if item is not None]
    source_index = _python_index(paths)
    return {
        "key": metadata["key"],
        "name": metadata["name"],
        "directory": prefix,
        "payment_method": metadata["payment_method"],
        "country": metadata["country"],
        "currency": metadata["currency"],
        "result_kind": metadata["result_kind"],
        "adapter": metadata["adapter"],
        "status": metadata["status"],
        "notes": metadata["notes"],
        "files": files,
        "file_count": len(files),
        "total_size": sum(int(item["size"]) for item in files),
        **source_index,
        "present_file_count": sum(1 for item in files if item["present"]),
    }


def _momo_summary(manifest: dict[str, Any]) -> dict[str, Any]:
    metadata = next(item for item in _PROTOCOL_MODULES if item["key"] == "momo")
    summary = _protocol_module_summary(manifest, metadata)
    source_text = "\n".join(_source_text(relative) for relative in _MOMO_FILES)
    summary["functions"] = [name for name in _MOMO_FUNCTIONS if f"def {name}(" in source_text]
    summary["key_functions"] = list(summary["functions"])
    summary["flow"] = [
        "ChatGPT checkout (VN/VND)",
        "Stripe init and zero-amount promotion check",
        "MoMo payment method confirmation",
        "ChatGPT approve",
        "payment.momo.vn redirect and QR extraction",
    ]
    summary["network_execution"] = False
    return summary


def _shared_adapter_files(manifest: dict[str, Any], paths: tuple[str, ...]) -> dict[str, Any]:
    files = [_file_record(manifest, relative) for relative in paths]
    files = [item for item in files if item is not None]
    return {
        "files": files,
        "file_count": len(files),
        "total_size": sum(int(item["size"]) for item in files),
        **_python_index(list(paths)),
        "present_file_count": sum(1 for item in files if item["present"]),
    }


def _wallet_summaries(manifest: dict[str, Any]) -> list[dict[str, Any]]:
    common = _shared_adapter_files(manifest, _WALLET_SOURCE_PATHS)
    return [
        {
            **metadata,
            "adapter": "shared_wallet",
            "source_files": common["files"],
            "source_functions": common["key_functions"],
            "source_function_count": common["function_count"],
            "source_hosts": common["hosts"],
        }
        for metadata in _WALLET_ADAPTERS
    ]


def _native_summaries(manifest: dict[str, Any]) -> list[dict[str, Any]]:
    output = []
    for metadata in _NATIVE_METHODS:
        source_paths = tuple(metadata["source_paths"])
        common = _shared_adapter_files(manifest, source_paths)
        output.append({
            **metadata,
            "adapter": "native_manager",
            "source_files": common["files"],
            "source_functions": common["key_functions"],
            "source_function_count": common["function_count"],
            "source_hosts": common["hosts"],
        })
    return output


def get_installation_analysis() -> dict[str, Any]:
    """Return the verified installer archive summary for optional diagnostics.

    The persisted files under ``artifacts/reverse`` are the source of truth;
    the API is retained only for local tooling and is not exposed as a console
    page.
    """

    manifest = _read_manifest()
    installer = dict(manifest["installer"])
    installer["archived_path"] = _manifest_path(manifest.get("archived_installer"))
    installer["payload_zip"] = _manifest_path(manifest.get("payload_zip"))
    modules = [
        _protocol_module_summary(manifest, metadata)
        for metadata in _PROTOCOL_MODULES
    ]
    wallet_adapters = _wallet_summaries(manifest)
    native_methods = _native_summaries(manifest)
    return {
        "ok": True,
        "archive_name": ARCHIVE_NAME,
        "manifest_path": _relative_path(MANIFEST_PATH),
        "analysis_report_path": _relative_path(ANALYSIS_REPORT_PATH),
        "installer": installer,
        "payload": {
            **_payload_stats(manifest),
            "directory": _manifest_path(manifest.get("payload_directory")),
        },
        "momo": _momo_summary(manifest),
        "modules": modules,
        "wallet_adapters": wallet_adapters,
        "native_methods": native_methods,
        "module_counts": {
            "protocol": len(modules),
            "wallet": len(wallet_adapters),
            "native": len(native_methods),
            "integrated": sum(
                item["status"] == "already_integrated"
                for item in [*modules, *wallet_adapters, *native_methods]
            ),
            "source_present_not_integrated": sum(
                item["status"] == "source_present_not_integrated"
                for item in [*modules, *wallet_adapters, *native_methods]
            ),
            "integrated_disabled": sum(
                item["status"] == "integrated_disabled"
                for item in [*modules, *wallet_adapters, *native_methods]
            ),
        },
        "current_registration_app": {
            "payment_method": "momo",
            "country": "VN",
            "currency": "VND",
            "result_field": "momo_url",
            "backend_provider": "app/backend/oai_payment_extractor/providers/local_methods.py",
            "frontend_view": "app/src/views/PaymentToolsView.vue",
            "status": "already_integrated",
            "integrated_methods": [
                item["payment_method"]
                for item in [*modules, *wallet_adapters, *native_methods]
                if item["status"] == "already_integrated"
            ],
            "source_only_methods": [
                item["payment_method"]
                for item in [*modules, *wallet_adapters, *native_methods]
                if item["status"] == "source_present_not_integrated"
            ],
        },
        "safety": {
            "read_only": True,
            "installer_executed": False,
            "credentials_loaded": False,
            "network_requests": False,
        },
    }
