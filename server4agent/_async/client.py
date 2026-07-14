from __future__ import annotations

import asyncio
import os
import time
from typing import Awaitable, Callable, List, Optional, Sequence, Union

from .. import _transport as _t
from .._models import (
    ApiKey,
    BUILD_TERMINAL_STATES,
    CreatedApiKey,
    CreatedWebhook,
    Deployment,
    ProjectTemplate,
    ServerAction,
    ServerTier,
    TASK_TERMINAL_STATES,
    Webhook,
    _Record,
    parse_build,
    parse_project,
    parse_server,
    parse_task,
)
from .._transport import quote_segment as _seg
from ..errors import APITimeoutError
from .transport import AsyncTransport


# =============================================================================
# Handles
# =============================================================================

class AsyncServer(_Record):
    id: str
    task: str
    tier: ServerTier
    status: str
    region: str
    credits_used: float
    storage_used_gb: float
    public_url: Optional[str]
    created_at: Optional[str]

    def __init__(self, transport: AsyncTransport, raw: dict) -> None:
        self._t = transport
        self.__dict__.update(parse_server(raw))

    @property
    def projects(self) -> "_ScopedProjects":
        return _ScopedProjects(self._t, self.id)

    @property
    def tasks(self) -> "_ScopedTasks":
        return _ScopedTasks(self._t, self.id)

    @property
    def builds(self) -> "_ScopedBuilds":
        return _ScopedBuilds(self._t, self.id)

    @property
    def files(self) -> "_ScopedFiles":
        return _ScopedFiles(self._t, self.id)

    async def refresh(self) -> "AsyncServer":
        r = await self._t.request("GET", f"/servers/{_seg(self.id)}")
        self.__dict__.update(parse_server(r["server"]))
        return self

    async def update(self, *, task: Optional[str] = None, region: Optional[str] = None) -> "AsyncServer":
        body = {k: v for k, v in {"task": task, "region": region}.items() if v is not None}
        r = await self._t.request("PATCH", f"/servers/{_seg(self.id)}", body=body)
        self.__dict__.update(parse_server(r["server"]))
        return self

    async def delete(self) -> None:
        await self._t.request("DELETE", f"/servers/{_seg(self.id)}")

    async def control(self, action: ServerAction) -> dict:
        return await self._t.request("POST", f"/servers/{_seg(self.id)}/actions", body={"action": action})

    async def start(self) -> dict:
        return await self.control("start")

    async def stop(self) -> dict:
        return await self.control("stop")

    async def restart(self) -> dict:
        return await self.control("restart")

    async def exec(self, command: str) -> str:
        r = await self._t.request("POST", f"/servers/{_seg(self.id)}/exec", body={"command": command})
        return r["stdout"]

    async def deploy(self) -> str:
        r = await self._t.request("POST", f"/servers/{_seg(self.id)}/deployments")
        return r["url"]

    async def deployments(self) -> List[Deployment]:
        r = await self._t.request("GET", f"/servers/{_seg(self.id)}/deployments")
        return [Deployment(**d) for d in r["deployments"]]


class AsyncProject(_Record):
    id: str
    server_id: str
    name: str
    slug: str
    description: Optional[str]
    visibility: str
    lifecycle: str
    runtime_status: str
    template: Optional[str]
    url: Optional[str]
    created_at: str
    updated_at: str

    def __init__(self, transport: AsyncTransport, raw: dict) -> None:
        self._t = transport
        self.__dict__.update(parse_project(raw))

    async def refresh(self) -> "AsyncProject":
        r = await self._t.request("GET", f"/projects/{_seg(self.id)}")
        self.__dict__.update(parse_project(r))
        return self

    async def update(
        self,
        *,
        name: Optional[str] = None,
        description: Optional[str] = None,
        visibility: Optional[str] = None,
        lifecycle: Optional[str] = None,
    ) -> "AsyncProject":
        body = {
            k: v
            for k, v in {
                "name": name,
                "description": description,
                "visibility": visibility,
                "lifecycle": lifecycle,
            }.items()
            if v is not None
        }
        r = await self._t.request("PATCH", f"/projects/{_seg(self.id)}", body=body)
        self.__dict__.update(parse_project(r))
        return self

    async def delete(self) -> None:
        await self._t.request("DELETE", f"/projects/{_seg(self.id)}")

    async def cleanup(self) -> None:
        await self._t.request("POST", f"/projects/{_seg(self.id)}/cleanup")


