import asyncio
import json

import httpx
import pytest

from server4agent import (
    APIStatusError,
    AsyncServer4Agent,
    NotFoundError,
    Server,
    Server4Agent,
    Server4AgentError,
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
