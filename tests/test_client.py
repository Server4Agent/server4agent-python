import asyncio
import base64
import inspect
import json
from urllib.parse import parse_qsl

import httpx
import pytest

from server4agent import (
    APIStatusError,
    AppStart,
    AsyncServer4Agent,
    ConflictError,
    DeployResult,
    LogsSnapshot,
    Me,
    NotFoundError,
    ProjectExport,
    ProjectImport,
    ProjectInit,
    Secret,
    SecretInput,
    Server,
    Server4Agent,
    Server4AgentError,
    WEBHOOK_EVENTS,
)
from server4agent._sync.client import (
    BuildsResource,
    FilesResource,
    KeysResource,
    ProjectsResource,
    ServersResource,
    TasksResource,
    TemplatesResource,
    WebhooksResource,
)
from server4agent._sync.transport import SyncTransport
from server4agent._transport import default_headers

BASE = "https://api.server4agent.com"


def transport(handler, api_key="sk_live_test", max_retries=2):
    client = httpx.Client(
        base_url=BASE,
        headers=default_headers(api_key),
        transport=httpx.MockTransport(handler),
    )
    return SyncTransport(BASE, api_key, max_retries=max_retries, client=client)


def _server_raw(**over):
    base = {"id": "srv_1", "task": "worker", "tier": "small", "status": "running", "region": "us"}
    base.update(over)
    return base


def test_requires_an_api_key():
    with pytest.raises(ValueError, match="no API key"):
        Server4Agent(api_key="")


def test_create_returns_a_server_handle_and_sends_bearer_and_body():
    seen = {}

    def handler(req):
        seen["auth"] = req.headers.get("authorization")
        seen["path"] = req.url.path
        seen["body"] = req.content.decode()
        return httpx.Response(200, json={"id": "srv_1", "url": None, "status": "running", "tier": "small"})

    srv = ServersResource(transport(handler)).create(task="worker")

    assert isinstance(srv, Server)
    assert (srv.id, srv.tier, srv.status, srv.public_url) == ("srv_1", "small", "running", None)
    assert seen["auth"] == "Bearer sk_live_test"
    assert seen["path"] == "/servers"
    assert json.loads(seen["body"]) == {"task": "worker"}


def test_server_kind_round_trips_and_defaults_to_standard():
    """A browser box is only distinguishable from a headless one by `kind`."""
    seen = {}

    def handler(req):
        seen["body"] = json.loads(req.content.decode())
        return httpx.Response(
            200,
            json={"id": "srv_1", "url": None, "status": "running", "tier": "medium", "kind": "browser"},
        )

    srv = ServersResource(transport(handler)).create(tier="medium", kind="browser")
    assert srv.kind == "browser"
    assert seen["body"] == {"tier": "medium", "kind": "browser"}

    # An API that predates the field says nothing, which is a headless box.
    listed = ServersResource(
        transport(lambda req: httpx.Response(200, json={"servers": [_server_raw()]}))
    ).list()
    assert listed[0].kind == "standard"


def test_normalizes_snake_case_project_fields():
    def handler(req):
        return httpx.Response(200, json={
            "id": "proj_1", "server_id": "srv_1", "name": "Landing", "slug": "landing-abc",
            "description": None, "visibility": "public", "lifecycle": "persistent",
            "runtime_status": "running", "template": None,
            "url": "https://landing-abc.apps.server4agent.com",
            "created_at": "2026-01-01T00:00:00Z", "updated_at": "2026-01-01T00:00:00Z",
        })

    project = ProjectsResource(transport(handler)).create("srv_1", name="Landing", visibility="public")
    assert project.server_id == "srv_1"
    assert project.runtime_status == "running"
    assert project.url.endswith(".apps.server4agent.com")


def test_file_read_builds_a_query_string():
    seen = {}

    def handler(req):
        seen["url"] = str(req.url)
        return httpx.Response(200, json={"path": "app.py", "content": "print(1)"})

    content = FilesResource(transport(handler)).read("srv_1", "app.py")
    assert content == "print(1)"
    assert seen["url"].endswith("/servers/srv_1/files?path=app.py")


def test_maps_status_to_a_typed_error_with_request_id():
    def handler(req):
        return httpx.Response(
            404,
            json={"error": "not_found", "message": "server not found"},
            headers={"x-request-id": "req_abc"},
        )

    with pytest.raises(NotFoundError) as ei:
        ServersResource(transport(handler)).get("missing")

    err = ei.value
    assert isinstance(err, APIStatusError) and isinstance(err, Server4AgentError)
    assert (err.status, err.code, err.message, err.request_id) == (
        404, "not_found", "server not found", "req_abc",
    )


def test_retries_a_retryable_status_then_succeeds(monkeypatch):
    import server4agent._transport as tr
    monkeypatch.setattr(tr, "retry_delay", lambda *a, **k: 0)  # no real backoff sleep

    calls = {"n": 0}

    def handler(req):
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(503, json={"error": "unavailable", "message": "later"})
        return httpx.Response(200, json={"server": _server_raw()})

    srv = ServersResource(transport(handler)).get("srv_1")
    assert calls["n"] == 2
    assert srv.id == "srv_1"


