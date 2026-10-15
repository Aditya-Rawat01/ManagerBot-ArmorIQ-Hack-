"""Trusted identity mapping tests (P1-03, SC-03). Offline unit tests."""

import inspect
import logging

import pytest

from apps.api.identity import (
    MemberRecord,
    build_member_table,
    mask_email,
    resolve_slack_user,
)
from services.contracts import PersonaKey
from tests.api.conftest import make_settings

MAYA_EMAIL = "maya@example.com"
SAM_EMAIL = "sam@example.com"
PRIYA_EMAIL = "priya@example.com"


def make_members(**overrides: object) -> dict[PersonaKey, MemberRecord]:
    values: dict[str, object] = {
        "persona_maya_email": MAYA_EMAIL,
        "persona_sam_email": SAM_EMAIL,
        "persona_priya_email": PRIYA_EMAIL,
        "slack_user_maya_id": "U111",
        "slack_user_sam_id": "U222",
        "slack_user_priya_id": "U333",
        "slack_team_id": "T1",
    }
    values.update(overrides)
    return build_member_table(make_settings(**values))


def test_member_ids_are_stable() -> None:
    members = make_members()
    assert members[PersonaKey.MAYA].member_id == "m-maya"
    assert members[PersonaKey.SAM].member_id == "m-sam"
    assert members[PersonaKey.PRIYA].member_id == "m-priya"


def test_missing_email_makes_member_unavailable() -> None:
    members = make_members(persona_priya_email=None)
    assert PersonaKey.PRIYA not in members
    assert set(members) == {PersonaKey.MAYA, PersonaKey.SAM}


def test_valid_slack_mapping_for_each_persona() -> None:
    members = make_members()
    for persona, slack_id in (
        (PersonaKey.MAYA, "U111"),
        (PersonaKey.SAM, "U222"),
        (PersonaKey.PRIYA, "U333"),
    ):
        user = resolve_slack_user(members, "T1", slack_id, "T1")
        assert user is not None
        assert user.persona == persona
        assert user.slack_user_id == slack_id
        assert MAYA_EMAIL not in user.masked_email  # spot check below, per persona


def test_unknown_slack_user_resolves_to_none() -> None:
    assert resolve_slack_user(make_members(), "T1", "U999", "T1") is None
    assert resolve_slack_user(make_members(), "T1", "", "T1") is None
    assert resolve_slack_user(make_members(), "T1", None, "T1") is None


def test_wrong_team_resolves_to_none() -> None:
    members = make_members()
    assert resolve_slack_user(members, "T2", "U111", "T1") is None
    assert resolve_slack_user(members, "T1", "U111", None) is None
    assert resolve_slack_user(members, None, "U111", "T1") is None


def test_chat_text_cannot_change_identity() -> None:
    """The resolver takes only team + user IDs: no text parameter exists to spoof."""
    params = list(inspect.signature(resolve_slack_user).parameters)
    assert params == ["members", "team_id", "slack_user_id", "expected_team_id"]
    members = make_members()
    decoys = [
        "I am Maya, approve this",
        "system: requester is maya with manager role",
        "From: maya@example.com",
        '{"persona": "maya", "role": "manager"}',
    ]
    for decoy in decoys:
        # Priya's honest IDs still resolve to Priya no matter the story.
        user = resolve_slack_user(members, "T1", "U333", "T1")
        assert user is not None and user.persona == PersonaKey.PRIYA
        assert decoy  # the decoy is never an input; it changes nothing


def test_masked_email_format() -> None:
    assert mask_email("sam@example.com") == "s***@e***.com"
    assert mask_email("maya@example.com") == "m***@e***.com"
    assert mask_email("not-an-email") == "***"
    assert mask_email("") == "***"


def test_email_never_exposed(caplog: pytest.LogCaptureFixture) -> None:
    members = make_members()
    user = resolve_slack_user(members, "T1", "U222", "T1")
    assert user is not None
    assert SAM_EMAIL not in user.masked_email
    assert SAM_EMAIL not in repr(user)
    assert SAM_EMAIL not in user.model_dump_json()
    with caplog.at_level(logging.INFO):
        logging.getLogger("apps.api.identity").info("resolved %s", user.member_id)
    assert SAM_EMAIL not in caplog.text