class AsyncTask(_Record):
    id: str
    server_id: str
    goal: str
    status: str
    created_at: str
    result: Optional[str]
    question: Optional[str]

    def __init__(self, transport: AsyncTransport, raw: dict) -> None:
        self._t = transport
        self.__dict__.update(parse_task(raw))

    async def refresh(self) -> "AsyncTask":
        r = await self._t.request("GET", f"/servers/{_seg(self.server_id)}/tasks/{_seg(self.id)}")
        self.__dict__.update(parse_task(r["task"]))
        return self

    async def wait(
        self,
        *,
        poll_interval: float = 2.0,
        timeout: float = 300.0,
        on_poll: Optional[Callable[["AsyncTask"], Union[None, Awaitable[None]]]] = None,
    ) -> "AsyncTask":
        """Await until the task reaches a terminal state. ``on_poll`` (sync or
        async) is called with ``self`` after each refresh."""
        deadline = time.monotonic() + timeout
        while self.status not in TASK_TERMINAL_STATES:
            if time.monotonic() >= deadline:
                raise APITimeoutError(f"Task {self.id} still {self.status!r} after {timeout}s.")
            await asyncio.sleep(poll_interval)
            await self.refresh()
            if on_poll is not None:
                res = on_poll(self)
                if res is not None:
                    await res
        return self


class AsyncBuild(_Record):
    id: str
    server_id: str
    goal: str
    status: str
    steps: list
    artifacts: list
    url: Optional[str]
    created_at: str

    def __init__(self, transport: AsyncTransport, raw: dict) -> None:
        self._t = transport
        self.__dict__.update(parse_build(raw))

    async def refresh(self) -> "AsyncBuild":
        r = await self._t.request("GET", f"/servers/{_seg(self.server_id)}/builds/{_seg(self.id)}")
        self.__dict__.update(parse_build(r["build"]))
        return self

    async def wait(
        self,
        *,
        poll_interval: float = 2.0,
        timeout: float = 600.0,
        on_poll: Optional[Callable[["AsyncBuild"], Union[None, Awaitable[None]]]] = None,
    ) -> "AsyncBuild":
        """Await until the build completes. ``on_poll`` (sync or async) is called
        with ``self`` after each refresh."""
        deadline = time.monotonic() + timeout
        while self.status not in BUILD_TERMINAL_STATES:
            if time.monotonic() >= deadline:
                raise APITimeoutError(f"Build {self.id} still {self.status!r} after {timeout}s.")
            await asyncio.sleep(poll_interval)
            await self.refresh()
            if on_poll is not None:
                res = on_poll(self)
                if res is not None:
                    await res
        return self


# =============================================================================
# Scoped sub-resources
# =============================================================================

class _ScopedProjects:
    def __init__(self, transport: AsyncTransport, server_id: str) -> None:
        self._t = transport
        self._sid = server_id

    async def list(self) -> List[AsyncProject]:
        r = await self._t.request("GET", f"/servers/{_seg(self._sid)}/projects")
        return [AsyncProject(self._t, p) for p in r["projects"]]

    async def create(
        self,
        *,
        name: Optional[str] = None,
        description: Optional[str] = None,
        visibility: Optional[str] = None,
        lifecycle: Optional[str] = None,
        template: Optional[str] = None,
    ) -> AsyncProject:
        body = {
            k: v
            for k, v in {
                "name": name,
                "description": description,
                "visibility": visibility,
                "lifecycle": lifecycle,
                "template": template,
            }.items()
            if v is not None
        }
        r = await self._t.request("POST", f"/servers/{_seg(self._sid)}/projects", body=body)
        return AsyncProject(self._t, r)


