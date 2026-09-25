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
        self.index_calls = []
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
    async def create_index(self, keys, **options):
        self.index_calls.append((keys, options))
        return "fake-index"
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
    async def delete_many(self, query):
        from types import SimpleNamespace
        deleted = 0
        for key, row in list(self.rows.items()):
            if self._match(row, query): self.rows.pop(key); deleted += 1
        return SimpleNamespace(deleted_count=deleted)




async def run_migration_twice(store, migration):
    first = await migration(store)
    second = await migration(store)
    return first, second


def test_mailcom_api_import_is_idempotent_and_redacts_credentials(tmp_path: Path) -> None:
    from backend.mailcom_service import MailComService

    manager = _OutlookFakeManager()
    app = create_app(settings_path=tmp_path / "settings.json", log_dir=tmp_path / "logs", mongo_manager=manager)
    client = TestClient(app)
    app.state.mailcom_service.cipher = _TestCipher()

    payload = {"rawText": "owner@mail.test----clear-secret\nalso@mail.test----other-secret"}
    first = client.post("/api/mailcom/accounts/import", json=payload)
    second = client.post("/api/mailcom/accounts/import", json=payload)
    listed = client.get("/api/mailcom/accounts")

    assert first.status_code == 200
    assert first.json() == {"total": 2, "imported": 2, "duplicateCount": 0, "errorCount": 0}
    assert second.json() == {"total": 2, "imported": 0, "duplicateCount": 2, "errorCount": 0}
    assert "clear-secret" not in listed.text and "passwordEncrypted" not in listed.text
    assert "other-secret" not in listed.text
    account = listed.json()["items"][0]
    alias = client.post(f"/api/mailcom/accounts/{account['id']}/aliases/import", json={"email": "alias@mail.test", "label": "main"})
    assert alias.status_code == 200 and alias.json()["status"] == "inserted"
    synced = client.post("/api/emails/sync-mailcom-aliases")
    assert synced.status_code == 200 and synced.json()["imported"] == 1
    email_rows = manager.database["emails"].rows
    synced_alias = next(row for row in email_rows.values() if row.get("email") == "alias@mail.test")
    assert synced_alias["accessUrl"].startswith("mailcom://alias/")


def test_outlook_and_mailcom_use_implicit_mongodb_id_indexes() -> None:
    from backend.mailcom_service import MailComService
    from backend.outlook_register_task_service import OutlookRegisterTaskService

    manager = _OutlookFakeManager()
    resources = MongoResourceStore(manager)
    outlook_tasks = OutlookRegisterTaskService(resources)
    from backend.outlook_register_task_service import OutlookRegistrationUnifiedAdapter
    assert isinstance(outlook_tasks.adapter, OutlookRegistrationUnifiedAdapter)
    mailcom = MailComService(resources, cipher=_TestCipher(), sqlite_path=Path("missing.db"))

    async def scenario() -> None:
        await outlook_tasks.ensure_indexes()
        await mailcom.ensure_indexes()

    asyncio.run(scenario())

    indexes = manager.database["outlook_register_config"].index_calls
    assert indexes == [("_id", {"name": "outlook_register_config_id"})]
    assert manager.database["outlook_register_tasks"].index_calls == [
        ("_id", {"name": "outlook_register_task_id"})
    ]
    assert manager.database["mailcom_migrations"].index_calls == [
        ("_id", {"name": "mailcom_migration_id"})
    ]


