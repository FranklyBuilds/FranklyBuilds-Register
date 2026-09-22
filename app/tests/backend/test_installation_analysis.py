from __future__ import annotations

from fastapi.testclient import TestClient

from backend.main import create_app
from backend.installation_analysis import ANALYSIS_REPORT_PATH, get_installation_analysis


def test_archived_installer_manifest_and_momo_inventory_are_available() -> None:
    assert ANALYSIS_REPORT_PATH.is_file()
    result = get_installation_analysis()

    assert result["ok"] is True
    assert result["analysis_report_path"].endswith("PROTOCOL-PAYMENT-ANALYSIS.md")
    assert result["installer"]["sha256"] == "5C83FED290B5EB19749775209F1BD150D96C2B920C00F53277D2C1FC27559590"
    assert result["installer"]["zip_entries"] == 231
    assert result["payload"]["file_count"] == 228
    assert result["momo"]["payment_method"] == "momo"
    assert result["momo"]["country"] == "VN"
    assert result["momo"]["currency"] == "VND"
    assert result["momo"]["functions"] == [
        "probe_token_blob",
        "create_momo_payment_method",
        "confirm_momo_payment_page",
        "follow_momo_redirect",
        "emit_momo_qr",
    ]

    assert [item["key"] for item in result["modules"]] == [
        "blik", "direct_card", "ideal", "kakao_pay", "momo", "pix", "twint",
    ]
    assert result["modules"][1]["status"] == "source_present_not_integrated"
    assert result["modules"][2]["status"] == "already_integrated"
    assert result["modules"][0]["status"] == "integrated_disabled"
    assert [item["key"] for item in result["wallet_adapters"]] == [
        "gopay", "gcash", "grabpay",
    ]
    assert result["wallet_adapters"][2]["status"] == "source_present_not_integrated"
    assert [item["key"] for item in result["native_methods"]] == ["paypal", "upi"]
    assert result["module_counts"] == {
        "protocol": 7,
        "wallet": 3,
        "native": 2,
        "integrated": 9,
        "source_present_not_integrated": 2,
        "integrated_disabled": 1,
    }
    assert "momo" in result["current_registration_app"]["integrated_methods"]
    assert "direct_card" in result["current_registration_app"]["source_only_methods"]


def test_installation_analysis_endpoint_exposes_read_only_inventory(tmp_path) -> None:
    client = TestClient(create_app(tmp_path / "settings.json", tmp_path / "logs"))

    response = client.get("/api/installation-analysis")

    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True
    assert body["analysis_report_path"].endswith("PROTOCOL-PAYMENT-ANALYSIS.md")
    assert body["momo"]["name"] == "MoMo"
    assert {item["key"] for item in body["modules"]} == {
        "blik", "direct_card", "ideal", "kakao_pay", "momo", "pix", "twint",
    }
    assert body["wallet_adapters"][2]["key"] == "grabpay"
    assert body["current_registration_app"]["status"] == "already_integrated"
    assert body["safety"] == {
        "read_only": True,
        "installer_executed": False,
        "credentials_loaded": False,
        "network_requests": False,
    }
