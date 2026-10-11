"""Write a file, install a dependency, and run it, all on the server.
The workspace is durable, so the file and the installed package persist.

    SERVER4AGENT_API_KEY=sk_live_... python exec_and_files.py
"""
from server4agent import Server4Agent


def main() -> None:
    with Server4Agent() as client:
        server = client.servers.create(tier="small")
        print(f"server {server.id} ({server.status})")

        server.files.write(
            "hello.py",
            "import httpx\nprint('httpx', httpx.__version__)\n",
        )

        server.exec("pip install httpx")
        print(server.exec("python hello.py"))

        print("files:", server.files.list())


if __name__ == "__main__":
    main()