def test_outlook_register_task_uses_independent_state_and_mongo_proxy_group(tmp_path: Path) -> None:
    from backend.outlook_register_task_service import OutlookRegisterTaskService
    from backend.outlook_service import OutlookStore
    from backend.resource_service import MongoResourceStore

    manager = _OutlookFakeManager()
    resources = MongoResourceStore(manager)
    sink = OutlookStore(resources)
    service = OutlookRegisterTaskService(resources, result_sink=sink)

    async def scenario() -> None:
        config = await service.update_config({"proxy": {"group": "shared"}, "tasks": 4})
        assert config["tasks"] == 4
        assert config["oauth2"]["clientIdConfigured"] is True
        proxies = await service.resolve_proxy_candidates(await service._internal_config())
        assert len(proxies) == 1 and proxies[0]["group"] == "shared"
        assert proxies[0]["password"] == "SECRET_PROXY"
        # It is passed only to the runtime adapter; the HTTP status contains no credentials.
        result = await service.status()
        assert result["status"] == "idle"
        assert "SECRET_PROXY" not in str(result)
        assert "runs" not in result
        await service.record_registered_result(email="created@outlook.test", password="account-secret")
        account = await manager.database["outlook_accounts"].find_one({"emailNormalized": "created@outlook.test"})
        assert account and account["password"] == "account-secret" and account["source"] == "registration"

    asyncio.run(scenario())


def test_outlook_registration_auto_mode_dispatches_browser_engine_when_pool_has_no_oauth() -> None:
    from backend.outlook_register_task_service import OutlookRegistrationUnifiedAdapter

    class Store:
        async def registration_candidates(self, *, limit: int = 500):
            _ = limit
            return []

    class Service:
        store = Store()

    class Control:
        def __init__(self) -> None:
            self.lines: list[str] = []

        async def log(self, line: str, level: str | None = None) -> None:
            self.lines.append(f"{level or 'INFO'}:{line}")

        def should_stop(self) -> bool:
            return False

    adapter = OutlookRegistrationUnifiedAdapter(object(), Service())
    called: list[str] = []

    def fake_engine(config, control):
        called.append(str(config["execution_mode"]))
        return {"status": "completed", "submitted": 1}

    adapter.engine = fake_engine
    control = Control()
    result = asyncio.run(adapter.run_async({"execution_mode": "auto", "tasks": 1}, control))

    assert result["status"] == "completed"
    assert called == ["auto"]
    assert any("启动浏览器注册执行引擎" in line for line in control.lines)


def test_outlook_registration_engine_bridges_proxy_candidates_and_mongo_result_sink() -> None:
    from types import SimpleNamespace
    from backend.outlook_register_task_service import OutlookRegistrationEngineAdapter

    captured: dict[str, object] = {}
    persisted: list[dict[str, object]] = []

    class Store:
        async def store_registration_result(self, **payload):
            persisted.append(dict(payload))
            return "account-1", True

    class Service:
        async def check_graph(self, *_args, **_kwargs):
            raise AssertionError("registered result should not call Graph validation")

    class Control:
        def __init__(self) -> None:
            self.logs: list[str] = []

        def call(self, coroutine):
            return asyncio.run(coroutine)

        def on_log(self, line, level=None):
            self.logs.append(f"{level or 'INFO'}:{line}")

        async def log(self, line, level=None):
            self.on_log(line, level)

    def runner(config, control, *, clear_profiles, install_signals, result_sink):
        _ = control, clear_profiles, install_signals
        captured.update(config)
        result_sink({
            "kind": "registered",
            "email": "created@outlook.test",
            "password": "account-secret",
            "proxy": "http://proxy-user:proxy-pass@proxy.example.test:8080",
        })
        return {"status": "completed", "submitted": 1, "succeeded": 1, "failed": 0}

    adapter = OutlookRegistrationEngineAdapter(Store(), Service())
    adapter._load_runner = lambda: (SimpleNamespace(OutlookController=None), runner)
    config = {
        "proxy": {
            "source": "mongo",
            "candidates": [{
                "scheme": "http",
                "host": "proxy.example.test",
                "port": 8080,
                "username": "proxy-user",
                "password": "proxy-pass",
            }],
        },
        "tasks": 1,
    }
    result = adapter(config, Control())

    assert result["status"] == "completed"
    assert captured["proxy"] == config["proxy"]
    assert persisted == [{
        "email": "created@outlook.test",
        "password": "account-secret",
        "client_id": "",
        "refresh_token": "",
        "oauth": False,
        "recovery_bound": False,
        "recovery_email": "",
        "country": "",
        "proxy": "http://proxy-user:proxy-pass@proxy.example.test:8080",
    }]


