# server4agent

Python client for the [Server4Agent](https://www.server4agent.com) REST API:
sync **and** async, fully typed, with automatic retries.

This SDK is for the code *around* your agent: your backend, a script, a
notebook, a webhook receiver. If your agent itself does tool-calling, point it
at the [MCP server](https://www.server4agent.com/docs/mcp) directly; it doesn't
need this package.

## Install

```bash
pip install server4agent
```

Requires Python 3.11+. Depends only on [`httpx`](https://www.python-httpx.org/).

## Quickstart

```python
from server4agent import Server4Agent

client = Server4Agent()  # reads SERVER4AGENT_API_KEY from the environment

# create() returns a handle you can act on directly
server = client.servers.create(tier="small")

# kick off a build and block until it's live
build = server.builds.start("a FastAPI todo API with a web UI").wait()
print(build.url)

# run a command right on the server
print(server.exec("ls -la"))
```

`api_key` can also be passed explicitly: `Server4Agent(api_key="sk_live_...")`.
Use it as a context manager to close the connection pool:

```python
with Server4Agent() as client:
    ...
```

## Async

The async client mirrors the sync one method-for-method:

```python
import asyncio
from server4agent import AsyncServer4Agent

async def main():
    async with AsyncServer4Agent() as client:
        server = await client.servers.create(tier="small")
        task = await server.tasks.create("Scrape today's HN front page to JSON.")
        await task.wait()
        print(task.status, task.result)

asyncio.run(main())
```

## Handles

`create()` and `get()` return rich handles, data records you can also act on:

```python
server = client.servers.get("srv_abc")
server.tasks.create("...")          # sub-resources are scoped to the server
server.files.write("app.py", "...")
server.deploy()
server.refresh()                    # re-fetch in place

project = client.projects.get("prj_xyz")
project.update(visibility="public")
# Ask visitors for a password before the URL opens. No account needed to get in.
project.update(access="password", access_password="a long shared passphrase")
```

Binary files (e.g. a generated `.xlsx`) round-trip through `write`/`read_binary`
without corruption, and `get_download_url` mints a shareable, expiring link:

```python
server.files.write("report.xlsx", xlsx_bytes)          # bytes -> base64 on the wire
data = server.files.read_binary("report.xlsx")         # decoded back to bytes

link = server.files.get_download_url("report.xlsx", ttl_seconds=3600)
print(link["url"], link["expires_at"])
```

`server.kind` says what sort of box you are holding: `"standard"` is headless,
`"browser"` has a screen with a browser on it, for sites an agent has to drive
rather than fetch. It is orthogonal to `tier`, and a nickname proves nothing, so
read the field rather than the name:

```python
server = client.servers.create(tier="medium", kind="browser")
with_screens = [s for s in client.servers.list() if s.kind == "browser"]
```

`task.wait()` / `build.wait()` poll until a terminal state; both accept
`poll_interval` and `timeout` (seconds).

Not every task in a server's list is one you sent. `task.message_type` is
`"system"` for runs the platform starts for you, such as the bring-up of a
project imported from git, and `"user"` for your own. A system task's `goal` is
a summary of what the run does, not the instructions its agent was given:

```python
mine = [t for t in server.tasks.list() if t.message_type == "user"]
```

`project_id` runs a task in one project's folder, and `follow_up_of` continues
an earlier task's conversation, e.g. to answer one that is `awaiting_input`:

```python
task = server.tasks.create("Add a dark mode toggle", project_id=project.id)
reply = server.tasks.create("Use the system setting by default", follow_up_of=task.id)
```

## Starting and deploying an app

`project.start()` runs the app as a background process with `PORT` set to the
project's routed port, keeps it running across restarts, and says whether it
answered. `deploy()` then makes the project public once something is listening:

```python
from server4agent import ConflictError

try:
    started = project.start("node server.js")
    print(started.status, started.listening)       # "running" True
except ConflictError as e:
    print(e.body.get("log_tail"))                   # the app's last output

url = project.deploy()                              # or server.deploy(project_id=...)
print(url, url.status)                              # "live", or "publishing" if not answering yet
```

`server.logs(project_id=None, lines=None)` (or `project.logs()`) returns the
log-file tails of the server's background processes, or `None` before any
were reported. When the server cannot be read live the API answers with its
last saved copy, so check `updated_at` before trusting a tail as current:

```python
snap = project.logs(lines=50)
if snap:
    print(snap.updated_at)
    for f in snap.files:
        print(f.path, f.tail)
```

## Secrets

Secrets are environment variables injected into agent runs and bring-up.
Values are write-only: reads return the key, a note, a masked hint and where it
is bound, never the value.

```python
from server4agent import SecretInput

client.secrets.set("STRIPE_KEY", "sk_test_...", description="test mode")
server.secrets.set([{"key": "DATABASE_URL", "value": "postgres://..."}])   # every project on the server
project.secrets.set([SecretInput("SENTRY_DSN", "https://...")])            # this project only
print([s.key for s in project.secrets.list()])
```

`servers.create()` and `projects.create()` also take `secrets=[...]`, so a
project seeded from git can come up without stopping to ask for them:

```python
project = server.projects.create(
    source={"kind": "git", "git_url": "https://github.com/northstar/app"},
    secrets=[{"key": "DATABASE_URL", "value": "postgres://..."}],
)
print(project.init_phase, project.init_run_id)      # bring-up starts on its own
project.init(proceed=True)                          # resume one waiting on secrets
```

## Moving a project

`export()` packs a project into one file behind a time-limited link, and
`import_bundle()` recreates it on any server, here or on another deployment.
A bundle carries the names of the environment variables it needs, never their
values:

```python
import httpx

bundle = project.export(ttl_seconds=3600)
data = httpx.get(bundle.url).content

result = other_server.projects.import_bundle(data, name="landing-copy")
print(result.project.id, result.missing_env)        # set these before the app can run
```

## Account and budget

```python
me = client.me()
print(me.account["plan"], me.usage.spend_usd, me.usage.hard_limit_usd)
if me.usage.blocked:
    print("new tasks are refused:", me.usage.reason)
```

## Errors

Every failure is a subclass of `Server4AgentError`, so you can catch broadly or
narrowly:

```python
from server4agent import NotFoundError, RateLimitError, APIStatusError

try:
    client.servers.get("srv_missing")
except NotFoundError:
    ...                              # 404
except RateLimitError as e:
    ...                              # 429 (already retried; quote e.request_id to support)
except APIStatusError as e:
    print(e.status, e.code, e.message, e.request_id)
```

Retryable failures (429, 5xx, network blips) are retried automatically with
exponential backoff. Tune it per client:

```python
Server4Agent(timeout=30.0, max_retries=4)
```

## Verifying webhooks

```python
import os
from server4agent import verify_webhook_signature

# request.data must be the raw body. Parse it as JSON only after verifying.
ok = verify_webhook_signature(
    secret=os.environ["SERVER4AGENT_WEBHOOK_SECRET"],
    body=request.data.decode("utf-8"),
    header=request.headers.get("Server4Agent-Signature"),
)
if not ok:
    return "invalid signature", 401
```

A delivery that times out or gets a 408, 425, 429 or 5xx is sent again, up to
four attempts in all, so the same event can arrive twice. Every payload has an
`id` (also in the `Server4Agent-Event-Id` header) that stays the same across
attempts: skip an `id` you have already handled.

## Examples

Runnable scripts in [`examples/`](examples/):

| File | What it shows |
| --- | --- |
| [`quickstart.py`](examples/quickstart.py) | Provision a server, build an app, print the live URL. |
| [`async_quickstart.py`](examples/async_quickstart.py) | The same flow on the async client. |
| [`exec_and_files.py`](examples/exec_and_files.py) | Write a file, install a dependency, and run it on the durable workspace. |
| [`await_input.py`](examples/await_input.py) | Handle a task that pauses to ask a question (`awaiting_input`). |
| [`preview_env.py`](examples/preview_env.py) | Ephemeral per-PR preview URL for CI. |
| [`webhook_receiver.py`](examples/webhook_receiver.py) | Verify webhook signatures in a Flask handler. |

## API surface

`servers`, `projects`, `templates`, `tasks`, `builds`, `files`, `secrets`,
`keys`, `webhooks`, and `client.me()`. Every REST operation has a
method on both clients; see the
[REST API docs](https://www.server4agent.com/docs/api) for the endpoints each
one wraps.

Server-side only: this SDK holds your `sk_live_` key. Never embed it in a
notebook or script you share.
