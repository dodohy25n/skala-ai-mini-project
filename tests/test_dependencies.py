import asyncio

from agent.mcp_client import MCPClient
from tests.conftest import DEPENDENCIES, MONITORING, FakeMCP


def test_dependency_data_covers_exactly_the_monitored_systems():
    assert set(DEPENDENCIES) == set(MONITORING)
    for system, depends_on in DEPENDENCIES.items():
        assert set(depends_on) <= set(MONITORING)
        assert system not in depends_on


def test_get_dependencies_tool_through_real_mcp_server():
    async def go():
        client = MCPClient(flaky_first_tool_call=False)
        await client.start()
        try:
            return [await client.call_tool("get_dependencies", {"system_name": s})
                    for s in ("web-01", "gateway-service", "billing-service")]
        finally:
            await client.stop()

    assert asyncio.run(go()) == [
        {"system_name": "web-01", "depends_on": ["auth-service"]},
        {"system_name": "gateway-service", "depends_on": ["payments-service"]},
        {"system_name": "billing-service", "depends_on": []},
    ]


def test_fake_mcp_answers_get_dependencies_and_counts_the_call():
    mcp = FakeMCP()
    data = asyncio.run(mcp.call_tool("get_dependencies", {"system_name": "payments-service"}))
    assert data == {"system_name": "payments-service", "depends_on": ["cache-service"]}
    assert mcp.calls["call_tool"] == 1
