"""Give the on-server agent a goal and await the result, using the async client.

    SERVER4AGENT_API_KEY=sk_live_... python async_quickstart.py
"""
import asyncio

from server4agent import AsyncServer4Agent


async def main() -> None:
    async with AsyncServer4Agent() as client:
        server = await client.servers.create(tier="small")
        task = await server.tasks.create("Summarize today's top HN posts into summary.md")
        await task.wait(on_poll=lambda t: print("task:", t.status))
        print(task.result)


if __name__ == "__main__":
    asyncio.run(main())
