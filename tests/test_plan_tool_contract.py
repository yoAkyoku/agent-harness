from agent_harness.tools import model_tools


def test_plan_tool_limits_visible_plan_to_actions_before_source_inspection() -> None:
    plan = next(
        tool["function"]
        for tool in model_tools()
        if tool["function"]["name"] == "plan_update"
    )

    description = plan["description"]
    assert "actions and their order only" in description
    assert "Do not guess field names, column names, values, or missing items before inspecting sources." in description