def test_outlook_registration_task_starts_unified_engine_with_mongo_proxy_group() -> None:
    import copy

    from backend.outlook_register_task_service import OutlookRegisterTaskService
    from backend.outlook_service import OutlookService, OutlookStore

    manager = _OutlookFakeManager()
    resources = MongoResourceStore(manager)
    store = OutlookStore(resources)
    outlook_service = OutlookService(store)
    task = OutlookRegisterTaskService(
        resources,
        result_sink=store,
        outlook_service=outlook_service,
    )
    captured: list[dict] = []

    def fake_engine(config, control):
        captured.append(copy.deepcopy(config))
        control.on_log("[Outlook] fixture 注册引擎已调用")
        control.on_stats(
            {"submitted": 1, "running": 0, "succeeded": 1, "failed": 0},
            {},
            {"status": "completed", "batch_index": 1},
        )
        return {"status": "completed", "submitted": 1, "succeeded": 1, "failed": 0}

    task.adapter.engine = fake_engine

    async def scenario() -> dict:
        await task.update_config(
            {
                "execution_mode": "registration",
                "tasks": 1,
                "concurrent_flows": 1,
                "proxy": {"source": "mongo", "group": "shared"},
            }
        )
        started = await task.start()
        assert started["status"] == "running"
        assert started["proxyGroup"] == "shared"
        assert started["proxyCount"] == 1
        for _ in range(40):
            state = await task.status()
            if state["status"] == "completed":
                await task.close()
                return state
            await asyncio.sleep(0.01)
        await task.close()
        raise AssertionError("Outlook task did not reach a terminal state")

    state = asyncio.run(scenario())

    assert state["stats"]["submitted"] == 1
    assert state["stats"]["succeeded"] == 1
    assert state["stats"]["failed"] == 0
    assert captured and captured[0]["execution_mode"] == "registration"
    assert captured[0]["proxy"]["candidates"][0]["password"] == "SECRET_PROXY"


def test_outlook_registration_execution_mode_can_force_authorized_path() -> None:
    from backend.outlook_register_task_service import OutlookRegistrationUnifiedAdapter

    class Service:
        class Store:
            async def registration_candidates(self, *, limit: int = 500):
                _ = limit
                return [{"_id": "account-1"}]

        store = Store()

    class Control:
        def __init__(self) -> None:
            self.lines: list[str] = []

        async def log(self, line: str, level: str | None = None) -> None:
            self.lines.append(f"{level or 'INFO'}:{line}")

        def should_stop(self) -> bool:
            return False

    class Authorized:
        async def run_async(self, config, control):
            _ = config, control
            return {"status": "completed", "processed": 1}

    adapter = OutlookRegistrationUnifiedAdapter(object(), Service())
    adapter.authorized = Authorized()
    control = Control()
    result = asyncio.run(adapter.run_async({"execution_mode": "authorized", "tasks": 1}, control))

    assert result == {"status": "completed", "processed": 1}
    assert any("仅校验 Mongo 中已有 OAuth 账号" in line for line in control.lines)


def test_outlook_task_sync_adapter_finalizes_without_self_deadlock() -> None:
    from backend.outlook_register_task_service import OutlookRegisterTaskService

    class Adapter:
        def __call__(self, config, control):
            _ = config
            control.on_stats({"submitted": 1}, {}, {"status": "done", "finished": True})
            return {"status": "completed"}

    manager = _OutlookFakeManager()
    service = OutlookRegisterTaskService(MongoResourceStore(manager), adapter=Adapter())

    async def scenario() -> None:
        await service.ensure_indexes()
        started = await service.start()
        assert started["status"] == "running"
        for _ in range(40):
            state = await service.status()
            if state["status"] == "completed":
                break
            await asyncio.sleep(0.01)
        state = await service.status()
        assert state["status"] == "completed"
        assert state["stats"]["submitted"] == 1
        assert state["finishedAt"] is not None
        assert state["stats"]["status"] == "completed"
        await service.close()

    asyncio.run(scenario())


