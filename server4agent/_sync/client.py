from __future__ import annotations

import os
import time
from typing import Callable, List, Optional, Sequence

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
from .transport import SyncTransport


# =============================================================================
# Handles — returned by create()/get(); data records you can also act on.
# =============================================================================

class Server(_Record):
    id: str
    task: str
    tier: ServerTier
    status: str
    region: str
    credits_used: float
    storage_used_gb: float
    public_url: Optional[str]
    created_at: Optional[str]

    def __init__(self, transport: SyncTransport, raw: dict) -> None:
        self._t = transport
        self.__dict__.update(parse_server(raw))

    # scoped sub-resources (properties, so they stay out of repr) --------------
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

    # actions ------------------------------------------------------------------
    def refresh(self) -> "Server":
        r = self._t.request("GET", f"/servers/{_seg(self.id)}")
        self.__dict__.update(parse_server(r["server"]))
        return self

    def update(self, *, task: Optional[str] = None, region: Optional[str] = None) -> "Server":
        body = {k: v for k, v in {"task": task, "region": region}.items() if v is not None}
        r = self._t.request("PATCH", f"/servers/{_seg(self.id)}", body=body)
        self.__dict__.update(parse_server(r["server"]))
        return self

    def delete(self) -> None:
        self._t.request("DELETE", f"/servers/{_seg(self.id)}")

    def control(self, action: ServerAction) -> dict:
        return self._t.request("POST", f"/servers/{_seg(self.id)}/actions", body={"action": action})

    def start(self) -> dict:
        return self.control("start")

    def stop(self) -> dict:
        return self.control("stop")

    def restart(self) -> dict:
        return self.control("restart")

    def exec(self, command: str) -> str:
        """Run a shell command on the server. Returns stdout."""
        r = self._t.request("POST", f"/servers/{_seg(self.id)}/exec", body={"command": command})
        return r["stdout"]

    def deploy(self) -> str:
        """Publish the current workspace to a live public URL. Returns the URL."""
        r = self._t.request("POST", f"/servers/{_seg(self.id)}/deployments")
        return r["url"]

    def deployments(self) -> List[Deployment]:
        r = self._t.request("GET", f"/servers/{_seg(self.id)}/deployments")
        return [Deployment(**d) for d in r["deployments"]]


class Project(_Record):
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

    def __init__(self, transport: SyncTransport, raw: dict) -> None:
        self._t = transport
        self.__dict__.update(parse_project(raw))

    def refresh(self) -> "Project":
        r = self._t.request("GET", f"/projects/{_seg(self.id)}")
        self.__dict__.update(parse_project(r))
        return self

    def update(
        self,
        *,
        name: Optional[str] = None,
        description: Optional[str] = None,
        visibility: Optional[str] = None,
        lifecycle: Optional[str] = None,
    ) -> "Project":
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
        r = self._t.request("PATCH", f"/projects/{_seg(self.id)}", body=body)
        self.__dict__.update(parse_project(r))
        return self

    def delete(self) -> None:
        self._t.request("DELETE", f"/projects/{_seg(self.id)}")

    def cleanup(self) -> None:
        """Ephemeral projects only. Use ``delete()`` for persistent ones."""
        self._t.request("POST", f"/projects/{_seg(self.id)}/cleanup")


class Task(_Record):
    id: str
    server_id: str
    goal: str
    status: str
    created_at: str
    result: Optional[str]
    question: Optional[str]

    def __init__(self, transport: SyncTransport, raw: dict) -> None:
        self._t = transport
        self.__dict__.update(parse_task(raw))

    def refresh(self) -> "Task":
        r = self._t.request("GET", f"/servers/{_seg(self.server_id)}/tasks/{_seg(self.id)}")
        self.__dict__.update(parse_task(r["task"]))
        return self

    def wait(
        self,
        *,
        poll_interval: float = 2.0,
        timeout: float = 300.0,
        on_poll: Optional[Callable[["Task"], None]] = None,
    ) -> "Task":
        """Block until the task reaches a terminal state (done, failed, cancelled,
        or awaiting_input). Raises ``APITimeoutError`` if ``timeout`` elapses first.

        ``on_poll`` is called with ``self`` after each refresh — handy for
        streaming progress to a log or UI."""
        deadline = time.monotonic() + timeout
        while self.status not in TASK_TERMINAL_STATES:
            if time.monotonic() >= deadline:
                raise APITimeoutError(f"Task {self.id} still {self.status!r} after {timeout}s.")
            time.sleep(poll_interval)
            self.refresh()
            if on_poll is not None:
                on_poll(self)
        return self