def test_does_not_retry_a_non_retryable_status(monkeypatch):
    import server4agent._transport as tr
    monkeypatch.setattr(tr, "retry_delay", lambda *a, **k: 0)

    calls = {"n": 0}

    def handler(req):
        calls["n"] += 1
        return httpx.Response(400, json={"error": "bad_request", "message": "nope"})

    with pytest.raises(APIStatusError):
        ServersResource(transport(handler)).get("srv_1")
    assert calls["n"] == 1


def test_server_handle_exec_and_deploy():
    def handler(req):
        if req.url.path == "/servers/srv_1/exec":
            return httpx.Response(200, json={"stdout": "hello\n"})
        if req.url.path == "/servers/srv_1/deployments":
            return httpx.Response(200, json={"url": "https://srv-1.apps.server4agent.com"})
        return httpx.Response(200, json={"server": _server_raw()})

    server = ServersResource(transport(handler)).get("srv_1")
    assert server.exec("echo hello") == "hello\n"
    assert server.deploy().endswith(".apps.server4agent.com")


def test_task_wait_polls_until_terminal():
    statuses = iter(["running", "completed"])

    def handler(req):
        if req.method == "POST" and req.url.path.endswith("/tasks"):
            return httpx.Response(200, json={"task_id": "tsk_1", "status": "running"})
        return httpx.Response(200, json={"task": {
            "id": "tsk_1", "serverId": "srv_1", "goal": "x",
            "status": next(statuses), "createdAt": "",
        }})

    task = TasksResource(transport(handler)).create("srv_1", "do x")
    assert task.status == "running"
    done = task.wait(poll_interval=0)
    assert done.status == "completed"


def test_task_reports_who_started_it():
    tasks = [
        {"id": "tsk_1", "serverId": "srv_1", "goal": "do x", "messageType": "user",
         "status": "completed", "createdAt": ""},
        {"id": "tsk_2", "serverId": "srv_1",
         "goal": "Project setup: import the code and start the app",
         "messageType": "system", "status": "completed", "createdAt": ""},
        # An API that predates the field: what it returned was always yours.
        {"id": "tsk_3", "serverId": "srv_1", "goal": "do y", "status": "completed", "createdAt": ""},
    ]

    def handler(req):
        return httpx.Response(200, json={"tasks": tasks})

    listed = TasksResource(transport(handler)).list("srv_1")
    assert [t.message_type for t in listed] == ["user", "system", "user"]


def test_task_you_create_is_your_own_message():
    def handler(req):
        return httpx.Response(200, json={"task_id": "tsk_1", "status": "running"})

    task = TasksResource(transport(handler)).create("srv_1", "do x")
    assert task.message_type == "user"


def test_percent_encodes_path_segments():
    seen = {}

    def handler(req):
        seen["raw"] = req.url.raw_path  # bytes, before decoding
        return httpx.Response(200, json={"server": _server_raw()})

    ServersResource(transport(handler)).get("a/b?c#d")  # a hostile id must not escape its segment
    assert seen["raw"] == b"/servers/a%2Fb%3Fc%23d"
    assert b"/a/b" not in seen["raw"]


def test_does_not_retry_post_on_5xx(monkeypatch):
    import server4agent._transport as tr
    monkeypatch.setattr(tr, "retry_delay", lambda *a, **k: 0)

    calls = {"n": 0}

    def handler(req):
        calls["n"] += 1
        return httpx.Response(500, json={"error": "boom", "message": "server error"})

    with pytest.raises(APIStatusError):
        ServersResource(transport(handler)).create(tier="small")  # POST — must not double-create
    assert calls["n"] == 1


def test_retries_post_on_503(monkeypatch):
    import server4agent._transport as tr
    monkeypatch.setattr(tr, "retry_delay", lambda *a, **k: 0)

    calls = {"n": 0}

    def handler(req):
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(503, json={"error": "unavailable", "message": "later"})
        return httpx.Response(200, json={"id": "srv_1", "url": None, "status": "running", "tier": "small"})

    srv = ServersResource(transport(handler)).create(tier="small")
    assert calls["n"] == 2
    assert srv.id == "srv_1"


def test_tolerates_a_non_json_error_body():
    def handler(req):
        return httpx.Response(400, text="<html>Bad Request</html>")

    with pytest.raises(APIStatusError) as ei:
        ServersResource(transport(handler)).get("srv_1")
    assert ei.value.status == 400  # a bare JSONDecodeError must not leak


def test_wait_invokes_on_poll():
    statuses = iter(["running", "completed"])
    seen = []

    def handler(req):
        if req.method == "POST" and req.url.path.endswith("/tasks"):
            return httpx.Response(200, json={"task_id": "tsk_1", "status": "running"})
        return httpx.Response(200, json={"task": {
            "id": "tsk_1", "serverId": "srv_1", "goal": "x", "status": next(statuses), "createdAt": "",
        }})

    task = TasksResource(transport(handler)).create("srv_1", "do x")
    task.wait(poll_interval=0, on_poll=lambda t: seen.append(t.status))
    assert seen[-1] == "completed"


