from __future__ import annotations

import asyncio
import base64
import os
import time
from typing import Any, Awaitable, Callable, List, Mapping, Optional, Sequence, Union

from .. import _transport as _t
from .._models import (
    ApiKey,
    AppStart,
    BUILD_TERMINAL_STATES,
    CreatedApiKey,
    CreatedWebhook,
    DeployResult,
    Deployment,
    LogsSnapshot,
    Me,
    ProjectExport,
    ProjectImport,
    ProjectInit,
    ProjectTemplate,
    Secret,
    SecretsArg,
    ServerAction,
    ServerKind,
    ServerTier,
    TASK_TERMINAL_STATES,
    Webhook,
    _Record,
    bundle_b64,
    parse_build,
    parse_project,
    parse_server,
    parse_task,
    secret_inputs,
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
    kind: ServerKind
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

    @property
    def secrets(self) -> "_ScopedSecrets":
        return _ScopedSecrets(self._t, f"/servers/{_seg(self.id)}/secrets")

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

    async def exec(self, command: str, *, project_id: Optional[str] = None) -> str:
        body = {k: v for k, v in {"command": command, "project_id": project_id}.items() if v is not None}
        r = await self._t.request("POST", f"/servers/{_seg(self.id)}/exec", body=body)
        return r["stdout"]

    async def deploy(self, *, project_id: Optional[str] = None) -> DeployResult:
        """Put a running project online. Raises ``ConflictError`` when nothing
        in the project is listening."""
        return await _deploy(self._t, self.id, project_id)

    async def deployments(self) -> List[Deployment]:
        r = await self._t.request("GET", f"/servers/{_seg(self.id)}/deployments")
        return [Deployment.parse(d) for d in r["deployments"]]

    async def logs(
        self, *, project_id: Optional[str] = None, lines: Optional[int] = None
    ) -> Optional[LogsSnapshot]:
        return await _logs(self._t, self.id, project_id, lines)


class AsyncProject(_Record):
    id: str
    server_id: str
    name: str
    slug: str
    description: Optional[str]
    visibility: str
    #: Who may open the URL: "public" (anyone with the link) or "team".
    access: str
    lifecycle: str
    runtime_status: str
    template: Optional[str]
    source: Optional[dict]
    init_phase: Optional[str]
    run_plan: Optional[dict]
    url: Optional[str]
    created_at: str
    updated_at: str
    #: The bring-up run this handle started, from ``create()``, ``import_bundle()``
    #: or ``init()``. None otherwise; a refresh does not change it.
    init_run_id: Optional[str]

    def __init__(self, transport: AsyncTransport, raw: dict) -> None:
        self._t = transport
        self.__dict__.update(parse_project(raw))
        self.init_run_id = raw.get("init_run_id")

    @property
    def secrets(self) -> "_ScopedSecrets":
        return _ScopedSecrets(self._t, f"/projects/{_seg(self.id)}/secrets")

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
        access: Optional[str] = None,
        access_password: Optional[str] = None,
        lifecycle: Optional[str] = None,
        run_plan: Optional[Mapping[str, Any]] = None,
    ) -> "AsyncProject":
        body = {
            k: v
            for k, v in {
                "name": name,
                "description": description,
                "visibility": visibility,
                "access": access,
                "access_password": access_password,
                "lifecycle": lifecycle,
                "run_plan": dict(run_plan) if run_plan is not None else None,
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

    async def init(self, *, proceed: Optional[bool] = None) -> ProjectInit:
        """Start or resume bring-up. Updates ``init_phase`` and ``init_run_id``."""
        result = await _init_project(self._t, self.id, proceed)
        self.init_run_id = result.init_run_id
        self.init_phase = result.init_phase
        return result

    async def deploy(self) -> DeployResult:
        return await _deploy(self._t, self.server_id, self.id)

    async def logs(self, *, lines: Optional[int] = None) -> Optional[LogsSnapshot]:
        return await _logs(self._t, self.server_id, self.id, lines)

    async def start(self, command: str, *, name: Optional[str] = None) -> AppStart:
        """Start the project's app (``name`` defaults to ``"web"``) and keep it running."""
        return await _start_app(self._t, self.id, command, name)

    async def export(self, *, ttl_seconds: Optional[float] = None) -> ProjectExport:
        return await _export_project(self._t, self.id, ttl_seconds)


class AsyncTask(_Record):
    id: str
    server_id: str
    project_id: Optional[str]
    #: What the task is doing. Yours verbatim; for a ``message_type == "system"``
    #: task (e.g. a project bring-up) a summary of the run, not the agent's
    #: instructions.
    goal: str
    #: ``"user"`` for a task you created, ``"system"`` for one the platform
    #: started for you.
    message_type: str
    status: str
    created_at: str
    result: Optional[str]
    question: Optional[str]
    log: Optional[str]
    change: Optional[dict]
    usage: Optional[dict]
    follow_up_of: Optional[str]
    resumed: Optional[bool]

    def __init__(self, transport: AsyncTransport, raw: dict) -> None:
        self._t = transport
        self.__dict__.update(parse_task(raw))
        self.follow_up_of = raw.get("follow_up_of")
        self.resumed = raw.get("resumed")

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
        access: Optional[str] = None,
        access_password: Optional[str] = None,
        lifecycle: Optional[str] = None,
        template: Optional[str] = None,
        source: Optional[Mapping[str, Any]] = None,
        auto_init: Optional[bool] = None,
        secrets: Optional[SecretsArg] = None,
    ) -> AsyncProject:
        body = {
            k: v
            for k, v in {
                "name": name,
                "description": description,
                "visibility": visibility,
                "access": access,
                "access_password": access_password,
                "lifecycle": lifecycle,
                "template": template,
                "source": dict(source) if source is not None else None,
                "auto_init": auto_init,
                "secrets": secret_inputs(secrets) if secrets is not None else None,
            }.items()
            if v is not None
        }
        r = await self._t.request("POST", f"/servers/{_seg(self._sid)}/projects", body=body)
        return AsyncProject(self._t, r)

    async def import_bundle(
        self, bundle: Union[bytes, str], *, name: Optional[str] = None
    ) -> ProjectImport[AsyncProject]:
        """Recreate a project from an export bundle (its bytes, or base64 str)."""
        body = {"bundle": bundle_b64(bundle)}
        if name is not None:
            body["name"] = name
        r = await self._t.request("POST", f"/servers/{_seg(self._sid)}/projects/import", body=body)
        return ProjectImport.parse(AsyncProject(self._t, r), r)


class _ScopedTasks:
    def __init__(self, transport: AsyncTransport, server_id: str) -> None:
        self._t = transport
        self._sid = server_id

    async def create(
        self, goal: str, *, project_id: Optional[str] = None, follow_up_of: Optional[str] = None
    ) -> AsyncTask:
        body = {
            k: v
            for k, v in {"goal": goal, "project_id": project_id, "follow_up_of": follow_up_of}.items()
            if v is not None
        }
        r = await self._t.request("POST", f"/servers/{_seg(self._sid)}/tasks", body=body)
        return AsyncTask(self._t, {"id": r["task_id"], "serverId": self._sid, "projectId": r.get("project_id", project_id), "goal": goal, "messageType": "user", "status": r["status"], "createdAt": r.get("createdAt", ""), "follow_up_of": r.get("follow_up_of"), "resumed": r.get("resumed")})

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

    async def read_binary(self, path: str) -> bytes:
        """Read a binary file (e.g. a generated .xlsx) without corrupting its bytes."""
        r = await self._t.request(
            "GET",
            f"/servers/{_seg(self._sid)}/files",
            query={"path": path, "encoding": "base64"},
        )
        return base64.b64decode(r["content"])

    async def write(self, path: str, content: "str | bytes") -> None:
        """`content` may be a str (written as UTF-8) or raw bytes (written as-is, base64 on the wire)."""
        if isinstance(content, bytes):
            body = {"path": path, "content": base64.b64encode(content).decode("ascii"), "encoding": "base64"}
        else:
            body = {"path": path, "content": content}
        await self._t.request("PUT", f"/servers/{_seg(self._sid)}/files", body=body)

    async def delete(self, path: str) -> None:
        await self._t.request("DELETE", f"/servers/{_seg(self._sid)}/files", query={"path": path})

    async def get_download_url(self, path: str, *, ttl_seconds: Optional[int] = None) -> dict:
        """Mint a signed, expiring link anyone can use to download this one file — no login required.

        Returns {"path": str, "url": str, "expires_at": str}.
        """
        query: dict = {"path": path}
        if ttl_seconds is not None:
            query["ttl_seconds"] = ttl_seconds
        r = await self._t.request(
            "GET", f"/servers/{_seg(self._sid)}/files/download-url", query=query
        )
        return {"path": r.get("path", path), "url": r["url"], "expires_at": r["expires_at"]}


class _ScopedSecrets:
    """Secrets seen from one server or one project. Values are write-only."""

    def __init__(self, transport: AsyncTransport, path: str) -> None:
        self._t = transport
        self._path = path

    async def list(self) -> List[Secret]:
        r = await self._t.request("GET", self._path)
        return [Secret.parse(s) for s in r["secrets"]]

    async def set(self, secrets: SecretsArg) -> List[Secret]:
        """Set each secret and bind it here. Returns everything in scope after."""
        r = await self._t.request("PUT", self._path, body={"secrets": secret_inputs(secrets)})
        return [Secret.parse(s) for s in r["secrets"]]


async def _init_project(t: AsyncTransport, project_id: str, proceed: Optional[bool]) -> ProjectInit:
    body = {"proceed": proceed} if proceed is not None else None
    r = await t.request("POST", f"/projects/{_seg(project_id)}/init", body=body)
    return ProjectInit.parse(r)


async def _export_project(
    t: AsyncTransport, project_id: str, ttl_seconds: Optional[float]
) -> ProjectExport:
    body = {"ttl_seconds": ttl_seconds} if ttl_seconds is not None else None
    r = await t.request("POST", f"/projects/{_seg(project_id)}/export", body=body)
    return ProjectExport.parse(r)


async def _start_app(t: AsyncTransport, project_id: str, command: str, name: Optional[str]) -> AppStart:
    body = {"command": command}
    if name is not None:
        body["name"] = name
    r = await t.request("POST", f"/projects/{_seg(project_id)}/start", body=body)
    return AppStart.parse(r)


async def _deploy(t: AsyncTransport, server_id: str, project_id: Optional[str]) -> DeployResult:
    body = {"project_id": project_id} if project_id is not None else None
    r = await t.request("POST", f"/servers/{_seg(server_id)}/deployments", body=body)
    return DeployResult.parse(r)


async def _logs(
    t: AsyncTransport, server_id: str, project_id: Optional[str], lines: Optional[int]
) -> Optional[LogsSnapshot]:
    r = await t.request(
        "GET", f"/servers/{_seg(server_id)}/logs", query={"project_id": project_id, "lines": lines}
    )
    return LogsSnapshot.parse(r["logs"]) if r.get("logs") is not None else None


# =============================================================================
# Top-level resources
# =============================================================================

class ServersResource:
    def __init__(self, transport: AsyncTransport) -> None:
        self._t = transport

    async def create(
        self,
        *,
        task: Optional[str] = None,
        tier: Optional[ServerTier] = None,
        kind: Optional[ServerKind] = None,
        region: Optional[str] = None,
        secrets: Optional[SecretsArg] = None,
    ) -> AsyncServer:
        body = {
            k: v
            for k, v in {
                "task": task,
                "tier": tier,
                "kind": kind,
                "region": region,
                "secrets": secret_inputs(secrets) if secrets is not None else None,
            }.items()
            if v is not None
        }
        r = await self._t.request("POST", "/servers", body=body)
        return AsyncServer(self._t, {
            "id": r["id"], "task": task or "", "tier": r["tier"], "status": r["status"],
            # Prefer what the control plane says it built; fall back to what was
            # asked for, since an older one answers without a kind at all.
            "kind": r.get("kind", kind or "standard"),
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

    async def exec(self, server_id: str, command: str, *, project_id: Optional[str] = None) -> str:
        body = {k: v for k, v in {"command": command, "project_id": project_id}.items() if v is not None}
        r = await self._t.request("POST", f"/servers/{_seg(server_id)}/exec", body=body)
        return r["stdout"]

    async def deploy(self, server_id: str, *, project_id: Optional[str] = None) -> DeployResult:
        return await _deploy(self._t, server_id, project_id)

    async def list_deployments(self, server_id: str) -> List[Deployment]:
        r = await self._t.request("GET", f"/servers/{_seg(server_id)}/deployments")
        return [Deployment.parse(d) for d in r["deployments"]]

    async def logs(
        self, server_id: str, *, project_id: Optional[str] = None, lines: Optional[int] = None
    ) -> Optional[LogsSnapshot]:
        return await _logs(self._t, server_id, project_id, lines)


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

    async def import_bundle(
        self, server_id: str, bundle: Union[bytes, str], *, name: Optional[str] = None
    ) -> ProjectImport[AsyncProject]:
        return await _ScopedProjects(self._t, server_id).import_bundle(bundle, name=name)

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

    async def init(self, project_id: str, *, proceed: Optional[bool] = None) -> ProjectInit:
        return await _init_project(self._t, project_id, proceed)

    async def export(self, project_id: str, *, ttl_seconds: Optional[float] = None) -> ProjectExport:
        return await _export_project(self._t, project_id, ttl_seconds)

    async def start(self, project_id: str, command: str, *, name: Optional[str] = None) -> AppStart:
        return await _start_app(self._t, project_id, command, name)


class TemplatesResource:
    def __init__(self, transport: AsyncTransport) -> None:
        self._t = transport

    async def list(self) -> List[ProjectTemplate]:
        r = await self._t.request("GET", "/templates")
        return [ProjectTemplate.parse(x) for x in r["templates"]]


class TasksResource:
    def __init__(self, transport: AsyncTransport) -> None:
        self._t = transport

    async def create(
        self,
        server_id: str,
        goal: str,
        *,
        project_id: Optional[str] = None,
        follow_up_of: Optional[str] = None,
    ) -> AsyncTask:
        return await _ScopedTasks(self._t, server_id).create(
            goal, project_id=project_id, follow_up_of=follow_up_of
        )

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

    async def read_binary(self, server_id: str, path: str) -> bytes:
        return await _ScopedFiles(self._t, server_id).read_binary(path)

    async def write(self, server_id: str, path: str, content: "str | bytes") -> None:
        await _ScopedFiles(self._t, server_id).write(path, content)

    async def delete(self, server_id: str, path: str) -> None:
        await _ScopedFiles(self._t, server_id).delete(path)

    async def get_download_url(self, server_id: str, path: str, *, ttl_seconds: Optional[int] = None) -> dict:
        return await _ScopedFiles(self._t, server_id).get_download_url(path, ttl_seconds=ttl_seconds)


class SecretsResource:
    """The account's secrets. Values are write-only; reads never return them."""

    def __init__(self, transport: AsyncTransport) -> None:
        self._t = transport

    async def list(self) -> List[Secret]:
        r = await self._t.request("GET", "/secrets")
        return [Secret.parse(s) for s in r["secrets"]]

    async def set(self, key: str, value: str, *, description: Optional[str] = None) -> Secret:
        body = {"key": key, "value": value}
        if description is not None:
            body["description"] = description
        return Secret.parse(await self._t.request("PUT", "/secrets", body=body))

    async def delete(self, secret_id: str) -> None:
        await self._t.request("DELETE", f"/secrets/{_seg(secret_id)}")

    async def list_for_server(self, server_id: str) -> List[Secret]:
        return await _ScopedSecrets(self._t, f"/servers/{_seg(server_id)}/secrets").list()

    async def set_for_server(self, server_id: str, secrets: SecretsArg) -> List[Secret]:
        return await _ScopedSecrets(self._t, f"/servers/{_seg(server_id)}/secrets").set(secrets)

    async def list_for_project(self, project_id: str) -> List[Secret]:
        return await _ScopedSecrets(self._t, f"/projects/{_seg(project_id)}/secrets").list()

    async def set_for_project(self, project_id: str, secrets: SecretsArg) -> List[Secret]:
        return await _ScopedSecrets(self._t, f"/projects/{_seg(project_id)}/secrets").set(secrets)


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
        body: dict = {"url": url}
        if events is not None:
            body["events"] = list(events)
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
        self.secrets = SecretsResource(self._t)
        self.keys = KeysResource(self._t)
        self.webhooks = WebhooksResource(self._t)

    async def me(self) -> Me:
        return Me.parse(await self._t.request("GET", "/me"))

    async def aclose(self) -> None:
        await self._t.aclose()

    async def __aenter__(self) -> "AsyncServer4Agent":
        return self

    async def __aexit__(self, *exc) -> None:
        await self.aclose()