def test_outlook_task_reset_clears_stale_proxy_metadata() -> None:
    from backend.outlook_register_task_service import OutlookRegisterTaskService

    manager = _OutlookFakeManager()
    service = OutlookRegisterTaskService(MongoResourceStore(manager), adapter=lambda config, control: {"status": "completed"})

    async def scenario() -> None:
        await service.ensure_indexes()
        await manager.database["outlook_register_tasks"].update_one(
            {"_id": "outlook-register"},
            {"$set": {
                "status": "completed",
                "stats": {"status": "completed"},
                "failureStats": {},
                "proxyGroup": "stale-group",
                "proxyCount": 3,
            }},
            upsert=True,
        )
        state = await service.reset()
        assert state["status"] == "idle"
        assert state["proxyGroup"] == ""
        assert state["proxyCount"] == 0

    asyncio.run(scenario())


def test_outlook_task_error_log_keeps_detail_but_redacts_secret() -> None:
    from backend.outlook_register_task_service import OutlookRegisterTaskService

    class Adapter:
        async def run_async(self, config, control):
            _ = config, control
            raise ValueError("proxy password=SECRET_PROXY")

    manager = _OutlookFakeManager()
    service = OutlookRegisterTaskService(MongoResourceStore(manager), adapter=Adapter())

    async def scenario() -> None:
        await service.ensure_indexes()
        await service.start()
        for _ in range(40):
            state = await service.status()
            if state["status"] == "failed":
                break
            await asyncio.sleep(0.01)
        assert state["status"] == "failed"
        logs = await service.logs()
        lines = [item["line"] for item in logs]
        assert any("ValueError" in line and "proxy password=[redacted]" in line for line in lines)
        assert all("SECRET_PROXY" not in line for line in lines)
        await service.close()

    asyncio.run(scenario())


def test_outlook_task_stop_keeps_terminal_ownership_until_worker_exits() -> None:
    from backend.outlook_register_task_service import OutlookRegisterTaskService

    class Adapter:
        def __call__(self, config, control):
            _ = config
            while not control.should_stop():
                control.on_stats({"running": 1}, {}, {"status": "done", "finished": True})
                import time
                time.sleep(0.005)
            return {"status": "interrupted"}

    manager = _OutlookFakeManager()
    service = OutlookRegisterTaskService(MongoResourceStore(manager), adapter=Adapter())

    async def scenario() -> None:
        await service.ensure_indexes()
        await service.start()
        await asyncio.sleep(0.02)
        state = await service.stop()
        assert state["status"] == "interrupted"
        assert state["finishedAt"] is not None
        assert state["stats"]["status"] == "interrupted"
        await service.close()

    asyncio.run(scenario())


def test_outlook_pool_mongo_runtime_generates_submailboxes_and_extracts_otp() -> None:
    from backend.outlook_pool_service import OutlookPoolService
    from backend.outlook_service import OutlookStore

    class FakeOutlookService:
        async def messages(self, account_id, *, folder="inbox", top=10, recipient=None):
            assert account_id == "outlook1"
            assert folder == "inbox" and top == 10
            assert recipient and "+" in recipient
            return {"messages": [{"id": "m1", "subject": "Security code 482931", "preview": "Your code is 482931"}]}

        async def check_oauth(self, account_id):
            return {"ok": True, "oauthStatus": "ok", "graphStatus": "ok", "poolStatus": "available"}

    manager = _OutlookFakeManager()
    resources = MongoResourceStore(manager)
    store = OutlookStore(resources)
    pool = OutlookPoolService(resources, store, FakeOutlookService())

    async def scenario() -> None:
        await pool.ensure_indexes()
        stats = await pool.stats()
        assert stats["total"] == 1 and stats["oauth2"] == 1
        created = await pool.generate_sub_emails("outlook1", count=2, tag_prefix="otp-")
        assert len(created["created"]) == 2
        listed = await pool.list_accounts(category="sub")
        assert listed["total"] == 2
        assert all(item["receiveUrl"] and item["receiveUiUrl"] for item in listed["items"])
        assert "SECRET_REFRESH" not in str(listed)
        token = created["created"][0]["receiveUrl"].rstrip("/").rsplit("/", 1)[-1]
        payload = await pool.receive_payload(token)
        assert payload["latest_code"] == "482931"
        assert payload["messages"][0]["codes"] == ["482931"]
        config = await pool.update_oauth_check_config({"enabled": False, "interval_sec": 300})
        assert config["enabled"] is False and config["interval_sec"] == 300
        checked = await pool.run_oauth_check(ids=["outlook1"])
        assert checked["ok"] is True and checked["ok_count"] == 1
        await pool.stop_scheduler()

    asyncio.run(scenario())


