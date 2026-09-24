from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from backend.main import create_app
from backend.mongo_manager import MongoManager
from backend.resource_service import (
    MongoResourceStore,
    ResourceService,
    infer_proxy_country,
    interleave_email_parent_groups,
    proxy_country_filter,
)
from backend.resource_models import (
    AccountExportInput,
    AccountPlanCheckItem,
    AccountPlanCheckResult,
    AccountRecord,
    ProxyGroupUpdate,
)


def test_unclassified_proxy_group_can_be_selected_for_reclassification() -> None:
    payload = ProxyGroupUpdate(country="zz", group="默认组", newCountry="jp")
    assert payload.country == "ZZ"
    assert payload.newCountry == "JP"


def test_email_candidates_are_interleaved_across_parent_mailboxes() -> None:
    candidates = [
        {"_id": "a1", "parentEmail": "a@example.com"},
        {"_id": "a2", "parentEmail": "a@example.com"},
        {"_id": "b1", "parentEmail": "b@example.com"},
        {"_id": "b2", "parentEmail": "b@example.com"},
        {"_id": "manual", "emailNormalized": "manual@example.net"},
        {"_id": "a3", "parentEmail": "a@example.com"},
    ]

    ordered = interleave_email_parent_groups(candidates)

    assert [item["_id"] for item in ordered] == [
        "a1",
        "b1",
        "manual",
        "a2",
        "b2",
        "a3",
    ]


def test_resource_records_expose_browser_ready_api798_urls(monkeypatch) -> None:
    monkeypatch.setenv("API798_AUTH_CODE", "AUTH_FIXTURE")
    created_at = datetime.now(timezone.utc)
    account = MongoResourceStore._account_record(
        {
            "_id": "account-fixture",
            "email": "person@example.com",
            "chatgptPassword": "PASSWORD_FIXTURE",
            "totpSecret": "TOTP_FIXTURE",
            "emailAccessUrl": (
                "https://api798.com/get_code?email=person%40example.com"
            ),
            "createdAt": created_at,
            "accountType": "free",
        }
    )
    email = MongoResourceStore._email_record(
        {
            "_id": "email-fixture",
            "email": "person@example.com",
            "accessUrl": "https://api798.com/get_code?email=person%40example.com",
            "importedAt": created_at,
        }
    )

    expected = (
        "https://api798.com/latest?email=person%40example.com"
        "&auth_code=AUTH_FIXTURE"
    )
    assert account.emailAccessUrl == expected
    assert email.accessUrl == expected


class ImportStore:
    def __init__(self) -> None:
        self.emails: set[str] = set()
        self.email_options: list[tuple[str, str | None]] = []
        self.email_sources: list[tuple[str, str | None]] = []
        self.proxies: set[tuple[str, int, str, str]] = set()
        self.proxy_schemes: list[str] = []
        self.proxy_groups: list[str | None] = []

    async def upsert_email(
        self,
        email: str,
        access_url: str,
        *,
        mailbox_kind: str = "url",
        mailbox_password: str | None = None,
        source_type: str = "manual",
        parent_email: str | None = None,
    ) -> bool:
        self.email_options.append((mailbox_kind, mailbox_password))
        self.email_sources.append((source_type, parent_email))
        key = f"{email}|{access_url}"
        if key in self.emails:
            return False
        self.emails.add(key)
        return True

    async def upsert_proxy(
        self,
        host: str,
        port: int,
        username: str,
        password: str,
        scheme: str = "http",
        country: str | None = None,
        group: str | None = None,
    ) -> bool:
        _ = country, group
        self.proxy_schemes.append(scheme)
        self.proxy_groups.append(group)
        key = (host, port, username, password)
        if key in self.proxies:
            return False
        self.proxies.add(key)
        return True


def test_resource_routes_fail_fast_while_mongodb_is_offline(tmp_path: Path) -> None:
    manager = MongoManager(uri="mongodb://127.0.0.1:1", database_name="offline_test")
    client = TestClient(
        create_app(
            settings_path=tmp_path / "settings.json",
            log_dir=tmp_path / "logs",
            mongo_manager=manager,
        )
    )

    for method, path, payload in [
        ("get", "/api/accounts", None),
        ("post", "/api/emails/import", {"rawText": ""}),
        ("post", "/api/proxies/import", {"rawText": ""}),
        ("post", "/api/proxies/bulk-delete", {"ids": ["proxy-id"]}),
        ("post", "/api/accounts/check-promotion", {"ids": ["account-id"]}),
        ("delete", "/api/proxies", None),
        ("get", "/api/stats/overview", None),
    ]:
        response = getattr(client, method)(path, json=payload) if payload is not None else getattr(client, method)(path)
        assert response.status_code == 503
        assert response.json() == {
            "detail": {
                "code": "mongodb_unavailable",
                "message": "MongoDB 当前不可用，请检查本机服务",
            }
        }

    assert client.get("/api/health").status_code == 200
    assert client.get("/api/settings/execution").status_code == 200
    assert client.get("/api/run-logs/runs").status_code == 200
    assert client.get("/api/docs").status_code == 200


