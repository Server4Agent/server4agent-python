from __future__ import annotations

import base64
import os
import time
from typing import Any, Callable, List, Mapping, Optional, Sequence, Union

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
from .transport import SyncTransport


# =============================================================================
# Handles — returned by create()/get(); data records you can also act on.
# =============================================================================

class Server(_Record):
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

    @property
    def secrets(self) -> "_ScopedSecrets":
        """Secrets bound server-wide, which every project on the server sees."""
        return _ScopedSecrets(self._t, f"/servers/{_seg(self.id)}/secrets")

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

    def exec(self, command: str, *, project_id: Optional[str] = None) -> str:
        """Run a shell command on the server. Returns stdout.

        With ``project_id`` it runs inside that project's folder rather than
        at the workspace root."""
        body = {k: v for k, v in {"command": command, "project_id": project_id}.items() if v is not None}
        r = self._t.request("POST", f"/servers/{_seg(self.id)}/exec", body=body)
        return r["stdout"]

    def deploy(self, *, project_id: Optional[str] = None) -> DeployResult:
        """Put a running project online.

        Checks that something in the project is listening, makes it public, and
        waits briefly for the URL to answer (``status`` says whether it did). It
        builds and starts nothing: when nothing is listening it raises
        ``ConflictError`` and leaves the project as it was. ``project_id`` may be
        left out when the server has exactly one project."""
        return _deploy(self._t, self.id, project_id)

    def deployments(self) -> List[Deployment]:
        """The server's public projects, each with its URL."""
        r = self._t.request("GET", f"/servers/{_seg(self.id)}/deployments")
        return [Deployment.parse(d) for d in r["deployments"]]

    def logs(self, *, project_id: Optional[str] = None, lines: Optional[int] = None) -> Optional[LogsSnapshot]:
        """Tails of the log files the server's background processes write.

        ``project_id`` keeps only that project's files; ``lines`` caps the tail
        of each (1 to 1000, default 200). None when the server has never
        reported any. See ``LogsSnapshot`` for how fresh they are."""
        return _logs(self._t, self.id, project_id, lines)