def test_keys_create_returns_plaintext_once():
    def handler(req):
        return httpx.Response(200, json={
            "id": "key_1", "label": "ci", "masked": "sk_live_ab...yz",
            "allowed_server_ids": [], "allowed_project_ids": [],
            "created_at": "t", "last_used_at": None, "key": "sk_live_plaintext",
        })

    k = KeysResource(transport(handler)).create("ci")
    assert k.key == "sk_live_plaintext"
    assert k.masked.startswith("sk_live_")


def test_webhooks_create_returns_secret_once():
    def handler(req):
        return httpx.Response(200, json={
            "id": "wh_1", "url": "https://x.example/hook",
            "events": ["task.completed"], "created_at": "t", "secret": "whsec_abc",
        })

    w = WebhooksResource(transport(handler)).create("https://x.example/hook", ["task.completed"])
    assert w.secret == "whsec_abc"
    assert w.events == ["task.completed"]


def test_templates_list_parses_defaults():
    def handler(req):
        return httpx.Response(200, json={"templates": [{
            "id": "t1", "label": "Flight watcher", "description": "d",
            "defaults": {"name": "n", "description": "d", "visibility": "public", "lifecycle": "persistent"},
        }]})

    templates = TemplatesResource(transport(handler)).list()
    assert templates[0].defaults.visibility == "public"


def test_files_write_sends_put_body():
    seen = {}

    def handler(req):
        seen["method"] = req.method
        seen["path"] = req.url.path
        seen["body"] = req.content.decode()
        return httpx.Response(200)

    FilesResource(transport(handler)).write("srv_1", "app.py", "print(1)")
    assert seen["method"] == "PUT"
    assert seen["path"] == "/servers/srv_1/files"
    assert json.loads(seen["body"]) == {"path": "app.py", "content": "print(1)"}


def test_files_write_bytes_sends_base64_with_encoding():
    seen = {}
    data = b"\x50\x4b\x03\x04"

    def handler(req):
        seen["body"] = req.content.decode()
        return httpx.Response(200)

    FilesResource(transport(handler)).write("srv_1", "book.xlsx", data)
    body = json.loads(seen["body"])
    assert body["encoding"] == "base64"
    assert base64.b64decode(body["content"]) == data


def test_files_read_binary_decodes_base64():
    data = b"\x50\x4b\x03\x04"

    def handler(req):
        assert req.url.query.decode() == "path=book.xlsx&encoding=base64"
        return httpx.Response(200, json={"path": "book.xlsx", "content": base64.b64encode(data).decode()})

    content = FilesResource(transport(handler)).read_binary("srv_1", "book.xlsx")
    assert content == data


def test_files_get_download_url():
    def handler(req):
        assert req.url.path == "/servers/srv_1/files/download-url"
        assert req.url.query.decode() == "path=book.xlsx&ttl_seconds=3600"
        return httpx.Response(
            200,
            json={"path": "book.xlsx", "url": "https://server4agent.com/api/files/v1.abc.def", "expires_at": "2026-07-20T00:00:00Z"},
        )

    link = FilesResource(transport(handler)).get_download_url("srv_1", "book.xlsx", ttl_seconds=3600)
    assert link == {
        "path": "book.xlsx",
        "url": "https://server4agent.com/api/files/v1.abc.def",
        "expires_at": "2026-07-20T00:00:00Z",
    }


def test_server_handle_control_sends_action():
    def handler(req):
        if req.url.path.endswith("/actions"):
            return httpx.Response(200, json={"id": "srv_1", "status": "running"})
        return httpx.Response(200, json={"server": _server_raw()})

    server = ServersResource(transport(handler)).get("srv_1")
    assert server.start()["status"] == "running"


def test_build_wait_polls_until_completed():
    statuses = iter(["running", "completed"])

    def handler(req):
        if req.method == "POST" and req.url.path.endswith("/builds"):
            return httpx.Response(200, json={"build_id": "bld_1", "status": "queued", "steps": []})
        return httpx.Response(200, json={"build": {
            "id": "bld_1", "serverId": "srv_1", "goal": "x", "status": next(statuses),
            "steps": [], "artifacts": [], "url": None, "createdAt": "",
        }})

    build = BuildsResource(transport(handler)).start("srv_1", "build x")
    done = build.wait(poll_interval=0)
    assert done.status == "completed"


def test_async_create_and_exec():
    def handler(req):
        if req.method == "POST" and req.url.path == "/servers":
            return httpx.Response(200, json={"id": "srv_1", "url": None, "status": "running", "tier": "small"})
        if req.url.path == "/servers/srv_1/exec":
            return httpx.Response(200, json={"stdout": "hi\n"})
        return httpx.Response(404, json={"error": "nf", "message": "no"})

    async def run():
        client = AsyncServer4Agent(api_key="sk_test")
        client._t._client = httpx.AsyncClient(
            base_url=BASE, headers=default_headers("sk_test"), transport=httpx.MockTransport(handler)
        )
        server = await client.servers.create(tier="small")
        out = await server.exec("echo hi")
        await client.aclose()
        return server.id, out

    sid, out = asyncio.run(run())
    assert sid == "srv_1"
    assert out == "hi\n"


