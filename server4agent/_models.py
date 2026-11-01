"""Wire types and parsers.

Pure-data responses are plain dataclasses. The four resources you can *act on*
(server, project, task, build) are returned as handles defined in the sync/async
clients; the field-parsing for those lives here as ``parse_*`` functions so both
clients stay identical. So do the few request-body helpers both clients share.
"""
from __future__ import annotations

import base64
from dataclasses import dataclass, field
from typing import Any, Dict, Generic, Iterable, List, Literal, Mapping, Optional, TypeVar, Union

ServerTier = Literal["small", "medium", "large"]
#: What kind of box a server is, orthogonal to its tier. "standard" is
#: headless; "browser" adds a screen with a browser on it, so a run can drive
#: sites that need a real one. A server from an API that predates the field
#: reads as "standard".
ServerKind = Literal["standard", "browser"]
ServerStatus = Literal["running", "sleeping", "building"]
ServerAction = Literal["start", "stop", "restart"]
ProjectVisibility = Literal["public", "private"]
#: Who may open a public project's URL. "team" puts it behind a Server4Agent
#: sign-in and membership of the owning account; "password" asks for a password
#: the owner set (``access_password``) and needs no account; "public" is anyone
#: with the link, which is the default and how every project behaved before this.
ProjectAccess = Literal["public", "team", "password"]
ProjectLifecycle = Literal["persistent", "ephemeral"]
ProjectRuntimeStatus = Literal["unbuilt", "running", "paused"]
#: Where a project's bring-up has got to, from seeding the workspace to a
#: confirmed-running app. ``"awaiting_secrets"`` waits on environment variables
#: the code needs; ``"awaiting_plan"`` waits on someone to confirm how to run it.
ProjectInitPhase = Literal[
    "seeding", "awaiting_plan", "awaiting_secrets", "installing", "starting", "ready", "failed"
]
TaskStatus = Literal["running", "completed", "failed", "awaiting_input", "cancelled"]
BuildStatus = Literal["queued", "running", "completed"]
#: ``"live"`` when the project's URL answered within the deploy's wait,
#: ``"publishing"`` when it is public but had not answered yet.
DeployStatus = Literal["live", "publishing"]

# A task poll stops here: nothing further happens without you (input) or it's over.
TASK_TERMINAL_STATES = frozenset({"completed", "failed", "cancelled", "awaiting_input"})
BUILD_TERMINAL_STATES = frozenset({"completed"})

WEBHOOK_EVENTS = (
    "task.created",
    "task.completed",
    "task.failed",
    "deployment.live",
    "deployment.removed",
    "server.created",
    "server.stopped",
    "server.deleted",
    "quota.threshold",
    "quota.cap_reached",
    "quota.egress_cap_reached",
)


class _Record:
    """Base for handle types: a readable repr over public (non-underscore) attrs."""

    def __repr__(self) -> str:
        fields = ", ".join(
            f"{k}={v!r}" for k, v in self.__dict__.items() if not k.startswith("_")
        )
        return f"{type(self).__name__}({fields})"


# --- pure data (no behavior) --------------------------------------------------

@dataclass
class Deployment:
    """A public project on a server, and its address."""
    url: str
    live: bool
    project_id: Optional[str] = None

    @staticmethod
    def parse(raw: dict) -> "Deployment":
        return Deployment(url=raw["url"], live=raw["live"], project_id=raw.get("project_id"))


class DeployResult(str):
    """What ``deploy()`` returns: the project's URL, as a ``str`` exactly as
    before, that also says how the deploy went.

    ``status`` is ``"live"`` when the URL answered within the deploy's wait and
    ``"publishing"`` when it had not yet. ``project_id`` is the project that was
    put online, which matters when the API picked the server's only one.
    """
    status: DeployStatus
    project_id: Optional[str]

    def __new__(cls, url: str, status: DeployStatus = "live", project_id: Optional[str] = None) -> "DeployResult":
        self = super().__new__(cls, url)
        self.status = status
        self.project_id = project_id
        return self

    @property
    def url(self) -> str:
        return str(self)

    def __repr__(self) -> str:
        return f"DeployResult(url={str(self)!r}, status={self.status!r}, project_id={self.project_id!r})"

    @staticmethod
    def parse(raw: dict) -> "DeployResult":
        return DeployResult(raw["url"], raw.get("status", "live"), raw.get("project_id"))


@dataclass
class ExecResult:
    stdout: str


@dataclass
class BuildStep:
    key: str
    label: str
    # A build is one agent run and its steps move together: "failed" when the
    # run failed, even though the build's own status still reads "completed".
    status: Literal["pending", "running", "done", "failed"]

    @staticmethod
    def parse(raw: dict) -> "BuildStep":
        return BuildStep(key=raw["key"], label=raw["label"], status=raw["status"])