class Project(_Record):
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
    #: Where the workspace was seeded from, e.g. ``{"kind": "git", "git_url": ...}``.
    source: Optional[dict]
    #: Bring-up progress (see ``ProjectInitPhase``), or None when there is none.
    init_phase: Optional[str]
    #: How bring-up installs and starts the project, once one is settled.
    run_plan: Optional[dict]
    url: Optional[str]
    created_at: str
    updated_at: str
    #: The bring-up run this handle started, from ``create()``, ``import_bundle()``
    #: or ``init()``. None otherwise; a refresh does not change it.
    init_run_id: Optional[str]

    def __init__(self, transport: SyncTransport, raw: dict) -> None:
        self._t = transport
        self.__dict__.update(parse_project(raw))
        self.init_run_id = raw.get("init_run_id")

    @property
    def secrets(self) -> "_ScopedSecrets":
        """Secrets this project sees: its own plus the server-wide ones."""
        return _ScopedSecrets(self._t, f"/projects/{_seg(self.id)}/secrets")

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
        access: Optional[str] = None,
        access_password: Optional[str] = None,
        lifecycle: Optional[str] = None,
        run_plan: Optional[Mapping[str, Any]] = None,
    ) -> "Project":
        """``run_plan`` replaces the stored plan wholesale and is recorded for the
        next bring-up, not acted on: ``init()`` is how you ask for the rebuild.
        ``access_password`` sets or changes the password for ``access="password"``;
        sent alone it changes the password and signs out everyone who used the
        old one. It is never returned."""
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
        r = self._t.request("PATCH", f"/projects/{_seg(self.id)}", body=body)
        self.__dict__.update(parse_project(r))
        return self

    def delete(self) -> None:
        self._t.request("DELETE", f"/projects/{_seg(self.id)}")

    def cleanup(self) -> None:
        """Ephemeral projects only. Use ``delete()`` for persistent ones."""
        self._t.request("POST", f"/projects/{_seg(self.id)}/cleanup")

    def init(self, *, proceed: Optional[bool] = None) -> ProjectInit:
        """Start bring-up for a project seeded from code, or resume one paused at
        ``awaiting_secrets``. ``proceed=True`` carries on past secrets that are
        still unset instead of waiting. Updates ``init_phase`` and ``init_run_id``."""
        result = _init_project(self._t, self.id, proceed)
        self.init_run_id = result.init_run_id
        self.init_phase = result.init_phase
        return result

    def deploy(self) -> DeployResult:
        """Put this project online once something in it is listening. See
        ``Server.deploy()``. The handle is not changed: ``refresh()`` to read
        the new visibility and URL."""
        return _deploy(self._t, self.server_id, self.id)

    def logs(self, *, lines: Optional[int] = None) -> Optional[LogsSnapshot]:
        """This project's log-file tails. See ``Server.logs()``."""
        return _logs(self._t, self.server_id, self.id, lines)

    def start(self, command: str, *, name: Optional[str] = None) -> AppStart:
        """Run ``command`` in the project folder as a background process, with
        ``PORT`` set to the project's routed port, and keep it running across
        server restarts. Waits about 10 seconds for it to answer.

        Starting again with the same ``name`` replaces the earlier process.
        Leave ``name`` out for the web app (``"web"``), the only process that
        serves the URL; name a worker or scheduler to run it beside it. Raises
        ``ConflictError`` when it cannot start; for ``code == "app_failed"``,
        ``err.body["log_tail"]`` is the app's last output."""
        return _start_app(self._t, self.id, command, name)

    def export(self, *, ttl_seconds: Optional[float] = None) -> ProjectExport:
        """Pack the project into one portable file and return a link to it.

        Feed the file to ``server.projects.import_bundle()`` here or on another
        deployment. The link lasts ``ttl_seconds`` (default 24 hours, at most 7
        days) and needs no credentials."""
        return _export_project(self._t, self.id, ttl_seconds)


class Task(_Record):
    id: str
    server_id: str
    #: The project the task ran in, or None when it ran at the workspace root.
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
    #: Latest progress note while the task runs.
    log: Optional[str]
    #: What the run changed, as one commit (``commit``, ``files``, ...), or None.
    change: Optional[dict]
    #: Tokens, turns, duration and cost (``cost`` is USD), or None.
    usage: Optional[dict]
    #: The task this one continues, when this handle created it with ``follow_up_of``.
    follow_up_of: Optional[str]
    #: For a follow-up this handle created: False when the earlier task left no
    #: conversation to pick up, so the agent works in the same folder without
    #: that memory. None otherwise.
    resumed: Optional[bool]

    def __init__(self, transport: SyncTransport, raw: dict) -> None:
        self._t = transport
        self.__dict__.update(parse_task(raw))
        # Only the create response says these; a refresh does not change them.
        self.follow_up_of = raw.get("follow_up_of")
        self.resumed = raw.get("resumed")

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
        access: Optional[str] = None,
        access_password: Optional[str] = None,
        lifecycle: Optional[str] = None,
        template: Optional[str] = None,
        source: Optional[Mapping[str, Any]] = None,
        auto_init: Optional[bool] = None,
        secrets: Optional[SecretsArg] = None,
    ) -> Project:
        """``source`` seeds the workspace, e.g. ``{"kind": "git", "git_url": ...}``.
        A git or archive source starts bring-up straight away unless
        ``auto_init=False``; ``project.init_run_id`` is that run. ``secrets`` are
        bound to this project only, so bring-up need not stop to ask for them."""
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
        r = self._t.request("POST", f"/servers/{_seg(self._sid)}/projects", body=body)
        return Project(self._t, r)

    def import_bundle(
        self, bundle: Union[bytes, str], *, name: Optional[str] = None
    ) -> ProjectImport[Project]:
        """Recreate a project from a file ``project.export()`` made, here or on
        another deployment. ``bundle`` is the file's bytes, or the file base64
        encoded as a str.

        The project lands private, and its bring-up starts right away. Values
        never travel in a bundle: ``missing_env`` lists what to set before the
        app can run."""
        body = {"bundle": bundle_b64(bundle)}
        if name is not None:
            body["name"] = name
        r = self._t.request("POST", f"/servers/{_seg(self._sid)}/projects/import", body=body)
        return ProjectImport.parse(Project(self._t, r), r)


