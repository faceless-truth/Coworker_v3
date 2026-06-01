"""Chat orchestrator (v2 — tool-use with specialist routing).

The orchestrator (Sonnet 4.6) drives a tool-use loop. On each round it
streams a response that may contain text and zero or more
``consult_specialist`` tool calls. For each tool call the orchestrator
opens a specialist consultation (Opus, the specialist's active prompt
as system) and captures the full output server-side. Specialist
tokens are NOT streamed to the user; the user sees consultation
started/complete badges only. After all consultations complete, the
orchestrator's continuation turn synthesises the specialist findings
into a brief, scannable summary which IS streamed. The persisted
``chat_messages.content`` is then the synthesis followed by one
``<details>``/``<summary>`` collapsible per consultation containing
the full verbatim specialist output. The turn ends when Sonnet
returns ``stop_reason="end_turn"``.

Every chat turn writes one ``agent_traces`` row via
``AgentTraceWriter``. The orchestrator's Sonnet calls land as
``model_call`` steps; each tool call lands as a ``tool_call`` step;
each consultation result lands as a ``tool_result`` step that
records the specialist's model, token usage, and
``specialist_prompt_version_id`` (Phase 8.6 reproducibility) in the
step's ``content`` jsonb.

Cost accounting is intentionally out of scope for v1: every step row
has ``cost_cents=0``. Token counts on the trace and the assistant
chat_message are accurate; the cost column gets filled in alongside
the dev-token CLI / pricing-map refactor on the backlog.

The route handler signature is unchanged from 003d-1 (the FastAPI
dependency injection is identical); only this module's internals
were rewritten.
"""
import json
import time
import uuid
from collections.abc import AsyncIterator
from typing import Any

from loguru import logger
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from coworker.chat.specialist_consultation import (
    ConsultationComplete,
    ConsultationError,
    ConsultationStarted,
    ConsultationTextDelta,
    consult_specialist,
)
from coworker.config import get_settings
from coworker.connectors.anthropic_client import (
    AnthropicClient,
    StreamCompletion,
    StreamTextDelta,
    StreamToolUseBlock,
)
from coworker.connectors.exceptions import ConnectorError
from coworker.db.models.chat import ChatConversation, ChatMessage
from coworker.orchestrator.trace import AgentTraceWriter

ORCHESTRATOR_SYSTEM_PROMPT = (
    "You are CoWorker, an AI assistant for an Australian accounting "
    "practice (MC & S Pty Ltd, Melbourne).\n\n"
    "For substantive tax or compliance questions, consult the relevant "
    "specialists using the consult_specialist tool. After consultations "
    "complete, your job is to SYNTHESISE the specialist findings into a "
    "brief, scannable summary for the user.\n\n"
    "Synthesis discipline:\n"
    "1. Open with a one-sentence framing that names the domains "
    "covered (e.g. 'GST, CGT and Division 7A all apply to this "
    "structure.').\n"
    "2. Provide a short, scannable summary organised by issue or by "
    "specialist. Aim for 150 to 400 words. Use bullet points or short "
    "paragraphs.\n"
    "3. Cover only the most important findings: the key statutory "
    "provisions, the highest-impact action items, and any cross-domain "
    "interactions the specialists identified.\n"
    "4. Do NOT restate the specialists' full analyses. The user can "
    "expand collapsible sections to see the full verbatim output. "
    "Trust that the full text is one click away.\n"
    "5. Where specialists disagree or where multiple paths exist, "
    "briefly note the tradeoff.\n"
    "6. If any consultation failed (you saw a tool_result indicating "
    "failure), briefly acknowledge it in your synthesis (e.g. 'The "
    "Div7A consultation was unavailable; the analysis above relies on "
    "GST and CGT only.'). The user can expand the failed specialist's "
    "collapsible below to see what went wrong.\n"
    "7. End with 'Click any specialist below to see the full analysis.' "
    "if at least one consultation occurred.\n\n"
    "When NOT consulting specialists (simple questions, definitions, "
    "clarifications), answer directly and concisely.\n\n"
    "Style notes:\n"
    "- Australian English.\n"
    "- Narrowest useful provision citations (e.g. 'ITAA 1997 s 152-10' "
    "not 'Division 152').\n"
    "- No em dashes (house style).\n"
    "- No 'Let me check with...' preambles when calling tools. The "
    "user sees badges indicating consultation in progress; you do not "
    "need to narrate.\n\n"
    "Tone:\n"
    "You are talking to a qualified Australian accountant. Be "
    "respectful of their time. They want to orient quickly, not read "
    "a memo. They will dig into the full specialist output if they "
    "want depth."
)

