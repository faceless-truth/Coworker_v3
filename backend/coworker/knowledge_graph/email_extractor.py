"""Extract entity mentions + relationships from an email.

The Phase 6 ``correspondence_logger`` plugin calls this on every
inbound email and stages the result for the KG populator. The
extractor itself is a single Claude call (Haiku 4.5 for speed —
correspondence logging is a hot path) producing strict JSON with
the entities mentioned and any relationships implied between them.

The KG layer doesn't auto-merge the output: every proposed entity
and edge carries a ``confidence`` score and the calling plugin (or
the Phase 8 specialist prompt review UI) decides which proposals
to materialise. A typical pipeline:

1. ``correspondence_logger`` runs ``extract_entities_from_email``
2. Each proposed entity is matched against existing entities via
   ``sharepoint_resolver`` / pg_trgm similarity
3. Confidence ≥ 0.95 + an unambiguous match → auto-merge under
   provenance ``{"source": "email"}``
4. Lower confidence or ambiguous → queue for approval (Phase 9)

This module owns step 1. Steps 2-4 live in their own modules.
"""
import json
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from coworker.connectors.anthropic_client import (
    AnthropicClient,
    CompletionMessage,
    CompletionResult,
)
from coworker.connectors.exceptions import ConnectorTransient

_DEFAULT_MAX_TOKENS = 2000
# Haiku is the right tier — fast, cheap, structured-output-capable.
# Callers can override per-call but the default works for the hot path.
_DEFAULT_MODEL = "claude-haiku-4-5-20251001"

_SYSTEM_PROMPT = """\
You extract structured entity references from accounting-firm emails.

Output JSON with two fields:

- "entities": a list of objects, each with:
    "name" (string, canonical name as written)
    "entity_type" (one of: individual, company, trust, smsf,
      partnership, sole_trader, other)
    "confidence" (float 0.0-1.0)
- "relationships": a list of objects, each with:
    "from_name" (string, must match an entity's "name")
    "to_name"   (string, must match an entity's "name")
    "relationship_type" (one of: director_of, trustee_of,
      beneficiary_of, appointor_of, shareholder_of, secretary_of,
      spouse_of, parent_of, child_of, member_of, employee_of,
      accountant_of, other)
    "confidence" (float 0.0-1.0)

Rules:

- Only extract entities the email DIRECTLY names. Do not infer
  entities from generic phrases ("the trust", "the company") unless
  the name is also given.
- The firm's own staff (the sender / recipient at the firm domain)
  count as individuals, but rarely warrant relationship edges.
- Confidence reflects how sure you are about (a) the entity exists
  and (b) the type is correct. Use 0.95+ only when the email
  text leaves no ambiguity.
- If nothing is extractable, return empty arrays. Do not invent.

Output JSON only — no preamble, no markdown fences, no commentary.
"""


@dataclass(frozen=True)
class ProposedEntity:
    name: str
    entity_type: str
    confidence: float


@dataclass(frozen=True)
class ProposedRelationship:
    from_name: str
    to_name: str
    relationship_type: str
    confidence: float


@dataclass(frozen=True)
class EmailExtraction:
    """The structured output of one email extraction.

    Both lists may be empty (junk mail, internal admin mail). The
    KG populator is responsible for matching ``ProposedEntity.name``
    against existing entities and either UPSERTing or queueing for
    approval.
    """

    entities: list[ProposedEntity]
    relationships: list[ProposedRelationship]


# A "complete" callable matches AnthropicClient.complete's signature.
# Accepting the function rather than the client keeps this module
# testable with a small stub.
CompleteFn = Callable[..., Awaitable[CompletionResult]]


async def extract_entities_from_email(
    anthropic: AnthropicClient,
    *,
    subject: str,
    body: str,
    model: str | None = None,
    max_tokens: int = _DEFAULT_MAX_TOKENS,
) -> EmailExtraction:
    """Run Claude over an email and parse the entity / relationship JSON.

    Args:
        anthropic: per-firm AnthropicClient. PII scrubbing runs at
            its layer; this function passes the raw subject + body
            and trusts the connector to scrub.
        subject, body: email content. The function concatenates them
            with a clear separator so the model can give the subject
            extra weight.
        model: override the default Haiku model. The orchestrator
            (Phase 5) may pass a Sonnet model when running a deeper
            extraction pass.
        max_tokens: response cap. 2000 covers the JSON for an email
            mentioning ~20 entities comfortably.

    Returns:
        ``EmailExtraction`` parsed from Claude's JSON output. Malformed
        responses raise ``ConnectorTransient`` so callers can retry
        with backoff — same shape as the other connector errors.

    Raises:
        ConnectorTransient: model returned non-JSON or unexpected
            schema. Wrapped from the underlying ``JSONDecodeError``
            or ``KeyError`` so the caller's existing retry path
            applies.
        Any ``ConnectorError`` subclass that ``AnthropicClient.complete``
            propagates (auth, rate limit, network).
    """
    user_msg = CompletionMessage(
        role="user",
        content=f"Subject: {subject}\n\nBody:\n{body}",
    )
    result = await anthropic.complete(
        messages=[user_msg],
        model=model or _DEFAULT_MODEL,
        max_tokens=max_tokens,
        system=_SYSTEM_PROMPT,
    )
    return _parse_extraction(result.text)