class _ScopedTasks:
    def __init__(self, transport: SyncTransport, server_id: str) -> None:
        self._t = transport
        self._sid = server_id

    def create(
        self, goal: str, *, project_id: Optional[str] = None, follow_up_of: Optional[str] = None
    ) -> Task:
        """``project_id`` runs the task in that project's folder. ``follow_up_of``
        continues an earlier task's conversation, where that task ran, e.g. to
        answer one that is ``awaiting_input``."""
        body = {
            k: v
            for k, v in {"goal": goal, "project_id": project_id, "follow_up_of": follow_up_of}.items()
            if v is not None
        }
        r = self._t.request("POST", f"/servers/{_seg(self._sid)}/tasks", body=body)
        return Task(self._t, {"id": r["task_id"], "serverId": self._sid, "projectId": r.get("project_id", project_id), "goal": goal, "messageType": "user", "status": r["status"], "createdAt": r.get("createdAt", ""), "follow_up_of": r.get("follow_up_of"), "resumed": r.get("resumed")})

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

    def read_binary(self, path: str) -> bytes:
        """Read a binary file (e.g. a generated .xlsx) without corrupting its bytes."""
        r = self._t.request(
            "GET",
            f"/servers/{_seg(self._sid)}/files",
            query={"path": path, "encoding": "base64"},
        )
        return base64.b64decode(r["content"])

    def write(self, path: str, content: "str | bytes") -> None:
        """`content` may be a str (written as UTF-8) or raw bytes (written as-is, base64 on the wire)."""
        if isinstance(content, bytes):
            body = {"path": path, "content": base64.b64encode(content).decode("ascii"), "encoding": "base64"}
        else:
            body = {"path": path, "content": content}
        self._t.request("PUT", f"/servers/{_seg(self._sid)}/files", body=body)

    def delete(self, path: str) -> None:
        self._t.request("DELETE", f"/servers/{_seg(self._sid)}/files", query={"path": path})

    def get_download_url(self, path: str, *, ttl_seconds: Optional[int] = None) -> dict:
        """Mint a signed, expiring link anyone can use to download this one file — no login required.

        Returns {"path": str, "url": str, "expires_at": str}.
        """
        query: dict = {"path": path}
        if ttl_seconds is not None:
            query["ttl_seconds"] = ttl_seconds
        r = self._t.request(
            "GET", f"/servers/{_seg(self._sid)}/files/download-url", query=query
        )
        return {"path": r.get("path", path), "url": r["url"], "expires_at": r["expires_at"]}


class _ScopedSecrets:
    """Secrets seen from one server (``server.secrets``) or one project
    (``project.secrets``). Values are write-only: no read returns one."""

    def __init__(self, transport: SyncTransport, path: str) -> None:
        self._t = transport
        self._path = path

    def list(self) -> List[Secret]:
        r = self._t.request("GET", self._path)
        return [Secret.parse(s) for s in r["secrets"]]

    def set(self, secrets: SecretsArg) -> List[Secret]:
        """Set each secret and bind it here. Returns everything in scope after
        the change. A key has one value per account, so this changes it
        wherever else it is bound too.

            server.secrets.set([{"key": "DATABASE_URL", "value": "postgres://..."}])
        """
        r = self._t.request("PUT", self._path, body={"secrets": secret_inputs(secrets)})
        return [Secret.parse(s) for s in r["secrets"]]