class Build(_Record):
    id: str
    server_id: str
    goal: str
    status: str
    steps: list
    artifacts: list
    url: Optional[str]
    created_at: str

    def __init__(self, transport: SyncTransport, raw: dict) -> None:
        self._t = transport
        self.__dict__.update(parse_build(raw))

    def refresh(self) -> "Build":
        r = self._t.request("GET", f"/servers/{_seg(self.server_id)}/builds/{_seg(self.id)}")
        self.__dict__.update(parse_build(r["build"]))
        return self

    def wait(
        self,
        *,
        poll_interval: float = 2.0,
        timeout: float = 600.0,
        on_poll: Optional[Callable[["Build"], None]] = None,
    ) -> "Build":
        """Block until the build completes. Raises ``APITimeoutError`` on timeout.

        ``on_poll`` is called with ``self`` after each refresh — handy for
        streaming build-step progress."""
        deadline = time.monotonic() + timeout
        while self.status not in BUILD_TERMINAL_STATES:
            if time.monotonic() >= deadline:
                raise APITimeoutError(f"Build {self.id} still {self.status!r} after {timeout}s.")
            time.sleep(poll_interval)
            self.refresh()
            if on_poll is not None:
                on_poll(self)
        return self


# =============================================================================
# Scoped sub-resources — reached via a Server handle (server.tasks, ...).
# =============================================================================

class _ScopedProjects:
    def __init__(self, transport: SyncTransport, server_id: str) -> None:
        self._t = transport
        self._sid = server_id

    def list(self) -> List[Project]:
        r = self._t.request("GET", f"/servers/{_seg(self._sid)}/projects")
        return [Project(self._t, p) for p in r["projects"]]

    def create(
        self,
        *,
        name: Optional[str] = None,
        description: Optional[str] = None,
        visibility: Optional[str] = None,
        lifecycle: Optional[str] = None,
        template: Optional[str] = None,
    ) -> Project:
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
        r = self._t.request("POST", f"/servers/{_seg(self._sid)}/projects", body=body)
        return Project(self._t, r)


class _ScopedTasks:
    def __init__(self, transport: SyncTransport, server_id: str) -> None:
        self._t = transport
        self._sid = server_id

    def create(self, goal: str) -> Task:
        r = self._t.request("POST", f"/servers/{_seg(self._sid)}/tasks", body={"goal": goal})
        return Task(self._t, {"id": r["task_id"], "serverId": self._sid, "goal": goal, "status": r["status"], "createdAt": r.get("createdAt", "")})

    def list(self) -> List[Task]:
        r = self._t.request("GET", f"/servers/{_seg(self._sid)}/tasks")
        return [Task(self._t, x) for x in r["tasks"]]

    def get(self, task_id: str) -> Task:
        r = self._t.request("GET", f"/servers/{_seg(self._sid)}/tasks/{_seg(task_id)}")
        return Task(self._t, r["task"])


class _ScopedBuilds:
    def __init__(self, transport: SyncTransport, server_id: str) -> None:
        self._t = transport
        self._sid = server_id

    def start(self, goal: str) -> Build:
        r = self._t.request("POST", f"/servers/{_seg(self._sid)}/builds", body={"goal": goal})
        return Build(self._t, {"id": r["build_id"], "serverId": self._sid, "goal": goal, "status": r["status"], "steps": r.get("steps", []), "createdAt": r.get("createdAt", "")})

    def list(self) -> List[Build]:
        r = self._t.request("GET", f"/servers/{_seg(self._sid)}/builds")
        return [Build(self._t, x) for x in r["builds"]]

    def get(self, build_id: str) -> Build:
        r = self._t.request("GET", f"/servers/{_seg(self._sid)}/builds/{_seg(build_id)}")
        return Build(self._t, r["build"])


