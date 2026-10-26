"""``generation_records``: every record of a project-wide status poll (#723)."""

from __future__ import annotations

from typing import Any

from gflow_cli.api.transports.batchexecute import generation_record, generation_records

WF_A = "11111111-1111-4111-8111-111111111111"
WF_B = "44444444-4444-4444-8444-444444444444"
PROJ = "22222222-2222-4222-8222-222222222222"
MEDIA = "33333333-3333-4333-8333-333333333333"


def _record(workflow_id: str, status: int, prompt: Any) -> list[Any]:
    details: list[Any] = [[1, 2], prompt, None, None, None, None, None, None, [status], 1]
    return [workflow_id, PROJ, MEDIA, None, None, details, None, [[None]]]


def test_every_record_of_a_poll_is_returned_in_order_with_its_prompt() -> None:
    payload = [None, 881, [[_record(WF_A, 2, "a heron")], [_record(WF_B, 6, "a crane")]]]
    recs = generation_records(payload)
    assert [(r.workflow_id, r.status, r.prompt) for r in recs] == [
        (WF_A, 2, "a heron"),
        (WF_B, 6, "a crane"),
    ]


def test_a_non_text_prompt_slot_decodes_as_none() -> None:
    assert generation_record("jwpduf", _record(WF_A, 2, None)).prompt is None
    assert generation_records([None, 1, []]) == []