def test_protocol_registration_route_forwards_normalized_resource_filters(
    tmp_path: Path,
) -> None:
    manager = MongoManager(uri="mongodb://127.0.0.1:1", database_name="offline_test")
    app = create_app(
        settings_path=tmp_path / "settings.json",
        log_dir=tmp_path / "logs",
        mongo_manager=manager,
    )
    captured: dict[str, object] = {}
    now = datetime.now(timezone.utc)

    async def start_protocol_registration(
        count: int, country: str, group: str, email_source: str
    ) -> dict[str, object]:
        captured.update(
            count=count,
            country=country,
            group=group,
            email_source=email_source,
        )
        return {
            "runId": "11111111-1111-4111-8111-111111111111",
            "kind": "protocol_registration",
            "status": "running",
            "requested": count,
            "pending": count,
            "processed": 0,
            "succeeded": 0,
            "failed": 0,
            "workerCount": count,
            "activeWorkers": 0,
            "startedAt": now,
            "updatedAt": now,
            "finishedAt": None,
            "registrationCountry": country,
            "registrationProxyGroup": group,
            "emailSource": email_source,
        }

    app.state.run_manager.start_protocol_registration = start_protocol_registration
    response = TestClient(app).post(
        "/api/runs/protocol-registration",
        json={
            "count": 2,
            "country": "tr",
            "group": "  Turkey   A  ",
            "emailSource": "standard",
        },
    )

    assert response.status_code == 202
    assert response.json()["kind"] == "protocol_registration"
    assert captured == {
        "count": 2,
        "country": "TR",
        "group": "Turkey A",
        "email_source": "standard",
    }


class FakeOnlineMongo(MongoManager):
    def require_online(self) -> None:
        return None


class FakePlanCheckService:
    def __init__(self) -> None:
        self.ids: list[str] = []
        self.proxy_id: str | None = None

    async def check_accounts(
        self, ids: list[str], *, proxy_id: str | None = None
    ) -> AccountPlanCheckResult:
        self.ids = ids
        self.proxy_id = proxy_id
        return AccountPlanCheckResult(
            requested=len(ids),
            succeeded=1,
            failed=0,
            skipped=max(0, len(ids) - 1),
            items=[
                AccountPlanCheckItem(id=ids[0], status="success"),
                *[
                    AccountPlanCheckItem(
                        id=value,
                        status="skipped",
                        errorCode="account_missing_token_or_busy",
                    )
                    for value in ids[1:]
                ],
            ],
        )




class _OutlookFakeCursor:
    def __init__(self, rows):
        self.rows = list(rows)
    def sort(self, key, direction=1):
        self.rows.sort(key=lambda row: str(row.get(key) or ""), reverse=direction < 0)
        return self
    def skip(self, count):
        self.rows = self.rows[count:]
        return self
    def limit(self, count):
        self.rows = self.rows[:count]
        return self
    async def to_list(self, length=None):
        import copy
        rows = self.rows if length is None else self.rows[:length]
        return copy.deepcopy(rows)


class _OutlookFakeCollection:
    def __init__(self, rows=()):
        import copy
        self.rows = {str(row["_id"]): copy.deepcopy(row) for row in rows}
    def _match(self, row, query):
        for key, expected in query.items():
            if key in ("$or", "$and"):
                checks = [self._match(row, item) for item in expected]
                if (key == "$or" and not any(checks)) or (key == "$and" and not all(checks)): return False
            elif isinstance(expected, dict):
                actual = row.get(key)
                if "$in" in expected and actual not in expected["$in"]: return False
                if "$nin" in expected and actual in expected["$nin"]: return False
                if "$exists" in expected and (key in row) != bool(expected["$exists"]): return False
                if "$regex" in expected:
                    import re
                    if re.search(expected["$regex"], str(actual or ""), re.I if expected.get("$options") == "i" else 0) is None: return False
            elif row.get(key) != expected:
                return False
        return True
    @staticmethod
    def _project(row, projection):
        import copy
        if not projection: return copy.deepcopy(row)
        if any(value for value in projection.values()):
            result = {"_id": row.get("_id")}
            result.update({key: copy.deepcopy(row[key]) for key, value in projection.items() if value and key in row})
            return result
        result = copy.deepcopy(row)
        for key, value in projection.items():
            if not value: result.pop(key, None)
        return result
    async def create_index(self, *_args, **_kwargs): return "fake-index"
    async def count_documents(self, query): return sum(self._match(row, query) for row in self.rows.values())
    def find(self, query, projection=None): return _OutlookFakeCursor([self._project(row, projection) for row in self.rows.values() if self._match(row, query)])
    async def find_one(self, query, projection=None):
        for row in self.rows.values():
            if self._match(row, query): return self._project(row, projection)
        return None
    async def distinct(self, field, query): return list(dict.fromkeys(row.get(field) for row in self.rows.values() if self._match(row, query) and row.get(field) is not None))
    async def update_one(self, query, update, upsert=False):
        from types import SimpleNamespace
        for key, row in self.rows.items():
            if self._match(row, query):
                values = update.get("$set", {})
                modified = any(row.get(field) != value for field, value in values.items())
                row.update(values)
                for field in update.get("$unset", {}): row.pop(field, None)
                return SimpleNamespace(matched_count=1, modified_count=int(modified), upserted_id=None)
        if upsert:
            row = dict(query)
            row.update(update.get("$setOnInsert", {}))
            row.update(update.get("$set", {}))
            row.setdefault("_id", row.get("emailNormalized", "generated"))
            self.rows[str(row["_id"])] = row
            return SimpleNamespace(matched_count=0, modified_count=0, upserted_id=row["_id"])
        return SimpleNamespace(matched_count=0, modified_count=0, upserted_id=None)
    async def update_many(self, query, update):
        from types import SimpleNamespace
        changed = 0
        for row in self.rows.values():
            if self._match(row, query):
                before = dict(row); row.update(update.get("$set", {}))
                for field in update.get("$unset", {}): row.pop(field, None)
                changed += int(row != before)
        return SimpleNamespace(matched_count=changed, modified_count=changed)
    async def insert_one(self, document):
        from types import SimpleNamespace
        import copy
        self.rows[str(document["_id"])] = copy.deepcopy(document)
        return SimpleNamespace(inserted_id=document["_id"])
    async def delete_one(self, query):
        from types import SimpleNamespace
        for key, row in list(self.rows.items()):
            if self._match(row, query): self.rows.pop(key); return SimpleNamespace(deleted_count=1)
        return SimpleNamespace(deleted_count=0)




