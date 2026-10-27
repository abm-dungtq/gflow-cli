# Lost Video Submit Recovery Implementation Plan (#1112)

> **For agentic workers:** Run `/gflow:status --feature lost-video-submit-recovery` to
> find the next unchecked task. Implement one task at a time. Run `/gflow:check` before
> every commit.

**Goal:** When Chrome loses the body of a video submit reply on flow.google.com, an r2v or
i2v run that uploaded a file in this run still finishes with its clip on disk and exit 0.
It does not fail with exit 9, and nothing is submitted twice.

**Architecture:** All changes are in `migrated_composer.submit_and_observe`.

- A lost submit body no longer fails `submitted` immediately. It sets a `lost` flag.
- While `lost` holds and no workflow is known, the status branch adopts a record carrying
  an id the run **uploaded itself**. That record then resolves `submitted` exactly as a
  readable reply would. `on_started`, `_await_terminal` and the caller's download path are
  unchanged.
- If nothing is adopted before the poll deadline, the existing exit 9 is raised, now
  naming the project URL.
- A listing readback in a second tab is the fallback, used only when no status record was
  adopted (task 4).

**Narrowed from SCENARIO.md (decision, 2026-10-27).** The match key is an id uploaded in
this run: the `--ref` media ids, or the uploaded start frame. Such an id is unique per run
(18 of 18 in the spike) and did not exist before the submit, so no pre-submit snapshot is
needed. Rows 1 and 2 hold by construction.

- t2v and character-only r2v carry no such id. They keep today's exit 9, so row 3 is
  satisfied by refusal, not by a candidate list.
- The existing t2v e2e (`migrated_video_submit_body_loss.feature`) keeps asserting
  exactly that.
- Character entity ids, prompt and model are **not** match keys: they repeat across runs.
- Widening to t2v needs a snapshot taken before the submit. That is a separate issue,
  filed only if the loss rate on t2v warrants it.

**Predict verdict:** CAUTION, 6/10 (2026-10-27).