class _ScopedTasks:
    def __init__(self, transport: AsyncTransport, server_id: str) -> None:
        self._t = transport
        self._sid = server_id

    async def create(self, goal: str) -> AsyncTask:
        r = await self._t.request("POST", f"/servers/{_seg(self._sid)}/tasks", body={"goal": goal})
        return AsyncTask(self._t, {"id": r["task_id"], "serverId": self._sid, "goal": goal, "status": r["status"], "createdAt": r.get("createdAt", "")})

    async def list(self) -> List[AsyncTask]:
        r = await self._t.request("GET", f"/servers/{_seg(self._sid)}/tasks")
        return [AsyncTask(self._t, x) for x in r["tasks"]]

    async def get(self, task_id: str) -> AsyncTask:
        r = await self._t.request("GET", f"/servers/{_seg(self._sid)}/tasks/{_seg(task_id)}")
        return AsyncTask(self._t, r["task"])


class _ScopedBuilds:
    def __init__(self, transport: AsyncTransport, server_id: str) -> None:
        self._t = transport
        self._sid = server_id

    async def start(self, goal: str) -> AsyncBuild:
        r = await self._t.request("POST", f"/servers/{_seg(self._sid)}/builds", body={"goal": goal})
        return AsyncBuild(self._t, {"id": r["build_id"], "serverId": self._sid, "goal": goal, "status": r["status"], "steps": r.get("steps", []), "createdAt": r.get("createdAt", "")})

    async def list(self) -> List[AsyncBuild]:
        r = await self._t.request("GET", f"/servers/{_seg(self._sid)}/builds")
        return [AsyncBuild(self._t, x) for x in r["builds"]]

    async def get(self, build_id: str) -> AsyncBuild:
        r = await self._t.request("GET", f"/servers/{_seg(self._sid)}/builds/{_seg(build_id)}")
        return AsyncBuild(self._t, r["build"])


class _ScopedFiles:
    def __init__(self, transport: AsyncTransport, server_id: str) -> None:
        self._t = transport
        self._sid = server_id

    async def list(self) -> List[str]:
        r = await self._t.request("GET", f"/servers/{_seg(self._sid)}/files")
        return r["files"]

    async def read(self, path: str) -> str:
        r = await self._t.request("GET", f"/servers/{_seg(self._sid)}/files", query={"path": path})
        return r["content"]

    async def write(self, path: str, content: str) -> None:
        await self._t.request("PUT", f"/servers/{_seg(self._sid)}/files", body={"path": path, "content": content})

    async def delete(self, path: str) -> None:
        await self._t.request("DELETE", f"/servers/{_seg(self._sid)}/files", query={"path": path})


# =============================================================================
# Top-level resources
# =============================================================================

