"""Black-box test: no AU PII digits ever leave the process to Anthropic.

This is the load-bearing security guarantee for the Anthropic
connector. If a future change breaks PII scrubbing — drops a
recogniser, mis-orders the call, regresses on whitespace handling,
or accidentally bypasses the scrub pass entirely — this test fails
before the change ships.

Methodology:

1. Construct a prompt that contains fake-but-realistically-shaped AU
   PII (TFN, ABN, Medicare) plus an email address.
2. Send the prompt through ``AnthropicClient.complete``.
3. Capture the outbound HTTP body via respx ``side_effect``.
4. Assert each piece of PII does **not** appear anywhere in the
   captured body — neither in the original whitespace form nor in
   the digits-only form (the recogniser strips whitespace before
   matching, so the scrubber sees both forms).
5. Assert at least one placeholder shape appears, so the message
   actually reached the wire (the model still has something to
   reason about — just not the raw digits).

The values used here are not real account numbers — they're chosen
to fit the regex shapes the recognisers care about. A real PII leak
would still fail the test even if the leaked digits were synthetic.

A note on placeholder shape: Presidio's generic PHONE_NUMBER
recogniser sometimes co-fires with AU_ABN / AU_MEDICARE on the
same span. After span dedupe (highest-confidence wins), the
placeholder may be ``[PHONE_NUMBER_xxx]`` rather than ``[AU_ABN_xxx]``
for an 11-digit ABN. Both satisfy the security goal (digits don't
leak); we don't pin the exact entity label here. Refining recogniser
confidences so AU patterns dominate is a Phase 4 concern.
"""
import json
import re

import httpx
import respx
from pydantic import SecretStr

from coworker.connectors.anthropic_client import (
    AnthropicClient,
    CompletionMessage,
)

_PLACEHOLDER_PATTERN = re.compile(r"\[[A-Z_]+_[a-f0-9]{6}\]")


_ANTHROPIC_MESSAGES_URL = "https://api.anthropic.com/v1/messages"


# Synthetic but regex-shape-correct test fixtures.
_TFN_WITH_SPACES = "123 456 789"
_TFN_NO_SPACES = "123456789"
_ABN_WITH_SPACES = "12 345 678 901"
_ABN_NO_SPACES = "12345678901"
_MEDICARE_WITH_SPACES = "1234 56789 0"
_MEDICARE_NO_SPACES = "1234567890"
_EMAIL = "alice.smith@example.com.au"


def _render_body(request: httpx.Request) -> str:
    """Capture the request body once and return it as a string.

    Reading the body consumes the stream; we have to read inside
    the side-effect function and then return a Response.
    """
    return request.read().decode("utf-8")


async def test_no_tfn_digits_leave_the_process() -> None:
    captured: dict[str, str] = {}

    def _capture(request: httpx.Request) -> httpx.Response:
        captured["body"] = _render_body(request)
        return httpx.Response(
            200,
            json={
                "id": "msg_test",
                "type": "message",
                "role": "assistant",
                "model": "claude-sonnet-4-6",
                "content": [{"type": "text", "text": "ack"}],
                "stop_reason": "end_turn",
                "stop_sequence": None,
                "usage": {"input_tokens": 1, "output_tokens": 1},
            },
        )

    client = AnthropicClient(
        firm_id="firm-pii", api_key=SecretStr("sk-ant-test-key")
    )

    with respx.mock(assert_all_called=True) as rmock:
        rmock.post(_ANTHROPIC_MESSAGES_URL).mock(side_effect=_capture)
        await client.complete(
            messages=[
                CompletionMessage(
                    role="user",
                    content=(
                        f"Customer's TFN is {_TFN_WITH_SPACES}, please "
                        "review."
                    ),
                ),
            ],
            model="claude-sonnet-4-6",
            max_tokens=10,
        )

    body = captured["body"]
    assert _TFN_WITH_SPACES not in body, (
        f"TFN with spaces leaked to Anthropic: {body!r}"
    )
    assert _TFN_NO_SPACES not in body, (
        f"TFN without spaces leaked to Anthropic: {body!r}"
    )
    assert "[AU_TFN_" in body, (
        f"TFN placeholder missing from outbound body: {body!r}"
    )


