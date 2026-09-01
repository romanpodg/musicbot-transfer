# Transfer Lifecycle

One transfer is a sequence of **phases**, each persisted before the next begins.
The whole point of the design is that any phase can be interrupted and the work
already confirmed is never repeated.

```
export → normalize → match → plan → [confirm] → execute → verify → report
```

---

## 1. Job state machine

`core/transfer/lifecycle.py` holds the authoritative transition table. Every
status change goes through `transition()`, which rejects anything not declared.

```
CREATED ──▶ AUTHENTICATING ──▶ EXPORTING ──▶ NORMALIZING ──▶ MATCHING
                                                                │
                                                                ▼
                                   WAITING_CONFIRMATION ◀── PLANNING
                                                │
                                                ▼
                                          IMPORTING ◀────── PAUSED
                                           │     │    └──────┘
                               (dry run)   │     ▼
                                   ────────┼──▶ VERIFYING ──▶ COMPLETED
                                           │                     ▲
                                           └─────────────────────┘
                                                 (dry run only)

CANCELLED ◀── reachable from every non-terminal state
FAILED    ◀── reachable from every non-terminal state
```

| Rule | Why |
|---|---|
| `COMPLETED`, `FAILED`, `CANCELLED` have no outgoing edges | Terminal; a finished job is retired, not resumed. |
| `PAUSED → IMPORTING` only | Pause is exclusively a pause of *writing*. |
| `WAITING_CONFIRMATION → PLANNING` allowed | Re-planning is read-only and cheap; replaying writes is not. |
| `IMPORTING → COMPLETED` allowed for dry runs | Dry runs execute no destination writes and skip verification. |
| `resume_target()` returns `None` for terminal jobs | Makes "resume a finished job" impossible by construction. |

`TransferService.resume()` enforces that only jobs with `resume_target() == IMPORTING`
can be resumed. Resuming any terminal job raises `InvalidStateTransition`.
Idempotent terminal operations:
- `cancel(CANCELLED)` is an idempotent no-op; cancelling `COMPLETED` or `FAILED` raises `InvalidStateTransition`.
- `fail(FAILED)` is an idempotent no-op; failing `COMPLETED` or `CANCELLED` raises `InvalidStateTransition`.

---

## 2. Item state machine

```
PENDING ──▶ MATCHED ──▶ TRANSFERRED
                │
                ├──▶ ALREADY_EXISTS   (terminal - nothing to do)
                ├──▶ SKIPPED          (terminal - deliberate)
                ├──▶ NOT_FOUND        (retryable, likely permanent)
                ├──▶ UNAVAILABLE      (retryable, region/rights)
                ├──▶ AMBIGUOUS        (do NOT retry blindly - see §6)
                └──▶ FAILED           (retryable)
```

Terminal: `TRANSFERRED`, `ALREADY_EXISTS`, `SKIPPED` — never replayed.
Retryable: `PENDING`, `MATCHED`, `FAILED`, `NOT_FOUND`, `AMBIGUOUS`,
`UNAVAILABLE`.

`RecoveryService.select_for_retry()` **raises `ValueError`** if a caller asks
for a terminal status. That is a caller bug, and silently downgrading it would
risk duplicating confirmed work.

---

## 3. Export

`adapter.export_library()` produces a `LibrarySnapshot`. The adapter reports
partial coverage instead of hiding it:

```python
if snapshot.is_partial:
    logger.warning("event=plan_source_partial job_id=%s sections=%s", ...)
```

A partial export produces a plan that says so. It never produces a plan that
looks complete.

---

## 4. Matching

Three tiers, cheapest and most reliable first:

| Tier | Method | Notes |
|---|---|---|
| 1 | `ISRC` | Authoritative. Skipped when `capabilities.exposes_isrc` is `False`. |
| 2 | `DIRECT_ID` | Used only when `adapter.can_reuse_identifier()` says a source id is portable. |
| 3 | Metadata | Exact → normalized → fuzzy, with weighted scoring. |

Normalization (`core/matching/normalization.py`) produces a **comparison key**;
it never mutates the original track. Rules:

- casefold, strip diacritics (`Björk` ≡ `Bjork`),
- collapse whitespace and punctuation,
- split featured artists (`feat.`, `ft.`, `featuring`) into a separate bucket,
- detect version markers (`remaster`, `live`, `acoustic`, `radio edit`, …),
- detect the explicit flag.

Confidence penalties (they lower the score but never force a match):

| Signal | Effect |
|---|---|
| Explicit source vs clean candidate | penalty |
| Version difference (remaster vs original, live vs studio) | penalty |
| Duration difference beyond tolerance | penalty |
| Two candidates within a near-tie margin | downgraded to `AMBIGUOUS` |

An `AMBIGUOUS` match is never auto-accepted. It becomes
`ItemStatus.NOT_FOUND` at plan time, and the user is told.

---

## 5. Planning

`TransferPlanner.build()` is **read-only**. It receives the destination wrapped
in `ReadOnlyAdapter`, so a mutating call raises before it reaches the network.

Outputs:

- a `TransferPlan` — frozen, with a `TransferPlanSummary` designed for a
  pre-execution confirmation screen;