def test_outlook_receive_ui_validates_capability_tokens_and_no_store_headers(tmp_path: Path) -> None:
    manager = _OutlookFakeManager()
    app = create_app(settings_path=tmp_path / "settings.json", log_dir=tmp_path / "logs", mongo_manager=manager)
    client = TestClient(app)

    invalid = client.get("/r/not-a-valid-token!")
    assert invalid.status_code == 404

    valid = client.get("/r/" + "A" * 32)
    assert valid.status_code == 200
    assert valid.headers["cache-control"] == "no-store"
    assert valid.headers["referrer-policy"] == "no-referrer"


def test_outlook_graph_message_detail_cannot_cross_submailbox_recipient() -> None:
    from backend.errors import ResourceNotFoundError
    from backend.outlook_service import OutlookService, OutlookStore

    manager = _OutlookFakeManager()
    service = OutlookService(OutlookStore(MongoResourceStore(manager)))

    async def ensure(_account):
        return None

    async def refresh(_account, *, proxy=None):
        _ = proxy
        return {"access_token": "ACCESS_FIXTURE", "refresh_token": "REFRESH_FIXTURE"}

    async def graph_get(_path, _access_token, _params, *, proxy=None):
        _ = proxy
        return {
            "id": "message-1",
            "subject": "OTP",
            "from": {"emailAddress": {"address": "sender@example.test", "name": "Sender"}},
            "toRecipients": [{"emailAddress": {"address": "sub+one@outlook.test"}}],
            "ccRecipients": [],
            "receivedDateTime": "2026-09-25T10:00:00Z",
            "body": {"contentType": "text", "content": "482931"},
            "bodyPreview": "482931",
        }

    service._ensure_graph_identity = ensure
    service._refresh = refresh
    service._graph_get = graph_get

    async def scenario() -> None:
        allowed = await service.message_detail("outlook1", "message-1", recipient="sub+one@outlook.test")
        assert allowed["message"]["subject"] == "OTP"
        with pytest.raises(ResourceNotFoundError):
            await service.message_detail("outlook1", "message-1", recipient="sub+two@outlook.test")

    asyncio.run(scenario())


def test_outlook_runtime_logs_redact_proxy_and_token_values() -> None:
    from backend.outlook_register_task_service import _redact_outlook_log

    import sys
    legacy_root = Path(__file__).resolve().parents[2] / "outlook_register"
    sys.path.insert(0, str(legacy_root))
    try:
        from controllers.outlook_controller import OutlookController
    finally:
        sys.path.remove(str(legacy_root))

    text = "proxy=http://SECRET_USER:SECRET_PASS@proxy.example.test:8080 refresh_token=SECRET_REFRESH"
    assert "SECRET_USER" not in _redact_outlook_log(text)
    assert "SECRET_PASS" not in _redact_outlook_log(text)
    assert "SECRET_REFRESH" not in _redact_outlook_log(text)
    assert "SECRET_USER" not in OutlookController._redact_log_text(text)
    assert "SECRET_PASS" not in OutlookController._redact_log_text(text)
    assert "SECRET_REFRESH" not in OutlookController._redact_log_text(text)


