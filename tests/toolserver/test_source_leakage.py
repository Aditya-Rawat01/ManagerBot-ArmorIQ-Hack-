"""Source-leakage tests (P2-08, R-55). Missing, wrong-tier, and inactive
lookups share one byte-identical `not_found` response body (the `_meta`
ledger id differs per call by design: auditability, not existence signal).
Live tests need a LOCAL DATABASE_URL_TOOLSERVER plus PROXYLEAD_DB_WRITE_TESTS=1.
"""

import json

from fastapi.testclient import TestClient

from services.contracts import DataTier

from .test_elevated_tier_tools import GET_SOURCE_TOOLS, member_ctx
from .test_tool_dispatch import (
    NEEDS_DB,
    call,
    dev_client,
    result_text,
)

TEAM_GET = GET_SOURCE_TOOLS[DataTier.TEAM]
CONF_ID = "conf-runway-review"
TEAM_ID = "team-friday-deploy-freeze"
OLD_ID = "team-release-checklist-v1"  # status superseded


def not_found_body(client: TestClient, source_ref: str) -> str:
    message = call(
        client,
        {"source_ref": source_ref, "_ctx": member_ctx("maya", TEAM_GET)},
        tool=TEAM_GET,
    )
    assert message["result"]["isError"] is True
    assert result_text(message)["code"] == "not_found"
    return message["result"]["content"][0]["text"]


@NEEDS_DB
def test_missing_wrong_tier_and_inactive_share_one_response() -> None:
    from .test_tool_dispatch import live_client as live

    with live() as client:
        missing = not_found_body(client, "no-such-source")
        wrong_tier = not_found_body(client, CONF_ID)
        inactive = not_found_body(client, OLD_ID)
    assert missing == wrong_tier == inactive
    assert json.loads(missing) == {
        "code": "not_found",
        "message": "No matching source found.",
    }


@NEEDS_DB
def test_no_restricted_metadata_in_errors() -> None:
    from .test_tool_dispatch import live_client as live

    with live() as client:
        bodies = [
            not_found_body(client, "restr-workforce-planning"),
            not_found_body(client, CONF_ID),
        ]
        denied = call(
            client,
            {
                "query": "workforce",
                "limit": 3,
                "_ctx": member_ctx("priya", "knowledge_search_restricted"),
            },
            tool="knowledge_search_restricted",
        )
        bodies.append(json.dumps(denied))
    blob = " ".join(bodies).lower()
    for secret in (
        "workforce planning scenarios",
        "meridian",
        "board eyes only",
        "northstar",
        "grow, hold",
    ):
        assert secret not in blob, secret


def test_unknown_view_rejected_without_database() -> None:
    with dev_client() as client:
        for view in ("approved_company_data_confidential", "anything_else", ""):
            message = call(
                client,
                {"view": view, "limit": 5, "_ctx": member_ctx("maya", "read_source_team")},
                tool="read_source_team",
            )
            assert result_text(message)["code"] == "invalid_arguments"