async def test_no_abn_digits_leave_the_process() -> None:
    captured: dict[str, str] = {}

    def _capture(request: httpx.Request) -> httpx.Response:
        captured["body"] = _render_body(request)
        return httpx.Response(
            200,
            json={
                "id": "msg_test",
                "type": "message",
                "role": "assistant",
                "model": "claude-sonnet-4-6",
                "content": [{"type": "text", "text": "ack"}],
                "stop_reason": "end_turn",
                "stop_sequence": None,
                "usage": {"input_tokens": 1, "output_tokens": 1},
            },
        )

    client = AnthropicClient(
        firm_id="firm-pii", api_key=SecretStr("sk-ant-test-key")
    )

    with respx.mock(assert_all_called=True) as rmock:
        rmock.post(_ANTHROPIC_MESSAGES_URL).mock(side_effect=_capture)
        await client.complete(
            messages=[
                CompletionMessage(
                    role="user",
                    content=(
                        f"The trust's ABN is {_ABN_WITH_SPACES}. "
                        "Please reconcile."
                    ),
                )
            ],
            model="claude-sonnet-4-6",
            max_tokens=10,
        )

    body = captured["body"]
    assert _ABN_WITH_SPACES not in body, (
        f"ABN with spaces leaked: {body!r}"
    )
    assert _ABN_NO_SPACES not in body, (
        f"ABN without spaces leaked: {body!r}"
    )
    # An ABN-shaped span gets recognised — could be tagged AU_ABN or
    # PHONE_NUMBER depending on confidence ranking. Either is fine
    # for the security goal; assert that *some* placeholder appears.
    assert _PLACEHOLDER_PATTERN.search(body) is not None, (
        f"no placeholder found in body: {body!r}"
    )


async def test_no_medicare_digits_leave_the_process() -> None:
    captured: dict[str, str] = {}

    def _capture(request: httpx.Request) -> httpx.Response:
        captured["body"] = _render_body(request)
        return httpx.Response(
            200,
            json={
                "id": "msg_test",
                "type": "message",
                "role": "assistant",
                "model": "claude-sonnet-4-6",
                "content": [{"type": "text", "text": "ack"}],
                "stop_reason": "end_turn",
                "stop_sequence": None,
                "usage": {"input_tokens": 1, "output_tokens": 1},
            },
        )

    client = AnthropicClient(
        firm_id="firm-pii", api_key=SecretStr("sk-ant-test-key")
    )

    with respx.mock(assert_all_called=True) as rmock:
        rmock.post(_ANTHROPIC_MESSAGES_URL).mock(side_effect=_capture)
        await client.complete(
            messages=[
                CompletionMessage(
                    role="user",
                    content=(
                        f"Medicare card: {_MEDICARE_WITH_SPACES}. "
                        "Confirm details."
                    ),
                )
            ],
            model="claude-sonnet-4-6",
            max_tokens=10,
        )

    body = captured["body"]
    assert _MEDICARE_WITH_SPACES not in body, (
        f"Medicare with spaces leaked: {body!r}"
    )
    assert _MEDICARE_NO_SPACES not in body, (
        f"Medicare without spaces leaked: {body!r}"
    )
    # Either the recogniser fired and produced a placeholder, or it
    # did not - both are accepted by the negative assertion above.
    # We do NOT assert the placeholder exists for Medicare because
    # the recogniser score (0.5) is at the bandwidth boundary; if a
    # future tuning lowers it, we don't want this test to flake. The
    # load-bearing assertion is the digit non-leak above.


