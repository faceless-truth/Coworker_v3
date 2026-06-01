"""Pin the PIIScrubber entity list at the scrubber boundary.

These tests target ``PIIScrubber.scrub`` directly (not via the
AnthropicClient plumbing in ``test_anthropic_pii_scrub.py``). Three
test surfaces:

- Statutory content must survive scrubbing untouched. Driven by the
  three real production failure patterns observed during the Task
  003d-2 specialist smoke test: statute years, amendment years, and
  heading-style section pinpoints. Bare section references are
  included too as defence-in-depth.
- Sensitive identifiers (PERSON, EMAIL_ADDRESS, PHONE_NUMBER,
  CREDIT_CARD) must still be replaced with placeholders.
- Round-trip scrub-then-restore must reproduce the original text
  byte-for-byte for content that mixes preserved and scrubbed spans.

If ``DATE_TIME`` is ever re-added to ``SCRUBBED_ENTITY_TYPES`` the
statutory survival tests will fail loudly. That is the regression
fence this file exists for.
"""
import pytest

from coworker.security.pii import PIIScrubber


@pytest.fixture(scope="module")
def scrubber() -> PIIScrubber:
    return PIIScrubber()


class TestStatutoryContentSurvivesScrubbing:
    """Production failure patterns from the 003d-2 smoke test.

    The patterns flagged below were observed locally against the live
    ``PIIScrubber`` with ``DATE_TIME`` in the entity list; they
    produced ``[DATE_TIME_xxx]`` residue in chat responses. After the
    fix, all of these strings pass through verbatim.
    """

    def test_statute_year_preserved_sis_act_1993(self, scrubber: PIIScrubber) -> None:
        text = (
            "The sole purpose test in s 62 of the SIS Act 1993 requires "
            "the fund to be maintained solely for retirement benefits."
        )
        result = scrubber.scrub(text)
        assert "1993" in result.text
        assert "SIS Act" in result.text
        assert "s 62" in result.text
        assert "[DATE_TIME" not in result.text

    def test_amendment_years_preserved_itaa_1936(self, scrubber: PIIScrubber) -> None:
        text = (
            "Per s 109D ITAA 1936 (as amended in 2014), private company "
            "loans are deemed dividends."
        )
        result = scrubber.scrub(text)
        assert "1936" in result.text
        assert "2014" in result.text
        assert "s 109D" in result.text
        assert "ITAA" in result.text
        assert "[DATE_TIME" not in result.text

    def test_heading_style_section_pinpoint_preserved(
        self, scrubber: PIIScrubber
    ) -> None:
        text = "s 62 SIS Act: the sole purpose test."
        result = scrubber.scrub(text)
        assert "s 62" in result.text
        assert "SIS Act" in result.text
        assert "[DATE_TIME" not in result.text

    def test_sis_act_section_number_preserved(self, scrubber: PIIScrubber) -> None:
        text = "Refer to s 62 of the SIS Act for the sole purpose test."
        result = scrubber.scrub(text)
        assert "s 62" in result.text
        assert "[DATE_TIME" not in result.text

    def test_itaa_section_numbers_preserved(self, scrubber: PIIScrubber) -> None:
        text = (
            "Division 7A applies via s 109D, with s 109N as an exception."
        )
        result = scrubber.scrub(text)
        assert "s 109D" in result.text
        assert "s 109N" in result.text
        assert "Division 7A" in result.text
        assert "[DATE_TIME" not in result.text

    def test_subdivision_references_preserved(self, scrubber: PIIScrubber) -> None:
        text = "Apply Subdivision 152-A small business CGT concessions."
        result = scrubber.scrub(text)
        assert "Subdivision 152-A" in result.text
        assert "[DATE_TIME" not in result.text

    def test_pinpoint_with_subsection_preserved(
        self, scrubber: PIIScrubber
    ) -> None:
        text = "Refer to s 62(1)(a) of the SIS Act."
        result = scrubber.scrub(text)
        assert "s 62(1)(a)" in result.text
        assert "[DATE_TIME" not in result.text

    def test_calendar_date_passes_through(self, scrubber: PIIScrubber) -> None:
        text = "The election must be made by 30 June 2024."
        result = scrubber.scrub(text)
        assert "30 June 2024" in result.text
        assert "[DATE_TIME" not in result.text

    def test_financial_year_passes_through(self, scrubber: PIIScrubber) -> None:
        text = "Revenue for FY2024-2025 totalled $1.2M."
        result = scrubber.scrub(text)
        assert "FY2024-2025" in result.text
        assert "[DATE_TIME" not in result.text

    def test_section_with_calendar_date_both_preserved(
        self, scrubber: PIIScrubber
    ) -> None:
        text = "Under s 62 as at 1 July 2024, the rules changed."
        result = scrubber.scrub(text)
        assert "s 62" in result.text
        assert "1 July 2024" in result.text
        assert "[DATE_TIME" not in result.text


