"""Tests for ``coworker.knowledge_graph.email_extractor``.

The Anthropic call is mocked by a stub that satisfies the
attribute access ``extract_entities_from_email`` performs
(``complete(...)`` returning a CompletionResult with a ``text``
field). Real-network coverage of AnthropicClient lives in
``test_anthropic_client.py``.
"""
import datetime as _dt
from dataclasses import dataclass

import pytest

from coworker.connectors.anthropic_client import CompletionResult
from coworker.connectors.exceptions import ConnectorTransient
from coworker.knowledge_graph.email_extractor import (
    EmailExtraction,
    extract_entities_from_email,
)


@dataclass
class _StubAnthropic:
    """Minimal stand-in for AnthropicClient.

    Records what was sent and returns a pre-configured text payload.
    """

    response_text: str
    captured_system: str | None = None
    captured_user: str | None = None

    async def complete(
        self,
        messages,
        *,
        model,
        max_tokens,
        system=None,
        thinking_budget=None,
    ):
        self.captured_system = system
        self.captured_user = messages[0].content
        return CompletionResult(
            text=self.response_text,
            stop_reason="end_turn",
            input_tokens=10,
            output_tokens=20,
            model=model,
            completed_at=_dt.datetime.now(_dt.UTC),
        )


# ---------------------------------------------------------------------------


async def test_extracts_clean_json_payload() -> None:
    payload = """{
        "entities": [
            {"name": "Acme Pty Ltd", "entity_type": "company", "confidence": 0.95},
            {"name": "Alice Smith", "entity_type": "individual", "confidence": 0.9}
        ],
        "relationships": [
            {"from_name": "Alice Smith", "to_name": "Acme Pty Ltd",
             "relationship_type": "director_of", "confidence": 0.9}
        ]
    }"""
    stub = _StubAnthropic(response_text=payload)
    result = await extract_entities_from_email(
        stub,  # type: ignore[arg-type]
        subject="BAS query",
        body="Hi, please find attached the Acme BAS for review. — Alice",
    )

    assert isinstance(result, EmailExtraction)
    assert len(result.entities) == 2
    assert result.entities[0].name == "Acme Pty Ltd"
    assert result.entities[0].entity_type == "company"
    assert result.entities[0].confidence == 0.95
    assert len(result.relationships) == 1
    assert result.relationships[0].relationship_type == "director_of"

    # Subject + body forwarded to the model.
    assert "BAS query" in stub.captured_user
    assert "Acme" in stub.captured_user
    # System prompt sent.
    assert stub.captured_system is not None
    assert "extract structured entity references" in stub.captured_system


async def test_tolerates_code_fence_wrapping() -> None:
    """Even with strict prompting Claude sometimes wraps JSON in fences."""
    payload = """```json
{
  "entities": [{"name": "Beta Co", "entity_type": "company", "confidence": 0.8}],
  "relationships": []
}
```"""
    stub = _StubAnthropic(response_text=payload)
    result = await extract_entities_from_email(
        stub,  # type: ignore[arg-type]
        subject="x", body="x",
    )
    assert len(result.entities) == 1
    assert result.entities[0].name == "Beta Co"


async def test_tolerates_preamble_before_json() -> None:
    payload = (
        "Here is the extraction:\n"
        '{"entities": [{"name": "Gamma Trust", "entity_type": "trust", '
        '"confidence": 0.7}], "relationships": []}\n'
        "Let me know if you need more."
    )
    stub = _StubAnthropic(response_text=payload)
    result = await extract_entities_from_email(
        stub,  # type: ignore[arg-type]
        subject="x", body="x",
    )
    assert len(result.entities) == 1
    assert result.entities[0].entity_type == "trust"


async def test_filters_relationships_referencing_unknown_entities() -> None:
    """A relationship pointing at a name not in the entity list is dropped."""
    payload = """{
        "entities": [
            {"name": "Alpha", "entity_type": "company", "confidence": 0.9}
        ],
        "relationships": [
            {"from_name": "Alpha", "to_name": "Mystery",
             "relationship_type": "director_of", "confidence": 0.9},
            {"from_name": "Phantom", "to_name": "Alpha",
             "relationship_type": "shareholder_of", "confidence": 0.9}
        ]
    }"""
    stub = _StubAnthropic(response_text=payload)
    result = await extract_entities_from_email(
        stub,  # type: ignore[arg-type]
        subject="x", body="x",
    )
    assert result.entities == [
        result.entities[0]
    ]  # only Alpha
    assert result.relationships == []  # both edges referenced missing names


async def test_filters_self_loop_relationships() -> None:
    payload = """{
        "entities": [
            {"name": "Alpha", "entity_type": "company", "confidence": 0.9}
        ],
        "relationships": [
            {"from_name": "Alpha", "to_name": "Alpha",
             "relationship_type": "director_of", "confidence": 0.9}
        ]
    }"""
    stub = _StubAnthropic(response_text=payload)
    result = await extract_entities_from_email(
        stub,  # type: ignore[arg-type]
        subject="x", body="x",
    )
    assert result.relationships == []


async def test_clamps_confidence_to_unit_interval() -> None:
    payload = """{
        "entities": [
            {"name": "Bravo", "entity_type": "company", "confidence": 2.5},
            {"name": "Charlie", "entity_type": "company", "confidence": -0.1}
        ],
        "relationships": []
    }"""
    stub = _StubAnthropic(response_text=payload)
    result = await extract_entities_from_email(
        stub,  # type: ignore[arg-type]
        subject="x", body="x",
    )
    assert result.entities[0].confidence == 1.0
    assert result.entities[1].confidence == 0.0


async def test_drops_entity_with_missing_name_or_type() -> None:
    payload = """{
        "entities": [
            {"name": "", "entity_type": "company", "confidence": 0.9},
            {"name": "Has Name", "entity_type": "", "confidence": 0.9},
            {"name": "Valid", "entity_type": "individual", "confidence": 0.8}
        ],
        "relationships": []
    }"""
    stub = _StubAnthropic(response_text=payload)
    result = await extract_entities_from_email(
        stub,  # type: ignore[arg-type]
        subject="x", body="x",
    )
    assert len(result.entities) == 1
    assert result.entities[0].name == "Valid"


async def test_empty_result_is_valid() -> None:
    """Nothing to extract returns empty lists, not an error."""
    payload = '{"entities": [], "relationships": []}'
    stub = _StubAnthropic(response_text=payload)
    result = await extract_entities_from_email(
        stub,  # type: ignore[arg-type]
        subject="x", body="x",
    )
    assert result.entities == []
    assert result.relationships == []


async def test_non_json_response_raises_transient() -> None:
    stub = _StubAnthropic(response_text="I cannot extract anything from this.")
    with pytest.raises(ConnectorTransient):
        await extract_entities_from_email(
            stub,  # type: ignore[arg-type]
            subject="x", body="x",
        )


async def test_non_object_root_raises_transient() -> None:
    stub = _StubAnthropic(response_text="[]")
    with pytest.raises(ConnectorTransient):
        await extract_entities_from_email(
            stub,  # type: ignore[arg-type]
            subject="x", body="x",
        )