async def run_migration_twice(store, migration):
    first = await migration(store)
    second = await migration(store)
    return first, second


def test_outlook_legacy_migration_is_idempotent_and_preserves_source_files(tmp_path: Path, monkeypatch) -> None:
    import asyncio
    import hashlib
    import json
    import stat
    from backend import outlook_service
    from backend.outlook_service import OutlookStore, migrate_legacy_outlook_data
    from backend.resource_service import MongoResourceStore

    results = tmp_path / "Results"
    results.mkdir()
    sources = {
        "pool.json": json.dumps({"accounts": [{"email": "A@Outlook.Test", "password": "pw1", "client_id": "client1", "refresh_token": "refresh1"}]}),
        "oauth2.txt": "a@outlook.test----pw2----client2----refresh2\nB@outlook.test----pw3----client3----refresh3\n",
        "registered.txt": "b@outlook.test----pw4\nC@outlook.test----pw5\n",
    }
    raw = {}
    for name, content in sources.items():
        path = results / name
        path.write_text(content, encoding="utf-8")
        raw[name] = path.read_bytes()
    monkeypatch.setattr(outlook_service, "RESULTS_ROOT", results)
    manager = _OutlookFakeManager()
    manager._test_database["outlook_accounts"] = _OutlookFakeCollection()
    store = OutlookStore(MongoResourceStore(manager))

    first, second = asyncio.run(run_migration_twice(store, migrate_legacy_outlook_data))

    assert first["parsedAccounts"] == 3
    assert first["imported"] == 3
    assert second["imported"] == 0 and second["duplicates"] == 3
    assert len(manager.database["outlook_accounts"].rows) == 3
    for name, before in raw.items():
        path = results / name
        assert path.read_bytes() == before
        digest = hashlib.sha256(before).hexdigest()[:16]
        backup = results / f"{name}.pre-mongo.{digest}.bak"
        assert backup.read_bytes() == before
        assert not (backup.stat().st_mode & stat.S_IWUSR)
    assert not any(path.suffix == ".tmp" for path in results.iterdir())



class _OutlookFakeDatabase(dict):
    def __getitem__(self, name):
        if name not in self: self[name] = _OutlookFakeCollection()
        return super().__getitem__(name)


class _OutlookFakeManager(FakeOnlineMongo):
    def __init__(self):
        super().__init__(uri="mongodb://127.0.0.1:1", database_name="outlook-test")
        self._test_database = _OutlookFakeDatabase({
            "accounts": _OutlookFakeCollection(), "emails": _OutlookFakeCollection(),
            "proxies": _OutlookFakeCollection([
                {"_id": "proxy1", "host": "proxy.example.test", "port": 8080, "username": "SECRET_USER", "password": "SECRET_PROXY", "enabled": True, "status": "available", "country": "US", "group": "shared", "scheme": "http", "createdAt": "2026-01-01"}
            ]), "outlook_accounts": _OutlookFakeCollection([
                {"_id": "outlook1", "email": "me@outlook.test", "emailNormalized": "me@outlook.test", "password": "SECRET_PASSWORD", "clientId": "SECRET_CLIENT", "refreshToken": "SECRET_REFRESH", "oauthStatus": "unknown", "graphStatus": "unknown", "source": "manual", "createdAt": "2026-01-01"}
            ]), "email_change_runs": _OutlookFakeCollection(), "runs": _OutlookFakeCollection(),
            "outlook_migrations": _OutlookFakeCollection(),
        })
    @property
    def database(self): return self._test_database
    async def start(self): self.online = True
    async def stop(self): self.online = False



def test_promotion_check_api_uses_account_id_batch(tmp_path: Path) -> None:
    manager = FakeOnlineMongo(
        uri="mongodb://127.0.0.1:1", database_name="plan_api_test"
    )
    app = create_app(
        settings_path=tmp_path / "settings.json",
        log_dir=tmp_path / "logs",
        mongo_manager=manager,
    )
    service = FakePlanCheckService()
    app.state.plan_check_service = service
    client = TestClient(app)

    response = client.post(
        "/api/accounts/check-promotion",
        json={"ids": ["one", "two"], "proxyId": "proxy-fixed"},
    )

    assert response.status_code == 200
    assert service.ids == ["one", "two"]
    assert service.proxy_id == "proxy-fixed"
    assert response.json()["succeeded"] == 1
    assert response.json()["skipped"] == 1


def test_server_side_import_validation_and_proxy_password_colons() -> None:
    store = ImportStore()
    service = ResourceService(store)  # type: ignore[arg-type]

    email_result = asyncio.run(
        service.import_emails(
            "\ufeffA@Example.com----https://example.com/s/secret-token/a@example.com\n"
            "a@example.com----https://example.com/duplicate\n"
            "bad-email----https://example.com/a\n"
            "b@example.com----file:///private/token\n"
        )
    )
    assert email_result.model_dump() == {
        "total": 4,
        "imported": 1,
        "duplicateCount": 1,
        "errorCount": 2,
    }

    proxy_result = asyncio.run(
        service.import_proxies(
            "proxy.example.test:10000:test-user:TEST_PASSWORD:tail\n"
            "proxy.example.test:10000:test-user:TEST_PASSWORD:tail\n"
            "host:70000:user:password\n"
        )
    )
    assert proxy_result.model_dump() == {
        "total": 3,
        "imported": 1,
        "duplicateCount": 1,
        "errorCount": 1,
    }
    assert store.proxies == {
        (
            "proxy.example.test",
            10000,
            "test-user",
            "TEST_PASSWORD:tail",
        )
    }