def test_outlook_registration_both_mode_aggregates_registration_and_authorized_stats() -> None:
    from backend.outlook_register_task_service import OutlookRegistrationUnifiedAdapter

    class Store:
        async def registration_candidates(self, *, limit: int = 500):
            _ = limit
            return [{"_id": "account-1"}]

    class Service:
        store = Store()

    class Control:
        def __init__(self) -> None:
            self.stats_rows = []
            self.lines = []

        async def log(self, line: str, level: str | None = None) -> None:
            self.lines.append((level, line))

        async def stats(self, runtime, failures, extra=None) -> None:
            self.stats_rows.append((dict(runtime or {}), dict(failures or {}), dict(extra or {})))

        def on_stats(self, runtime, failures, extra=None) -> None:
            self.stats_rows.append((dict(runtime or {}), dict(failures or {}), dict(extra or {})))

        def on_log(self, line, level=None) -> None:
            self.lines.append((level, line))

        def should_stop(self) -> bool:
            return False

    class Authorized:
        async def run_async(self, config, control):
            _ = config
            await control.stats({"submitted": 2, "succeeded": 1, "failed": 1}, {"oauth": 1}, {"status": "running"})
            return {"status": "completed", "submitted": 2, "succeeded": 1, "failed": 1}

    adapter = OutlookRegistrationUnifiedAdapter(object(), Service())
    adapter.engine = lambda config, control: {"status": "completed", "submitted": 3, "succeeded": 2, "failed": 1}
    adapter.authorized = Authorized()
    control = Control()
    result = asyncio.run(adapter.run_async({"execution_mode": "both", "tasks": 2}, control))

    assert result["submitted"] == 5
    assert result["succeeded"] == 3
    assert result["failed"] == 2
    assert control.stats_rows[-1][0]["submitted"] == 5
    assert control.stats_rows[-1][0]["succeeded"] == 3
    assert control.stats_rows[-1][0]["failed"] == 2


def test_mailcom_migration_preserves_existing_account_and_skips_its_aliases(tmp_path: Path) -> None:
    import sqlite3
    from backend.mailcom_service import MailComService

    source = tmp_path / "mailcom.db"
    with sqlite3.connect(source) as db:
        db.executescript("""
            CREATE TABLE accounts (
                id TEXT PRIMARY KEY, email TEXT, password_encrypted BLOB,
                status TEXT, message_count INTEGER, last_checked_at TEXT,
                last_error TEXT, created_at TEXT, updated_at TEXT
            );
            CREATE TABLE aliases (
                id TEXT PRIMARY KEY, account_id TEXT, email TEXT, label TEXT,
                created_at TEXT, updated_at TEXT
            );
        """)
        db.execute("INSERT INTO accounts VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)", ("legacy-1", "same@mail.test", b"protected:legacy-secret", "unknown", None, None, None, "2024", "2024"))
        db.execute("INSERT INTO aliases VALUES (?, ?, ?, ?, ?, ?)", ("alias-1", "legacy-1", "old-alias@mail.test", "old", "2024", "2024"))

    manager = _OutlookFakeManager()
    service = MailComService(
        MongoResourceStore(manager),
        cipher=_TestCipher(),
        legacy_cipher=_TestCipher(),
        sqlite_path=source,
    )

    async def scenario() -> dict:
        await service.import_account("same@mail.test", "mongo-secret", source="manual")
        return await service.migrate_legacy()

    result = asyncio.run(scenario())
    account = next(iter(manager.database["mailcom_accounts"].rows.values()))
    assert account["emailNormalized"] == "same@mail.test"
    assert account["passwordEncrypted"] == b"protected:mongo-secret"
    assert account["source"] == "manual"
    assert manager.database["mailcom_aliases"].rows == {}
    assert sum(row["emailNormalized"] == "same@mail.test" for row in manager.database["mailcom_accounts"].rows.values()) == 1
    assert result["conflicts"] == 2  # source account conflict + its orphaned alias
    assert result["status"] == "completed_with_conflicts"
    assert source.exists()
    assert Path(result["backup"]).exists()