class ServersResource:
    def __init__(self, transport: AsyncTransport) -> None:
        self._t = transport

    async def create(
        self, *, task: Optional[str] = None, tier: Optional[ServerTier] = None, region: Optional[str] = None
    ) -> AsyncServer:
        body = {k: v for k, v in {"task": task, "tier": tier, "region": region}.items() if v is not None}
        r = await self._t.request("POST", "/servers", body=body)
        return AsyncServer(self._t, {
            "id": r["id"], "task": task or "", "tier": r["tier"], "status": r["status"],
            "region": region or "", "publicUrl": r.get("url"),
        })

    async def list(self) -> List[AsyncServer]:
        r = await self._t.request("GET", "/servers")
        return [AsyncServer(self._t, s) for s in r["servers"]]

    async def get(self, server_id: str) -> AsyncServer:
        r = await self._t.request("GET", f"/servers/{_seg(server_id)}")
        return AsyncServer(self._t, r["server"])

    async def update(self, server_id: str, *, task: Optional[str] = None, region: Optional[str] = None) -> AsyncServer:
        body = {k: v for k, v in {"task": task, "region": region}.items() if v is not None}
        r = await self._t.request("PATCH", f"/servers/{_seg(server_id)}", body=body)
        return AsyncServer(self._t, r["server"])

    async def delete(self, server_id: str) -> None:
        await self._t.request("DELETE", f"/servers/{_seg(server_id)}")

    async def control(self, server_id: str, action: ServerAction) -> dict:
        return await self._t.request("POST", f"/servers/{_seg(server_id)}/actions", body={"action": action})

    async def exec(self, server_id: str, command: str) -> str:
        r = await self._t.request("POST", f"/servers/{_seg(server_id)}/exec", body={"command": command})
        return r["stdout"]

    async def deploy(self, server_id: str) -> str:
        r = await self._t.request("POST", f"/servers/{_seg(server_id)}/deployments")
        return r["url"]

    async def list_deployments(self, server_id: str) -> List[Deployment]:
        r = await self._t.request("GET", f"/servers/{_seg(server_id)}/deployments")
        return [Deployment(**d) for d in r["deployments"]]


class ProjectsResource:
    def __init__(self, transport: AsyncTransport) -> None:
        self._t = transport

    async def list(self) -> List[AsyncProject]:
        r = await self._t.request("GET", "/projects")
        return [AsyncProject(self._t, p) for p in r["projects"]]

    async def list_for_server(self, server_id: str) -> List[AsyncProject]:
        return await _ScopedProjects(self._t, server_id).list()

    async def create(self, server_id: str, **opts) -> AsyncProject:
        return await _ScopedProjects(self._t, server_id).create(**opts)

    async def get(self, project_id: str) -> AsyncProject:
        r = await self._t.request("GET", f"/projects/{_seg(project_id)}")
        return AsyncProject(self._t, r)

    async def update(self, project_id: str, **patch) -> AsyncProject:
        body = {k: v for k, v in patch.items() if v is not None}
        r = await self._t.request("PATCH", f"/projects/{_seg(project_id)}", body=body)
        return AsyncProject(self._t, r)

    async def delete(self, project_id: str) -> None:
        await self._t.request("DELETE", f"/projects/{_seg(project_id)}")

    async def cleanup(self, project_id: str) -> None:
        await self._t.request("POST", f"/projects/{_seg(project_id)}/cleanup")


class TemplatesResource:
    def __init__(self, transport: AsyncTransport) -> None:
        self._t = transport

    async def list(self) -> List[ProjectTemplate]:
        r = await self._t.request("GET", "/templates")
        return [ProjectTemplate.parse(x) for x in r["templates"]]


class TasksResource:
    def __init__(self, transport: AsyncTransport) -> None:
        self._t = transport

    async def create(self, server_id: str, goal: str) -> AsyncTask:
        return await _ScopedTasks(self._t, server_id).create(goal)

    async def list(self, server_id: str) -> List[AsyncTask]:
        return await _ScopedTasks(self._t, server_id).list()

    async def get(self, server_id: str, task_id: str) -> AsyncTask:
        return await _ScopedTasks(self._t, server_id).get(task_id)


class BuildsResource:
    def __init__(self, transport: AsyncTransport) -> None:
        self._t = transport

    async def start(self, server_id: str, goal: str) -> AsyncBuild:
        return await _ScopedBuilds(self._t, server_id).start(goal)

    async def list(self, server_id: str) -> List[AsyncBuild]:
        return await _ScopedBuilds(self._t, server_id).list()

    async def get(self, server_id: str, build_id: str) -> AsyncBuild:
        return await _ScopedBuilds(self._t, server_id).get(build_id)