# =============================================================================
# 0.7.0: every test below runs on both clients. `api.client(handler)` builds a
# Server4Agent or AsyncServer4Agent over a mocked transport, `api.call(x)`
# resolves a result (awaiting it on the async client), and `api.seen` records
# each request as {method, path, query, body}, with the path still escaped.
# =============================================================================

HOSTILE = "a/b?c#d"
HOSTILE_SEG = "a%2Fb%3Fc%23d"


class _Api:
    def __init__(self, flavour):
        self.flavour = flavour
        self.seen = []

    def client(self, handler):
        def recording(req):
            raw = req.url.raw_path.split(b"?")[0].decode()
            self.seen.append({
                "method": req.method,
                "path": raw,
                "query": dict(parse_qsl(req.url.query.decode())),
                "body": json.loads(req.content) if req.content else None,
            })
            return handler(req)

        mock = httpx.MockTransport(recording)
        if self.flavour == "sync":
            c = Server4Agent(api_key="sk_live_test", max_retries=0)
            c._t._client.close()
            c._t._client = httpx.Client(base_url=BASE, headers=default_headers("sk_live_test"), transport=mock)
        else:
            c = AsyncServer4Agent(api_key="sk_live_test", max_retries=0)
            c._t._client = httpx.AsyncClient(base_url=BASE, headers=default_headers("sk_live_test"), transport=mock)
        return c

    def call(self, x):
        return asyncio.run(x) if inspect.iscoroutine(x) else x

    @property
    def last(self):
        return self.seen[-1]


@pytest.fixture(params=["sync", "async"])
def api(request):
    return _Api(request.param)


def _project_raw(**over):
    base = {
        "id": "prj_1", "server_id": "srv_1", "name": "web", "slug": "web-abc",
        "description": None, "visibility": "private", "access": "public",
        "lifecycle": "persistent", "runtime_status": "running", "template": None,
        "source": None, "init_phase": None, "run_plan": None, "url": None,
        "created_at": "2026-10-01T00:00:00Z", "updated_at": "2026-10-01T00:00:00Z",
    }
    base.update(over)
    return base


def _secret_raw(**over):
    base = {
        "id": "sec_1", "key": "DATABASE_URL", "description": None, "masked": "••••5432",
        "server_ids": [], "project_ids": [], "created_at": "t", "updated_at": "t",
    }
    base.update(over)
    return base


def _routes(table):
    """A handler answering (METHOD, path) from a table; anything else is a 404."""
    def handler(req):
        key = (req.method, req.url.raw_path.split(b"?")[0].decode())
        if key in table:
            status, body = table[key]
            return httpx.Response(status, json=body)
        return httpx.Response(404, json={"error": "not_found", "message": f"no route {key}"})
    return handler


# --- secrets ------------------------------------------------------------------

def test_secrets_account_list_set_delete(api):
    c = api.client(_routes({
        ("GET", "/secrets"): (200, {"secrets": [_secret_raw(server_ids=["srv_1"])]}),
        ("PUT", "/secrets"): (200, _secret_raw(description="prod db")),
        ("DELETE", f"/secrets/{HOSTILE_SEG}"): (200, {"id": HOSTILE, "deleted": True}),
    }))

    listed = api.call(c.secrets.list())
    assert isinstance(listed[0], Secret)
    assert (listed[0].key, listed[0].masked, listed[0].server_ids) == ("DATABASE_URL", "••••5432", ["srv_1"])
    assert not hasattr(listed[0], "value")

    s = api.call(c.secrets.set("DATABASE_URL", "postgres://x", description="prod db"))
    assert api.last["method"] == "PUT" and api.last["path"] == "/secrets"
    assert api.last["body"] == {"key": "DATABASE_URL", "value": "postgres://x", "description": "prod db"}
    assert s.description == "prod db"

    api.call(c.secrets.set("TOKEN", "t"))
    assert api.last["body"] == {"key": "TOKEN", "value": "t"}  # no null description

    assert api.call(c.secrets.delete(HOSTILE)) is None
    assert api.last["method"] == "DELETE" and api.last["path"] == f"/secrets/{HOSTILE_SEG}"


def test_secrets_scoped_to_a_server_and_a_project(api):
    after = {"secrets": [_secret_raw(key="A", server_ids=[HOSTILE])]}
    c = api.client(_routes({
        ("GET", f"/servers/{HOSTILE_SEG}/secrets"): (200, after),
        ("PUT", f"/servers/{HOSTILE_SEG}/secrets"): (200, after),
        ("GET", f"/projects/{HOSTILE_SEG}/secrets"): (200, after),
        ("PUT", f"/projects/{HOSTILE_SEG}/secrets"): (200, after),
    }))

    out = api.call(c.secrets.set_for_server(HOSTILE, [
        {"key": "A", "value": "1"},
        SecretInput("B", "2", description="note"),
    ]))
    assert api.last["method"] == "PUT" and api.last["path"] == f"/servers/{HOSTILE_SEG}/secrets"
    assert api.last["body"] == {"secrets": [
        {"key": "A", "value": "1"},
        {"key": "B", "value": "2", "description": "note"},
    ]}
    assert out[0].server_ids == [HOSTILE]

    api.call(c.secrets.list_for_server(HOSTILE))
    assert api.last == {"method": "GET", "path": f"/servers/{HOSTILE_SEG}/secrets", "query": {}, "body": None}

    api.call(c.secrets.set_for_project(HOSTILE, [SecretInput("C", "3")]))
    assert api.last["path"] == f"/projects/{HOSTILE_SEG}/secrets"
    assert api.last["body"] == {"secrets": [{"key": "C", "value": "3"}]}

    listed = api.call(c.secrets.list_for_project(HOSTILE))
    assert api.last["method"] == "GET" and api.last["path"] == f"/projects/{HOSTILE_SEG}/secrets"
    assert listed[0].key == "A"