def _init_project(t: SyncTransport, project_id: str, proceed: Optional[bool]) -> ProjectInit:
    body = {"proceed": proceed} if proceed is not None else None
    r = t.request("POST", f"/projects/{_seg(project_id)}/init", body=body)
    return ProjectInit.parse(r)


def _export_project(t: SyncTransport, project_id: str, ttl_seconds: Optional[float]) -> ProjectExport:
    body = {"ttl_seconds": ttl_seconds} if ttl_seconds is not None else None
    r = t.request("POST", f"/projects/{_seg(project_id)}/export", body=body)
    return ProjectExport.parse(r)


def _start_app(t: SyncTransport, project_id: str, command: str, name: Optional[str]) -> AppStart:
    body = {"command": command}
    if name is not None:
        body["name"] = name
    r = t.request("POST", f"/projects/{_seg(project_id)}/start", body=body)
    return AppStart.parse(r)


def _deploy(t: SyncTransport, server_id: str, project_id: Optional[str]) -> DeployResult:
    body = {"project_id": project_id} if project_id is not None else None
    r = t.request("POST", f"/servers/{_seg(server_id)}/deployments", body=body)
    return DeployResult.parse(r)


def _logs(
    t: SyncTransport, server_id: str, project_id: Optional[str], lines: Optional[int]
) -> Optional[LogsSnapshot]:
    r = t.request(
        "GET", f"/servers/{_seg(server_id)}/logs", query={"project_id": project_id, "lines": lines}
    )
    return LogsSnapshot.parse(r["logs"]) if r.get("logs") is not None else None


# =============================================================================
# Top-level resources — the flat client.servers.* API.
# =============================================================================

