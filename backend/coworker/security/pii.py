import uuid
from dataclasses import dataclass
from typing import Any

from presidio_analyzer import AnalyzerEngine, Pattern, PatternRecognizer
from presidio_analyzer.nlp_engine import NlpEngineProvider

# Australian-specific recognisers
TFN_PATTERN = PatternRecognizer(
    supported_entity="AU_TFN",
    patterns=[Pattern("TFN nine-digit", r"\b\d{3}\s?\d{3}\s?\d{3}\b", 0.6)],
)
ABN_PATTERN = PatternRecognizer(
    supported_entity="AU_ABN",
    patterns=[Pattern("ABN eleven-digit", r"\b\d{2}\s?\d{3}\s?\d{3}\s?\d{3}\b", 0.6)],
)
MEDICARE_PATTERN = PatternRecognizer(
    supported_entity="AU_MEDICARE",
    patterns=[Pattern("Medicare ten or eleven digit",
                      r"\b\d{4}\s?\d{5}\s?\d{1,2}\b", 0.5)],
)
DRIVERS_LICENCE_VIC = PatternRecognizer(
    supported_entity="AU_DL_VIC",
    patterns=[Pattern("VIC drivers licence", r"\b[0-9]{8,10}\b", 0.3)],
)


# Entity types replaced with placeholders before any payload reaches
# Anthropic. Australian identifiers first, then the generic presidio
# defaults that are relevant to an accounting practice.
#
# DATE_TIME is deliberately excluded. spaCy's DATE_TIME recogniser
# false-positives on statute references ("SIS Act 1993", "ITAA 1936"),
# legislative amendment years ("as amended in 2014"), and heading-style
# section pinpoints ("s 62 SIS Act: ..."), which leaves user-visible
# residue like "SIS Act s [DATE_TIME_xxx]" when scrub-restore round
# trips through the chat streaming path. Calendar dates are not
# sensitive identifiers for an accounting practice; the load-bearing
# ones (TFN, ABN, Medicare, names, emails, phone numbers, card numbers)
# are still scrubbed.
SCRUBBED_ENTITY_TYPES = [
    "AU_TFN",
    "AU_ABN",
    "AU_MEDICARE",
    "AU_DL_VIC",
    "PHONE_NUMBER",
    "EMAIL_ADDRESS",
    "CREDIT_CARD",
    "IBAN_CODE",
    "PERSON",
]


@dataclass
class ScrubResult:
    text: str
    mapping: dict[str, str]  # placeholder -> original

    def restore(self, text: str) -> str:
        for placeholder, original in self.mapping.items():
            text = text.replace(placeholder, original)
        return text


class PIIScrubber:
    def __init__(self) -> None:
        provider = NlpEngineProvider(nlp_configuration={
            "nlp_engine_name": "spacy",
            "models": [{"lang_code": "en", "model_name": "en_core_web_lg"}],
        })
        self.analyzer = AnalyzerEngine(
            nlp_engine=provider.create_engine(),
            supported_languages=["en"],
        )
        for r in (TFN_PATTERN, ABN_PATTERN, MEDICARE_PATTERN, DRIVERS_LICENCE_VIC):
            self.analyzer.registry.add_recognizer(r)

    def scrub(self, text: str, *, entities: list[str] | None = None) -> ScrubResult:
        entities = entities or SCRUBBED_ENTITY_TYPES
        results = self.analyzer.analyze(text=text, language="en", entities=entities)
        # Recognisers can produce overlapping hits — Presidio's
        # generic PHONE_NUMBER often co-fires with our AU_ABN /
        # AU_MEDICARE patterns on the same span, and PERSON can
        # cover the same offsets as EMAIL_ADDRESS. Replacing
        # overlapping hits naively (end-to-start) corrupts the
        # output: a later replacement lands inside an earlier one,
        # leaving a mangled tail like `]a3a]` past the placeholder.
        # Resolve by keeping the highest-confidence non-overlapping
        # spans first and discarding any later span that overlaps
        # one we've already accepted.
        accepted = _select_non_overlapping(results)
        mapping: dict[str, str] = {}
        scrubbed = text
        # Replace from end to start to preserve offsets.
        for r in sorted(accepted, key=lambda x: x.start, reverse=True):
            placeholder = f"[{r.entity_type}_{uuid.uuid4().hex[:6]}]"
            original = scrubbed[r.start:r.end]
            mapping[placeholder] = original
            scrubbed = scrubbed[:r.start] + placeholder + scrubbed[r.end:]
        return ScrubResult(text=scrubbed, mapping=mapping)


def _select_non_overlapping(results: list[Any]) -> list[Any]:
    """Greedy non-overlapping selection by descending confidence.

    Sort recogniser results by score (highest first), then by length
    (longer first, so a 9-digit TFN wins over an 8-digit substring).
    Walk in order and keep each span only if it does not overlap any
    previously kept span.
    """
    by_priority = sorted(
        results,
        key=lambda r: (r.score, r.end - r.start),
        reverse=True,
    )
    accepted: list[Any] = []
    for r in by_priority:
        if not any(_overlaps(r, k) for k in accepted):
            accepted.append(r)
    return accepted


def _overlaps(a: Any, b: Any) -> bool:
    return bool(a.start < b.end and b.start < a.end)