@dataclass
class ProjectTemplateDefaults:
    name: str
    description: str
    visibility: ProjectVisibility
    lifecycle: ProjectLifecycle


@dataclass
class ProjectTemplate:
    id: str
    label: str
    description: str
    defaults: ProjectTemplateDefaults
    #: ``"git"`` or ``"archive"`` on an import template, which seeds the project
    #: from your own code rather than a starter. None for ordinary starters.
    source_kind: Optional[str] = None

    @staticmethod
    def parse(raw: dict) -> "ProjectTemplate":
        d = raw["defaults"]
        return ProjectTemplate(
            id=raw["id"],
            label=raw["label"],
            description=raw["description"],
            defaults=ProjectTemplateDefaults(
                name=d["name"],
                description=d["description"],
                visibility=d["visibility"],
                lifecycle=d["lifecycle"],
            ),
            source_kind=raw.get("source_kind"),
        )


@dataclass
class ApiKey:
    id: str
    label: str
    masked: str  # e.g. "sk_live_abc···wxyz"; never the full secret
    allowed_server_ids: List[str]
    allowed_project_ids: List[str]
    created_at: str
    last_used_at: Optional[str]

    @staticmethod
    def parse(raw: dict) -> "ApiKey":
        return ApiKey(
            id=raw["id"],
            label=raw["label"],
            masked=raw["masked"],
            allowed_server_ids=raw.get("allowed_server_ids", []),
            allowed_project_ids=raw.get("allowed_project_ids", []),
            created_at=raw["created_at"],
            last_used_at=raw.get("last_used_at"),
        )


@dataclass
class CreatedApiKey(ApiKey):
    # The plaintext sk_live_ key. Store it now — it can't be retrieved again.
    key: str = ""


@dataclass
class Webhook:
    id: str
    url: str
    events: List[str]
    created_at: str

    @staticmethod
    def parse(raw: dict) -> "Webhook":
        return Webhook(id=raw["id"], url=raw["url"], events=raw["events"], created_at=raw["created_at"])


@dataclass
class CreatedWebhook(Webhook):
    # The signing secret (whsec_...). Store it now — it can't be retrieved again.
    secret: str = ""


@dataclass
class Secret:
    """The safe view of a secret: its key, note, a masked hint and where it is
    bound. Values are write-only, so no read ever returns one."""
    id: str
    key: str
    description: Optional[str]
    masked: Optional[str]  # e.g. "••••3f9a"; at most the last four characters
    #: Servers the secret is bound to server-wide (every project on them sees it).
    server_ids: List[str]
    #: Projects the secret is bound to on their own.
    project_ids: List[str]
    created_at: str
    updated_at: str

    @staticmethod
    def parse(raw: dict) -> "Secret":
        return Secret(
            id=raw["id"],
            key=raw["key"],
            description=raw.get("description"),
            masked=raw.get("masked"),
            server_ids=raw.get("server_ids", []),
            project_ids=raw.get("project_ids", []),
            created_at=raw["created_at"],
            updated_at=raw["updated_at"],
        )


@dataclass
class SecretInput:
    """A secret to set. One value per key per account, so setting a key that
    exists changes its value everywhere it is bound."""
    key: str  # an environment variable name: letters, digits and underscores
    value: str
    description: Optional[str] = None


#: What the ``secrets=`` arguments take: ``SecretInput`` objects, or dicts with
#: the same ``key`` / ``value`` / ``description`` fields.
SecretsArg = Iterable[Union[SecretInput, Mapping[str, Any]]]


@dataclass
class LogFile:
    path: str  # workspace-relative
    #: The project whose folder the file lives in, or None at the workspace root.
    project_slug: Optional[str]
    name: str
    #: Full size on disk; ``tail`` may be much smaller.
    size_bytes: int
    modified_at: str
    #: The newest lines of the file, cut at a line boundary.
    tail: str

    @staticmethod
    def parse(raw: dict) -> "LogFile":
        return LogFile(
            path=raw["path"],
            project_slug=raw.get("projectSlug", raw.get("project_slug")),
            name=raw["name"],
            size_bytes=raw.get("sizeBytes", raw.get("size_bytes", 0)),
            modified_at=raw.get("modifiedAt", raw.get("modified_at", "")),
            tail=raw.get("tail", ""),
        )


@dataclass
class LogsSnapshot:
    """Log-file tails from a server's background processes.

    The API reads them live, but falls back to the last saved copy when the
    server cannot be reached (it may be starting, restarting or paused), and
    that copy can be old. ``updated_at`` is when the tails were captured, so
    check it before reading them as what is running now.
    """
    updated_at: str
    files: List[LogFile]

    @staticmethod
    def parse(raw: dict) -> "LogsSnapshot":
        return LogsSnapshot(
            updated_at=raw.get("updatedAt", raw.get("updated_at", "")),
            files=[LogFile.parse(f) for f in raw.get("files", [])],
        )


