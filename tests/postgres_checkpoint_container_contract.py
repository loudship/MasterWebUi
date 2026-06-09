"""Validate AsyncPostgresSaver setup, resume lookup, and deletion."""

from __future__ import annotations

import asyncio
import os
from typing import TypedDict

from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer
from langgraph.graph import END, StateGraph


class State(TypedDict):
    value: int


async def increment(state: State) -> dict:
    return {"value": state["value"] + 1}


async def main() -> None:
    config = {"configurable": {"thread_id": "checkpoint-contract"}}
    async with AsyncPostgresSaver.from_conn_string(
        os.environ["POSTGRES_LANGGRAPH_URL"],
        serde=JsonPlusSerializer(pickle_fallback=False),
    ) as saver:
        await saver.setup()
        workflow = StateGraph(State)
        workflow.add_node("increment", increment)
        workflow.set_entry_point("increment")
        workflow.add_edge("increment", END)
        graph = workflow.compile(checkpointer=saver)

        result = await graph.ainvoke({"value": 1}, config=config)
        assert result["value"] == 2
        checkpoint = await saver.aget(config)
        assert checkpoint is not None
        assert checkpoint["channel_values"]["value"] == 2

        await saver.adelete_thread("checkpoint-contract")
        assert await saver.aget(config) is None
    print("postgres-checkpoint-contract-ok")


if __name__ == "__main__":
    asyncio.run(main())