class _TestCipher:
    def encrypt(self, value: str) -> bytes:
        return ("protected:" + value).encode()

    def decrypt(self, value: bytes) -> str:
        return value.decode().removeprefix("protected:")


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


def test_outlook_register_task_runs_authorized_accounts_through_main_service(tmp_path: Path) -> None:
    import time
    import httpx

    manager = _OutlookFakeManager()
    app = create_app(settings_path=tmp_path / "settings.json", log_dir=tmp_path / "logs", mongo_manager=manager)
    requests: list[httpx.Request] = []
    client_options: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.host == "login.microsoftonline.com":
            return httpx.Response(200, json={"access_token": "TASK_ACCESS", "refresh_token": "TASK_REFRESH"})
        if request.url.path.endswith("/me"):
            return httpx.Response(200, json={"id": "task-user", "mail": "me@outlook.test", "userPrincipalName": "me@outlook.test"})
        return httpx.Response(404)

    real_client = httpx.AsyncClient

    def factory(**kwargs):
        client_options.append(dict(kwargs))
        # MockTransport does not route through httpx's proxy transport; retain
        # the option for the assertion while keeping this test offline.
        kwargs.pop("proxy", None)
        return real_client(transport=httpx.MockTransport(handler), **kwargs)

    app.state.outlook_service.http_client_factory = factory
    with TestClient(app) as client:
        saved = client.put("/api/outlook/register", json={"tasks": 1, "proxy": {"group": "shared"}})
        assert saved.status_code == 200
        started = client.post("/api/outlook/register/start")
        assert started.status_code == 202
        for _ in range(50):
            status = client.get("/api/outlook/register").json()
            if status["status"] == "completed":
                break
            time.sleep(0.02)
        else:
            raise AssertionError(status)

        assert status["stats"]["submitted"] == 1
        assert status["stats"]["succeeded"] == 1
        assert status["stats"]["failed"] == 0
        assert status["failure_stats"] == {}
        account = manager.database["outlook_accounts"].rows["outlook1"]
        assert account["oauthStatus"] == "ok"
        assert account["graphStatus"] == "ok"
        assert manager.database["emails"].rows["outlook:outlook1"]["status"] == "available"
        assert any(options.get("proxy") == "http://SECRET_USER:SECRET_PROXY@proxy.example.test:8080" for options in client_options)
        logs = client.get("/api/outlook/register/logs").json()["items"]
        assert any("授权账号执行器已启用" in item["line"] for item in logs)
        assert all(secret not in client.get("/api/outlook/register").text for secret in ("SECRET_USER", "SECRET_PROXY", "TASK_REFRESH", "TASK_ACCESS"))


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


@pytest.mark.parametrize("scopes", [["offline_access", "Mail.Read"], []])
def test_outlook_config_public_scopes_replace_existing_scopes(scopes) -> None:
    from backend.outlook_register_task_service import OutlookRegisterTaskService
    manager = _OutlookFakeManager()
    service = OutlookRegisterTaskService(MongoResourceStore(manager), adapter=lambda config, control: {})
    async def scenario() -> None:
        await service.update_config({"oauth2": {"client_id": "CLIENT_ID_FIXTURE", "Scopes": ["old.scope"]}})
        saved = await service.update_config({"oauth2": {"scopes": scopes, "client_id": "", "clientIdConfigured": False}})
        assert saved["oauth2"]["scopes"] == scopes
        assert saved["oauth2"]["clientIdConfigured"] is True
        assert "client_id" not in saved["oauth2"]
        internal = await service._internal_config()
        assert internal["oauth2"]["Scopes"] == scopes
        assert internal["oauth2"]["client_id"] == "CLIENT_ID_FIXTURE"
        assert "clientIdConfigured" not in internal["oauth2"]
    asyncio.run(scenario())
