"""Unit tests for pure helpers in coworker.workers.processor.

DB-free, no fixtures. Exercises _extract_azure_oid_from_resource against
the three Graph notification resource shapes observed in production plus
mixed casing and malformed input.
"""
import pytest

from coworker.workers.processor import _extract_azure_oid_from_resource

_OID_LOWER = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
_OID_UPPER = "AAAAAAAA-BBBB-CCCC-DDDD-EEEEEEEEEEEE"


@pytest.mark.parametrize(
    "resource",
    [
        # PascalCase rewritten message notification.
        f"Users/{_OID_LOWER}/Messages/AAQkADAwATM0MAItYjg4Ny0wMA",
        # Subscribed mail resource.
        f"users/{_OID_LOWER}/mailFolders('Inbox')/messages",
        # Reused calendar subscription.
        f"users/{_OID_LOWER}/events",
    ],
)
def test_extract_oid_observed_shapes_return_lowercase_oid(resource: str) -> None:
    assert _extract_azure_oid_from_resource(resource) == _OID_LOWER


def test_extract_oid_mixed_case_first_segment_returns_oid() -> None:
    assert (
        _extract_azure_oid_from_resource(f"USERS/{_OID_LOWER}/messages")
        == _OID_LOWER
    )


def test_extract_oid_canonicalises_uppercase_uuid_to_lowercase() -> None:
    assert (
        _extract_azure_oid_from_resource(f"Users/{_OID_UPPER}/Messages/x")
        == _OID_LOWER
    )


@pytest.mark.parametrize(
    "resource",
    [
        None,
        "",
        # Wrong first segment.
        f"groups/{_OID_LOWER}/members",
        # Leading slash pushes the literal "users" to index 1.
        f"/users/{_OID_LOWER}/messages",
        # Non-UUID at segment 1.
        "users/not-a-uuid/messages",
        # Empty segment 1.
        "users/",
        # Single segment, no oid present.
        "users",
    ],
)
def test_extract_oid_invalid_inputs_return_none(resource: str | None) -> None:
    assert _extract_azure_oid_from_resource(resource) is None
