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
                                                │      └──────┘
                                                ▼
                                           VERIFYING ──▶ COMPLETED

CANCELLED ◀── reachable from every non-terminal state
FAILED    ◀── reachable from every non-terminal state
```

| Rule | Why |
|---|---|
| `COMPLETED`, `FAILED`, `CANCELLED` have no outgoing edges | Terminal; a finished job is retired, not resumed. |
| `PAUSED → IMPORTING` only | Pause is exclusively a pause of *writing*. |
| `WAITING_CONFIRMATION → PLANNING` allowed | Re-planning is read-only and cheap; replaying writes is not. |
| `resume_target()` returns `None` for terminal jobs | Makes "resume a finished job" impossible by construction. |

`TransferService.resume()` additionally raises
`TransferConfigurationError("job_already_finished")`. **Resume means "the
process died"; retry means "some items failed"** — different operations with
different safety properties.

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

## 7. Verification

Verification is separate from the API acknowledgement (Invariant G): a platform
can return success and still not hold the item.

- **Set-like sections** (liked tracks, albums, artists): membership only.
- **Playlists**: membership **and** order.
- Duplicates are compared as multisets, so `A, B, A` verifies correctly.

`SequenceComparison` reports `missing`, `unexpected`, and `order_mismatches`
separately. Order mismatches are capped at 50 so a large library cannot flood a
report. If the destination cannot be read back, the result is
`success=False` with a `verification_unsupported:` warning — never a silent
pass.

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
