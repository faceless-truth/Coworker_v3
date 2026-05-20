"""Individual return prep plugin — methodology-grounding probe (Task B, Slice 1 Phase 2 only).

This plugin's purpose is narrow: given prior-year and current-year individual
client documents carried in ``PluginRun.event_data``, produce a structured set
of findings (deductibles found, missing-vs-last-year, follow-up questions)
that the harness renders as a Word document for an accountant's review.

Grounding source
----------------
The plugin's ``system_prompt`` loads the MC&S Individual Tax Return Methodology
verbatim from ``docs/methodology/individual_tax_return.md`` at module import.
Thin sections (notably §4 occupation-specific) ship thin per the methodology
as-written; the plugin does NOT add tax knowledge beyond the methodology.

The year-over-year comparison scoping rule (§3 "Scope of year-over-year
comparison" — ATO-prefilled income out, interest and dividends in) is loaded
with the rest of §3 via ``_METHODOLOGY_TEXT``. There is no plugin-side
encoding of this rule and none should be added: duplicating methodology in
code creates drift the next time the methodology changes. The methodology
document is the single source of truth for the scope of the comparison.

Gap 1 override (load-bearing)
-----------------------------
Methodology §3 references "material" variances. For this engine that language
is OVERRIDDEN: there is NO materiality threshold. The variance test is
presence/absence — every category present last year and absent this year is a
finding. Magnitude changes in a category present both years are NOT flagged.
The override is encoded explicitly in the system prompt so the model cannot
drift back to a percentage / "significant movement" heuristic.

Occupation
----------
Occupation is read from the prior-year return. Mid-year occupation change is
out of scope for this probe (no email ingestion).

Exercise path
-------------
This plugin is exercised by a feed-direct harness (no executor, no DB, no
installations, no tool registry, no engine). The harness builds a PluginRun
directly, calls the plugin's classmethods, and makes a single Anthropic
completion mirroring engine.py:234-238 (prepending the data-vs-instructions
rule to the system prompt). ``enabled_tool_categories`` is therefore empty.

Recorded future targets (DO NOT build here)
-------------------------------------------
- ATO *Individual tax return instructions* booklet AND ATO per-occupation
  deduction guides belong in the Phase-4 retrieval/``documents`` layer
  (reachable via ``memory_query``), NOT in this plugin's system prompt. They
  are perishable, annually-restated facts the methodology *applies*; the
  methodology is the prompt, the rates/guides are retrieved facts.
- Once client persistence + Phase 3 storage + structured-correction capture
  land, prior-year comparison runs against the stored, accountant-validated
  prior-year record instead of a re-attached artifact. This is why the
  harness output is discretely structured (per Task B §2) — so the future
  correction-capture layer can attach corrections to specific findings
  without a rewrite.
"""
from pathlib import Path

from coworker.plugins.base import OrchestratorPlugin, PluginRun

# Load the methodology from its tracked permanent home. parents[4] resolves
# from backend/coworker/plugins/builtin/<this file>.py up to the repo root.
_REPO_ROOT = Path(__file__).resolve().parents[4]
_METHODOLOGY_PATH = (
    _REPO_ROOT / "docs" / "methodology" / "individual_tax_return.md"
)
_METHODOLOGY_TEXT = _METHODOLOGY_PATH.read_text(encoding="utf-8")


_GAP_1_OVERRIDE = """\
# MC&S Override Rule — Variance Test is Presence/Absence (NOT Materiality)

The methodology's §3 ("Mandatory Questions for Variances") and elsewhere
references "material" movements / variances. For this engine, that language
is OVERRIDDEN as follows:

- There is NO materiality threshold for individual returns.
- The variance test is presence/absence.
- For every income and deduction category present in the prior-year return,
  check whether it is present in the current-year documents.
- ANY category present last year and absent this year is a finding and
  generates a client follow-up question.
- Magnitude changes in a category present in BOTH years are NOT flagged.
  DO NOT introduce a percentage threshold, a "material variance" heuristic,
  a "significant movement" filter, or any other magnitude-based gate under
  any rationale. "Material" here means "absent", not "different in size".

This override is non-negotiable for this run.
"""


_OCCUPATION_AND_LIMITATIONS = """\
# Occupation and known limitations

- Occupation is read from the prior-year return.
- Mid-year occupation change is out of scope for this probe — there is no
  email ingestion that would surface a job change. If the prior-year
  occupation is ambiguous or absent, say so explicitly rather than guessing.
- §4 (occupation-specific deductions) ships as-written in the methodology.
  Thin sections ship thin. Do NOT enrich §4 with ATO occupation-guide content
  that is not in this prompt — that is a recorded future target (Phase-4
  retrieval layer), not your job here.
"""


