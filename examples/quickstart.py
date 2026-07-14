"""Provision a server, have the agent build an app, and print the live URL.

    SERVER4AGENT_API_KEY=sk_live_... python quickstart.py
"""
from server4agent import Server4Agent


def main() -> None:
    with Server4Agent() as client:
        server = client.servers.create(tier="small")
        print(f"server {server.id} ({server.status})")

        build = server.builds.start("a FastAPI todo API with a small web UI")
        build.wait(on_poll=lambda b: print(f"  build: {b.status}"))
        print("live at", build.url)


if __name__ == "__main__":
    main()
