"""Tests for ``AnthropicClient.complete_tool_use``.

Mocks the Anthropic SDK via monkeypatch so we control the response
content blocks precisely. PII scrubbing runs for real against the
Presidio engine so the load-bearing privacy guarantee is exercised
end-to-end (no monkeypatched scrubber here — that would defeat the
test's purpose).
"""
from dataclasses import dataclass

import anthropic
import httpx
import pytest
from pydantic import SecretStr

from coworker.connectors.anthropic_client import (
    AnthropicClient,
    ToolUseResult,
)
from coworker.connectors.exceptions import (
    ConnectorAuthError,
    ConnectorRateLimited,
    ConnectorTransient,
)

# ---------------------------------------------------------------------------
# SDK response stubs
# ---------------------------------------------------------------------------


@dataclass
class _StubBlock:
    """Loose stand-in for anthropic.types.TextBlock / ToolUseBlock."""

    type: str
    text: str | None = None
    id: str | None = None
    name: str | None = None
    input: dict | None = None


@dataclass
class _StubUsage:
    input_tokens: int = 100
    output_tokens: int = 20


@dataclass
class _StubResponse:
    content: list[_StubBlock]
    stop_reason: str = "tool_use"
    model: str = "claude-sonnet-4-6"
    usage: _StubUsage = None  # type: ignore[assignment]

    def __post_init__(self):
        if self.usage is None:
            self.usage = _StubUsage()


@dataclass
class _CapturedCall:
    kwargs: dict


class _FakeMessages:
    """Mimics anthropic.AsyncAnthropic.messages with capturable create()."""

    def __init__(self):
        self.responses: list = []  # _StubResponse | Exception
        self.calls: list[_CapturedCall] = []

    async def create(self, **kwargs):
        self.calls.append(_CapturedCall(kwargs=kwargs))
        nxt = self.responses.pop(0)
        if isinstance(nxt, Exception):
            raise nxt
        return nxt


@pytest.fixture
def patched_client(monkeypatch):
    """AnthropicClient with the SDK swapped for a fake."""
    client = AnthropicClient(firm_id="test-firm", api_key=SecretStr("k"))
    fake_messages = _FakeMessages()
    monkeypatch.setattr(client._client, "messages", fake_messages)
    return client, fake_messages


def _text(text: str) -> _StubBlock:
    return _StubBlock(type="text", text=text)


def _tool_use(name: str, input_data: dict, tool_use_id: str = "tu_1") -> _StubBlock:
    return _StubBlock(type="tool_use", id=tool_use_id, name=name, input=input_data)


# ===========================================================================
# Happy path + return shape
# ===========================================================================


async def test_returns_tool_use_result_with_content_blocks(patched_client) -> None:
    client, fake = patched_client
    fake.responses.append(
        _StubResponse(content=[_text("done"), _tool_use("memory_query", {"q": "x"})])
    )

    result = await client.complete_tool_use(
        messages=[{"role": "user", "content": "hello"}],
        system=None,
        tools=[],
        model="claude-sonnet-4-6",
        max_tokens=1000,
    )

    assert isinstance(result, ToolUseResult)
    assert result.stop_reason == "tool_use"
    assert result.input_tokens == 100
    assert len(result.content) == 2
    assert result.content[0]["type"] == "text"
    assert result.content[0]["text"] == "done"
    assert result.content[1]["type"] == "tool_use"
    assert result.content[1]["name"] == "memory_query"
    assert result.content[1]["input"] == {"q": "x"}


async def test_tools_are_forwarded_to_sdk(patched_client) -> None:
    client, fake = patched_client
    fake.responses.append(_StubResponse(content=[_text("ok")], stop_reason="end_turn"))

    tools = [
        {
            "name": "memory_query",
            "description": "Search memory.",
            "input_schema": {"type": "object", "properties": {}},
        }
    ]
    await client.complete_tool_use(
        messages=[{"role": "user", "content": "hi"}],
        system="be helpful",
        tools=tools,
        model="claude-sonnet-4-6",
        max_tokens=500,
    )

    kwargs = fake.calls[0].kwargs
    assert kwargs["tools"] == tools
    assert kwargs["system"] == "be helpful"
    assert kwargs["model"] == "claude-sonnet-4-6"
    assert kwargs["max_tokens"] == 500


# ===========================================================================
# PII scrubbing + restoration
# ===========================================================================


async def test_pii_scrubbed_in_user_message_before_sdk_call(patched_client) -> None:
    """A TFN in a user message must not appear in the outbound body."""
    client, fake = patched_client
    fake.responses.append(_StubResponse(content=[_text("ok")], stop_reason="end_turn"))

    # 9-digit TFN per ATO format (the recogniser triggers on any 8-9 digit run).
    user_text = "Process this: TFN 123 456 789 for the client."
    await client.complete_tool_use(
        messages=[{"role": "user", "content": user_text}],
        system=None,
        tools=[],
        model="claude-sonnet-4-6",
        max_tokens=500,
    )

    sent = fake.calls[0].kwargs["messages"][0]["content"]
    # The TFN digits must have been replaced with a placeholder.
    assert "123 456 789" not in sent
    assert "123456789" not in sent


