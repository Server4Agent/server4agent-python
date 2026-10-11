# Changelog

## 0.7.0

The client now covers every operation in the REST API. Everything below is
additive: code written against 0.6.0 keeps working unchanged.

New:

- Secrets, the environment variables injected into agent runs and bring-up.
  `client.secrets.list()`, `set(key, value, description=None)` and
  `delete(secret_id)` manage the account's secrets; `server.secrets` and
  `project.secrets` (or `client.secrets.list_for_server()` /
  `set_for_server()` / `list_for_project()` / `set_for_project()`) list and
  bind them where they apply. Values are write-only: a `Secret` carries the
  key, a note, a masked hint and its bindings, never the value.
- `server.logs(project_id=None, lines=None)`, `project.logs()` and
  `client.servers.logs(server_id, ...)` return the log-file tails a server's
  background processes write, or `None` before any were reported. When the
  server cannot be read live the API answers with its last saved copy, so
  check `LogsSnapshot.updated_at` before reading a tail as current.
- `project.start(command, name=None)` starts the project's app as a
  background process on its routed port and keeps it running across restarts.
  It returns an `AppStart` saying whether the app answered. When the app exits
  instead, the `ConflictError` it raises carries the app's last output in
  `err.body["log_tail"]`.
- `project.init(proceed=None)` starts bring-up for a project seeded from code,
  or resumes one paused waiting for secrets.
- `project.export(ttl_seconds=None)` packs a project into one portable file
  and returns a link to it, and `server.projects.import_bundle(bundle,
  name=None)` recreates a project from that file, here or on another
  deployment. `bundle` is the file's bytes or a base64 string. The result
  lists the environment variables still to set in `missing_env`, because a
  bundle carries their names and never their values.
- `client.tuning` starts, lists, reads and reports tuning runs, which fix an
  agent from an evaluation platform's report. A `TuningSession` has `wait()`,
  like a task or a build: reading a session is what advances it, so polling
  is how it reaches `reported` or `failed`.
- `client.me()` returns the account the key acts on, its spend and budget this
  period (`usage.blocked` says whether new tasks are refused), and how many
  servers, projects and keys it has.
- `APIStatusError.body` is the whole JSON error response, for details beyond
  the message, such as why a file write was refused (`reason`).

Catching up with the API:

- `server.deploy()` takes `project_id`, and `project.deploy()` deploys that
  project. A server with several projects used to refuse every deploy from
  this client with a 400, because there was no way to say which one. Deploy
  now only puts a project online once something in it is listening, and
  raises `ConflictError` when nothing is. It still returns the URL as a `str`,
  which now also carries `.status` (`"live"`, or `"publishing"` when the URL
  had not answered yet) and `.project_id`.
- `server.deployments()` raised `TypeError` on every call once the API began
  returning each deployment's `project_id`. `Deployment.project_id` reads it.
- `webhooks.create(url)` without `events` sent `"events": null`, which the API
  refuses. It now leaves the field out, which subscribes to every event.
  `WEBHOOK_EVENTS` adds `quota.egress_cap_reached`, `tuning.started` and
  `tuning.completed`.
- `server.exec()` takes `project_id`, to run inside that project's folder.
- `tasks.create()` takes `project_id`, and `follow_up_of` to continue an
  earlier task's conversation. A task now reads `project_id`, `log` (the live
  progress note), `change` (what the run committed) and `usage` (tokens, turns
  and cost); one created as a follow-up also reads `follow_up_of` and
  `resumed`.
- `servers.create()` and `projects.create()` take `secrets`, bound server-wide
  or to the project. `projects.create()` also takes `source` (a template, a git
  repository or an uploaded archive) and `auto_init`.
- Projects read `source`, `init_phase` and `run_plan`, plus `init_run_id` on
  the handle that started a bring-up. `project.update()` takes `run_plan`.
- `files.get_download_url()` also returns the file's `path`.
- Templates read `source_kind`, and a build step's status can be `"failed"`.
- `ProjectAccess` and `ProjectVisibility` are exported from the package, as
  0.4.0 said they were.
- `examples/preview_env.py` builds the branch in the project's own folder,
  starts the app with `project.start()` and puts it online with
  `project.deploy()`. It used to build at the server root, so the project URL
  it printed never had anything behind it.

## 0.6.0

- `server.kind` on every server handle: `"standard"` for a headless box,
  `"browser"` for one with a screen and a browser on it. The field was already
  set at creation but no read path returned it, so a browser server was
  indistinguishable from a headless one once you had it. A server from an API
  that predates the field reads as `"standard"`.
- `kind` is accepted by `servers.create()` on both the sync and async clients,
  which is what provisions a browser box. It needs a plan that allows the
  medium tier, and costs more to run.

## 0.5.0

- Requires Python 3.11 or newer. 3.9 is past end of life and 3.10 reaches it in
  October 2026, so the floor now sits on the oldest release that still gets
  upstream fixes. The client itself is unchanged: on 3.11 and newer, 0.5.0
  behaves exactly as 0.4.0 did.

## 0.4.0

- `project.access` and the `access` keyword on `projects.create()` and
  `project.update()`. `"public"` is anyone with the link, which is what every
  project did before and stays the default. `"team"` puts the project's URL
  behind a Server4Agent sign-in, so only people in the account that owns it can
  open it. A project from an API that predates the field reads as `"public"`.
- `ProjectAccess` is exported alongside `ProjectVisibility`. The two are
  separate on purpose: `visibility` decides whether the URL serves at all,
  `access` decides who gets through once it does.

## 0.3.0

- `task.message_type`: `"user"` for a task you created, `"system"` for one the
  platform started for you (a project imported from git brings itself up this
  way). A system task's `goal` is a short summary of the run, not the
  instructions its agent was given. Tasks from an API that predates the field
  read as `"user"`.

## 0.2.0

- `files.get_download_url(server_id, path, ttl_seconds=None)` — mint a
  signed, expiring link anyone can use to download one workspace file, no
  API key required to fetch it.
- `files.read_binary(server_id, path)` and `files.write(server_id, path,
  content)` — read/write binary files (e.g. a generated `.xlsx`) without
  corrupting bytes; `write` now also accepts `bytes` alongside `str`.

## 0.1.0

Initial release.

- Sync `Server4Agent` and async `AsyncServer4Agent` clients.
- Resource-oriented handles: `create()`/`get()` return `Server`/`Project`/`Task`/`Build`
  objects with scoped sub-resources (`server.tasks`, `server.files`, ...) and actions
  (`server.exec()`, `server.deploy()`, `project.update()`).
- `task.wait()` / `build.wait()` poll until a terminal state, with an `on_poll` callback.
- Typed error hierarchy under `Server4AgentError` (`NotFoundError`, `RateLimitError`,
  `APIStatusError`, ...) exposing `status`, `code`, and `request_id`.
- Automatic retries with exponential backoff + jitter; retries are method-aware so a
  `POST` is never retried in a way that could double-create. Configurable `timeout`
  and `max_retries`.
- Webhook signature verification (`verify_webhook_signature`).
- Ships `py.typed`. Opt-in request logging via `SERVER4AGENT_LOG=debug`.