def test_server_side_import_accepts_only_valid_icsms_fragment_urls() -> None:
    store = ImportStore()
    service = ResourceService(store)  # type: ignore[arg-type]
    pickup_url = (
        "https://icsms.top/pickup#"
        "email=person%40example.com&key=tok_TEST_fixture_123"
    )

    result = asyncio.run(
        service.import_emails(
            f"person@example.com----{pickup_url}\n"
            "generic@example.com----https://mail.example/inbox#token=secret\n"
            "other@example.com----https://icsms.top/pickup#"
            "email=mismatch%40example.com&key=tok_TEST_fixture_456\n"
            "broken@example.com----https://icsms.top/pickup#"
            "email=broken%40example.com&key=invalid-token\n"
        )
    )

    assert result.model_dump() == {
        "total": 4,
        "imported": 1,
        "duplicateCount": 0,
        "errorCount": 3,
    }
    assert store.emails == {f"person@example.com|{pickup_url}"}


def test_proxy_import_accepts_yaml_proxy_lists() -> None:
    store = ImportStore()
    service = ResourceService(store)  # type: ignore[arg-type]
    result = asyncio.run(service.import_proxies("""
proxies:
  - name: jp-one
    type: socks5
    server: jp.proxy.test
    port: 1080
    username: yaml-user
    password: yaml-pass
    country: JP
    group: YAML-JP
  - name: us-one
    type: http
    server: us.proxy.test
    port: 8080
    username: yaml-user-2
    password: yaml-pass-2
    country_code: US
"""))

    assert result.model_dump() == {
        "total": 2, "imported": 2, "duplicateCount": 0, "errorCount": 0
    }
    assert store.proxy_schemes == ["socks5", "http"]
    assert store.proxy_groups[0] == "YAML-JP"


def test_server_side_import_accepts_mailcom_password_without_public_exposure() -> None:
    store = ImportStore()
    service = ResourceService(store)  # type: ignore[arg-type]

    result = asyncio.run(
        service.import_emails(
            "person@gardener.com----mail-password\n"
            "broken@example.com----file:///private/mail-password\n"
        )
    )

    assert result.model_dump() == {
        "total": 2,
        "imported": 1,
        "duplicateCount": 0,
        "errorCount": 1,
    }
    assert store.emails == {
        "person@gardener.com|https://www.mail.com/int/"
    }
    assert store.email_options == [("mailcom_imap", "mail-password")]


def test_mailcom_alias_sync_imports_only_alias_items_with_local_urls() -> None:
    store = ImportStore()
    service = ResourceService(store)  # type: ignore[arg-type]
    result = asyncio.run(
        service.sync_mailcom_aliases(
            [
                {
                    "email": "owner@gardener.com",
                    "accountEmail": "owner@gardener.com",
                    "isAlias": False,
                    "accessUrl": (
                        "http://127.0.0.1:3211/api/mail/latest?"
                        "email=owner%40gardener.com"
                    ),
                },
                {
                    "email": "alias.one@example.com",
                    "accountEmail": "owner@gardener.com",
                    "isAlias": True,
                    "accessUrl": (
                        "http://127.0.0.1:3211/api/mail/latest?"
                        "email=alias.one%40example.com"
                    ),
                },
                {
                    "email": "outside@example.com",
                    "accountEmail": "owner@gardener.com",
                    "isAlias": True,
                    "accessUrl": (
                        "https://outside.example/api/mail/latest?"
                        "email=outside%40example.com"
                    ),
                },
            ]
        )
    )

    assert result.model_dump() == {
        "total": 2,
        "imported": 1,
        "duplicateCount": 0,
        "errorCount": 1,
    }
    assert store.emails == {
        "alias.one@example.com|"
        "http://127.0.0.1:3211/api/mail/latest?email=alias.one%40example.com"
    }
    assert store.email_sources == [("mailcom_alias", "owner@gardener.com")]


def test_proxy_country_inference_and_explicit_import_classification() -> None:
    assert infer_proxy_country("sid-region-TR-sid-abc") == "TR"
    assert infer_proxy_country("sid_area-TR_life-5") == "TR"
    assert infer_proxy_country("sid-country-JP-sid-abc") == "JP"
    assert infer_proxy_country("plain-user", "edge-area-DE-host") == "DE"
    assert infer_proxy_country("plain-user", "edge.example") == "ZZ"

    country_filter = proxy_country_filter("tr")
    assert country_filter["$or"][0] == {"country": "TR"}
    legacy = country_filter["$or"][1]["$and"]
    assert {"country": "ZZ"} in legacy[0]["$or"]
    assert {"host": legacy[1]["$or"][1]["host"]} in legacy[1]["$or"]

    store = ImportStore()
    service = ResourceService(store)  # type: ignore[arg-type]
    result = asyncio.run(
        service.import_proxies(
            "proxy.example.test:10000:plain-user:password\n",
            country="tr",
        )
    )
    assert result.imported == 1