class TestSensitiveIdentifiersStillScrubbed:
    """Regression fence: the DATE_TIME removal must not weaken any
    other recogniser. If any of these fail, the entity list change in
    ``pii.py`` went further than intended.
    """

    def test_person_name_scrubbed(self, scrubber: PIIScrubber) -> None:
        text = "John Smith called about the trust distribution."
        result = scrubber.scrub(text)
        assert "John Smith" not in result.text
        assert "[PERSON" in result.text

    def test_email_scrubbed(self, scrubber: PIIScrubber) -> None:
        text = "Send the BAS to client@example.com.au"
        result = scrubber.scrub(text)
        assert "client@example.com.au" not in result.text
        assert "[EMAIL_ADDRESS" in result.text

    def test_phone_scrubbed(self, scrubber: PIIScrubber) -> None:
        text = "Client phone: 03 9555 1234"
        result = scrubber.scrub(text)
        assert "03 9555 1234" not in result.text
        assert "[PHONE_NUMBER" in result.text

    def test_credit_card_scrubbed(self, scrubber: PIIScrubber) -> None:
        # Standard Luhn-valid test number; presidio's CreditCardRecognizer
        # validates the checksum, so non-Luhn fixtures are silently ignored.
        text = "Card on file: 4111 1111 1111 1111."
        result = scrubber.scrub(text)
        assert "4111 1111 1111 1111" not in result.text
        assert "[CREDIT_CARD" in result.text

    def test_tfn_scrubbed(self, scrubber: PIIScrubber) -> None:
        text = "TFN 123 456 789 on file."
        result = scrubber.scrub(text)
        assert "123 456 789" not in result.text
        assert "123456789" not in result.text


class TestScrubRestoreRoundTrip:
    """Mixed-content round trip: preserved spans must remain literal,
    placeholder spans must restore byte-for-byte. This guards the
    streaming path's restore step in ``anthropic_client._restore_*``.
    """

    def test_round_trip_preserves_statutory_content_and_restores_pii(
        self, scrubber: PIIScrubber
    ) -> None:
        original = (
            "Per s 62 of the SIS Act 1993, the trustee John Smith must "
            "observe the sole purpose test by 30 June 2024."
        )
        result = scrubber.scrub(original)

        # PII span replaced.
        assert "John Smith" not in result.text
        assert "[PERSON" in result.text

        # Statutory + date spans untouched.
        assert "s 62" in result.text
        assert "SIS Act" in result.text
        assert "1993" in result.text
        assert "30 June 2024" in result.text
        assert "[DATE_TIME" not in result.text

        # Restoration brings the original back exactly.
        assert result.restore(result.text) == original

    def test_round_trip_with_no_pii_is_identity(
        self, scrubber: PIIScrubber
    ) -> None:
        original = (
            "Division 7A and s 109D ITAA 1936 interact with s 62 of the "
            "SIS Act in an SMSF context. See Subdivision 152-A."
        )
        result = scrubber.scrub(original)
        assert result.text == original
        assert result.mapping == {}
        assert result.restore(result.text) == original