def test_secrets_from_server_and_project_handles(api):
    c = api.client(_routes({
        ("GET", "/servers/srv_1"): (200, {"server": _server_raw()}),
        ("GET", "/projects/prj_1"): (200, _project_raw()),
        ("PUT", "/servers/srv_1/secrets"): (200, {"secrets": [_secret_raw(server_ids=["srv_1"])]}),
        ("GET", "/projects/prj_1/secrets"): (200, {"secrets": [_secret_raw(project_ids=["prj_1"])]}),
    }))
    server = api.call(c.servers.get("srv_1"))
    out = api.call(server.secrets.set([{"key": "DATABASE_URL", "value": "x"}]))
    assert api.last["path"] == "/servers/srv_1/secrets" and out[0].server_ids == ["srv_1"]

    project = api.call(c.projects.get("prj_1"))
    seen = api.call(project.secrets.list())
    assert api.last["path"] == "/projects/prj_1/secrets" and seen[0].project_ids == ["prj_1"]


def test_secrets_set_refuses_a_bare_mapping(api):
    c = api.client(_routes({}))
    with pytest.raises(TypeError, match="list"):
        api.call(c.secrets.set_for_server("srv_1", {"key": "A", "value": "1"}))
    assert api.seen == []


# --- logs ---------------------------------------------------------------------

_LOGS = {"logs": {
    "updatedAt": "2026-10-11T03:00:00Z",
    "files": [{
        "path": "web/app.log", "projectSlug": "web-abc", "name": "app.log",
        "sizeBytes": 4096, "modifiedAt": "2026-10-11T02:59:00Z", "tail": "listening on 3000\n",
    }],
}}


def test_server_logs_query_and_parse(api):
    c = api.client(_routes({
        ("GET", "/servers/srv_1"): (200, {"server": _server_raw()}),
        ("GET", "/servers/srv_1/logs"): (200, _LOGS),
    }))
    server = api.call(c.servers.get("srv_1"))
    snap = api.call(server.logs(project_id="prj_1", lines=50))
    assert api.last["path"] == "/servers/srv_1/logs"
    assert api.last["query"] == {"project_id": "prj_1", "lines": "50"}
    assert isinstance(snap, LogsSnapshot)
    assert snap.updated_at == "2026-10-11T03:00:00Z"
    f = snap.files[0]
    assert (f.path, f.project_slug, f.size_bytes, f.modified_at, f.tail) == (
        "web/app.log", "web-abc", 4096, "2026-10-11T02:59:00Z", "listening on 3000\n",
    )

    api.call(server.logs())
    assert api.last["query"] == {}  # no project_id=None / lines=None on the wire


def test_logs_flat_and_from_a_project_and_null(api):
    c = api.client(_routes({
        ("GET", f"/servers/{HOSTILE_SEG}/logs"): (200, {"logs": None}),
        ("GET", "/projects/prj_1"): (200, _project_raw(server_id="srv_9")),
        ("GET", "/servers/srv_9/logs"): (200, _LOGS),
    }))
    assert api.call(c.servers.logs(HOSTILE, lines=5)) is None  # never reported
    assert api.last["path"] == f"/servers/{HOSTILE_SEG}/logs" and api.last["query"] == {"lines": "5"}

    project = api.call(c.projects.get("prj_1"))
    snap = api.call(project.logs(lines=10))
    assert api.last["path"] == "/servers/srv_9/logs"
    assert api.last["query"] == {"project_id": "prj_1", "lines": "10"}
    assert snap.files[0].name == "app.log"


# --- init / export / import ---------------------------------------------------

def test_project_init_sends_proceed_and_updates_the_handle(api):
    c = api.client(_routes({
        ("GET", "/projects/prj_1"): (200, _project_raw(init_phase="awaiting_secrets")),
        ("POST", "/projects/prj_1/init"): (201, {"init_run_id": "tsk_9", "init_phase": "seeding"}),
        ("POST", f"/projects/{HOSTILE_SEG}/init"): (201, {"init_run_id": None, "init_phase": None}),
    }))
    project = api.call(c.projects.get("prj_1"))
    assert project.init_run_id is None

    result = api.call(project.init(proceed=True))
    assert api.last["method"] == "POST" and api.last["body"] == {"proceed": True}
    assert result == ProjectInit(init_run_id="tsk_9", init_phase="seeding")
    assert (project.init_run_id, project.init_phase) == ("tsk_9", "seeding")

    flat = api.call(c.projects.init(HOSTILE))
    assert api.last["path"] == f"/projects/{HOSTILE_SEG}/init" and api.last["body"] is None
    assert flat.init_run_id is None