@dataclass
class AppStart:
    """A project's app process, started (or restarted) by ``project.start()``."""
    project_id: str
    #: The process name. Only ``"web"`` serves the project's URL.
    name: str
    #: ``"running"``, or ``"starting"`` while the web app has not answered yet.
    status: str
    listening: bool
    listen_port: Optional[int]
    #: The project's URL when it is public, otherwise None.
    url: Optional[str]
    visibility: Optional[str]
    access: Optional[str]
    #: True when an earlier process with this name was stopped to make way.
    replaced_previous: bool
    #: What a server restart runs to bring this process back.
    restart_command: str
    #: The process's latest output, or None.
    log_tail: Optional[str]

    @staticmethod
    def parse(raw: dict) -> "AppStart":
        return AppStart(
            project_id=raw["project_id"],
            name=raw["name"],
            status=raw["status"],
            listening=raw["listening"],
            listen_port=raw.get("listen_port"),
            url=raw.get("url"),
            visibility=raw.get("visibility"),
            access=raw.get("access"),
            replaced_previous=raw.get("replaced_previous", False),
            restart_command=raw.get("restart_command", ""),
            log_tail=raw.get("log_tail"),
        )


@dataclass
class AccountUsage:
    """Spend this billing period, in USD, and whether new tasks are refused."""
    spend_usd: float
    included_credits_usd: float
    #: True when new tasks are currently refused; ``reason`` says which ceiling.
    blocked: bool
    budget_cap_usd: Optional[float] = None
    #: Remaining one-time welcome credit for this period, 0 if none.
    bonus_usd: float = 0
    #: The ceiling that pauses new tasks (cap or plan credits), or None.
    hard_limit_usd: Optional[float] = None
    overage_allowed: bool = False
    reason: Optional[str] = None

    @staticmethod
    def parse(raw: dict) -> "AccountUsage":
        return AccountUsage(
            spend_usd=raw["spend_usd"],
            included_credits_usd=raw["included_credits_usd"],
            blocked=raw["blocked"],
            budget_cap_usd=raw.get("budget_cap_usd"),
            bonus_usd=raw.get("bonus_usd", 0),
            hard_limit_usd=raw.get("hard_limit_usd"),
            overage_allowed=raw.get("overage_allowed", False),
            reason=raw.get("reason"),
        )


@dataclass
class Me:
    """The account a key acts on (not necessarily the person holding it)."""
    #: ``{"name", "email", "plan"}`` of the account.
    account: Dict[str, Any]
    usage: AccountUsage
    #: ``{"id", "label"}`` of the key making the call.
    authenticated_key: Dict[str, Any]
    servers: int
    projects: int
    keys: int
    #: ``{"name", "email"}`` of the key's holder, set only when that is a
    #: teammate rather than the account owner.
    you: Optional[Dict[str, Any]] = None

    @staticmethod
    def parse(raw: dict) -> "Me":
        return Me(
            account=raw["account"],
            usage=AccountUsage.parse(raw["usage"]),
            authenticated_key=raw["authenticated_key"],
            servers=raw.get("servers", 0),
            projects=raw.get("projects", 0),
            keys=raw.get("keys", 0),
            you=raw.get("you"),
        )


@dataclass
class ProjectInit:
    """A bring-up that was started or resumed."""
    #: The run doing it. Follow it like any task: ``server.tasks.get(init_run_id)``.
    init_run_id: Optional[str]
    init_phase: Optional[str]

    @staticmethod
    def parse(raw: dict) -> "ProjectInit":
        return ProjectInit(init_run_id=raw.get("init_run_id"), init_phase=raw.get("init_phase"))


@dataclass
class ProjectExport:
    """A project packed into one portable file, and a link to fetch it.

    The link needs no credentials, so treat it like the code itself. The bundle
    carries the names of the environment variables the project reads (``env``),
    never their values.
    """
    url: str
    expires_at: str
    filename: str
    bytes: int
    env: List[str]
    project_id: Optional[str] = None

    @staticmethod
    def parse(raw: dict) -> "ProjectExport":
        return ProjectExport(
            url=raw["url"],
            expires_at=raw["expires_at"],
            filename=raw["filename"],
            bytes=raw["bytes"],
            env=raw.get("env", []),
            project_id=raw.get("project_id"),
        )


P = TypeVar("P")