class FilesResource:
    def __init__(self, transport: AsyncTransport) -> None:
        self._t = transport

    async def list(self, server_id: str) -> List[str]:
        return await _ScopedFiles(self._t, server_id).list()

    async def read(self, server_id: str, path: str) -> str:
        return await _ScopedFiles(self._t, server_id).read(path)

    async def write(self, server_id: str, path: str, content: str) -> None:
        await _ScopedFiles(self._t, server_id).write(path, content)

    async def delete(self, server_id: str, path: str) -> None:
        await _ScopedFiles(self._t, server_id).delete(path)


class KeysResource:
    def __init__(self, transport: AsyncTransport) -> None:
        self._t = transport

    async def list(self) -> List[ApiKey]:
        r = await self._t.request("GET", "/keys")
        return [ApiKey.parse(k) for k in r["keys"]]

    async def create(self, label: str) -> CreatedApiKey:
        r = await self._t.request("POST", "/keys", body={"label": label})
        base = ApiKey.parse(r)
        return CreatedApiKey(key=r["key"], **base.__dict__)

    async def update_scope(
        self,
        key_id: str,
        *,
        allowed_server_ids: Optional[Sequence[str]] = None,
        allowed_project_ids: Optional[Sequence[str]] = None,
    ) -> dict:
        body = {
            "allowed_server_ids": list(allowed_server_ids) if allowed_server_ids else [],
            "allowed_project_ids": list(allowed_project_ids) if allowed_project_ids else [],
        }
        return await self._t.request("PATCH", f"/keys/{_seg(key_id)}", body=body)

    async def revoke(self, key_id: str) -> None:
        await self._t.request("DELETE", f"/keys/{_seg(key_id)}")


class WebhooksResource:
    def __init__(self, transport: AsyncTransport) -> None:
        self._t = transport

    async def list(self) -> List[Webhook]:
        r = await self._t.request("GET", "/webhooks")
        return [Webhook.parse(w) for w in r["webhooks"]]

    async def create(self, url: str, events: Optional[Sequence[str]] = None) -> CreatedWebhook:
        body = {"url": url, "events": list(events) if events is not None else None}
        r = await self._t.request("POST", "/webhooks", body=body)
        base = Webhook.parse(r)
        return CreatedWebhook(secret=r["secret"], **base.__dict__)

    async def delete(self, webhook_id: str) -> None:
        await self._t.request("DELETE", f"/webhooks/{_seg(webhook_id)}")


class AsyncServer4Agent:
    """Async client for the Server4Agent REST API.

        import asyncio
        from server4agent import AsyncServer4Agent

        async def main():
            async with AsyncServer4Agent() as client:
                server = await client.servers.create(tier="small")
                build = await (await server.builds.start("a FastAPI todo API")).wait()
                print(build.url)

        asyncio.run(main())
    """

    def __init__(
        self,
        api_key: Optional[str] = None,
        *,
        base_url: str = _t.DEFAULT_BASE_URL,
        timeout: float = _t.DEFAULT_TIMEOUT,
        max_retries: int = _t.DEFAULT_MAX_RETRIES,
    ) -> None:
        key = api_key or os.environ.get("SERVER4AGENT_API_KEY")
        if not key:
            raise ValueError(
                "Server4Agent: no API key. Pass api_key=... or set SERVER4AGENT_API_KEY."
            )
        self._t = AsyncTransport(base_url, key, timeout=timeout, max_retries=max_retries)

        self.servers = ServersResource(self._t)
        self.projects = ProjectsResource(self._t)
        self.templates = TemplatesResource(self._t)
        self.tasks = TasksResource(self._t)
        self.builds = BuildsResource(self._t)
        self.files = FilesResource(self._t)
        self.keys = KeysResource(self._t)
        self.webhooks = WebhooksResource(self._t)

    async def aclose(self) -> None:
        await self._t.aclose()

    async def __aenter__(self) -> "AsyncServer4Agent":
        return self

    async def __aexit__(self, *exc) -> None:
        await self.aclose()
