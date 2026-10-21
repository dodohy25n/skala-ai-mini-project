import asyncio

from agent import systems


class ListOnlyMCP:
    def __init__(self, result=None, fail=False):
        self.result, self.fail = result, fail

    async def call_tool(self, name, arguments, server="ops"):
        if self.fail:
            raise ConnectionError("mcp down")
        return self.result


def test_refresh_updates_the_shared_set_in_place():
    shared = systems.KNOWN_SYSTEMS
    saved = set(shared)
    try:
        source = asyncio.run(systems.refresh_from_mcp(ListOnlyMCP({"systems": ["new-service"]})))
        assert source == "mcp"
        assert shared == {"new-service"} and systems.KNOWN_SYSTEMS is shared
    finally:
        shared.clear()
        shared.update(saved)


def test_refresh_falls_back_to_file_list_when_mcp_fails():
    before = set(systems.KNOWN_SYSTEMS)
    assert asyncio.run(systems.refresh_from_mcp(ListOnlyMCP(fail=True))) == "file"
    assert systems.KNOWN_SYSTEMS == before


def test_mentioned_systems_keep_question_order():
    q = "gateway-service에서 payments-service로 가는 요청"
    assert systems.mentioned_systems(q) == ["gateway-service", "payments-service"]


def test_guess_target_defaults_when_no_system_named():
    assert systems.guess_target("로그인이 느려요") == "auth-service"
