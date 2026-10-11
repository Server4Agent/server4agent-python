"""Ephemeral preview environment for CI: one disposable public URL per PR.
Run it on PR open; call project.cleanup() on PR close.

    PR_NUMBER=42 PR_BRANCH=feature/login REPO_URL=https://github.com/you/app \\
    SERVER4AGENT_API_KEY=sk_live_... python preview_env.py

START_COMMAND overrides how the app starts (default "npm start"). It must stay
in the foreground and listen on $PORT.
"""
import os

from server4agent import ConflictError, Server4Agent


def main() -> None:
    pr = os.environ.get("PR_NUMBER", "0")
    branch = os.environ.get("PR_BRANCH", "main")
    repo = os.environ.get("REPO_URL", "https://github.com/you/app")
    with Server4Agent() as client:
        server = client.servers.create(task=f"pr-{pr}")
        project = server.projects.create(name=f"pr-{pr}", lifecycle="ephemeral")

        # A project's URL serves whatever listens on its port, so the code has
        # to live and run in the project's folder, not at the server root. A
        # task given the project works in that folder.
        task = server.tasks.create(
            f"Clone {repo} at branch {branch} into this folder, install its "
            "dependencies and build it. Do not start it.",
            project_id=project.id,
        )
        task.wait(timeout=900, on_poll=lambda t: print("task:", t.status))
        if task.status != "completed":
            raise SystemExit(f"Preview build {task.status}: {task.question or task.result or ''}")

        # Run the app on the project's port; it keeps running across restarts.
        try:
            project.start(os.environ.get("START_COMMAND", "npm start"))
        except ConflictError as err:
            print(err.body.get("log_tail"))  # why it exited
            raise

        # deploy() builds and starts nothing: it checks the app is listening,
        # makes the project public, and waits for the URL to answer.
        url = project.deploy()
        print(f"Preview ({url.status}): {url}")
        # On PR close, tear it down:
        # project.cleanup()


if __name__ == "__main__":
    main()