async def test_placeholder_restored_in_response_text_block(patched_client) -> None:
    """Claude's response with a placeholder gets restored to real data."""
    client, _ = patched_client

    # We rig the test by scrubbing a known input first, then crafting a
    # response that references the placeholder. The PII scrubber returns
    # a ScrubResult with `mapping` we can read indirectly by sending the
    # user text and noting what comes back in the call.
    user_text = "Forward to alice@example.com please."

    async def _peek(messages, system, tools, model, max_tokens, thinking_budget=None):
        # Stage 1: capture the placeholder Anthropic would have seen.
        return None  # not used

    # Trick: instead of two stages, use the SAME run — set up a response
    # that includes whatever the scrubber produced for the email. We
    # don't know the placeholder up front, so we configure the fake to
    # echo back the user message verbatim, then check the response has
    # the original email restored.
    def _echo_response(**kwargs):
        # The SDK's create() is async — but we're configuring the stub.
        pass

    # Simpler: capture the placeholder by inspecting kwargs after one
    # call where the response references that same placeholder.
    captured_placeholder = {"value": None}

    class _SmartFake(_FakeMessages):
        async def create(self, **kwargs):
            self.calls.append(_CapturedCall(kwargs=kwargs))
            sent_msg = kwargs["messages"][0]["content"]
            # Find any [TYPE_xxx] placeholder in the sent text — the
            # email may be tagged EMAIL_ADDRESS or PERSON depending
            # on which recogniser scored highest.
            import re
            match = re.search(r"\[[A-Z_]+_[A-Za-z0-9]+\]", sent_msg)
            if match is None:
                raise AssertionError(
                    f"no placeholder found in scrubbed message: {sent_msg!r}"
                )
            placeholder = match.group(0)
            captured_placeholder["value"] = placeholder
            return _StubResponse(
                content=[_text(f"I will forward to {placeholder}")],
                stop_reason="end_turn",
            )

    smart_fake = _SmartFake()
    monkeypatch_messages(client, smart_fake)

    result = await client.complete_tool_use(
        messages=[{"role": "user", "content": user_text}],
        system=None,
        tools=[],
        model="claude-sonnet-4-6",
        max_tokens=500,
    )

    # The response text should have the placeholder restored to the
    # original email address.
    assert captured_placeholder["value"] is not None
    text_block = result.content[0]
    assert text_block["type"] == "text"
    assert "alice@example.com" in text_block["text"]
    assert captured_placeholder["value"] not in text_block["text"]


async def test_placeholder_restored_in_tool_use_input(patched_client) -> None:
    """Tool_use input dict values with placeholders get restored too."""
    client, _ = patched_client
    user_text = "Email bob@example.com about the BAS."

    captured = {"placeholder": None}

    class _SmartFake(_FakeMessages):
        async def create(self, **kwargs):
            import re
            sent = kwargs["messages"][0]["content"]
            match = re.search(r"\[[A-Z_]+_[A-Za-z0-9]+\]", sent)
            if match is None:
                raise AssertionError(
                    f"no placeholder in scrubbed message: {sent!r}"
                )
            placeholder = match.group(0)
            captured["placeholder"] = placeholder
            return _StubResponse(
                content=[
                    _tool_use(
                        "email_create_draft",
                        {
                            "to": [placeholder],
                            "subject": "BAS query",
                            "body": f"Hi {placeholder}, ...",
                        },
                    )
                ],
                stop_reason="tool_use",
            )

    smart_fake = _SmartFake()
    monkeypatch_messages(client, smart_fake)

    result = await client.complete_tool_use(
        messages=[{"role": "user", "content": user_text}],
        system=None,
        tools=[],
        model="claude-sonnet-4-6",
        max_tokens=500,
    )

    tool_use_block = result.content[0]
    assert tool_use_block["type"] == "tool_use"
    assert tool_use_block["input"]["to"] == ["bob@example.com"]
    assert "bob@example.com" in tool_use_block["input"]["body"]
    assert captured["placeholder"] not in tool_use_block["input"]["body"]