- a list of `TransferItem`s, persisted immediately.

Warnings the plan can carry:

- `destination_deduplicates_playlists:<id>` — duplicates will not survive.
- `playlist_item_unresolved:<id>:<position>` — an entry could not be mapped.
- `destination_state_incomplete` — reconciliation will be best-effort.

**Nothing in this phase writes.** A unit test asserts the planner makes zero
write calls against a recording adapter.

---

## 5.1 Plan Identity, Deterministic Hashing, and Exact Confirmation (Phase 1.3B)

Destination mutations must be authorized by one exact persisted `TransferPlan` identified by:
* `plan_id`: Unique identifier of an immutable plan snapshot.
* `revision`: Monotonically increasing revision number within a job (1, 2, ...).
* `plan_hash`: Deterministic SHA-256 integrity digest of canonical plan intent.

```text
ANALYZE
  ↓
PLAN revision N
  ↓
persist immutable plan
  ↓
WAITING_CONFIRMATION
  ↓
confirm exact (plan_id + revision + plan_hash)
  ↓
preflight integrity validation
  ↓
destination-precondition validation
  ↓
IMPORTING
```

### Invariants:
1. **Boolean confirmation alone never authorizes writes**: Calling `execute(..., confirmed=True)` without matching durable confirmation stored on the job fails closed (`ConfirmationRequired`), issuing zero destination writes.
2. **Deterministic Plan Hash**: Computed using SHA-256 over a sorted canonical JSON representation of plan intent (entity types, source IDs, destination IDs, operations, positions, match metadata, context settings). Excludes runtime fields (`status`, `attempt_count`, `mutation_state`, error details, runtime timestamps).
   > **Note on Cryptographic Semantics**: The plan SHA-256 hash protects **integrity** against accidental drift, tampering, or stale execution — it is an integrity digest, not a cryptographic authentication signature.
3. **Re-planning invalidates previous confirmation**:
   ```text
   re-plan
     → revision N+1
     → previous confirmation for revision N is invalidated
     → zero destination writes until revision N+1 is explicitly confirmed
   ```
4. **Re-planning lifecycle**: Allowed from `WAITING_CONFIRMATION` while no writes have started. Re-planning after writes have entered execution is blocked.

---

## 5.2 Execution Preflight Checks

Before the **first destination mutation**, the service performs strict preflight validation:
1. **Active Plan Exists**: The job's `active_plan_id` must be present in durable storage.
2. **Identity Agrees**: The persisted plan must match `active_plan_id`, `active_plan_revision`, and `active_plan_hash`.
3. **Integrity Validation**: Recomputes the plan hash from canonical payload and asserts equality with stored `plan_hash`.
4. **Exact Confirmation Exists**: `confirmed_plan_id`, `confirmed_plan_revision`, and `confirmed_plan_hash` must match the active plan.
5. **Execution Item Intent Match**: Pre-execution `TransferItem` records must match approved plan intent (`destination_id`, `operation`, `write_position`, container IDs).
6. **Destination Drift Preconditions**: Verifies captured trustworthy preconditions against destination state (e.g. absent items remain absent, already existing items remain present). If drift is detected, the plan is marked stale (`PlanStaleError`), confirmation is cleared, and zero writes occur.
7. **Preflight Failure Policy**: Temporary destination read errors fail closed (`PlanValidationUnavailableError`), while `AuthenticationError` / `AuthorizationError` transition the job to `FAILED` with `verification_status = NOT_RUN`.

---

## 6. Execution

`TransferExecutor.execute()` walks items in a computed write order and
checkpoints after every one.

### Write order

```python
def _write_order(job, items):
    playlists = [i for i in items if i.entity_type is EntityType.PLAYLIST]
    others    = [i for i in items
                 if i.entity_type not in (EntityType.PLAYLIST, EntityType.PLAYLIST_ITEM)]
    ...playlist, then its entries in original_position order...
```

`PLAYLIST_ITEM` must be excluded from the leading "everything else" bucket, or
entries are written before their container exists.

### Per-item guarantees

1. Terminal items are skipped — this is what makes resume safe.
2. `attempt_count` is bounded by `settings.max_item_attempts`.
3. A `MusicTransferError` is classified into a specific item status.
4. **Any other exception is also classified and recorded** — one SDK bug must
   not abort a 5 000-track library. It is logged with `exc_info` and the item
   becomes retryable.
5. `AuthenticationError` / `AuthorizationError` abort the run: every remaining
   item would fail for the same reason.

### Idempotency (Invariant F)

`_reconcile()` re-reads a playlist before appending:

- if the destination already has the planned prefix, the item is marked
  `TRANSFERRED` without writing again;
- if the destination content does **not** match the expected prefix, the item
  is marked `AMBIGUOUS` and no append happens — blindly appending would corrupt
  the ordering.

### Cancellation

Cooperative. The executor checks `CancellationToken` and
`job.cancellation_requested` **between** items, finishes the current one,
persists it, and stops. Nothing already written is rolled back automatically.

### Dry run

`settings.dry_run` marks every item `SKIPPED` and reports what *would* happen.
No adapter write is issued.

---

