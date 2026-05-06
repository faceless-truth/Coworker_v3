"""Unit tests for the Loguru secret-redaction patcher."""
from coworker.loguru_patcher import redact_secrets


def test_refresh_token_in_extras_is_redacted() -> None:
    record = {"extra": {"refresh_token": "abc123def"}}
    redact_secrets(record)
    assert record["extra"]["refresh_token"] == "[REDACTED]"


def test_record_with_no_sensitive_fields_is_untouched() -> None:
    record = {"extra": {"user_id": "user-1", "action": "login", "duration_ms": 42}}
    redact_secrets(record)
    assert record["extra"] == {
        "user_id": "user-1",
        "action": "login",
        "duration_ms": 42,
    }


def test_nested_dict_gets_recursive_redaction() -> None:
    record = {
        "extra": {
            "request_body": {
                "client_id": "abc-not-sensitive",
                "client_secret": "shh-this-is-secret",
                "code": "auth-code-xyz",
                "code_verifier": "pkce-verifier",
                "scope": "User.Read",
            }
        }
    }
    redact_secrets(record)
    body = record["extra"]["request_body"]
    assert body["client_id"] == "abc-not-sensitive"
    assert body["client_secret"] == "[REDACTED]"
    assert body["code"] == "[REDACTED]"
    assert body["code_verifier"] == "[REDACTED]"
    assert body["scope"] == "User.Read"


def test_case_insensitive_matching() -> None:
    record = {
        "extra": {
            "Refresh_Token": "v1",
            "REFRESH_TOKEN": "v2",
            "refresh_token": "v3",
            "rEfReSh_ToKeN": "v4",
        }
    }
    redact_secrets(record)
    for key in ("Refresh_Token", "REFRESH_TOKEN", "refresh_token", "rEfReSh_ToKeN"):
        assert record["extra"][key] == "[REDACTED]", (
            f"{key!r} was not redacted"
        )


def test_list_of_dicts_gets_recursive_redaction() -> None:
    record = {
        "extra": {
            "items": [
                {"name": "A", "password": "p1"},
                {"name": "B", "secret": "s1"},
                {"name": "C", "harmless": "ok"},
            ]
        }
    }
    redact_secrets(record)
    items = record["extra"]["items"]
    assert items[0] == {"name": "A", "password": "[REDACTED]"}
    assert items[1] == {"name": "B", "secret": "[REDACTED]"}
    assert items[2] == {"name": "C", "harmless": "ok"}


def test_deeply_nested_structure() -> None:
    record = {
        "extra": {
            "outer": {
                "level1": {
                    "level2": [
                        {"access_token": "leak"},
                        {"benign": "fine"},
                    ]
                }
            }
        }
    }
    redact_secrets(record)
    deep = record["extra"]["outer"]["level1"]["level2"]
    assert deep[0]["access_token"] == "[REDACTED]"
    assert deep[1]["benign"] == "fine"


def test_substring_match_catches_prefixed_variants() -> None:
    """ms_refresh_token, azure_client_secret etc. should be redacted."""
    record = {
        "extra": {
            "ms_refresh_token": "leak",
            "azure_client_secret": "leak",
            "user_password": "leak",
        }
    }
    redact_secrets(record)
    assert record["extra"]["ms_refresh_token"] == "[REDACTED]"
    assert record["extra"]["azure_client_secret"] == "[REDACTED]"
    assert record["extra"]["user_password"] == "[REDACTED]"


def test_record_without_extra_does_not_crash() -> None:
    record: dict = {"message": "no extras here"}
    redact_secrets(record)  # should be a no-op
    assert record == {"message": "no extras here"}
