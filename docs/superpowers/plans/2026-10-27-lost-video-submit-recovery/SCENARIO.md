# Scenario: recover a video clip whose `MZZa6b` submit reply was lost (#1112)

**Proposal.** When the video submit reply body is unreadable on flow.google.com, do not
raise at once. Keep the generating page listening and adopt the status record that is
**new since the pre-submit snapshot** and **carries our submitted ids**. That means the
uploaded `--ref` media ids plus character entity ids for r2v, the uploaded start-frame id
for i2v, and the model and prompt for t2v. Then finish through the normal terminal wait
and the caller's `save`. If no record is adopted in time, read the project listing
(`Zzl0ze`) from a second tab opened after submit. With 0 or more than 1 candidate, keep
today's non-retryable exit 9 and list the candidates. Never resubmit.

**Evidence:** [spike 2026-10-27](../../spikes/2026-10-27-lost-video-submit-recovery.md).
**Predict:** CAUTION 6/10. Its mitigations are rows 1–4, 9 and 12 below.

## Coverage map

| Dimension | Active | Why |
|---|---|---|
| D2 WAF/reCAPTCHA | yes | the request body holds the reCAPTCHA token; the fallback adds one page load |
| D5 Concurrency / page | yes | second tab, focus, listener lifetime, pooled page state |
| D6 Data layer | yes | `on_started` never fires today on a lost reply; the recorder row has to come out right |
| D7 Errors / exit codes | yes | exit 9 stays for the unrecovered case; billing must not double |
| D9 Transport | yes | the core: status records that are not ours, timing, both rpcs |
| D12 Observability | yes | new events; candidates must survive redaction |
| D13 MCP | yes | direct and queued paths; `on_started` feeds the worker row |
| D11 Input | partly | t2v and r2v with characters only have no uploaded id |
| D1, D3, D4, D8, D10 | no | no auth change, no new selector, no batch ledger, no path change, no headed/headless split |

## Scenario table

