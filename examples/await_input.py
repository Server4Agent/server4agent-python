"""Start a task and handle the case where the on-server agent stops to ask
a question. wait() returns as soon as the task reaches a terminal state,
and "awaiting_input" is one of them.

    SERVER4AGENT_API_KEY=sk_live_... python await_input.py
"""
from server4agent import Server4Agent


def main() -> None:
    with Server4Agent() as client:
        server = client.servers.create(tier="small")
        task = server.tasks.create(
            "Refactor the auth module. Ask me before changing any public API."
        )

        task.wait(on_poll=lambda t: print("task:", t.status))

        if task.status == "completed":
            print("done:", task.result)
        elif task.status == "awaiting_input":
            print("agent asks:", task.question)
            # Answer by starting a follow-up task on the same server:
            # server.tasks.create("Yes, go ahead. Keep the response shape.")
        else:
            print("ended as:", task.status)


if __name__ == "__main__":
    main()