def _parse_extraction(raw: str) -> EmailExtraction:
    """Parse Claude's JSON output. Tolerant of incidental wrappers.

    Even with strict prompting some models occasionally wrap JSON in
    ```json fences``` or prefix it with "Here is the extraction:".
    We try to peel that off before parsing. A malformed payload
    raises ``ConnectorTransient`` so the caller can retry.
    """
    payload = _strip_json_envelope(raw)
    try:
        data = json.loads(payload)
    except json.JSONDecodeError as exc:
        raise ConnectorTransient(
            f"email extractor returned non-JSON: {exc}"
        ) from exc

    if not isinstance(data, dict):
        raise ConnectorTransient(
            "email extractor returned non-object root"
        )

    raw_entities = data.get("entities") or []
    raw_rels = data.get("relationships") or []
    if not isinstance(raw_entities, list) or not isinstance(raw_rels, list):
        raise ConnectorTransient(
            "email extractor returned non-list entities/relationships"
        )

    entities: list[ProposedEntity] = []
    for item in raw_entities:
        parsed = _parse_entity(item)
        if parsed is not None:
            entities.append(parsed)

    # Build a name set so we can filter relationships referencing
    # entities the model didn't list. Reduces downstream cleanup.
    known_names = {e.name for e in entities}

    relationships: list[ProposedRelationship] = []
    for item in raw_rels:
        rel = _parse_relationship(item)
        if rel is None:
            continue
        if rel.from_name not in known_names or rel.to_name not in known_names:
            continue
        if rel.from_name == rel.to_name:
            continue
        relationships.append(rel)

    return EmailExtraction(entities=entities, relationships=relationships)


def _strip_json_envelope(raw: str) -> str:
    """Peel off code fences and chat preambles around a JSON object.

    Conservative — only strips if a JSON object is visibly present;
    otherwise hands the raw text through so the JSON decoder's
    error surfaces normally.
    """
    text = raw.strip()
    fence = re.match(r"```(?:json)?\s*(.+?)\s*```", text, re.DOTALL)
    if fence:
        text = fence.group(1).strip()
    # Find the outermost {...} if the model added preamble.
    if text and text[0] != "{":
        start = text.find("{")
        end = text.rfind("}")
        if start != -1 and end > start:
            text = text[start : end + 1]
    return text


def _parse_entity(raw: object) -> ProposedEntity | None:
    if not isinstance(raw, dict):
        return None
    name = raw.get("name")
    entity_type = raw.get("entity_type")
    confidence = raw.get("confidence")
    if not isinstance(name, str) or not name.strip():
        return None
    if not isinstance(entity_type, str) or not entity_type.strip():
        return None
    try:
        conf = float(confidence) if confidence is not None else 0.0
    except (TypeError, ValueError):
        conf = 0.0
    return ProposedEntity(
        name=name.strip(),
        entity_type=entity_type.strip().lower(),
        confidence=max(0.0, min(1.0, conf)),
    )


def _parse_relationship(raw: object) -> ProposedRelationship | None:
    if not isinstance(raw, dict):
        return None
    from_name = raw.get("from_name")
    to_name = raw.get("to_name")
    rel_type = raw.get("relationship_type")
    confidence = raw.get("confidence")
    if not all(
        isinstance(v, str) and v.strip()
        for v in (from_name, to_name, rel_type)
    ):
        return None
    try:
        conf = float(confidence) if confidence is not None else 0.0
    except (TypeError, ValueError):
        conf = 0.0
    return ProposedRelationship(
        from_name=from_name.strip(),  # type: ignore[union-attr]
        to_name=to_name.strip(),  # type: ignore[union-attr]
        relationship_type=rel_type.strip().lower(),  # type: ignore[union-attr]
        confidence=max(0.0, min(1.0, conf)),
    )
