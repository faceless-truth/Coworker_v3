"""``OrchestratorPlugin`` base class and registry.

A plugin is a class that declares:

- Static metadata: name / display_name / description / version,
  the triggers it listens to (email_received / scheduled /
  manual / fusesign_event / calendar_event), the tool categories
  it needs (memory / kg / email / etc.), a cron expression for
  scheduled triggers, an optional Pydantic config_schema, and a
  cost budget.
- Behaviour: ``goal(run)`` returning the natural-language goal the
  agent loop will pursue; optionally ``system_prompt(run)`` for
  a plugin-specific system message.

The Phase 6-2 ``PluginExecutor`` takes a ``PluginRun`` (event +
firm config + dry-run flag), constructs an AgentContext with the
plugin's tool slice (via ``ToolRegistry.filter_by_categories``,
honouring ``allow_side_effects``), and runs the engine.

Plugins are stateless: ``goal`` and ``system_prompt`` are
classmethods. The PluginRun carries everything the methods need
to know about the current invocation.
"""
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, ClassVar, Literal

from pydantic import BaseModel

from coworker.orchestrator.tools import ToolCategory

Trigger = Literal[
    "email_received",
    "scheduled",
    "manual",
    "fusesign_event",
    "calendar_event",
]

_VALID_TRIGGERS: frozenset[str] = frozenset(
    {"email_received", "scheduled", "manual", "fusesign_event", "calendar_event"}
)


class _EmptyConfig(BaseModel):
    """Default config schema for plugins that take no config."""


@dataclass(frozen=True)
class PluginRun:
    """A single invocation context.

    Constructed by the PluginExecutor from the triggering event +
    the firm's plugin_installations row. The plugin's ``goal`` and
    ``system_prompt`` methods read from this and ONLY this — they
    don't touch global state or external APIs.

    Attributes:
        plugin_name: redundant with the plugin class but useful in
            generic execution / logging paths.
        firm_id: target firm. The executor sets up
            ``firm_context(firm_id)`` before invoking the plugin.
        trigger: which trigger fired this run.
        event_data: trigger-specific payload. For email_received,
            ``{"message_id": "...", "subject": "...", ...}``; for
            scheduled, ``{"scheduled_at": "...", "schedule_cron": ...}``;
            for fusesign_event, ``{"envelope_id": "...", "event_type": ...}``.
            The plugin reads what it needs; missing keys raise on
            access so plugin code surfaces incomplete events
            immediately.
        config: parsed firm-specific config (already validated
            against the plugin's ``config_schema``) as a dict for
            JSON friendliness. Plugins can re-parse into the
            BaseModel if they want typed access.
        is_dry_run: from plugin_installations.is_dry_run. The
            executor uses this to filter side-effect tools out
            of the registry before invoking the engine; plugins
            can also branch on it (e.g. note "dry run" in the
            goal text).
        requested_at: when the executor accepted the run, UTC.
            Distinct from when the trigger fired (the queue may
            have backed up).
    """

    plugin_name: str
    firm_id: Any
    trigger: Trigger
    event_data: dict[str, Any]
    config: dict[str, Any] = field(default_factory=dict)
    is_dry_run: bool = False
    requested_at: datetime | None = None