def test_project_export(api):
    exported = {
        "project_id": "prj_1", "url": "https://server4agent.com/api/files/v1.x.y",
        "expires_at": "2026-10-12T00:00:00Z", "filename": "web.s4a.tar.gz",
        "bytes": 2048, "env": ["DATABASE_URL"],
    }
    c = api.client(_routes({
        ("GET", "/projects/prj_1"): (200, _project_raw()),
        ("POST", "/projects/prj_1/export"): (201, exported),
        ("POST", f"/projects/{HOSTILE_SEG}/export"): (201, exported),
    }))
    project = api.call(c.projects.get("prj_1"))
    out = api.call(project.export(ttl_seconds=600))
    assert api.last["body"] == {"ttl_seconds": 600}
    assert isinstance(out, ProjectExport)
    assert (out.filename, out.bytes, out.env, out.project_id) == ("web.s4a.tar.gz", 2048, ["DATABASE_URL"], "prj_1")

    api.call(c.projects.export(HOSTILE))
    assert api.last["path"] == f"/projects/{HOSTILE_SEG}/export" and api.last["body"] is None


_IMPORTED = dict(
    _project_raw(id="prj_2", name="imported", init_phase="seeding"),
    init_run_id="tsk_7",
    imported_from="https://www.server4agent.com",
    exported_at="2026-10-10T00:00:00Z",
    bound_env=["DATABASE_URL"],
    missing_env=["STRIPE_KEY"],
    warnings=["lifecycle ephemeral is not available here"],
)


def test_import_bundle_from_bytes(api):
    data = b"\x1f\x8b\x08\x00bundle"
    c = api.client(_routes({
        ("GET", "/servers/srv_1"): (200, {"server": _server_raw()}),
        ("POST", "/servers/srv_1/projects/import"): (201, _IMPORTED),
    }))
    server = api.call(c.servers.get("srv_1"))
    result = api.call(server.projects.import_bundle(data, name="imported"))

    assert api.last["method"] == "POST" and api.last["path"] == "/servers/srv_1/projects/import"
    assert api.last["body"] == {"bundle": base64.b64encode(data).decode(), "name": "imported"}
    assert isinstance(result, ProjectImport)
    assert result.project.id == "prj_2" and result.project.init_run_id == "tsk_7"
    assert result.project.init_phase == "seeding"
    assert result.init_run_id == "tsk_7"
    assert (result.bound_env, result.missing_env) == (["DATABASE_URL"], ["STRIPE_KEY"])
    assert result.imported_from == "https://www.server4agent.com"
    assert result.exported_at == "2026-10-10T00:00:00Z"
    assert result.warnings == ["lifecycle ephemeral is not available here"]
    # The project is a real handle you can act on.
    assert type(result.project).__name__ in ("Project", "AsyncProject")


def test_import_bundle_flat_takes_base64_text_as_is(api):
    c = api.client(_routes({("POST", f"/servers/{HOSTILE_SEG}/projects/import"): (201, _IMPORTED)}))
    api.call(c.projects.import_bundle(HOSTILE, " QUJD\n"))
    assert api.last["path"] == f"/servers/{HOSTILE_SEG}/projects/import"
    assert api.last["body"] == {"bundle": "QUJD"}


# --- start app ----------------------------------------------------------------

_STARTED = {
    "project_id": "prj_1", "name": "web", "status": "running", "listening": True,
    "listen_port": 3000, "url": None, "visibility": "private", "access": "public",
    "replaced_previous": False, "restart_command": "cd web && PORT=3000 node server.js",
    "log_tail": "listening on 3000",
}


def test_project_start_app(api):
    c = api.client(_routes({
        ("GET", "/projects/prj_1"): (200, _project_raw()),
        ("POST", "/projects/prj_1/start"): (200, _STARTED),
        ("POST", f"/projects/{HOSTILE_SEG}/start"): (200, dict(_STARTED, name="worker", status="running")),
    }))
    project = api.call(c.projects.get("prj_1"))
    started = api.call(project.start("node server.js"))
    assert api.last["method"] == "POST" and api.last["body"] == {"command": "node server.js"}
    assert isinstance(started, AppStart)
    assert (started.status, started.listening, started.listen_port, started.url) == ("running", True, 3000, None)
    assert started.restart_command.endswith("node server.js")

    api.call(c.projects.start(HOSTILE, "python worker.py", name="worker"))
    assert api.last["path"] == f"/projects/{HOSTILE_SEG}/start"
    assert api.last["body"] == {"command": "python worker.py", "name": "worker"}


def test_start_app_failure_keeps_the_log_tail_on_the_error(api):
    c = api.client(_routes({
        ("POST", "/projects/prj_1/start"): (409, {
            "error": "app_failed", "message": "The app exited before it answered on port 3000.",
            "log_tail": "Error: Cannot find module 'express'",
        }),
    }))
    with pytest.raises(ConflictError) as ei:
        api.call(c.projects.start("prj_1", "node server.js"))
    assert ei.value.code == "app_failed"
    assert ei.value.body["log_tail"] == "Error: Cannot find module 'express'"


# --- me -----------------------------------------------------------------------