class _ScopedFiles:
    def __init__(self, transport: SyncTransport, server_id: str) -> None:
        self._t = transport
        self._sid = server_id

    def list(self) -> List[str]:
        r = self._t.request("GET", f"/servers/{_seg(self._sid)}/files")
        return r["files"]

    def read(self, path: str) -> str:
        r = self._t.request("GET", f"/servers/{_seg(self._sid)}/files", query={"path": path})
        return r["content"]

    def write(self, path: str, content: str) -> None:
        self._t.request("PUT", f"/servers/{_seg(self._sid)}/files", body={"path": path, "content": content})

    def delete(self, path: str) -> None:
        self._t.request("DELETE", f"/servers/{_seg(self._sid)}/files", query={"path": path})


# =============================================================================
# Top-level resources — the flat client.servers.* API.
# =============================================================================

class ServersResource:
    def __init__(self, transport: SyncTransport) -> None:
        self._t = transport

    def create(
        self, *, task: Optional[str] = None, tier: Optional[ServerTier] = None, region: Optional[str] = None
    ) -> Server:
        body = {k: v for k, v in {"task": task, "tier": tier, "region": region}.items() if v is not None}
        r = self._t.request("POST", "/servers", body=body)
        # create returns a partial shape ({id,url,status,tier}); normalize to a Server.
        return Server(self._t, {
            "id": r["id"], "task": task or "", "tier": r["tier"], "status": r["status"],
            "region": region or "", "publicUrl": r.get("url"),
        })

    def list(self) -> List[Server]:
        r = self._t.request("GET", "/servers")
        return [Server(self._t, s) for s in r["servers"]]

    def get(self, server_id: str) -> Server:
        r = self._t.request("GET", f"/servers/{_seg(server_id)}")
        return Server(self._t, r["server"])

    def update(self, server_id: str, *, task: Optional[str] = None, region: Optional[str] = None) -> Server:
        body = {k: v for k, v in {"task": task, "region": region}.items() if v is not None}
        r = self._t.request("PATCH", f"/servers/{_seg(server_id)}", body=body)
        return Server(self._t, r["server"])

    def delete(self, server_id: str) -> None:
        self._t.request("DELETE", f"/servers/{_seg(server_id)}")

    def control(self, server_id: str, action: ServerAction) -> dict:
        return self._t.request("POST", f"/servers/{_seg(server_id)}/actions", body={"action": action})

    def exec(self, server_id: str, command: str) -> str:
        r = self._t.request("POST", f"/servers/{_seg(server_id)}/exec", body={"command": command})
        return r["stdout"]

    def deploy(self, server_id: str) -> str:
        r = self._t.request("POST", f"/servers/{_seg(server_id)}/deployments")
        return r["url"]

    def list_deployments(self, server_id: str) -> List[Deployment]:
        r = self._t.request("GET", f"/servers/{_seg(server_id)}/deployments")
        return [Deployment(**d) for d in r["deployments"]]


class ProjectsResource:
    def __init__(self, transport: SyncTransport) -> None:
        self._t = transport

    def list(self) -> List[Project]:
        r = self._t.request("GET", "/projects")
        return [Project(self._t, p) for p in r["projects"]]

    def list_for_server(self, server_id: str) -> List[Project]:
        return _ScopedProjects(self._t, server_id).list()

    def create(self, server_id: str, **opts) -> Project:
        return _ScopedProjects(self._t, server_id).create(**opts)

    def get(self, project_id: str) -> Project:
        r = self._t.request("GET", f"/projects/{_seg(project_id)}")
        return Project(self._t, r)

    def update(self, project_id: str, **patch) -> Project:
        body = {k: v for k, v in patch.items() if v is not None}
        r = self._t.request("PATCH", f"/projects/{_seg(project_id)}", body=body)
        return Project(self._t, r)

    def delete(self, project_id: str) -> None:
        self._t.request("DELETE", f"/projects/{_seg(project_id)}")

    def cleanup(self, project_id: str) -> None:
        self._t.request("POST", f"/projects/{_seg(project_id)}/cleanup")


class TemplatesResource:
    def __init__(self, transport: SyncTransport) -> None:
        self._t = transport

    def list(self) -> List[ProjectTemplate]:
        r = self._t.request("GET", "/templates")
        return [ProjectTemplate.parse(x) for x in r["templates"]]