_OUTPUT_FORMAT = """\
# Required output format (strict)

Output EXACTLY one JSON object. No preamble. No markdown fences. No trailing
prose. The harness parses your output as JSON; any deviation from this shape
will cause the run to fail.

Schema:

{
  "deductibles_found": [
    {
      "section": "deductibles_found",
      "category": "<deduction category, e.g. 'Motor vehicle' or 'Union fees'>",
      "observation": "<what you found in the current-year documents, brief>",
      "prior_year_present": <true|false>,
      "current_year_present": true,
      "client_question": null
    }
  ],
  "missing_vs_last_year": [
    {
      "section": "missing_vs_last_year",
      "category": "<category present in prior-year return>",
      "observation": "<what was in the prior-year return, brief>",
      "prior_year_present": true,
      "current_year_present": false,
      "client_question": "<follow-up question to ask the client>"
    }
  ],
  "client_follow_up_questions": [
    {
      "section": "client_follow_up_questions",
      "category": "<topic, e.g. 'Work from home' or 'Investments'>",
      "observation": "<why this question matters, brief>",
      "prior_year_present": <true|false|null>,
      "current_year_present": <true|false|null>,
      "client_question": "<question, drawn from methodology §15 framework>"
    }
  ]
}

Rules:

- ``missing_vs_last_year`` items MUST have prior_year_present=true,
  current_year_present=false, and a non-null client_question. This is the
  Gap 1 presence/absence rule encoded. Scope of which categories are
  eligible for ``missing_vs_last_year`` is governed by the methodology's
  §3 "Scope of year-over-year comparison" subsection — do not include
  categories §3 places out of scope.
- ``client_follow_up_questions`` may include items not tied to a specific
  missing category — for example questions from methodology §15 that probe
  areas the current-year documents are silent on (marital status, super
  contributions, etc.). Use the §15 framework as your source of question
  shape.
- ``deductibles_found`` are deductions actually supported by the
  current-year documents (per the methodology's occupation/category review).
  Do not invent deductions not evidenced in the documents.
- Do NOT invent figures. Do NOT enter amounts into a return. This is a
  review draft; a human accountant lodges manually.
- If a section has no items, emit it as an empty array.
"""


class IndividualReturnPrepPlugin(OrchestratorPlugin):
    """Individual tax return prep probe (Task B, methodology grounding test).

    Exercised by a feed-direct harness, not the orchestrator engine. The
    grounded prompt is the MC&S Individual Tax Return Methodology + the
    Gap 1 presence/absence override + an output-format spec. See module
    docstring for the recorded future targets that are deliberately NOT
    built here.
    """

    name = "individual_return_prep"
    display_name = "Individual Return Prep"
    description = (
        "Apply the MC&S Individual Tax Return Methodology to a single "
        "individual client's prior-year and current-year documents, "
        "producing discrete findings (deductibles found, missing vs last "
        "year, follow-up questions) for an accountant to review."
    )
    version = "0.1.0"
    triggers = frozenset({"manual"})
    enabled_tool_categories = frozenset()  # direct-exercise path needs no tools
    allow_side_effects = False
    cost_budget_cents = 0  # harness manages spend directly; engine path unused

    @classmethod
    def system_prompt(cls, run: PluginRun) -> str:
        """Return methodology + Gap 1 override + occupation rule + output format.

        The harness mirrors engine.py:234-238 by prepending the
        ``_DATA_VS_INSTRUCTIONS_RULE`` constant; this method returns the
        plugin's contribution only.
        """
        return (
            _METHODOLOGY_TEXT
            + "\n\n---\n\n"
            + _GAP_1_OVERRIDE
            + "\n---\n\n"
            + _OCCUPATION_AND_LIMITATIONS
            + "\n---\n\n"
            + _OUTPUT_FORMAT
        )

    @classmethod
    def goal(cls, run: PluginRun) -> str:
        """Compose the user-message goal from documents carried in event_data.

        Expected event_data keys:
            prior_year_text: str — extracted text of the prior-year return PDF.
            current_year_docs: list[dict] — each {"name": str, "text": str}.

        Both keys are required. Missing keys raise KeyError on access so bad
        harness invocations surface immediately rather than silently running
        with empty input.
        """
        prior_year_text = run.event_data["prior_year_text"]
        current_year_docs = run.event_data["current_year_docs"]

        current_year_block = "\n\n".join(
            f"## Current-year document: {doc['name']}\n\n{doc['text']}"
            for doc in current_year_docs
        )

        return (
            "You are preparing review notes for one individual tax return. "
            "Apply the MC&S Individual Tax Return Methodology (in your "
            "system prompt). Apply the Gap 1 presence/absence override "
            "(also in your system prompt) — do NOT use a magnitude / "
            "materiality heuristic.\n\n"
            "Step 1. Read the prior-year return below. Identify the "
            "occupation and the set of income/deduction categories "
            "present.\n\n"
            "Step 2. Read each current-year document. For each, identify "
            "what is evidenced.\n\n"
            "Step 3. Build three discrete sections:\n"
            "- deductibles_found: deduction categories supported by the "
            "current-year documents, per the methodology's "
            "occupation/category review.\n"
            "- missing_vs_last_year: every prior-year category in scope "
            "per the methodology's §3 \"Scope of year-over-year "
            "comparison\" subsection that is absent from the current-"
            "year documents. Presence/absence rule — no magnitude "
            "threshold.\n"
            "- client_follow_up_questions: from missing items plus the "
            "methodology's §15 review-question framework. Include "
            "questions for areas the current-year documents are silent "
            "on (e.g. marital status, super contributions) even if they "
            "aren't tied to a prior-year category.\n\n"
            "Step 4. Output EXACTLY one JSON object matching the schema "
            "in your system prompt. No markdown fences. No preamble.\n\n"
            "---\n\n"
            "# Prior-year return (extracted text)\n\n"
            f"{prior_year_text}\n\n"
            "---\n\n"
            "# Current-year documents\n\n"
            f"{current_year_block}"
        )