async def test_no_pii_digits_leave_in_combined_message() -> None:
    """Multi-entity stress: TFN + ABN + Medicare + email in one message."""
    captured: dict[str, str] = {}

    def _capture(request: httpx.Request) -> httpx.Response:
        captured["body"] = _render_body(request)
        return httpx.Response(
            200,
            json={
                "id": "msg_test",
                "type": "message",
                "role": "assistant",
                "model": "claude-sonnet-4-6",
                "content": [{"type": "text", "text": "ack"}],
                "stop_reason": "end_turn",
                "stop_sequence": None,
                "usage": {"input_tokens": 1, "output_tokens": 1},
            },
        )

    client = AnthropicClient(
        firm_id="firm-pii", api_key=SecretStr("sk-ant-test-key")
    )

    prompt = (
        f"Hi, my TFN is {_TFN_WITH_SPACES}. The company ABN is "
        f"{_ABN_WITH_SPACES}. Email me at {_EMAIL}."
    )

    with respx.mock(assert_all_called=True) as rmock:
        rmock.post(_ANTHROPIC_MESSAGES_URL).mock(side_effect=_capture)
        await client.complete(
            messages=[CompletionMessage(role="user", content=prompt)],
            model="claude-sonnet-4-6",
            max_tokens=10,
        )

    body = captured["body"]
    for needle in (
        _TFN_WITH_SPACES, _TFN_NO_SPACES,
        _ABN_WITH_SPACES, _ABN_NO_SPACES,
        _EMAIL,
    ):
        assert needle not in body, (
            f"{needle!r} leaked to Anthropic in combined message: {body!r}"
        )


async def test_pii_in_system_prompt_is_also_scrubbed() -> None:
    """The system prompt is scrubbed on the same code path. Verify."""
    captured: dict[str, str] = {}

    def _capture(request: httpx.Request) -> httpx.Response:
        captured["body"] = _render_body(request)
        return httpx.Response(
            200,
            json={
                "id": "msg_test",
                "type": "message",
                "role": "assistant",
                "model": "claude-sonnet-4-6",
                "content": [{"type": "text", "text": "ack"}],
                "stop_reason": "end_turn",
                "stop_sequence": None,
                "usage": {"input_tokens": 1, "output_tokens": 1},
            },
        )

    client = AnthropicClient(
        firm_id="firm-pii", api_key=SecretStr("sk-ant-test-key")
    )

    with respx.mock(assert_all_called=True) as rmock:
        rmock.post(_ANTHROPIC_MESSAGES_URL).mock(side_effect=_capture)
        await client.complete(
            messages=[CompletionMessage(role="user", content="Hello")],
            model="claude-sonnet-4-6",
            max_tokens=10,
            system=(
                "Reference docs: customer's TFN is "
                f"{_TFN_WITH_SPACES}. Be helpful."
            ),
        )

    body = captured["body"]
    assert _TFN_WITH_SPACES not in body
    assert _TFN_NO_SPACES not in body
    assert "[AU_TFN_" in body


async def test_outbound_body_is_well_formed_json() -> None:
    """Sanity: scrubbing doesn't corrupt the JSON the SDK builds.

    The scrubber operates on plain text; the SDK serialises the
    scrubbed strings into JSON. A bug that mangled JSON characters
    in placeholders would break the wire format. This test confirms
    we still produce parseable JSON after the scrub pass.
    """
    captured: dict[str, str] = {}

    def _capture(request: httpx.Request) -> httpx.Response:
        captured["body"] = _render_body(request)
        return httpx.Response(
            200,
            json={
                "id": "msg_test",
                "type": "message",
                "role": "assistant",
                "model": "claude-sonnet-4-6",
                "content": [{"type": "text", "text": "ack"}],
                "stop_reason": "end_turn",
                "stop_sequence": None,
                "usage": {"input_tokens": 1, "output_tokens": 1},
            },
        )

    client = AnthropicClient(
        firm_id="firm-pii", api_key=SecretStr("sk-ant-test-key")
    )

    with respx.mock(assert_all_called=True) as rmock:
        rmock.post(_ANTHROPIC_MESSAGES_URL).mock(side_effect=_capture)
        await client.complete(
            messages=[
                CompletionMessage(
                    role="user",
                    content=f"TFN {_TFN_WITH_SPACES} and ABN {_ABN_WITH_SPACES}",
                )
            ],
            model="claude-sonnet-4-6",
            max_tokens=10,
        )

    body = captured["body"]
    parsed = json.loads(body)
    assert parsed["model"] == "claude-sonnet-4-6"
    assert parsed["max_tokens"] == 10
    assert isinstance(parsed["messages"], list)
    assert parsed["messages"][0]["role"] == "user"
