"""No device service may be called by any path in preview mode."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.components import conversation
from homeassistant.core import Context
from homeassistant.helpers import intent

from custom_components.typesafe_conversation.agent import AgentSettings, TypeSafeAgent
from custom_components.typesafe_conversation.router import Plan, Route
from custom_components.typesafe_conversation.system_one import SystemOneUnavailableError


def _input():
    return conversation.ConversationInput(
        text="turn on the lamp",
        context=Context(),
        conversation_id=None,
        device_id=None,
        satellite_id=None,
        language="en",
        agent_id="conversation.test",
    )


@pytest.fixture(name="agent")
def agent_fixture(hass):
    return TypeSafeAgent(
        hass,
        SimpleNamespace(domains=(), entities=(), areas=()),
        AsyncMock(),
        None,
        AgentSettings(execution_enabled=False),
    )


@pytest.mark.parametrize("route", [Route.COMMAND, Route.CONFIRM, Route.CANCEL])
async def test_preview_blocks_all_actuation_routes(agent, route):
    with (
        patch("custom_components.typesafe_conversation.agent.async_execute") as execute,
        patch(
            "custom_components.typesafe_conversation.agent.async_execute_cancel"
        ) as cancel,
    ):
        response = await agent._carry_out(
            Plan(route, action="turn_on"), None, _input(), None
        )
    execute.assert_not_called()
    cancel.assert_not_called()
    assert "nothing executed" in response.speech["plain"]["speech"]


async def test_outage_does_not_bypass_preview_via_local_intents(agent):
    agent._ask = AsyncMock(side_effect=SystemOneUnavailableError("offline"))
    with patch("homeassistant.components.conversation.async_handle_intents") as local:
        result = await agent.async_process(_input(), None)
    local.assert_not_called()
    assert result.error_code is not None


async def test_api_failure_does_not_override_disabled_local_fallback(agent):
    agent.settings.execution_enabled = True
    agent.settings.local_fallback_enabled = False
    agent.llm = AsyncMock()
    with patch("homeassistant.components.conversation.async_handle_intents") as local:
        result = await agent._fallback(_input(), None, None)
    local.assert_not_called()
    agent.llm.answer_freeform.assert_not_called()
    assert result.error_code is not None


async def test_preview_query_does_not_call_the_local_command_matcher(agent):
    response = intent.IntentResponse(language="en")
    response.async_set_speech("The lamp is off.")
    with (
        patch("homeassistant.components.conversation.async_handle_intents") as local,
        patch(
            "custom_components.typesafe_conversation.agent.async_execute_query",
            return_value=response,
        ) as query,
    ):
        result = await agent._carry_out(
            Plan(Route.QUERY, query_kind="state"),
            None,
            _input(),
            None,
        )
    local.assert_not_called()
    query.assert_called_once()
    assert result is response


async def test_compound_preview_never_executes_subcommands(agent):
    agent.llm = SimpleNamespace(split_compound=AsyncMock(return_value=["a", "b"]))
    agent._ask = AsyncMock(return_value=(None, ()))
    with (
        patch(
            "custom_components.typesafe_conversation.agent.route",
            return_value=Plan(Route.COMMAND, action="turn_on"),
        ),
        patch("custom_components.typesafe_conversation.agent.async_execute") as execute,
    ):
        result = await agent._handle_compound(_input(), None)
    execute.assert_not_called()
    assert "nothing executed" in result.speech["plain"]["speech"]


async def test_compound_preflight_blocks_every_step_if_one_is_ambiguous(agent):
    agent.settings.execution_enabled = True
    agent.llm = SimpleNamespace(split_compound=AsyncMock(return_value=["a", "b"]))
    agent._ask = AsyncMock(return_value=(None, ()))
    with (
        patch(
            "custom_components.typesafe_conversation.agent.route",
            side_effect=[
                Plan(Route.COMMAND, action="turn_on"),
                Plan(Route.CLARIFY),
            ],
        ),
        patch("custom_components.typesafe_conversation.agent.async_execute") as execute,
    ):
        result = await agent._handle_compound(_input(), None)
    execute.assert_not_called()
    assert result.error_code is not None
