"""Unit tests for the tool registry.

Pure-Python; no DB / Redis / HTTP. The registry is a small data
structure with validation logic; tests exercise the boundaries
(category validation, name validation, Anthropic schema
rendering) without standing up an environment.
"""
import pytest
from pydantic import BaseModel, Field

from coworker.orchestrator.tools import (
    ToolDefinition,
    ToolError,
    ToolRegistry,
)


class _Input(BaseModel):
    """A tool input model for tests."""

    query: str = Field(description="The search string.")
    limit: int = Field(default=10, description="Max results.")


async def _noop_handler(inp, ctx):  # pragma: no cover - never called in unit
    return {}


def _tool(
    name: str = "memory_query",
    category: str = "memory",
    side_effect: bool = False,
    cost: int = 1,
) -> ToolDefinition:
    return ToolDefinition(
        name=name,
        description=f"Test tool {name}.",
        category=category,  # type: ignore[arg-type]
        input_model=_Input,
        handler=_noop_handler,
        cost_estimate_cents=cost,
        side_effect=side_effect,
    )


# ---------------------------------------------------------------------------
# ToolDefinition validation
# ---------------------------------------------------------------------------


def test_unknown_category_raises_at_construction() -> None:
    with pytest.raises(ValueError, match="unknown tool category"):
        ToolDefinition(
            name="x",
            description="",
            category="not_a_real_category",  # type: ignore[arg-type]
            input_model=_Input,
            handler=_noop_handler,
        )


def test_invalid_name_raises_at_construction() -> None:
    with pytest.raises(ValueError, match="must be alphanumeric"):
        ToolDefinition(
            name="invalid name",
            description="",
            category="memory",
            input_model=_Input,
            handler=_noop_handler,
        )
    with pytest.raises(ValueError):
        ToolDefinition(
            name="",
            description="",
            category="memory",
            input_model=_Input,
            handler=_noop_handler,
        )


def test_negative_cost_raises_at_construction() -> None:
    with pytest.raises(ValueError, match="cost_estimate_cents must be >= 0"):
        ToolDefinition(
            name="x",
            description="",
            category="memory",
            input_model=_Input,
            handler=_noop_handler,
            cost_estimate_cents=-5,
        )


# ---------------------------------------------------------------------------
# ToolRegistry
# ---------------------------------------------------------------------------


def test_register_and_lookup_round_trip() -> None:
    reg = ToolRegistry()
    tool = _tool()
    reg.register(tool)
    assert "memory_query" in reg
    assert reg.get("memory_query") is tool
    assert reg.get("does_not_exist") is None
    assert len(reg) == 1


def test_duplicate_name_raises() -> None:
    reg = ToolRegistry()
    reg.register(_tool())
    with pytest.raises(ValueError, match="already registered"):
        reg.register(_tool())


def test_filter_by_categories_keeps_only_requested() -> None:
    reg = ToolRegistry()
    reg.register(_tool(name="memory_query", category="memory"))
    reg.register(_tool(name="kg_lookup", category="kg"))
    reg.register(_tool(name="email_draft", category="email", side_effect=True))

    subset = reg.filter_by_categories({"memory", "kg"})
    names = {t.name for t in subset.all()}
    assert names == {"memory_query", "kg_lookup"}


def test_filter_excludes_side_effects_when_dry_run() -> None:
    reg = ToolRegistry()
    reg.register(_tool(name="memory_query", category="memory"))
    reg.register(_tool(name="email_draft", category="email", side_effect=True))
    reg.register(_tool(name="email_mark", category="email", side_effect=True))

    subset = reg.filter_by_categories(
        {"memory", "email"}, exclude_side_effects=True,
    )
    names = {t.name for t in subset.all()}
    assert names == {"memory_query"}


def test_filter_returns_independent_registry() -> None:
    reg = ToolRegistry()
    reg.register(_tool(name="a", category="memory"))
    subset = reg.filter_by_categories({"memory"})
    subset.register(_tool(name="b", category="memory"))
    # Mutating the subset doesn't bleed back to the source.
    assert "b" in subset
    assert "b" not in reg


# ---------------------------------------------------------------------------
# Anthropic schema rendering
# ---------------------------------------------------------------------------


def test_to_anthropic_definitions_shape_matches_tool_use_spec() -> None:
    reg = ToolRegistry()
    reg.register(_tool())
    defs = reg.to_anthropic_definitions()
    assert len(defs) == 1
    d = defs[0]
    assert d["name"] == "memory_query"
    assert "Test tool" in d["description"]

    schema = d["input_schema"]
    assert schema["type"] == "object"
    # Properties land verbatim with field descriptions preserved.
    assert "query" in schema["properties"]
    assert schema["properties"]["query"]["description"] == "The search string."
    assert schema["properties"]["limit"]["description"] == "Max results."
    # Required reflects the model's required fields.
    assert "query" in schema.get("required", [])
    assert "limit" not in schema.get("required", [])


def test_anthropic_schema_strips_titles() -> None:
    """Pydantic adds title fields that add noise to the model's prompt."""
    reg = ToolRegistry()
    reg.register(_tool())
    schema = reg.to_anthropic_definitions()[0]["input_schema"]
    # Neither the root nor any property should carry a title field.
    assert "title" not in schema
    for prop in schema["properties"].values():
        assert "title" not in prop


def test_empty_registry_renders_to_empty_list() -> None:
    assert ToolRegistry().to_anthropic_definitions() == []


# ---------------------------------------------------------------------------
# ToolError is importable + isinstance-checkable
# ---------------------------------------------------------------------------


def test_tool_error_is_an_exception() -> None:
    err = ToolError("client not found")
    assert isinstance(err, Exception)
    assert str(err) == "client not found"
