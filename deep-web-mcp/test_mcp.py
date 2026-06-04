import asyncio
from mcp.client.sse import sse_client
from mcp.client.session import ClientSession

async def main():
    async with sse_client("http://calendar-mcp:8000/mcp", headers={"Authorization": "Bearer eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiJ0ZXN0IiwiaXNzIjoiY2FsZW5kYXItbWNwLWlzc3VlciIsImF1ZCI6ImNhbGVuZGFyLW1jcC1hcGkifQ.4h3c-sHhMANe9ipqBnNScBrjHk2wZUh3U53VlkZpc_0"}) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            tools = await session.list_tools()
            print([t.name for t in tools.tools])

asyncio.run(main())