class OrchestratorPlugin(ABC):
    """Base class for every plugin.

    Subclasses set the ClassVar metadata and implement ``goal``
    (mandatory) and ``system_prompt`` (optional). Both are
    classmethods because plugins hold no per-instance state.

    Naming
    ------

    ``name`` is the stable identifier used in DB rows and the
    audit log. Convention: snake_case, ASCII only. Examples:
    ``smart_responder``, ``correspondence_logger``, ``bas_reminder``.

    Triggers
    --------

    A plugin can declare multiple triggers; the executor decides
    which to enqueue when. ``schedule_cron`` is required when
    ``"scheduled"`` is in triggers and ignored otherwise.

    Tool categories
    ---------------

    Phase 6 starts plugins with read-only category access.
    ``allow_side_effects=True`` opts in to shadow-guarded writes
    in the plugin's declared categories. Defaults to False so a
    new plugin can't accidentally produce drafts or send
    reminders during testing.
    """

    # Static metadata. Subclasses MUST override name / display_name /
    # description / version / triggers / enabled_tool_categories.
    name: ClassVar[str] = ""
    display_name: ClassVar[str] = ""
    description: ClassVar[str] = ""
    version: ClassVar[str] = "0.1.0"
    triggers: ClassVar[frozenset[Trigger]] = frozenset()
    enabled_tool_categories: ClassVar[frozenset[ToolCategory]] = frozenset()
    schedule_cron: ClassVar[str | None] = None
    config_schema: ClassVar[type[BaseModel]] = _EmptyConfig
    cost_budget_cents: ClassVar[int] = 100  # 1 dollar default
    allow_side_effects: ClassVar[bool] = False

    @classmethod
    def validate_metadata(cls) -> None:
        """Self-check called by ``PluginRegistry.register``.

        Surfaces metadata bugs at registration time (the worker
        startup) rather than at run time. Cheap; runs once per
        plugin class.
        """
        if not cls.name:
            raise ValueError(f"{cls.__name__}.name must be set")
        if not cls.name.replace("_", "").isalnum():
            raise ValueError(
                f"{cls.__name__}.name must be alphanumeric+underscore; "
                f"got {cls.name!r}"
            )
        if not cls.display_name:
            raise ValueError(f"{cls.__name__}.display_name must be set")
        if not cls.description:
            raise ValueError(f"{cls.__name__}.description must be set")
        if not cls.triggers:
            raise ValueError(
                f"{cls.__name__} must declare at least one trigger"
            )
        for t in cls.triggers:
            if t not in _VALID_TRIGGERS:
                raise ValueError(
                    f"{cls.__name__} unknown trigger {t!r}; "
                    f"expected one of {sorted(_VALID_TRIGGERS)}"
                )
        if "scheduled" in cls.triggers and not cls.schedule_cron:
            raise ValueError(
                f"{cls.__name__} declares 'scheduled' trigger but "
                f"schedule_cron is not set"
            )
        if cls.cost_budget_cents < 0:
            raise ValueError(
                f"{cls.__name__}.cost_budget_cents must be >= 0"
            )

    @classmethod
    @abstractmethod
    def goal(cls, run: PluginRun) -> str:
        """Construct the natural-language goal for this run.

        The engine receives this as the initial user message. Be
        specific: the goal text is what the model sees first, so
        ambiguity here cascades into wasted tool calls.
        """

    @classmethod
    def system_prompt(cls, run: PluginRun) -> str | None:
        """Optional plugin-specific system prompt.

        Default returns None — the engine sends no system prompt
        and Claude uses its default behaviour. Override when the
        plugin needs persistent tone / framing instructions
        beyond what fits in the goal.
        """
        return None


class PluginRegistry:
    """Process-global plugin catalogue.

    Populated at startup by importing each plugin module and
    calling ``register(SmartResponderPlugin)``. The Phase 6
    scheduler and webhook receiver both consult the registry to
    decide what to do with a given trigger.
    """

    def __init__(self) -> None:
        self._plugins: dict[str, type[OrchestratorPlugin]] = {}

    def register(self, plugin_cls: type[OrchestratorPlugin]) -> None:
        """Add a plugin class. Raises if the name is taken or
        metadata is invalid.
        """
        plugin_cls.validate_metadata()
        if plugin_cls.name in self._plugins:
            raise ValueError(
                f"plugin {plugin_cls.name!r} already registered"
            )
        self._plugins[plugin_cls.name] = plugin_cls

    def get(self, name: str) -> type[OrchestratorPlugin] | None:
        return self._plugins.get(name)

    def all(self) -> list[type[OrchestratorPlugin]]:
        return list(self._plugins.values())

    def filter_by_trigger(
        self, trigger: Trigger
    ) -> list[type[OrchestratorPlugin]]:
        """Return every plugin that listens to a given trigger."""
        return [p for p in self._plugins.values() if trigger in p.triggers]

    def __len__(self) -> int:
        return len(self._plugins)

    def __contains__(self, name: str) -> bool:
        return name in self._plugins
