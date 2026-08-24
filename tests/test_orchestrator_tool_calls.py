"""Regression test for the orchestrator emitting multiple tool_calls.

Some OpenAI-compatible endpoints (e.g. DeepSeek) ignore ``parallel_tool_calls=False``
and return several tool_calls in one assistant message. The orchestrator's router and
responder nodes only answer the first call, so any extra calls are left unanswered and
the next LLM call fails with OpenAI 400 "insufficient tool messages following
tool_calls message". The orchestrator node must therefore trim the response to a single
tool_call so every emitted call gets a matching ToolMessage.
"""

from unittest.mock import MagicMock

from langchain_core.messages import AIMessage, HumanMessage

from biodsa.agents.deepevidence.agent import DeepEvidenceAgent
from biodsa.agents.deepevidence.state import DeepEvidenceAgentState


def test_orchestrator_node_trims_to_single_tool_call():
    # Arrange: an agent whose heavy dependencies are mocked out, so we can drive
    # _orchestrator_agent_node directly without a live model or graph.
    agent = DeepEvidenceAgent.__new__(DeepEvidenceAgent)
    agent.main_search_rounds_budget = 5
    agent.main_action_rounds_budget = 25
    agent.model_name = "test-model"
    agent.model_kwargs = {}
    agent.light_mode = True
    agent._get_tools_for_orchestrator_agent = MagicMock(return_value=[])
    agent._build_system_prompt_for_orchestrator_agent = MagicMock(return_value="system")
    agent._build_existing_entities_prompt = MagicMock(return_value=None)
    agent._get_input_output_tokens = MagicMock(return_value=(0, 0))

    multi_call = AIMessage(
        content="",
        tool_calls=[
            {
                "name": "go_breadth_first_search",
                "args": {"search_target": "x", "knowledge_bases": ["gene"]},
                "id": "call-1",
            },
            {
                "name": "AddToGraph",
                "args": {},
                "id": "call-2",
            },
        ],
    )
    agent._call_model = MagicMock(return_value=multi_call)

    state = DeepEvidenceAgentState(
        messages=[HumanMessage(content="query")],
        knowledge_bases=["gene"],
    )

    # Act
    result = agent._orchestrator_agent_node(state, None)

    # Assert: exactly one tool_call survives, so the responder can answer it fully.
    assert len(result["messages"]) == 1
    last_message = result["messages"][-1]
    assert last_message.tool_calls is not None
    assert len(last_message.tool_calls) == 1
    assert last_message.tool_calls[0]["name"] == "go_breadth_first_search"