async def test_tool_result_content_is_scrubbed(patched_client) -> None:
    """A tool_result block carrying real PII gets scrubbed on the wire."""
    client, fake = patched_client
    fake.responses.append(_StubResponse(content=[_text("ack")], stop_reason="end_turn"))

    messages = [
        {"role": "user", "content": "go"},
        {
            "role": "assistant",
            "content": [
                _tool_use_dict("memory_query", {"q": "Alice"}, "tu_1"),
            ],
        },
        {
            "role": "user",
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": "tu_1",
                    "content": "Found contact: alice@firm.com.au, ABN 12 345 678 901",
                }
            ],
        },
    ]
    await client.complete_tool_use(
        messages=messages,
        system=None,
        tools=[],
        model="claude-sonnet-4-6",
        max_tokens=500,
    )

    sent_messages = fake.calls[0].kwargs["messages"]
    last_user = sent_messages[-1]
    tool_result_block = last_user["content"][0]
    # The PII strings should have been replaced with placeholders.
    content = tool_result_block["content"]
    assert "alice@firm.com.au" not in content
    assert "12 345 678 901" not in content


# ===========================================================================
# Input validation
# ===========================================================================


async def test_empty_messages_raises_value_error(patched_client) -> None:
    client, _ = patched_client
    with pytest.raises(ValueError, match="at least one"):
        await client.complete_tool_use(
            messages=[],
            system=None,
            tools=[],
            model="claude-sonnet-4-6",
            max_tokens=500,
        )


async def test_invalid_max_tokens_raises_value_error(patched_client) -> None:
    client, _ = patched_client
    with pytest.raises(ValueError):
        await client.complete_tool_use(
            messages=[{"role": "user", "content": "x"}],
            system=None,
            tools=[],
            model="claude-sonnet-4-6",
            max_tokens=0,
        )


async def test_thinking_budget_below_minimum_raises(patched_client) -> None:
    client, _ = patched_client
    with pytest.raises(ValueError, match=r"thinking_budget must be >= 1024"):
        await client.complete_tool_use(
            messages=[{"role": "user", "content": "x"}],
            system=None,
            tools=[],
            model="claude-sonnet-4-6",
            max_tokens=4000,
            thinking_budget=500,
        )


async def test_thinking_budget_above_max_tokens_raises(patched_client) -> None:
    client, _ = patched_client
    with pytest.raises(ValueError, match="strictly less than max_tokens"):
        await client.complete_tool_use(
            messages=[{"role": "user", "content": "x"}],
            system=None,
            tools=[],
            model="claude-sonnet-4-6",
            max_tokens=2000,
            thinking_budget=2000,
        )


# ===========================================================================
# Error mapping
# ===========================================================================


def _http_response(status: int, headers: dict | None = None) -> httpx.Response:
    request = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
    return httpx.Response(
        status_code=status,
        headers=headers or {},
        text="error",
        request=request,
    )


async def test_auth_error_maps_to_connector_auth_error(patched_client) -> None:
    client, fake = patched_client
    err = anthropic.AuthenticationError(
        message="bad key", response=_http_response(401), body=None
    )
    fake.responses.append(err)
    with pytest.raises(ConnectorAuthError):
        await client.complete_tool_use(
            messages=[{"role": "user", "content": "x"}],
            system=None,
            tools=[],
            model="claude-sonnet-4-6",
            max_tokens=500,
        )


async def test_rate_limit_error_maps_with_retry_after(patched_client) -> None:
    client, fake = patched_client
    err = anthropic.RateLimitError(
        message="slow down",
        response=_http_response(429, headers={"Retry-After": "13"}),
        body=None,
    )
    fake.responses.append(err)
    with pytest.raises(ConnectorRateLimited) as excinfo:
        await client.complete_tool_use(
            messages=[{"role": "user", "content": "x"}],
            system=None,
            tools=[],
            model="claude-sonnet-4-6",
            max_tokens=500,
        )
    assert excinfo.value.retry_after == 13.0


async def test_5xx_status_maps_to_transient(patched_client) -> None:
    client, fake = patched_client
    err = anthropic.APIStatusError(
        message="upstream broken", response=_http_response(503), body=None
    )
    fake.responses.append(err)
    with pytest.raises(ConnectorTransient):
        await client.complete_tool_use(
            messages=[{"role": "user", "content": "x"}],
            system=None,
            tools=[],
            model="claude-sonnet-4-6",
            max_tokens=500,
        )


async def test_connection_error_maps_to_transient(patched_client) -> None:
    client, fake = patched_client
    err = anthropic.APIConnectionError(request=httpx.Request("POST", "http://x"))
    fake.responses.append(err)
    with pytest.raises(ConnectorTransient):
        await client.complete_tool_use(
            messages=[{"role": "user", "content": "x"}],
            system=None,
            tools=[],
            model="claude-sonnet-4-6",
            max_tokens=500,
        )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def monkeypatch_messages(client, new_messages):
    """Replace the client's messages object (no monkeypatch fixture)."""
    client._client.messages = new_messages  # type: ignore[attr-defined]


def _tool_use_dict(name: str, input_data: dict, tool_use_id: str) -> dict:
    return {
        "type": "tool_use",
        "id": tool_use_id,
        "name": name,
        "input": input_data,
    }