def test_me(api):
    c = api.client(_routes({("GET", "/me"): (200, {
        "account": {"name": "Northstar Labs", "email": "team@northstar.example", "plan": "pro"},
        "usage": {
            "spend_usd": 12.5, "included_credits_usd": 50, "budget_cap_usd": None,
            "bonus_usd": 0, "hard_limit_usd": 50, "overage_allowed": False,
            "blocked": False, "reason": None,
        },
        "authenticated_key": {"id": "key_1", "label": "ci"},
        "servers": 2, "projects": 5, "keys": 3,
    })}))
    me = api.call(c.me())
    assert api.last["method"] == "GET" and api.last["path"] == "/me"
    assert isinstance(me, Me)
    assert me.account["plan"] == "pro" and me.you is None
    assert (me.usage.spend_usd, me.usage.hard_limit_usd, me.usage.blocked) == (12.5, 50, False)
    assert (me.servers, me.projects, me.keys) == (2, 5, 3)
    assert me.authenticated_key == {"id": "key_1", "label": "ci"}


# --- drift fixed in existing methods ------------------------------------------

def test_deployments_parse_with_project_id(api):
    # The API added project_id to each deployment, which used to raise TypeError.
    c = api.client(_routes({("GET", "/servers/srv_1/deployments"): (200, {"deployments": [
        {"url": "https://web-abc.apps.server4agent.com", "live": True, "project_id": "prj_1"},
    ]})}))
    deps = api.call(c.servers.list_deployments("srv_1"))
    assert (deps[0].url, deps[0].live, deps[0].project_id) == ("https://web-abc.apps.server4agent.com", True, "prj_1")


def test_deploy_takes_a_project_and_still_returns_the_url(api):
    c = api.client(_routes({
        ("GET", "/servers/srv_1"): (200, {"server": _server_raw()}),
        ("GET", "/projects/prj_1"): (200, _project_raw()),
        ("POST", "/servers/srv_1/deployments"): (201, {
            "url": "https://web-abc.apps.server4agent.com", "status": "publishing", "project_id": "prj_1",
        }),
    }))
    server = api.call(c.servers.get("srv_1"))
    url = api.call(server.deploy(project_id="prj_1"))
    assert api.last["body"] == {"project_id": "prj_1"}
    # Still the URL str it always was, now also saying how the deploy went.
    assert isinstance(url, str) and url == "https://web-abc.apps.server4agent.com"
    assert isinstance(url, DeployResult)
    assert (url.url, url.status, url.project_id) == (url, "publishing", "prj_1")

    api.call(c.servers.deploy("srv_1"))
    assert api.last["body"] is None  # a server with one project deploys that one

    project = api.call(c.projects.get("prj_1"))
    api.call(project.deploy())
    assert api.last["path"] == "/servers/srv_1/deployments" and api.last["body"] == {"project_id": "prj_1"}


def test_deploy_refusal_is_a_conflict(api):
    c = api.client(_routes({("POST", "/servers/srv_1/deployments"): (409, {
        "error": "not_listening", "message": "Nothing is listening on port 3000. Start the app first.",
    })}))
    with pytest.raises(ConflictError) as ei:
        api.call(c.servers.deploy("srv_1"))
    assert ei.value.code == "not_listening"


def test_webhooks_create_without_events_omits_them(api):
    # Sending "events": null was refused with 400 "events must be an array".
    c = api.client(_routes({("POST", "/webhooks"): (201, {
        "id": "wh_1", "url": "https://hooks.northstar.example/s4a", "events": list(WEBHOOK_EVENTS),
        "created_at": "t", "secret": "whsec_x",
    })}))
    api.call(c.webhooks.create("https://hooks.northstar.example/s4a"))
    assert api.last["body"] == {"url": "https://hooks.northstar.example/s4a"}
    assert "quota.egress_cap_reached" in WEBHOOK_EVENTS
    assert "tuning.started" not in WEBHOOK_EVENTS and "tuning.completed" not in WEBHOOK_EVENTS


def test_exec_in_a_project_folder(api):
    c = api.client(_routes({
        ("GET", "/servers/srv_1"): (200, {"server": _server_raw()}),
        ("POST", "/servers/srv_1/exec"): (200, {"stdout": "ok\n"}),
    }))
    server = api.call(c.servers.get("srv_1"))
    assert api.call(server.exec("ls", project_id="prj_1")) == "ok\n"
    assert api.last["body"] == {"command": "ls", "project_id": "prj_1"}
    api.call(c.servers.exec("srv_1", "ls"))
    assert api.last["body"] == {"command": "ls"}


def test_tasks_create_in_a_project_and_as_a_follow_up(api):
    c = api.client(_routes({("POST", "/servers/srv_1/tasks"): (201, {
        "task_id": "tsk_2", "status": "running", "project_id": "prj_1",
        "follow_up_of": "tsk_1", "resumed": False,
    })}))
    task = api.call(c.tasks.create("srv_1", "now add a footer", follow_up_of="tsk_1"))
    assert api.last["body"] == {"goal": "now add a footer", "follow_up_of": "tsk_1"}
    assert (task.id, task.project_id, task.follow_up_of, task.resumed) == ("tsk_2", "prj_1", "tsk_1", False)

    api.call(c.tasks.create("srv_1", "build it", project_id="prj_1"))
    assert api.last["body"] == {"goal": "build it", "project_id": "prj_1"}