class ServersResource:
    def __init__(self, transport: SyncTransport) -> None:
        self._t = transport

    def create(
        self,
        *,
        task: Optional[str] = None,
        tier: Optional[ServerTier] = None,
        kind: Optional[ServerKind] = None,
        region: Optional[str] = None,
        secrets: Optional[SecretsArg] = None,
    ) -> Server:
        """``secrets`` are bound server-wide, so every project on it sees them."""
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
        r = self._t.request("POST", "/servers", body=body)
        # create returns a partial shape ({id,url,status,tier,kind}); normalize to a Server.
        return Server(self._t, {
            "id": r["id"], "task": task or "", "tier": r["tier"], "status": r["status"],
            # Prefer what the control plane says it built; fall back to what was
            # asked for, since an older one answers without a kind at all.
            "kind": r.get("kind", kind or "standard"),
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

    def exec(self, server_id: str, command: str, *, project_id: Optional[str] = None) -> str:
        body = {k: v for k, v in {"command": command, "project_id": project_id}.items() if v is not None}
        r = self._t.request("POST", f"/servers/{_seg(server_id)}/exec", body=body)
        return r["stdout"]

    def deploy(self, server_id: str, *, project_id: Optional[str] = None) -> DeployResult:
        return _deploy(self._t, server_id, project_id)

    def list_deployments(self, server_id: str) -> List[Deployment]:
        r = self._t.request("GET", f"/servers/{_seg(server_id)}/deployments")
        return [Deployment.parse(d) for d in r["deployments"]]

    def logs(
        self, server_id: str, *, project_id: Optional[str] = None, lines: Optional[int] = None
    ) -> Optional[LogsSnapshot]:
        return _logs(self._t, server_id, project_id, lines)


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

    def import_bundle(
        self, server_id: str, bundle: Union[bytes, str], *, name: Optional[str] = None
    ) -> ProjectImport[Project]:
        return _ScopedProjects(self._t, server_id).import_bundle(bundle, name=name)

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

    def init(self, project_id: str, *, proceed: Optional[bool] = None) -> ProjectInit:
        return _init_project(self._t, project_id, proceed)

    def export(self, project_id: str, *, ttl_seconds: Optional[float] = None) -> ProjectExport:
        return _export_project(self._t, project_id, ttl_seconds)

    def start(self, project_id: str, command: str, *, name: Optional[str] = None) -> AppStart:
        return _start_app(self._t, project_id, command, name)


class TemplatesResource:
    def __init__(self, transport: SyncTransport) -> None:
        self._t = transport

    def list(self) -> List[ProjectTemplate]:
        r = self._t.request("GET", "/templates")
        return [ProjectTemplate.parse(x) for x in r["templates"]]


class TasksResource:
    def __init__(self, transport: SyncTransport) -> None:
        self._t = transport

    def create(
        self,
        server_id: str,
        goal: str,
        *,
        project_id: Optional[str] = None,
        follow_up_of: Optional[str] = None,
    ) -> Task:
        return _ScopedTasks(self._t, server_id).create(
            goal, project_id=project_id, follow_up_of=follow_up_of
        )

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

    def read_binary(self, server_id: str, path: str) -> bytes:
        return _ScopedFiles(self._t, server_id).read_binary(path)

    def write(self, server_id: str, path: str, content: "str | bytes") -> None:
        _ScopedFiles(self._t, server_id).write(path, content)

    def delete(self, server_id: str, path: str) -> None:
        _ScopedFiles(self._t, server_id).delete(path)

    def get_download_url(self, server_id: str, path: str, *, ttl_seconds: Optional[int] = None) -> dict:
        return _ScopedFiles(self._t, server_id).get_download_url(path, ttl_seconds=ttl_seconds)


class SecretsResource:
    """The account's secrets: environment variables injected into agent runs and
    bring-up. Values are write-only; reads return keys, notes, masked hints and
    bindings. Managing them needs an account-wide key."""

    def __init__(self, transport: SyncTransport) -> None:
        self._t = transport

    def list(self) -> List[Secret]:
        r = self._t.request("GET", "/secrets")
        return [Secret.parse(s) for s in r["secrets"]]

    def set(self, key: str, value: str, *, description: Optional[str] = None) -> Secret:
        """Create a secret, or change the value of the one with this key. It is
        not bound anywhere new; use ``set_for_server`` / ``set_for_project``."""
        body = {"key": key, "value": value}
        if description is not None:
            body["description"] = description
        return Secret.parse(self._t.request("PUT", "/secrets", body=body))

    def delete(self, secret_id: str) -> None:
        """Delete a secret and every binding it has."""
        self._t.request("DELETE", f"/secrets/{_seg(secret_id)}")

    def list_for_server(self, server_id: str) -> List[Secret]:
        return _ScopedSecrets(self._t, f"/servers/{_seg(server_id)}/secrets").list()

    def set_for_server(self, server_id: str, secrets: SecretsArg) -> List[Secret]:
        return _ScopedSecrets(self._t, f"/servers/{_seg(server_id)}/secrets").set(secrets)

    def list_for_project(self, project_id: str) -> List[Secret]:
        return _ScopedSecrets(self._t, f"/projects/{_seg(project_id)}/secrets").list()

    def set_for_project(self, project_id: str, secrets: SecretsArg) -> List[Secret]:
        return _ScopedSecrets(self._t, f"/projects/{_seg(project_id)}/secrets").set(secrets)


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
        """Leave ``events`` out to subscribe to all of them (see ``WEBHOOK_EVENTS``)."""
        body: dict = {"url": url}
        if events is not None:
            body["events"] = list(events)
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
        self.secrets = SecretsResource(self._t)
        self.keys = KeysResource(self._t)
        self.webhooks = WebhooksResource(self._t)

    def me(self) -> Me:
        """The account this key acts on, its spend and budget this period, and
        how many servers, projects and keys it has. Check ``usage.blocked``
        before a long series of tasks. Needs an account-wide key."""
        return Me.parse(self._t.request("GET", "/me"))

    def close(self) -> None:
        self._t.close()

    def __enter__(self) -> "Server4Agent":
        return self

    def __exit__(self, *exc) -> None:
        self.close()
