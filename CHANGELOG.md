# Changelog

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