@dataclass
class ProjectImport(Generic[P]):
    """The project an export bundle became, and what the import could not carry.

    The project lands private with its own slug and URL, and its bring-up has
    already started (``project.init_run_id``).
    """
    project: P
    #: The bring-up run that unpacks the bundle and starts the app (also
    #: ``project.init_run_id``).
    init_run_id: Optional[str]
    #: The deployment the bundle was exported from, as its manifest recorded it.
    imported_from: Optional[str]
    exported_at: Optional[str]
    #: Variables the bundle named that this account already had a value for.
    bound_env: List[str]
    #: Variables that still need a value here before the app can run. Set them
    #: with ``client.secrets.set()`` or ``project.secrets.set()``.
    missing_env: List[str]
    #: Things the bundle asked for that this deployment could not honour. Not
    #: failures: the import worked.
    warnings: List[str]

    @staticmethod
    def parse(project: Any, raw: dict) -> "ProjectImport[Any]":
        return ProjectImport(
            project=project,
            init_run_id=raw.get("init_run_id"),
            imported_from=raw.get("imported_from"),
            exported_at=raw.get("exported_at"),
            bound_env=raw.get("bound_env", []),
            missing_env=raw.get("missing_env", []),
            warnings=raw.get("warnings", []),
        )


# --- request-body helpers ------------------------------------------------------

def secret_inputs(secrets: SecretsArg) -> List[dict]:
    """Turn a ``secrets=`` argument into the wire shape. The API validates keys."""
    if isinstance(secrets, (str, bytes, Mapping)):
        raise TypeError(
            "secrets must be a list of SecretInput or {'key': ..., 'value': ...} dicts"
        )
    out: List[dict] = []
    for s in secrets:
        if isinstance(s, SecretInput):
            item: Dict[str, Any] = {"key": s.key, "value": s.value, "description": s.description}
        elif isinstance(s, Mapping):
            item = dict(s)
        else:
            raise TypeError(f"each secret must be a SecretInput or a dict, not {type(s).__name__}")
        out.append({k: v for k, v in item.items() if v is not None})
    return out


def bundle_b64(bundle: Union[bytes, bytearray, memoryview, str]) -> str:
    """An export bundle as the import endpoint takes it: the file's raw bytes are
    base64 encoded here, and a str is taken to be base64 already."""
    if isinstance(bundle, str):
        return bundle.strip()
    if isinstance(bundle, (bytes, bytearray, memoryview)):
        return base64.b64encode(bytes(bundle)).decode("ascii")
    raise TypeError("bundle must be the export file's bytes, or that file base64 encoded as a str")


# --- field parsers for the handle types --------------------------------------

def parse_server(raw: dict) -> Dict[str, Any]:
    return dict(
        id=raw["id"],
        task=raw["task"],
        tier=raw["tier"],
        # Absent on an older control plane, which only ever built headless boxes.
        kind=raw.get("kind", "standard"),
        status=raw["status"],
        region=raw["region"],
        credits_used=raw.get("creditsUsed", raw.get("credits_used", 0)),
        storage_used_gb=raw.get("storageUsedGb", raw.get("storage_used_gb", 0)),
        public_url=raw.get("publicUrl", raw.get("public_url")),
        created_at=raw.get("createdAt", raw.get("created_at")),
    )


def parse_project(raw: dict) -> Dict[str, Any]:
    return dict(
        id=raw["id"],
        server_id=raw["server_id"],
        name=raw["name"],
        slug=raw["slug"],
        description=raw.get("description"),
        visibility=raw["visibility"],
        # Absent on an older control plane, which only served open URLs.
        access=raw.get("access", "public"),
        lifecycle=raw["lifecycle"],
        runtime_status=raw["runtime_status"],
        template=raw.get("template"),
        # Absent on an older control plane, which had no sources or bring-up.
        source=raw.get("source"),
        init_phase=raw.get("init_phase"),
        run_plan=raw.get("run_plan"),
        url=raw.get("url"),
        created_at=raw["created_at"],
        updated_at=raw["updated_at"],
    )


def parse_task(raw: dict) -> Dict[str, Any]:
    return dict(
        id=raw["id"],
        server_id=raw.get("serverId", raw.get("server_id")),
        project_id=raw.get("projectId", raw.get("project_id")),
        goal=raw["goal"],
        # Older API responses carry no message type; everything they returned
        # was a task the caller had created.
        message_type=raw.get("messageType", raw.get("message_type", "user")),
        status=raw["status"],
        created_at=raw.get("createdAt", raw.get("created_at")),
        result=raw.get("result"),
        question=raw.get("question"),
        log=raw.get("log"),
        change=raw.get("change"),
        usage=raw.get("usage"),
    )


def parse_build(raw: dict) -> Dict[str, Any]:
    return dict(
        id=raw["id"],
        server_id=raw.get("serverId", raw.get("server_id")),
        goal=raw["goal"],
        status=raw["status"],
        steps=[BuildStep.parse(s) for s in raw.get("steps", [])],
        artifacts=raw.get("artifacts", []),
        url=raw.get("url"),
        created_at=raw.get("createdAt", raw.get("created_at")),
    )