def test_proxy_import_accepts_whitespace_separated_socks5_urls() -> None:
    store = ImportStore()
    service = ResourceService(store)  # type: ignore[arg-type]
    result = asyncio.run(
        service.import_proxies(
            "socks5://user-region-GB-sid-one:password@proxy.example.test:3000 "
            "socks5://user-region-GB-sid-two:password@proxy.example.test:3000",
            group="英国住宅公式 A",
        )
    )

    assert result.model_dump() == {
        "total": 2,
        "imported": 2,
        "duplicateCount": 0,
        "errorCount": 0,
    }
    assert len(store.proxies) == 2
    assert store.proxy_schemes == ["socks5", "socks5"]
    assert store.proxy_groups == ["英国住宅公式 A", "英国住宅公式 A"]


def test_direct_proxy_import_accepts_http_socks_and_no_auth_entries() -> None:
    store = ImportStore()
    service = ResourceService(store)  # type: ignore[arg-type]
    result = asyncio.run(
        service.import_proxies(
            "http://open-http.example:8080\n"
            "socks5://open-socks.example:1080\n"
            "socks://alias-socks.example:1081\n"
            "open-bare.example:3128\n"
            "csv.example,9000\n"
            "csv-auth.example,9001,csv-user,csv-pass\n",
            group="direct-input",
        )
    )

    assert result.model_dump() == {
        "total": 6,
        "imported": 6,
        "duplicateCount": 0,
        "errorCount": 0,
    }
    assert store.proxy_schemes == [
        "http",
        "socks5",
        "socks5",
        "http",
        "http",
        "http",
    ]
    assert ("open-http.example", 8080, "", "") in store.proxies
    assert ("open-socks.example", 1080, "", "") in store.proxies
    assert ("csv-auth.example", 9001, "csv-user", "csv-pass") in store.proxies


def test_proxy_import_keeps_quoted_csv_spaces_and_original_line_numbers() -> None:
    store = ImportStore()
    service = ResourceService(store)  # type: ignore[arg-type]
    result = asyncio.run(
        service.import_proxies(
            "\n"
            "# keep this comment in the line map\n"
            "proxy.example,9000,csv-user,\"password with spaces\"\n",
            group="csv",
        )
    )

    assert result.model_dump() == {
        "total": 1,
        "imported": 1,
        "duplicateCount": 0,
        "errorCount": 0,
    }
    assert ("proxy.example", 9000, "csv-user", "password with spaces") in store.proxies


def test_proxy_import_does_not_treat_url_credential_commas_as_csv() -> None:
    store = ImportStore()
    service = ResourceService(store)  # type: ignore[arg-type]
    result = asyncio.run(service.import_proxies("http://csv-user:p,q@proxy.example:9002"))

    assert result.imported == 1
    assert ("proxy.example", 9002, "csv-user", "p,q") in store.proxies


def test_proxy_upsert_keeps_mutable_fields_out_of_set_on_insert() -> None:
    class CapturingCollection:
        def __init__(self) -> None:
            self.update: dict | None = None

        async def find_one(self, _identity):
            return None

        async def update_one(self, _identity, update, *, upsert):
            assert upsert is True
            self.update = update
            return type("UpdateResult", (), {"upserted_id": "fixture-id"})()

    class CapturingManager:
        def __init__(self) -> None:
            self.collection = CapturingCollection()
            self.database = {"proxies": self.collection}

        def require_online(self) -> None:
            return None

    manager = CapturingManager()
    store = MongoResourceStore(manager)  # type: ignore[arg-type]

    inserted = asyncio.run(
        store.upsert_proxy(
            "proxy.example.test",
            3000,
            "user-region-GB",
            "password",
            scheme="socks5",
            country="GB",
            group="英国住宅公式 A",
        )
    )

    assert inserted is True
    assert manager.collection.update is not None
    assert manager.collection.update["$set"] == {
        "scheme": "socks5",
        "country": "GB",
        "group": "英国住宅公式 A",
    }
    assert "scheme" not in manager.collection.update["$setOnInsert"]
    assert "country" not in manager.collection.update["$setOnInsert"]
    assert "group" not in manager.collection.update["$setOnInsert"]


def test_mailcom_alias_upsert_avoids_mongodb_path_conflicts() -> None:
    class CapturingAccounts:
        async def find_one(self, _identity, _projection):
            return None

    class CapturingCollection:
        def __init__(self) -> None:
            self.update: dict | None = None

        async def find_one(self, _identity):
            return None

        async def update_one(self, _identity, update, *, upsert):
            assert upsert is True
            self.update = update
            return type("UpdateResult", (), {"upserted_id": "fixture-id"})()

    class CapturingManager:
        def __init__(self) -> None:
            self.collection = CapturingCollection()
            self.database = {
                "accounts": CapturingAccounts(),
                "emails": self.collection,
            }

        def require_online(self) -> None:
            return None

    manager = CapturingManager()
    store = MongoResourceStore(manager)  # type: ignore[arg-type]

    inserted = asyncio.run(
        store.upsert_email(
            "alias@example.com",
            "http://127.0.0.1:3211/api/mail/latest?email=alias%40example.com",
            source_type="mailcom_alias",
            parent_email="owner@example.com",
        )
    )

    assert inserted is True
    assert manager.collection.update is not None
    mutable = manager.collection.update["$set"]
    inserted_fields = manager.collection.update["$setOnInsert"]
    assert mutable == {
        "accessUrl": (
            "http://127.0.0.1:3211/api/mail/latest?email=alias%40example.com"
        ),
        "sourceType": "mailcom_alias",
        "parentEmail": "owner@example.com",
    }
    assert not set(mutable).intersection(inserted_fields)


