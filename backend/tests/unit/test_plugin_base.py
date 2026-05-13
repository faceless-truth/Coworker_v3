"""Unit tests for the plugin base class + registry.

Pure-Python; no DB / HTTP. The plugin base is metadata + dispatch
shape; tests exercise the validation boundary and registry CRUD.
"""
from datetime import UTC, datetime

import pytest
from pydantic import BaseModel

from coworker.plugins.base import (
    OrchestratorPlugin,
    PluginRegistry,
    PluginRun,
)


def _minimal_plugin(
    *,
    name: str = "test_plugin",
    triggers=("manual",),
    schedule_cron: str | None = None,
    enabled_tool_categories=("reasoning",),
):
    """Build a valid plugin class with overridable fields."""

    class TestPlugin(OrchestratorPlugin):
        pass

    TestPlugin.name = name
    TestPlugin.display_name = f"Display: {name}"
    TestPlugin.description = f"Description for {name}"
    TestPlugin.triggers = frozenset(triggers)
    TestPlugin.schedule_cron = schedule_cron
    TestPlugin.enabled_tool_categories = frozenset(enabled_tool_categories)
    # Concrete goal so the class isn't abstract for the test.
    TestPlugin.goal = classmethod(lambda cls, run: f"test goal for {run.plugin_name}")
    return TestPlugin


def _minimal_run(plugin_name: str = "test_plugin") -> PluginRun:
    return PluginRun(
        plugin_name=plugin_name,
        firm_id="firm-uuid",
        trigger="manual",
        event_data={"key": "value"},
        config={},
        is_dry_run=False,
        requested_at=datetime.now(UTC),
    )


# ---------------------------------------------------------------------------
# Plugin metadata validation
# ---------------------------------------------------------------------------


def test_valid_plugin_registers_without_error() -> None:
    reg = PluginRegistry()
    p = _minimal_plugin()
    reg.register(p)
    assert "test_plugin" in reg
    assert reg.get("test_plugin") is p


def test_missing_name_fails_validation() -> None:
    reg = PluginRegistry()
    p = _minimal_plugin(name="")
    with pytest.raises(ValueError, match="name must be set"):
        reg.register(p)


def test_invalid_name_chars_fail_validation() -> None:
    reg = PluginRegistry()
    p = _minimal_plugin(name="bad name with spaces")
    with pytest.raises(ValueError, match="alphanumeric"):
        reg.register(p)


def test_no_triggers_fails_validation() -> None:
    reg = PluginRegistry()
    p = _minimal_plugin(triggers=())
    with pytest.raises(ValueError, match="at least one trigger"):
        reg.register(p)


def test_unknown_trigger_fails_validation() -> None:
    reg = PluginRegistry()
    p = _minimal_plugin(triggers=("blueberries",))
    with pytest.raises(ValueError, match="unknown trigger"):
        reg.register(p)


def test_scheduled_without_cron_fails_validation() -> None:
    reg = PluginRegistry()
    p = _minimal_plugin(triggers=("scheduled",), schedule_cron=None)
    with pytest.raises(ValueError, match="schedule_cron"):
        reg.register(p)


def test_scheduled_with_cron_validates() -> None:
    reg = PluginRegistry()
    p = _minimal_plugin(
        triggers=("scheduled",), schedule_cron="0 6 * * *"
    )
    reg.register(p)  # no raise


def test_duplicate_registration_raises() -> None:
    reg = PluginRegistry()
    reg.register(_minimal_plugin())
    with pytest.raises(ValueError, match="already registered"):
        reg.register(_minimal_plugin())


# ---------------------------------------------------------------------------
# Registry queries
# ---------------------------------------------------------------------------


def test_filter_by_trigger_returns_matching_plugins() -> None:
    reg = PluginRegistry()
    p_email = _minimal_plugin(name="responder", triggers=("email_received",))
    p_sched = _minimal_plugin(
        name="briefing", triggers=("scheduled",), schedule_cron="0 6 * * *"
    )
    p_both = _minimal_plugin(
        name="bothy",
        triggers=("email_received", "scheduled"),
        schedule_cron="0 0 * * *",
    )
    reg.register(p_email)
    reg.register(p_sched)
    reg.register(p_both)

    by_email = reg.filter_by_trigger("email_received")
    assert {p.name for p in by_email} == {"responder", "bothy"}

    by_sched = reg.filter_by_trigger("scheduled")
    assert {p.name for p in by_sched} == {"briefing", "bothy"}


def test_empty_registry_renders_to_empty() -> None:
    reg = PluginRegistry()
    assert reg.all() == []
    assert len(reg) == 0
    assert "any_name" not in reg


# ---------------------------------------------------------------------------
# Goal / system_prompt dispatch
# ---------------------------------------------------------------------------


def test_goal_classmethod_receives_run() -> None:
    p = _minimal_plugin()
    run = _minimal_run()
    assert p.goal(run) == "test goal for test_plugin"


def test_default_system_prompt_returns_none() -> None:
    p = _minimal_plugin()
    run = _minimal_run()
    assert p.system_prompt(run) is None


def test_subclass_can_override_system_prompt() -> None:
    class WithSystem(OrchestratorPlugin):
        name = "with_system"
        display_name = "With System"
        description = "..."
        triggers = frozenset({"manual"})
        enabled_tool_categories = frozenset({"reasoning"})

        @classmethod
        def goal(cls, run: PluginRun) -> str:
            return "g"

        @classmethod
        def system_prompt(cls, run: PluginRun) -> str | None:
            return "You are a careful assistant."

    run = _minimal_run("with_system")
    assert WithSystem.system_prompt(run) == "You are a careful assistant."


def test_config_schema_defaults_to_empty_basemodel() -> None:
    p = _minimal_plugin()
    # Empty config validates against the default.
    inst = p.config_schema()
    assert isinstance(inst, BaseModel)
