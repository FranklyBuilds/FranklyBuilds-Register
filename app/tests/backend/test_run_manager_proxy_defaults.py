from datetime import datetime, timezone

from backend.probe_store import (
    DEFAULT_PROXY_GROUP,
    LOCAL_PROXY_GROUP,
    MongoProbeStore,
)
from backend.run_manager import resolve_registration_proxy_group


def test_empty_registration_proxy_group_uses_direct_import_pool() -> None:
    assert resolve_registration_proxy_group("") == DEFAULT_PROXY_GROUP
    assert resolve_registration_proxy_group("   ") == DEFAULT_PROXY_GROUP
    assert resolve_registration_proxy_group(None) == DEFAULT_PROXY_GROUP


def test_explicit_proxy_group_and_legacy_local_group_are_preserved() -> None:
    assert resolve_registration_proxy_group("  mobile-a  ") == "mobile-a"
    assert resolve_registration_proxy_group(LOCAL_PROXY_GROUP) == LOCAL_PROXY_GROUP


def test_country_launch_keeps_unknown_direct_imports_eligible() -> None:
    query = MongoProbeStore._available_proxy_filter(
        datetime.now(timezone.utc),
        country="US",
        group=DEFAULT_PROXY_GROUP,
        include_unknown_country=True,
    )
    country_clause = query["$and"][1]
    assert {"country": "ZZ"} in country_clause["$or"]