class AccessTokenExportStore:
    async def access_tokens_for_export(self, ids):
        _ = ids
        now = datetime.now(timezone.utc)
        return [
            {
                "_id": "valid",
                "accessTokenConfigured": True,
                "accessToken": "VALID_TEST_AT",
                "accessTokenExpiresAt": now + timedelta(hours=1),
            },
            {"_id": "missing", "accessTokenConfigured": False},
            {
                "_id": "expired",
                "accessTokenConfigured": True,
                "accessToken": "EXPIRED_TEST_AT",
                "accessTokenExpiresAt": now - timedelta(seconds=1),
            },
        ]


class CredentialExportStore:
    async def accounts_for_export(self, ids):
        assert ids == ["account-fixture"]
        return [
            AccountRecord(
                id="account-fixture",
                email="person@example.test",
                chatgptPassword="PASSWORD_FIXTURE",
                totpSecret="TOTP_FIXTURE",
                emailAccessUrl="https://mail.example.test/inbox/person",
                createdAt=datetime(2026, 8, 24, tzinfo=timezone.utc),
                accountType="free",
            )
        ]


@pytest.mark.parametrize(
    ("export_format", "expected", "suffix"),
    [
        (
            "credentials-mail-links-totp",
            "person@example.test----PASSWORD_FIXTURE----"
            "https://mail.example.test/inbox/person----TOTP_FIXTURE",
            "credentials-mail-links-totp",
        ),
        (
            "credentials-mail-links",
            "person@example.test----PASSWORD_FIXTURE----"
            "https://mail.example.test/inbox/person",
            "credentials-mail-links",
        ),
    ],
)
def test_combined_credential_exports_preserve_requested_field_order(
    export_format: str,
    expected: str,
    suffix: str,
) -> None:
    service = ResourceService(CredentialExportStore())  # type: ignore[arg-type]

    result = asyncio.run(
        service.export_accounts(
            AccountExportInput(
                format=export_format,
                scope="selected",
                ids=["account-fixture"],
            )
        )
    )

    assert result.content == expected
    assert result.format == export_format
    assert result.filename.startswith(f"accounts-1-{suffix}-")


def test_combined_totp_export_preserves_empty_field_positions() -> None:
    class EmptyCredentialExportStore:
        async def accounts_for_export(self, ids):
            _ = ids
            return [
                AccountRecord(
                    id="empty-fixture",
                    email="empty@example.test",
                    chatgptPassword="",
                    totpSecret="",
                    emailAccessUrl="https://mail.example.test/inbox/empty",
                    createdAt=datetime(2026, 8, 24, tzinfo=timezone.utc),
                    accountType="free",
                )
            ]

    result = asyncio.run(
        ResourceService(EmptyCredentialExportStore()).export_accounts(  # type: ignore[arg-type]
            AccountExportInput(
                format="credentials-mail-links-totp",
                scope="all",
                ids=[],
            )
        )
    )

    assert result.content.split("----") == [
        "empty@example.test",
        "",
        "https://mail.example.test/inbox/empty",
        "",
    ]


def test_access_token_export_contains_only_valid_tokens_and_reports_skips() -> None:
    service = ResourceService(AccessTokenExportStore())  # type: ignore[arg-type]
    result = asyncio.run(
        service.export_accounts(
            AccountExportInput(
                format="access-tokens",
                scope="selected",
                ids=["valid", "missing", "expired"],
            )
        )
    )

    assert result.content == "VALID_TEST_AT"
    assert result.count == 1
    assert result.skippedMissingCount == 1
    assert result.skippedExpiredCount == 1
    assert "EXPIRED_TEST_AT" not in result.content