## 7. Verification and VerificationStatus

Verification is separate from the API acknowledgement (Invariant G): a platform
can return success and still not hold the item.

Job lifecycle status (`JobStatus`) and post-write verification outcome (`VerificationStatus`)
are decoupled concepts:
* `JobStatus` tracks overall workflow progression (`IMPORTING -> VERIFYING -> COMPLETED`).
* `VerificationStatus` tracks observed destination state correctness (`NOT_RUN`, `PASSED`, `FAILED`, `PARTIAL`).

### Status Combinations

* `COMPLETED + PASSED`: All destination writes finished, and verification confirmed exact membership and order.
* `COMPLETED + FAILED`: Destination writes finished, but verification observed discrepancies (missing items, unexpected items, or playlist order mismatches). **This does NOT mean execution was rolled back.** It means writes landed or were attempted, but observed destination state does not match the plan.
* `COMPLETED + PARTIAL`: Destination writes finished, but verification could only be partially completed (e.g. read capability was unsupported for a section, or destination state was incomplete).
* `COMPLETED + NOT_RUN`: Execution completed under a dry run where no destination mutations occurred, so destination verification was intentionally skipped.
* `FAILED + NOT_RUN`: Execution or verification aborted fatally (e.g. `AuthenticationError` / `AuthorizationError`). Credential loss during verification halts immediately and transitions `VERIFYING -> FAILED`, never leaving a job stuck in `VERIFYING`.

### Section Verification Rules

- **Set-like sections** (liked tracks, albums, artists, videos, mixes): subset membership (`expected ⊆ actual`). Verification proves that every job-expected destination ID is present in the destination library section. Unrelated pre-existing items in the destination account are outside job scope and do not populate `unexpected` or fail verification. `actual_count` counts observed expected IDs.
- **Playlists**: exact container comparison (membership, duplicate counts, **and** exact order). `SequenceComparison` reports `missing`, `unexpected`, and `order_mismatches` separately. Duplicates are compared as multisets (`A, B, A`).
- Order mismatches in playlists are capped at 50 so a large library cannot flood a report. If the destination cannot be read back, the result is `success=False` with a `verification_unsupported:` warning — never a silent pass.

---

## 8. Resume and recovery

> **Resume is item-level.** There is no `last_position` cursor.

After a crash the destination may hold a different number of objects than a
local counter suggests, because **a write can land and its acknowledgement can
be lost**. Per-item status is the only trustworthy record.

```python
service.resume(job, destination, confirmed=True)
```

1. Refuse a finished job (`job_already_finished`).
2. Read destination state; resolve any `AMBIGUOUS` item whose identifier is now
   present → `TRANSFERRED`. Best-effort: an unreadable destination logs a
   warning and continues.
3. Return the job to `IMPORTING` and run the executor, which skips everything
   terminal.

The canonical regression:

```python
# 5 tracks, the process is killed after 3 are confirmed
with assertRaises(KeyboardInterrupt):
    service.execute(job, destination, confirmed=True)

reloaded = service.jobs.get(job.id)
assert not reloaded.is_finished          # a crashed job is not "finished"
service.resume(reloaded, destination, confirmed=True)
assert destination.saved_tracks == ["0", "1", "2", "3", "4"]   # no repeats
```

The same guarantee holds for playlists: a container created before the crash is
reused, not recreated, and entries continue from where they stopped
(`["a"] → ["a", "b", "c"]`).

### Retry, not resume

```python
retry_job = service.create_retry_job(job)
```

Copies only retryable items into a **new** job (`metadata["retry_of"]` points
back). The original stays untouched so its history remains auditable.

---

## 9. Ordering

Two distinct concepts, and conflating them is the source of "the transfer
worked but my library is backwards" bugs:

```
requested logical order  +  destination insertion behaviour  =  write order
```

- `apply_logical_order(items, mode, ...)` — the order the user asked for.
- `to_write_order(items, mode, insertion_behavior, preserve_visible_order=True)`
  — the order items must actually be sent, reversing for a `PREPEND`
  destination so the **visible** result matches the request.

Items with a missing sort value go **last in both directions**. That is done by
partitioning, not by a flag inside the sort key: `reverse=True` would flip the
flag and promote "date unknown" to "most recently added".

`restore_positions()` rebuilds a playlist's exact source sequence from
`original_position`, which is what lets resume continue a partially written
playlist.

---

## 10. Reporting

`TransferReport.from_items()` derives every number from **durable item state**,
not from transient in-memory counters. A report can therefore be regenerated
after a crash, or in a different process than the one that ran the job.

---

## 11. Safety summary

| Concern | Mechanism |
|---|---|
| Planning writes | `ReadOnlyAdapter` blocks non-`READ` methods |
| Destructive actions | Classified `DESTRUCTIVE`; only reachable from cleanup, behind confirmation |
| Unconfirmed writes | `AMBIGUOUS` + destination reconciliation, never a blind retry |
| Missing confirmation | `ConfirmationRequired` raised by the service |
| Credentials in logs | `SecretRedactionFilter` on every handler |
| Runaway work | `max_pages` / `max_items` bounds raise typed errors instead of truncating |