def test_task_reports_project_log_change_and_usage(api):
    change = {"commit": "abc123", "filesChanged": 1, "insertions": 3, "deletions": 0, "files": ["index.html"]}
    usage = {"cost": 0.12, "inputTokens": 10, "outputTokens": 5, "cacheReadTokens": 0,
             "cacheWriteTokens": 0, "turns": 2, "durationMs": 900}
    c = api.client(_routes({("GET", "/servers/srv_1/tasks/tsk_1"): (200, {"task": {
        "id": "tsk_1", "serverId": "srv_1", "projectId": "prj_1", "goal": "x", "messageType": "user",
        "status": "completed", "createdAt": "t", "log": "done", "change": change, "usage": usage,
    }})}))
    task = api.call(c.tasks.get("srv_1", "tsk_1"))
    assert (task.project_id, task.log, task.change, task.usage) == ("prj_1", "done", change, usage)
    assert task.follow_up_of is None and task.resumed is None


def test_servers_create_binds_secrets(api):
    c = api.client(_routes({("POST", "/servers"): (201, {"id": "srv_1", "url": None, "status": "running", "tier": "small"})}))
    api.call(c.servers.create(tier="small", secrets=[SecretInput("OPENAI_KEY", "sk-x")]))
    assert api.last["body"] == {"tier": "small", "secrets": [{"key": "OPENAI_KEY", "value": "sk-x"}]}


def test_projects_create_from_a_source_with_secrets(api):
    created = _project_raw(
        source={"kind": "git", "git_url": "https://github.com/northstar/app"},
        init_phase="seeding", init_run_id="tsk_3",
    )
    c = api.client(_routes({("POST", "/servers/srv_1/projects"): (201, created)}))
    project = api.call(c.projects.create(
        "srv_1",
        source={"kind": "git", "git_url": "https://github.com/northstar/app"},
        auto_init=True,
        secrets=[{"key": "DATABASE_URL", "value": "postgres://x"}],
    ))
    assert api.last["body"] == {
        "source": {"kind": "git", "git_url": "https://github.com/northstar/app"},
        "auto_init": True,
        "secrets": [{"key": "DATABASE_URL", "value": "postgres://x"}],
    }
    assert project.source["kind"] == "git"
    assert (project.init_phase, project.init_run_id) == ("seeding", "tsk_3")


def test_project_update_run_plan_and_refresh_keeps_init_run_id(api):
    plan = {"stack": "Node 20", "install": "npm ci", "start": "node server.js", "port": 3000, "env": []}
    c = api.client(_routes({
        ("POST", "/servers/srv_1/projects"): (201, _project_raw(init_run_id="tsk_3")),
        ("PATCH", "/projects/prj_1"): (200, _project_raw(run_plan=plan)),
    }))
    project = api.call(c.projects.create("srv_1", name="web"))
    api.call(project.update(run_plan=plan))
    assert api.last["method"] == "PATCH" and api.last["body"] == {"run_plan": plan}
    assert project.run_plan == plan
    assert project.init_run_id == "tsk_3"  # only the create response carries it


def test_project_access_password_goes_out_as_access_password(api):
    c = api.client(_routes({
        ("POST", "/servers/srv_1/projects"): (201, _project_raw(access="password")),
        ("PATCH", "/projects/prj_1"): (200, _project_raw(access="password")),
    }))
    project = api.call(c.projects.create(
        "srv_1", name="web", access="password", access_password="correct horse battery",
    ))
    assert api.last["body"] == {
        "name": "web", "access": "password", "access_password": "correct horse battery",
    }
    assert project.access == "password"
    api.call(project.update(access_password="a new passphrase"))
    assert api.last["method"] == "PATCH" and api.last["body"] == {"access_password": "a new passphrase"}


def test_template_source_kind_and_failed_build_steps(api):
    c = api.client(_routes({
        ("GET", "/templates"): (200, {"templates": [{
            "id": "git", "label": "From git", "description": "d", "source_kind": "git",
            "defaults": {"name": "n", "description": "d", "visibility": "private", "lifecycle": "persistent"},
        }]}),
        ("GET", "/servers/srv_1/builds/bld_1"): (200, {"build": {
            "id": "bld_1", "serverId": "srv_1", "goal": "x", "status": "completed",
            "steps": [{"key": "plan", "label": "Plan", "status": "failed"}],
            "artifacts": [], "url": None, "createdAt": "",
        }}),
    }))
    assert api.call(c.templates.list())[0].source_kind == "git"
    assert api.call(c.builds.get("srv_1", "bld_1")).steps[0].status == "failed"


def test_error_body_is_kept_whole():
    def handler(req):
        return httpx.Response(413, json={"error": "write_failed", "reason": "too_large", "message": "too big"})

    with pytest.raises(APIStatusError) as ei:
        FilesResource(transport(handler, max_retries=0)).write("srv_1", "big.bin", b"x")
    assert ei.value.body["reason"] == "too_large"