class TasksResource:
    def __init__(self, transport: SyncTransport) -> None:
        self._t = transport

    def create(self, server_id: str, goal: str) -> Task:
        return _ScopedTasks(self._t, server_id).create(goal)

    def list(self, server_id: str) -> List[Task]:
        return _ScopedTasks(self._t, server_id).list()

    def get(self, server_id: str, task_id: str) -> Task:
        return _ScopedTasks(self._t, server_id).get(task_id)


class BuildsResource:
    def __init__(self, transport: SyncTransport) -> None:
        self._t = transport

    def start(self, server_id: str, goal: str) -> Build:
        return _ScopedBuilds(self._t, server_id).start(goal)

    def list(self, server_id: str) -> List[Build]:
        return _ScopedBuilds(self._t, server_id).list()

    def get(self, server_id: str, build_id: str) -> Build:
        return _ScopedBuilds(self._t, server_id).get(build_id)


class FilesResource:
    def __init__(self, transport: SyncTransport) -> None:
        self._t = transport

    def list(self, server_id: str) -> List[str]:
        return _ScopedFiles(self._t, server_id).list()

    def read(self, server_id: str, path: str) -> str:
        return _ScopedFiles(self._t, server_id).read(path)

    def write(self, server_id: str, path: str, content: str) -> None:
        _ScopedFiles(self._t, server_id).write(path, content)

    def delete(self, server_id: str, path: str) -> None:
        _ScopedFiles(self._t, server_id).delete(path)


class KeysResource:
    def __init__(self, transport: SyncTransport) -> None:
        self._t = transport

    def list(self) -> List[ApiKey]:
        r = self._t.request("GET", "/keys")
        return [ApiKey.parse(k) for k in r["keys"]]

    def create(self, label: str) -> CreatedApiKey:
        r = self._t.request("POST", "/keys", body={"label": label})
        base = ApiKey.parse(r)
        return CreatedApiKey(key=r["key"], **base.__dict__)

    def update_scope(
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
        return self._t.request("PATCH", f"/keys/{_seg(key_id)}", body=body)

    def revoke(self, key_id: str) -> None:
        self._t.request("DELETE", f"/keys/{_seg(key_id)}")


class WebhooksResource:
    def __init__(self, transport: SyncTransport) -> None:
        self._t = transport

    def list(self) -> List[Webhook]:
        r = self._t.request("GET", "/webhooks")
        return [Webhook.parse(w) for w in r["webhooks"]]

    def create(self, url: str, events: Optional[Sequence[str]] = None) -> CreatedWebhook:
        body = {"url": url, "events": list(events) if events is not None else None}
        r = self._t.request("POST", "/webhooks", body=body)
        base = Webhook.parse(r)
        return CreatedWebhook(secret=r["secret"], **base.__dict__)

    def delete(self, webhook_id: str) -> None:
        self._t.request("DELETE", f"/webhooks/{_seg(webhook_id)}")


class Server4Agent:
    """Client for the Server4Agent REST API.

    This SDK is for the code *around* your agent: your backend, a CI job, a
    webhook receiver, an admin script. If your agent itself does tool-calling,
    point it at the MCP server directly — it doesn't need this.

        from server4agent import Server4Agent

        client = Server4Agent()  # reads SERVER4AGENT_API_KEY
        server = client.servers.create(tier="small")
        build = server.builds.start("a FastAPI todo API").wait()
        print(build.url)

    Usable as a context manager to close the underlying connection pool.
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
        self._t = SyncTransport(base_url, key, timeout=timeout, max_retries=max_retries)

        self.servers = ServersResource(self._t)
        self.projects = ProjectsResource(self._t)
        self.templates = TemplatesResource(self._t)
        self.tasks = TasksResource(self._t)
        self.builds = BuildsResource(self._t)
        self.files = FilesResource(self._t)
        self.keys = KeysResource(self._t)
        self.webhooks = WebhooksResource(self._t)

    def close(self) -> None:
        self._t.close()

    def __enter__(self) -> "Server4Agent":
        return self

    def __exit__(self, *exc) -> None:
        self.close()