def test_outlook_graph_and_mailbox_apis_use_mock_transport_and_gate_publication(tmp_path: Path, monkeypatch) -> None:
    import httpx

    manager = _OutlookFakeManager()
    manager._test_database["outlook_accounts"] = _OutlookFakeCollection([
        {"_id": "ok", "email": "me@outlook.test", "emailNormalized": "me@outlook.test", "password": "LOCAL_SECRET", "clientId": "CLIENT_SECRET", "refreshToken": "REFRESH_SECRET", "oauthStatus": "unknown", "graphStatus": "unknown", "source": "manual", "createdAt": "2026-01-01"},
        {"_id": "bad", "email": "other@outlook.test", "emailNormalized": "other@outlook.test", "clientId": "BAD_CLIENT_SECRET", "refreshToken": "BAD_REFRESH_SECRET", "oauthStatus": "unknown", "graphStatus": "unknown", "source": "manual", "createdAt": "2026-01-02"},
    ])
    monkeypatch.setattr("backend.outlook_service.RESULTS_ROOT", tmp_path / "missing-results")
    app = create_app(settings_path=tmp_path / "settings.json", log_dir=tmp_path / "logs", mongo_manager=manager)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "login.microsoftonline.com" or request.url.path.endswith("/token"):
            assert request.content
            bad_account = b"BAD_CLIENT_SECRET" in request.content
            assert b"refresh_token=" in request.content
            token = "ACCESS_BAD_SECRET" if bad_account else "ACCESS_SECRET"
            refresh = "ROTATED_BAD_SECRET" if bad_account else "ROTATED_SECRET"
            return httpx.Response(200, json={"access_token": token, "refresh_token": refresh})
        bad_account = request.headers.get("Authorization") == "Bearer ACCESS_BAD_SECRET"
        assert request.headers.get("Authorization") in {"Bearer ACCESS_SECRET", "Bearer ACCESS_BAD_SECRET"}
        if request.url.path.endswith("/me"):
            email = "wrong@outlook.test" if bad_account else "me@outlook.test"
            return httpx.Response(200, json={"id": "graph-user", "mail": email, "userPrincipalName": email})
        if request.url.path.endswith("/messages/msg-1"):
            return httpx.Response(200, json={"id": "msg-1", "subject": "Login code", "from": {"emailAddress": {"address": "sender@example.test", "name": "Sender"}}, "receivedDateTime": "2026-01-01T00:00:00Z", "bodyPreview": "code 123456", "body": {"contentType": "text", "content": "code 123456"}})
        if "/mailFolders/" in request.url.path:
            return httpx.Response(200, json={"value": [{"id": "msg-1", "subject": "Login code", "from": {"emailAddress": {"address": "sender@example.test", "name": "Sender"}}, "receivedDateTime": "2026-01-01T00:00:00Z", "isRead": False, "bodyPreview": "code 123456", "hasAttachments": False, "toRecipients": [{"emailAddress": {"address": "me@outlook.test"}}], "body": {"contentType": "text", "content": "code 123456"}}]})
        return httpx.Response(404)

    real_client = httpx.AsyncClient
    def factory(**kwargs):
        return real_client(transport=httpx.MockTransport(handler), **kwargs)
    app.state.outlook_service.http_client_factory = factory
    client = TestClient(app)

    oauth = client.post("/api/outlook/accounts/ok/check-oauth")
    assert oauth.status_code == 200 and oauth.json()["ok"] is True
    assert manager.database["emails"].rows == {}

    graph = client.post("/api/outlook/accounts/ok/check-graph")
    assert graph.status_code == 200 and graph.json()["poolStatus"] == "available", graph.json()
    mailbox = next(iter(manager.database["emails"].rows.values()))
    assert mailbox["sourceType"] == "outlook" and mailbox["outlookAccountId"] == "ok"
    oauth_again = client.post("/api/outlook/accounts/ok/check-oauth")
    assert oauth_again.status_code == 200
    assert oauth_again.json()["graphStatus"] == "ok"
    assert mailbox["status"] == "available"
    messages = client.get("/api/outlook/accounts/ok/messages")
    assert messages.status_code == 200 and messages.json()["messages"][0]["id"] == "msg-1"
    detail = client.get("/api/outlook/accounts/ok/messages/msg-1")
    assert detail.status_code == 200 and "123456" in detail.json()["message"]["body"]
    mailbox["status"] = "assigned"
    assigned_messages = client.get("/api/outlook/accounts/ok/messages")
    assert assigned_messages.status_code == 200
    pool = client.get("/api/emails?source=outlook")
    assert pool.status_code == 200 and pool.json()["items"][0]["assignmentStatus"] == "assigned"
    assert pool.json()["items"][0]["outlookAccountId"] == "ok"
    assert all(secret not in pool.text for secret in ("LOCAL_SECRET", "CLIENT_SECRET", "REFRESH_SECRET"))
    public = client.get("/api/outlook/accounts")
    assert all(secret not in public.text for secret in ("LOCAL_SECRET", "CLIENT_SECRET", "REFRESH_SECRET", "ROTATED_SECRET", "ROTATED_BAD_SECRET", "ACCESS_SECRET", "ACCESS_BAD_SECRET"))

    mismatch = client.post("/api/outlook/accounts/bad/check-graph")
    assert mismatch.status_code == 200 and mismatch.json()["graphStatus"] == "error"
    assert "bad" not in manager.database["emails"].rows


def test_outlook_mailbox_client_reads_graph_mail_for_otp_polling() -> None:
    import asyncio
    import httpx
    from backend.outlook_service import MongoOutlookMailboxClient

    manager = _OutlookFakeManager()
    manager.database["outlook_accounts"].rows["outlook1"].update({
        "oauthStatus": "ok",
        "graphStatus": "ok",
    })
    requests = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.host == "login.microsoftonline.com":
            return httpx.Response(200, json={"access_token": "ACCESS", "refresh_token": "ROTATED"})
        return httpx.Response(200, json={"value": [{
            "id": "otp-1",
            "subject": "OpenAI verification code",
            "receivedDateTime": "2026-09-24T00:00:00Z",
            "bodyPreview": "Your verification code is 654321",
            "body": {"contentType": "text", "content": "Your verification code is 654321"},
            "toRecipients": [{"emailAddress": {"address": "me@outlook.test"}}],
        }]})

    transport = httpx.MockTransport(handler)
    client = MongoOutlookMailboxClient(manager)
    client.outlook_service.http_client_factory = lambda **kwargs: httpx.AsyncClient(
        transport=transport, **kwargs
    )
    snapshot = asyncio.run(client.get_snapshot(
        "outlook://outlook1", "me@outlook.test", purpose="verification"
    ))

    assert snapshot.verification_code == "654321"
    assert snapshot.received_at_utc is not None
    assert any(request.url.path.endswith("/mailFolders/inbox/messages") for request in requests)
    assert manager.database["outlook_accounts"].rows["outlook1"]["refreshToken"] == "ROTATED"


def test_outlook_publication_does_not_take_over_an_existing_manual_mailbox() -> None:
    import asyncio
    from backend.outlook_service import OutlookStore
    from backend.resource_service import MongoResourceStore

    manager = _OutlookFakeManager()
    manual = {
        "_id": "manual-mailbox",
        "email": "me@outlook.test",
        "emailNormalized": "me@outlook.test",
        "accessUrl": "https://mail.example.test/inbox/me",
        "status": "available",
        "sourceType": "manual",
        "mailboxKind": "url",
    }
    manager.database["emails"].rows[manual["_id"]] = dict(manual)
    account = manager.database["outlook_accounts"].rows["outlook1"]
    account.update({"oauthStatus": "ok", "graphStatus": "ok"})
    store = OutlookStore(MongoResourceStore(manager))

    result = asyncio.run(store.publish_if_valid("outlook1"))

    assert result == "conflict"
    assert manager.database["emails"].rows["manual-mailbox"] == manual
    assert account["poolStatus"] == "conflict"


