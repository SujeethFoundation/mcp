"""Tests that tool calls run on worker threads, so one slow search cannot stop the server answering anything else."""
import asyncio
import inspect
import os
import time

os.environ.setdefault("MCP_TRUSTED_RANGE_URLS", "none")

import mcp_server as server  # noqa: E402


def test_every_registered_tool_is_async():
    """FastMCP runs a plain `def` tool directly on the event loop. Every tool must be wrapped."""
    tools = server.mcp._tool_manager.list_tools()
    assert tools, "no tools registered"
    assert [t.name for t in tools if not t.is_async] == []


def test_tool_parameters_survive_the_wrapper():
    by_name = {t.name: t for t in server.mcp._tool_manager.list_tools()}
    assert list(by_name["get_documents"].parameters["properties"]) == ["urls", "max_chars"]
    assert "query" in by_name["search_sujeeth_foundation"].parameters["properties"]
    assert by_name["search_sujeeth_foundation"].description  # the docstring the agent reads


def test_wrapper_keeps_name_docstring_and_signature():
    def sample(query: str, limit: int = 5):
        """Sample docstring."""
        return query, limit

    wrapped = server.in_thread(sample)
    assert wrapped.__name__ == "sample" and wrapped.__doc__ == "Sample docstring."
    assert list(inspect.signature(wrapped).parameters) == ["query", "limit"]
    assert inspect.iscoroutinefunction(wrapped)
    assert asyncio.run(wrapped("q", limit=2)) == ("q", 2)


def test_blocking_calls_overlap_instead_of_queueing():
    def slow():
        time.sleep(0.2)
        return "done"

    wrapped = server.in_thread(slow)

    async def run():
        began = time.perf_counter()
        results = await asyncio.gather(*[wrapped() for _ in range(8)])
        return results, time.perf_counter() - began

    results, seconds = asyncio.run(run())
    assert results == ["done"] * 8
    assert seconds < 0.8, f"8 calls of 0.2s took {seconds:.2f}s; they ran one at a time"


def test_event_loop_keeps_running_during_a_blocking_call():
    def slow():
        time.sleep(0.3)

    wrapped = server.in_thread(slow)

    async def run():
        ticks = 0

        async def ticker():
            nonlocal ticks
            while True:
                await asyncio.sleep(0.01)
                ticks += 1

        task = asyncio.create_task(ticker())
        await wrapped()
        task.cancel()
        return ticks

    assert asyncio.run(run()) >= 15  # a blocked loop would manage about 0


def test_errors_inside_a_tool_reach_the_caller():
    def boom():
        raise ValueError("bad")

    try:
        asyncio.run(server.in_thread(boom)())
    except ValueError as exc:
        assert str(exc) == "bad"
    else:
        raise AssertionError("the error was swallowed")
