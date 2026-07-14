"""Wire types and parsers.

Pure-data responses are plain dataclasses. The four resources you can *act on*
(server, project, task, build) are returned as handles defined in the sync/async
clients; the field-parsing for those lives here as ``parse_*`` functions so both
clients stay identical.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Literal, Optional

ServerTier = Literal["small", "medium", "large"]
ServerStatus = Literal["running", "sleeping", "building"]
ServerAction = Literal["start", "stop", "restart"]
ProjectVisibility = Literal["public", "private"]
ProjectLifecycle = Literal["persistent", "ephemeral"]
ProjectRuntimeStatus = Literal["unbuilt", "running", "paused"]
TaskStatus = Literal["running", "completed", "failed", "awaiting_input", "cancelled"]
BuildStatus = Literal["queued", "running", "completed"]

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
    url: str
    live: bool


@dataclass
class ExecResult:
    stdout: str


@dataclass
class BuildStep:
    key: str
    label: str
    status: Literal["pending", "running", "done"]


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

    @staticmethod
    def parse(raw: dict) -> "ProjectTemplate":
        return ProjectTemplate(
            id=raw["id"],
            label=raw["label"],
            description=raw["description"],
            defaults=ProjectTemplateDefaults(**raw["defaults"]),
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


# --- field parsers for the handle types --------------------------------------

def parse_server(raw: dict) -> Dict[str, Any]:
    return dict(
        id=raw["id"],
        task=raw["task"],
        tier=raw["tier"],
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
        lifecycle=raw["lifecycle"],
        runtime_status=raw["runtime_status"],
        template=raw.get("template"),
        url=raw.get("url"),
        created_at=raw["created_at"],
        updated_at=raw["updated_at"],
    )


def parse_task(raw: dict) -> Dict[str, Any]:
    return dict(
        id=raw["id"],
        server_id=raw.get("serverId", raw.get("server_id")),
        goal=raw["goal"],
        status=raw["status"],
        created_at=raw.get("createdAt", raw.get("created_at")),
        result=raw.get("result"),
        question=raw.get("question"),
    )


def parse_build(raw: dict) -> Dict[str, Any]:
    return dict(
        id=raw["id"],
        server_id=raw.get("serverId", raw.get("server_id")),
        goal=raw["goal"],
        status=raw["status"],
        steps=[BuildStep(**s) for s in raw.get("steps", [])],
        artifacts=raw.get("artifacts", []),
        url=raw.get("url"),
        created_at=raw.get("createdAt", raw.get("created_at")),
    )