**Risk register:**
| Severity | Risk | Mitigation |
|---|---|---|
| Critical | Adopting another clip (an older one in the project, the uploaded image's own record) | Match only on a per-run uploaded id, in a video-shaped record; more than 1 match is refused (exit 9 with the ids) |
| Critical | Double billing | No code path clicks submit again; the e2e counts submit requests (exactly 1) |
| High | Polls stop or lose their bodies after the loss | Task 4 listing fallback; exit 9 if that also finds nothing |
| High | Request body (reCAPTCHA token) leaks into logs or errors | Match on ids gflow already holds; never parse or log the request body |
| High | #1111 edits the same status branch | Land #1111 first or rebase onto it before task 2 |
| Medium | The second tab steals focus | It opens only after submit, closes in `finally`; the generating page never navigates |

---

## File structure

### Modified files
```
src/gflow_cli/api/transports/batchexecute.py
  generation_records_carrying(rpcid, payload, ids) -> list[GenerationRecord]: video
  records whose request ([5][6][1]) carries any of `ids`
src/gflow_cli/api/transports/migrated_composer.py
  submit_and_observe: `adopt_ids` parameter, `lost` flag, adoption in the status branch,
  exit-9 detail names the project; run_video passes the ids it uploaded
src/gflow_cli/api/transports/migrated_recover.py (task 4 only)
  reuse its listing read for the fallback
tests/api/transports/test_batchexecute.py          (unit: the matcher)
tests/api/transports/test_migrated_composer.py      (unit/integration: the observer)
tests/features/migrated_video_submit_body_loss.feature   (+ r2v scenario, CLI + MCP)
tests/e2e/test_migrated_submit_body_loss_bdd.py      (bind the new scenarios)
KNOWN_ISSUES.md, docs/MCP.md, skills/gflow-cli/SKILL.md, CHANGELOG.md
```

No new module. No new error class or exit code. No CLI or MCP parameter changes. The MCP
twin reaches the same `run_video`, so task 6 is a verification, not a code change.

---

## Task 1: the matcher (test first)

**What:** A pure function that picks, out of one status payload, the video records whose
submitted request carries any of the given ids.

**Files:** `batchexecute.py`, `tests/api/transports/test_batchexecute.py`.
Fixtures are cut from the spike's own frames, redacted.

**Steps:**
- [x] Red tests, then `generation_records_carrying`. Reuse `_at`, `_as_list` and the
      record-shape walk that `find_record` already uses.

**Tests (red first):**
- [x] A payload holding an older done clip and our new clip returns only ours (row 1)
- [x] The uploaded image's own record (length 7, status null) is never returned (row 2)
- [x] A record carrying our id only in the prompt text, not in the request refs, is not
      returned
- [x] An empty `ids` argument returns `[]`

---

## Task 2: adopt in `submit_and_observe` (test first)

**What:** A lost submit body sets `lost`. The status branch adopts the single record that
carries an adopted id, and the run continues as though the reply had been read.

**Files:** `migrated_composer.py`, `tests/api/transports/test_migrated_composer.py`.

**Steps:**
- [x] Add a keyword parameter `adopt_ids: tuple[str, ...] = ()`. `run_video` passes
      `reference_ids` (uploaded refs only; never `entity_ids`) plus the uploaded start
      frame. Extend and other callers pass nothing, so their behaviour is unchanged
      (row 20).
- [x] On a lost submit body with `adopt_ids`, log `migrated.video_submit_reply_lost` and
      do not fail `submitted`. Without `adopt_ids`, keep today's immediate exit 9.
- [x] Status branch: when `lost` holds and no workflow is known, call the matcher.
  - One match: set `workflow["id"]`, `submitted.set_result(rec)`, `_settle(rec)`, and log
    `migrated.video_adopted_from_status` (rpc, media_id, matched_on).
  - More than one: `submitted.set_exception(_outcome_unknown(...))` naming the media ids,
    and log `migrated.video_recovery_ambiguous`.
- [x] The unresolved path (`not submitted.done()` at the budget) names the project URL in
      the detail.
- [x] Budget: once `lost` is set, the submit wait extends to the poll deadline. It is not
      `SUBMIT_REPLY_BUDGET_S`.

**Tests (red first):**
- [x] Lost body, then a status reply with an older clip and our clip: our clip is
      adopted, `on_started` fires once with its ids, and the result is ours (rows 1, 5, 11)
- [x] Lost body, then our record at status 4: the normal failed-generation result, not
      exit 9 (row 7)
- [x] Lost body, then two records carrying our id: exit 9, non-retryable, both media ids
      in the detail (row 3)
- [x] Lost body, no `adopt_ids` (t2v): exit 9 immediately, as today
- [x] Lost body, no status match before the deadline: exit 9 naming the project (row 10)
- [x] No second submit click in any of these (row 4): the click mock is called once
- [x] A lost **status** body is skipped and the next poll settles it (row 15)
- [x] Logged events carry no request body (row 12)

---

## Task 3: BDD, offline and live (red first)

**What:** Extend `tests/features/migrated_video_submit_body_loss.feature` with an r2v
scenario, run on the CLI and on the MCP tool. The file is already `@e2e @e2e_video` and
bound from `tests/e2e/test_migrated_submit_body_loss_bdd.py`, which already provokes the
loss with `Response.text` and counts submits.

**Steps:**
- [x] Scenario: "An r2v submit whose reply body is lost still delivers its clip". Then:
      exactly one video submit, the clip on disk, success. Run it through the CLI
      subprocess and through `mcp_tools.gflow_generate_video(wait=True)`.
- [x] Keep the t2v scenario as is: it now pins the narrowed scope.
- [x] `tests/features/test_e2e_binding_guard.py` stays green.

---

## Task 4: listing fallback

**What:** When `lost` holds and the poll deadline passes with nothing adopted, read
`Zzl0ze` once from a second tab opened after the submit. Apply the same matcher. With one
done match, download it through the caller's path. Otherwise raise exit 9.

**Steps:**
- [x] Reuse `migrated_extend.find_record` / `LISTING_RPC` and the second-tab read pattern
      from `migrated_recover`. Close the tab in `finally`; the generating page never
      navigates (row 19).
- [x] Log `migrated.video_adopted_from_listing`.

**Tests:**
- [x] Integration (mocked pages): status bodies are also lost, the listing has our record,
      and the run is adopted (row 8)
- [x] The second tab is created only after the submit click (row 9: order assertion)

---

## Task 5: console line

- [x] On `migrated.video_submit_reply_lost`, print one console line: the submit reply was
      lost, and gflow is following the clip in the project (row 18). It must not use Rich
      markup that would eat brackets.

---

## Task 6: MCP twin (verify, no new surface)

- [x] Check the six mirror axes from `skills/check/SKILL.md` step 1b. No parameter, payload
      key or leaf changes, so expect none.
- [x] Queued path: the worker row gets the adopted workflow id through `on_started` (row
      14). Integration test against `worker/codec.py` round trip, or name the existing one
      that covers it.
- [x] Update the `gflow_generate_video` docstring if it promises "exit 9 on a lost reply".

---

## Task 7: docs

- [x] `KNOWN_ISSUES.md:853`: add video submits and #1112 to *Affects*; say that r2v and i2v
      with an uploaded file recover, and t2v does not yet.
- [x] `docs/MCP.md:195-198` and `skills/gflow-cli/SKILL.md:278`: the same split. Then
      `generate_plugin_skills.py` to refresh the plugin copy.
- [x] `CHANGELOG.md` `[Unreleased]` → Fixed.

---

## As built (2026-10-27)

- Task 4's fallback runs after the 60 s submit-reply budget, not at the poll deadline, so a
  listing match still leaves the generating page time to report the clip done.
- Task 5 needed no new console code: the lost-reply event is a WARNING, which the default
  log level already prints as one line; it gained a readable `note`.
- Task 6: no parameter, payload key or leaf changed, so the six axes reduce to docs. The
  queued path gets the adopted ids through the worker's own `on_started`
  (`worker/daemon.py`), which the observer fires on adoption exactly as on a read reply.
- Row 4 (one submit) is pinned by the e2e's reply count; offline, the observer has one
  click call and no retry loop.

## Task 8: gates and the paid proof

- [ ] `/gflow:check` green.
- [ ] Paid e2e, with approval: about 24 credits (task 3: CLI + MCP, r2v). Run
      `--collect-only -q` first and count the ids against the budget.
- [ ] Record in the PR whether the adopted run reached status 3 through the status polls
      or through the fallback. That is the spike's open question.

---

## Definition of done

- [ ] All task steps checked off
- [ ] `/gflow:check` green
- [ ] `CHANGELOG.md` `[Unreleased]` updated
- [ ] Docs updated (task 7)
- [ ] BDD covers SCENARIO.md rows 1–14 as narrowed above
- [ ] Paid e2e passed on CLI **and** MCP, output pasted with ids redacted
- [ ] No `# TODO` without an issue link
