# server4agent

Python client for the [Server4Agent](https://www.server4agent.com) REST API —
sync **and** async, fully typed, with automatic retries.

This SDK is for the code *around* your agent — your backend, a script, a
notebook, a webhook receiver. If your agent itself does tool-calling, point it
at the [MCP server](https://www.server4agent.com/docs/mcp) directly; it doesn't
need this package.

## Install

```bash
pip install server4agent
```

Requires Python 3.9+. Depends only on [`httpx`](https://www.python-httpx.org/).

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

`create()` and `get()` return rich handles — data records you can also act on:

```python
server = client.servers.get("srv_abc")
server.tasks.create("...")          # sub-resources are scoped to the server
server.files.write("app.py", "...")
server.deploy()
server.refresh()                    # re-fetch in place

project = client.projects.get("prj_xyz")
project.update(visibility="public")
```

`task.wait()` / `build.wait()` poll until a terminal state; both accept
`poll_interval` and `timeout` (seconds).

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

# request.data must be the raw body — parse it as JSON only after verifying.
ok = verify_webhook_signature(
    secret=os.environ["SERVER4AGENT_WEBHOOK_SECRET"],
    body=request.data.decode("utf-8"),
    header=request.headers.get("Server4Agent-Signature"),
)
if not ok:
    return "invalid signature", 401
```

## API surface

`servers`, `projects`, `templates`, `tasks`, `builds`, `files`, `keys`,
`webhooks` — see the [REST API docs](https://www.server4agent.com/docs/api) for
the endpoints each method wraps.

Server-side only: this SDK holds your `sk_live_` key. Never embed it in a
notebook or script you share.