_SPECIALIST_NAMES: list[str] = [
    "gst",
    "smsf",
    "div7a",
    "trust_tax",
    "cgt_concessions_rollovers",
]

CONSULT_SPECIALIST_TOOL: dict[str, Any] = {
    "name": "consult_specialist",
    "description": (
        "Consult an internal specialist agent for substantive technical "
        "analysis on Australian tax law or compliance matters. The "
        "specialist's full answer is shown directly to the user, so you "
        "do not need to restate it. You may call this tool multiple "
        "times in one turn if a question spans multiple specialist "
        "domains.\n\n"
        "Available specialists:\n"
        "- gst: Australian GST law (taxable / GST-free / input-taxed "
        "supplies, input tax credits, going concern, margin scheme, "
        "BAS preparation, attribution)\n"
        "- smsf: SMSF compliance (contributions caps, transfer balance "
        "cap, in-house assets, related-party transactions, LRBA, audit "
        "independence)\n"
        "- div7a: Division 7A ITAA 1936 (complying loans, distributable "
        "surplus, UPEs, interposed entities, integrity provisions)\n"
        "- trust_tax: Trust taxation under Division 6 (trustee "
        "resolutions, present entitlement, streaming, resettlement "
        "risk, deed mechanics)\n"
        "- cgt_concessions_rollovers: CGT concessions and rollovers "
        "(Division 152 small business CGT concessions, Subdivisions "
        "122 / 124-M / 615 / 328-G rollovers)"
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "specialist_name": {
                "type": "string",
                "enum": _SPECIALIST_NAMES,
                "description": "The specialist to consult.",
            },
            "question": {
                "type": "string",
                "description": (
                    "The specific, focused question to ask the "
                    "specialist. Reformulate from the user's question "
                    "to be clear and scoped to this specialist's "
                    "domain. Include relevant facts from the "
                    "conversation if needed."
                ),
            },
        },
        "required": ["specialist_name", "question"],
    },
}

_GOAL_MAX_LEN = 500
_TRACE_TYPE = "chat_turn"


def _sse(event: str, data: dict[str, object]) -> str:
    return f"event: {event}\ndata: {json.dumps(data)}\n\n"


_CONSULTATIONS_START_MARKER = "<!-- specialist-consultations-start -->"
_CONSULTATIONS_END_MARKER = "<!-- specialist-consultations-end -->"


def _assemble_assistant_content(
    synthesis_text: str,
    completed_consultations: list[dict[str, Any]],
) -> str:
    """Combine the orchestrator's synthesis with one ``<details>``
    collapsible per consultation.

    Consultations appear in the order they completed. Successful
    consultations carry the specialist's full verbatim text. Failed
    consultations carry the error message and any partial text that
    streamed before the failure. Markers around the collapsibles
    bracket the region so existing chat history (no markers) renders
    unchanged.
    """
    if not completed_consultations:
        return synthesis_text

    parts: list[str] = [synthesis_text.rstrip(), "", _CONSULTATIONS_START_MARKER, ""]
    for c in completed_consultations:
        version_id = c.get("prompt_version_id")
        version_label = (
            f"prompt v{version_id[:8]}"
            if isinstance(version_id, str) and version_id
            else "prompt version unknown"
        )
        model_label = c.get("model") or "model unknown"
        display_name = c["display_name"]

        if c["status"] == "ok":
            summary_label = (
                f"{display_name} — full analysis "
                f"({version_label}, {model_label})"
            )
            body = c["full_text"]
        else:
            summary_label = (
                f"{display_name} — consultation FAILED "
                f"({version_label}, {model_label})"
            )
            body_lines = [c.get("error", "Unknown error")]
            partial = c.get("partial_text") or ""
            if partial:
                body_lines.extend(["", partial])
            body = "\n".join(body_lines)

        parts.append("<details>")
        parts.append(f"<summary>{summary_label}</summary>")
        parts.append("")
        parts.append(body)
        parts.append("")
        parts.append("</details>")
        parts.append("")
    parts.append(_CONSULTATIONS_END_MARKER)
    return "\n".join(parts)