def test_outlook_list_and_proxy_api_hide_private_credentials(tmp_path: Path) -> None:
    manager = _OutlookFakeManager()
    app = create_app(settings_path=tmp_path / "settings.json", log_dir=tmp_path / "logs", mongo_manager=manager)
    client = TestClient(app)
    listed = client.get("/api/outlook/accounts")
    assert listed.status_code == 200
    encoded = listed.text
    assert "hasClientId" in encoded and "hasRefreshToken" in encoded
    assert all(secret not in encoded for secret in ("SECRET_PASSWORD", "SECRET_CLIENT", "SECRET_REFRESH"))
    detail = client.get("/api/outlook/accounts/outlook1")
    assert detail.status_code == 200
    assert all(secret not in detail.text for secret in ("SECRET_PASSWORD", "SECRET_CLIENT", "SECRET_REFRESH"))
    missing = client.get("/api/outlook/accounts/missing")
    assert missing.status_code == 404
    proxies = client.get("/api/outlook/proxies")
    assert proxies.status_code == 200
    assert "shared" in proxies.text and "proxy.example.test" in proxies.text
    assert "SECRET_USER" not in proxies.text and "SECRET_PROXY" not in proxies.text
    groups = client.get("/api/outlook/proxy-groups")
    assert groups.status_code == 200
    assert "shared" in groups.text
    results = client.get("/api/outlook/results")
    assert results.status_code == 200
    assert all(secret not in results.text for secret in ("SECRET_PASSWORD", "SECRET_CLIENT", "SECRET_REFRESH"))




def test_outlook_import_is_idempotent_and_export_is_local_only(tmp_path: Path) -> None:
    manager = _OutlookFakeManager()
    app = create_app(settings_path=tmp_path / "settings.json", log_dir=tmp_path / "logs", mongo_manager=manager)
    client = TestClient(app, client=("127.0.0.1", 50000))
    payload = {
        "accounts": [
            {"email": "New@Outlook.Test", "password": "IMPORT_PASSWORD", "clientId": "IMPORT_CLIENT", "refreshToken": "IMPORT_REFRESH"},
            {"email": "new@outlook.test", "password": "DUPLICATE_PASSWORD", "clientId": "OTHER_CLIENT", "refreshToken": "OTHER_REFRESH"},
        ]
    }
    imported = client.post("/api/outlook/import", json=payload)
    assert imported.status_code == 200
    assert imported.json() == {"total": 2, "imported": 1, "duplicates": 1, "errors": 0}
    repeated = client.post("/api/outlook/import", json=payload)
    assert repeated.status_code == 200
    assert repeated.json() == {"total": 2, "imported": 0, "duplicates": 2, "errors": 0}
    rows = manager.database["outlook_accounts"].rows
    created = next(row for row in rows.values() if row["email"] == "new@outlook.test")
    assert created["refreshToken"] == "IMPORT_REFRESH"
    assert created["clientId"] == "IMPORT_CLIENT"
    edited = client.patch(f"/api/outlook/accounts/{created['_id']}", json={"clientId": "UPDATED_CLIENT"})
    assert edited.status_code == 200
    assert edited.json()["account"]["oauthStatus"] == "unknown"
    public = client.get("/api/outlook/accounts")
    emails = client.get("/api/emails?source=outlook")
    assert public.status_code == emails.status_code == 200
    assert all(secret not in public.text + emails.text for secret in (
        "IMPORT_PASSWORD", "IMPORT_CLIENT", "IMPORT_REFRESH", "DUPLICATE_PASSWORD", "OTHER_CLIENT", "OTHER_REFRESH"
    ))

    exported = client.post("/api/outlook/export", json={"ids": [created["_id"]]})
    assert exported.status_code == 200
    assert exported.text == "new@outlook.test----IMPORT_PASSWORD----UPDATED_CLIENT----IMPORT_REFRESH\n"
    assert exported.headers["cache-control"] == "no-store"
    remote = TestClient(app, client=("192.0.2.10", 50000)).post("/api/outlook/export", json={"ids": [created["_id"]]})
    assert remote.status_code == 403



def test_successful_protocol_registration_keeps_assigned_outlook_mailbox_link() -> None:
    from datetime import datetime, timedelta, timezone

    manager = _OutlookFakeManager()
    manager._test_database["emails"] = _OutlookFakeCollection([
        {
            "_id": "outlook:assigned-1",
            "email": "assigned@outlook.test",
            "emailNormalized": "assigned@outlook.test",
            "accessUrl": "outlook://outlook-1",
            "mailboxKind": "outlook_graph",
            "sourceType": "outlook",
            "outlookAccountId": "outlook-1",
            "status": "reserved",
            "reservedBy": "run-1",
            "reservationKind": "registration",
        }
    ])
    source = {
        **next(iter(manager.database["emails"].rows.values())),
    }
    account = asyncio.run(
        MongoResourceStore(manager).complete_protocol_registration_success(
            source,
            "run-1",
            access_token="ACCESS_TOKEN_FIXTURE",
            access_token_expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
        )
    )
    mailbox = manager.database["emails"].rows["outlook:assigned-1"]
    persisted_account = manager.database["accounts"].rows[account.id]
    assert mailbox["status"] == "assigned"
    assert mailbox["outlookAccountId"] == "outlook-1"
    assert "reservedBy" not in mailbox
    assert persisted_account["outlookAccountId"] == "outlook-1"
    assert persisted_account["emailAccessUrl"] == "outlook://outlook-1"