| # | Dim | Scenario | Severity | Expected behaviour | Test category |
|---|---|---|---|---|---|
| 1 | D9 | A status poll reports **another** clip of the project (already done, in the baseline) right after the lost reply (spike run A: `as29s` for an older clip) | Critical | not adopted: it is in the pre-submit snapshot and lacks our ids | Unit + BDD |
| 2 | D9 | A status poll reports the **uploaded reference image's own record** (status null, workflow id = upload id) | Critical | not adopted: it is not a video generation and carries no request | Unit |
| 3 | D9/D11 | Ref-less run (t2v, or r2v with characters only) while a second clip with the same prompt and cast is generating in the same project (another tab, another machine) | Critical | 2 candidates → exit 9, non-retryable, both media ids and the project URL in the detail; never download either | Unit + BDD |
| 4 | D7 | Any recovery path | Critical | no second submit click and no second `MZZa6b` request: asserted by counting submit requests | Unit + E2E |
| 5 | D9 | r2v with `--ref`: the lost reply is followed by `jwpduf` naming a new record that carries our uploaded ref id | High | adopted; normal wait to status 3; clip saved through the caller's `save`; exit 0 | BDD + **E2E video** |
| 6 | D9 | i2v: the record carries our uploaded start-frame id (`expect_media_id`) | High | adopted like row 5 | Unit (fixture) |
| 7 | D9 | Adopted record later reports status 4 (failed, e.g. `[13, "Generation job finished with state: FAILED"]`) | High | the normal failed-generation path, not exit 9; no recovery download | Unit |
| 8 | D9 | No matching status record arrives before the fallback deadline (polls stop, or their bodies are lost too) | High | second tab opens **after** submit, reads `Zzl0ze`, applies the same matching rule against the pre-submit snapshot; one done match → download | Integration + E2E (ablated) |
| 9 | D5 | The fallback tab is opened during upload or chip binding | High | impossible by construction: the tab opens only after the submit click; it is closed in `finally` | Unit (order assertion) |
| 10 | D9 | The whole poll deadline passes with no candidate on either side | High | exit 9 as today, now naming the project URL; hint says the clip may still appear in the project | Unit |
| 11 | D6 | Adoption happens | High | `on_started` fires once with the adopted ids before the terminal wait, so the recorder writes STARTED then SUCCEEDED (or FAILED keyed by media id) | Integration |
| 12 | D2 | Recovery code logs or raises with request data | High | no request body is parsed or logged; matching uses ids gflow already holds (`expect_*`, entity ids, model, prompt) | Unit (grep-style assertion on events) |
| 13 | D13 | MCP direct `gflow_generate_video` hits a lost reply | High | `status: completed` with the file; on an unrecovered run, `retryable: false` and the candidates in the message | **E2E video (MCP)** |
| 14 | D13 | MCP queued path (worker daemon) hits a lost reply | High | worker row gets the adopted workflow id via `on_started`; unrecovered detail keeps ids after `redact_error_detail` | Integration |
| 15 | D9 | The lost body is the **status** reply, not the submit (another DevTools loss) | Medium | skipped like today; the next 5 s poll settles it | Unit |
| 16 | D9 | Adopted record reaches status 3 with no signed URL inside the grace (the exit-7 path) | Medium | out of scope here: same as today; tracked separately (`recover_clip` by known media id) | — (deferred) |
| 17 | D12 | Events | Medium | `migrated.video_submit_reply_lost`, `migrated.video_adopted_from_status` (rpc, media_id, matched_on), `migrated.video_adopted_from_listing`, `migrated.video_recovery_ambiguous` (candidates) | Unit |
| 18 | D7 | Console during the wait | Medium | one line: submit reply lost, following the clip in the project | Unit (CLI) |
| 19 | D5 | Page returned to the pool after a listing fallback | Medium | the generating page never navigates; only the second tab does | Integration |
| 20 | D9 | Extend (`fZytfe`) and image submits share `submit_and_observe` / the lost-body branch | Low | unchanged: the change is gated on the video submit rpcs; images already recover (#1075) | Unit |

## Must-cover before merge (Critical + High)

Rows 1–14. The two e2e rows (5 on the CLI, 13 on MCP) run with the submit-reply body
**ablated in-process** (`Response.text` raises for `MZZa6b`, as the spike probe does): one
paid r2v clip each, about 24 credits. The fallback (row 8) is proven by its integration
test plus one ablated run that also suppresses status bodies, if credit is approved.

## Deferred (Medium + Low)

- Row 16: exit 7 "done, no signed URL" → `recover_clip` by its known media id. This is a
  separate small change; file it as its own issue if none exists.
- Rows 15, 17–20 land with the change. None of them blocks it.

## Suggested BDD scenarios

Offline (bound from `tests/features/`, fixtures cut from the spike's own frames):

```gherkin
Feature: A video whose submit reply was lost is followed to its clip (#1112)

  Scenario: The record carrying our uploaded reference is adopted
    Given an r2v submit whose reply body was unreadable
    And a status reply naming an older clip and a new clip carrying our reference id
    When the observer settles
    Then it adopts the new clip, fires on_started once, and never submits again

  Scenario: Two new clips that both match a ref-less run are refused
    Given a t2v submit whose reply body was unreadable
    And two new records with our prompt and model
    When the observer gives up
    Then it raises a non-retryable outcome-unknown naming both media ids and the project
```

Live (bound from `tests/e2e/test_migrated_lost_submit_recovery_bdd.py`):

```gherkin
@e2e @e2e_video
Feature: A lost video submit reply still delivers the clip on flow.google.com (#1112)

  Scenario Outline: <surface> recovers an r2v clip after its submit reply is lost
    Given a project with a saved character and the submit reply body ablated
    When <surface> generates an r2v clip with a local reference
    Then the clip is on disk, exactly one submit request was sent, and the run succeeded

    Examples:
      | surface |
      | the CLI |
      | the MCP video tool |
```

## Known-issues cross-reference

- `KNOWN_ISSUES.md:853` (#1009 / #1075, "reply arrives, body unreadable"): today it covers
  uploads, project create/rename and image submits, which recover by readback. This change
  extends it to video submits. Update the entry's *Affects* line and add #1112.
- #1111 (bare status 4) edits the same status branch. Land it first or rebase on it.
- #1113 (character matrix e2e) is the harness that measures the natural loss rate before
  and after.