async def stream_chat(
    session: AsyncSession,
    *,
    conversation_id: uuid.UUID,
    user_content: str,
    firm_id: uuid.UUID,
    client: AnthropicClient | None = None,
) -> AsyncIterator[str]:
    """Drive one chat turn with tool-use + specialist routing.

    Caller must already be inside ``firm_context(firm_id)``. Yields
    SSE-formatted strings: ``token`` (with ``source``),
    ``specialist_consultation_started``, ``specialist_consultation_
    complete``, ``specialist_consultation_error``, then either
    ``done`` or ``error``.

    ``client`` is injectable for tests; production wiring constructs
    a fresh per-firm ``AnthropicClient`` per turn.
    """
    settings = get_settings()
    if client is None:
        client = AnthropicClient(firm_id=str(firm_id))

    user_msg = ChatMessage(
        conversation_id=conversation_id,
        firm_id=firm_id,
        role="user",
        content=user_content,
    )
    session.add(user_msg)
    await session.flush()

    writer = AgentTraceWriter(session, firm_id)
    trace_id = await writer.start_trace(
        goal=user_content[:_GOAL_MAX_LEN],
        metadata={
            "trace_type": _TRACE_TYPE,
            "conversation_id": str(conversation_id),
            "user_message_id": str(user_msg.id),
        },
    )

    history_rows = (
        await session.execute(
            select(ChatMessage)
            .where(ChatMessage.conversation_id == conversation_id)
            .order_by(ChatMessage.created_at.asc())
        )
    ).scalars().all()

    api_messages: list[dict[str, Any]] = [
        {"role": row.role, "content": row.content}
        for row in history_rows
        if row.role in ("user", "assistant") and row.content
    ]

    displayed_full_text_parts: list[str] = []
    completed_consultations: list[dict[str, Any]] = []
    total_input_tokens = 0
    total_output_tokens = 0
    final_error: str | None = None
    final_status = "completed"
    final_completion_reason: str | None = None

    try:
        while True:
            round_text_parts: list[str] = []
            round_tool_uses: list[StreamToolUseBlock] = []
            round_stop_reason = "end_turn"
            round_input_tokens = 0
            round_output_tokens = 0
            round_model = settings.ANTHROPIC_MODEL_DEFAULT
            round_full_text = ""
            round_start = time.perf_counter()

            async for event in client.stream_message_with_tools(
                messages=api_messages,
                system=ORCHESTRATOR_SYSTEM_PROMPT,
                tools=[CONSULT_SPECIALIST_TOOL],
                model=settings.ANTHROPIC_MODEL_DEFAULT,
                max_tokens=settings.ANTHROPIC_MAX_TOKENS_DEFAULT,
            ):
                if isinstance(event, StreamTextDelta):
                    round_text_parts.append(event.text)
                    yield _sse(
                        "token",
                        {"text": event.text, "source": "orchestrator"},
                    )
                elif isinstance(event, StreamToolUseBlock):
                    round_tool_uses.append(event)
                elif isinstance(event, StreamCompletion):
                    round_full_text = event.full_text
                    round_input_tokens = event.input_tokens
                    round_output_tokens = event.output_tokens
                    round_stop_reason = event.stop_reason
                    round_model = event.model

            round_duration_ms = int(
                (time.perf_counter() - round_start) * 1000
            )
            total_input_tokens += round_input_tokens
            total_output_tokens += round_output_tokens
            if round_full_text:
                displayed_full_text_parts.append(round_full_text)

            response_content_blocks: list[dict[str, Any]] = []
            if round_full_text:
                response_content_blocks.append(
                    {"type": "text", "text": round_full_text}
                )
            for tu in round_tool_uses:
                response_content_blocks.append(
                    {
                        "type": "tool_use",
                        "id": tu.id,
                        "name": tu.name,
                        "input": tu.input,
                    }
                )

            await writer.record_model_call(
                model=round_model,
                request_messages=api_messages,
                response_content=response_content_blocks,
                input_tokens=round_input_tokens,
                output_tokens=round_output_tokens,
                cost_cents=0,
                duration_ms=round_duration_ms,
                stop_reason=round_stop_reason,
            )

            if round_stop_reason != "tool_use" or not round_tool_uses:
                break

            assistant_blocks: list[dict[str, Any]] = list(
                response_content_blocks
            )
            api_messages.append(
                {"role": "assistant", "content": assistant_blocks}
            )

            tool_result_blocks: list[dict[str, Any]] = []
            for tu in round_tool_uses:
                await writer.record_tool_call(
                    tool_name=tu.name,
                    tool_use_id=tu.id,
                    input_data=dict(tu.input),
                )

                if tu.name != "consult_specialist":
                    err_text = f"Unknown tool: {tu.name}"
                    await writer.record_tool_result(
                        tool_name=tu.name,
                        tool_use_id=tu.id,
                        result=err_text,
                        is_error=True,
                        duration_ms=0,
                        cost_cents=0,
                        error_class="UnknownTool",
                    )
                    tool_result_blocks.append(
                        {
                            "type": "tool_result",
                            "tool_use_id": tu.id,
                            "content": err_text,
                            "is_error": True,
                        }
                    )
                    continue

                specialist_name = str(tu.input.get("specialist_name", ""))
                question = str(tu.input.get("question", ""))

                consultation_start = time.perf_counter()
                consultation_full_text = ""
                consultation_partial_text = ""
                consultation_error: str | None = None
                consultation_error_class: str | None = None
                consultation_prompt_version_id: uuid.UUID | None = None
                consultation_model: str | None = None
                consultation_input_tokens = 0
                consultation_output_tokens = 0
                consultation_started_emitted = False
                consultation_display_name = ""

                async for cev in consult_specialist(
                    session,
                    client,
                    specialist_name=specialist_name,
                    question=question,
                ):
                    if isinstance(cev, ConsultationStarted):
                        consultation_started_emitted = True
                        consultation_prompt_version_id = cev.prompt_version_id
                        consultation_model = cev.model
                        consultation_display_name = cev.display_name
                        yield _sse(
                            "specialist_consultation_started",
                            {
                                "specialist_name": cev.specialist_name,
                                "display_name": cev.display_name,
                                "prompt_version_id": str(
                                    cev.prompt_version_id
                                ),
                                "model": cev.model,
                                "step_index": writer.next_step_index,
                            },
                        )
                    elif isinstance(cev, ConsultationTextDelta):
                        # Specialist tokens are no longer streamed to
                        # the user; the orchestrator's post-consultation
                        # synthesis is what the user reads. Full
                        # specialist text is collected here and surfaced
                        # via a <details> collapsible appended to
                        # chat_messages.content after the stream ends.
                        consultation_full_text += cev.text
                    elif isinstance(cev, ConsultationComplete):
                        consultation_full_text = cev.full_text
                        consultation_input_tokens = cev.input_tokens
                        consultation_output_tokens = cev.output_tokens
                        total_input_tokens += cev.input_tokens
                        total_output_tokens += cev.output_tokens
                        yield _sse(
                            "specialist_consultation_complete",
                            {
                                "specialist_name": cev.specialist_name,
                                "input_tokens": cev.input_tokens,
                                "output_tokens": cev.output_tokens,
                                "step_index": writer.next_step_index,
                            },
                        )
                    elif isinstance(cev, ConsultationError):
                        consultation_error = cev.error
                        consultation_error_class = cev.error.split(":", 1)[0]
                        consultation_partial_text = cev.partial_text
                        if cev.prompt_version_id is not None:
                            consultation_prompt_version_id = (
                                cev.prompt_version_id
                            )
                        if cev.model is not None:
                            consultation_model = cev.model
                        yield _sse(
                            "specialist_consultation_error",
                            {
                                "specialist_name": cev.specialist_name,
                                "error": cev.error,
                                "step_index": writer.next_step_index,
                            },
                        )

                consultation_duration_ms = int(
                    (time.perf_counter() - consultation_start) * 1000
                )

                # Capture each consultation for the post-stream
                # assembly of chat_messages.content as a <details>
                # collapsible. Successes carry the full text;
                # failures carry the error and any partial text.
                collapsible_display_name = (
                    consultation_display_name or specialist_name or "Specialist"
                )
                if consultation_error is None:
                    completed_consultations.append(
                        {
                            "status": "ok",
                            "specialist_name": specialist_name,
                            "display_name": collapsible_display_name,
                            "prompt_version_id": (
                                str(consultation_prompt_version_id)
                                if consultation_prompt_version_id is not None
                                else None
                            ),
                            "model": consultation_model,
                            "full_text": consultation_full_text,
                        }
                    )
                else:
                    completed_consultations.append(
                        {
                            "status": "failed",
                            "specialist_name": specialist_name,
                            "display_name": collapsible_display_name,
                            "prompt_version_id": (
                                str(consultation_prompt_version_id)
                                if consultation_prompt_version_id is not None
                                else None
                            ),
                            "model": consultation_model,
                            "error": consultation_error,
                            "partial_text": consultation_partial_text,
                        }
                    )

                tool_result_text = (
                    consultation_full_text
                    if consultation_error is None
                    else (
                        f"Consultation failed: {consultation_error}. "
                        "Proceed without this specialist or try a "
                        "different approach."
                    )
                )

                extra_content: dict[str, Any] = {
                    "specialist_name": specialist_name,
                    "specialist_started": consultation_started_emitted,
                }
                if consultation_prompt_version_id is not None:
                    extra_content["specialist_prompt_version_id"] = str(
                        consultation_prompt_version_id
                    )

                await writer.record_tool_result(
                    tool_name=tu.name,
                    tool_use_id=tu.id,
                    result=tool_result_text,
                    is_error=consultation_error is not None,
                    duration_ms=consultation_duration_ms,
                    cost_cents=0,
                    error_class=consultation_error_class,
                    model=consultation_model,
                    input_tokens=(
                        consultation_input_tokens
                        if consultation_input_tokens
                        else None
                    ),
                    output_tokens=(
                        consultation_output_tokens
                        if consultation_output_tokens
                        else None
                    ),
                    extra_content=extra_content,
                )

                tool_result_blocks.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": tu.id,
                        "content": tool_result_text,
                        **(
                            {"is_error": True}
                            if consultation_error is not None
                            else {}
                        ),
                    }
                )

            api_messages.append(
                {"role": "user", "content": tool_result_blocks}
            )

    except ConnectorError as exc:
        final_error = f"{type(exc).__name__}: {exc}"
        final_status = "failed"
        final_completion_reason = "connector_error"
        logger.warning(
            "chat.stream_failed: conversation_id={} firm_id={} error={}",
            conversation_id,
            firm_id,
            final_error,
        )
        yield _sse("error", {"error": final_error})
    except Exception as exc:
        final_error = f"{type(exc).__name__}: {exc}"
        final_status = "failed"
        final_completion_reason = "unexpected_error"
        logger.exception(
            "chat.stream_unexpected: conversation_id={} firm_id={}",
            conversation_id,
            firm_id,
        )
        yield _sse("error", {"error": final_error})

    await writer.finish_trace(
        status=final_status,
        completion_reason=final_completion_reason,
    )

    synthesis_text = "".join(displayed_full_text_parts)
    assistant_content = _assemble_assistant_content(
        synthesis_text, completed_consultations
    )
    assistant_msg = ChatMessage(
        conversation_id=conversation_id,
        firm_id=firm_id,
        role="assistant",
        content=assistant_content,
        model=settings.ANTHROPIC_MODEL_DEFAULT,
        input_tokens=total_input_tokens or None,
        output_tokens=total_output_tokens or None,
        error=final_error,
        trace_id=trace_id,
    )
    session.add(assistant_msg)

    conv = (
        await session.execute(
            select(ChatConversation).where(
                ChatConversation.id == conversation_id
            )
        )
    ).scalar_one()
    conv.updated_at = func.now()

    await session.commit()

    if final_error is None:
        yield _sse(
            "done",
            {
                "message_id": str(assistant_msg.id),
                "trace_id": str(trace_id),
                "total_input_tokens": total_input_tokens,
                "total_output_tokens": total_output_tokens,
            },
        )
