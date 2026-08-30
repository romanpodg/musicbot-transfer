# Roadmap

Status legend: **implemented** (shipped and tested) · **planned** (designed; a
seam exists, no implementation) · **experimental** (present but not for
production use).

---

## 1. Where the project is now

Phase 0/1 delivered a tested, platform-independent core with TIDAL as its first
real adapter. The two hard correctness problems are fixed and pinned by
regressions:

| Problem | Status |
|---|---|
| Pagination stopped at the first short page | Fixed — Invariant A, 6 tests |
| Plan/execute/verify interleaved with no resume | Fixed — item-level state machine, 13 resume tests |

Test suite: **164 tests, all passing** (149 new + 15 legacy, unchanged).

---

## 2. Current status by area

| Area | Status | Notes |
|---|---|---|
| Universal domain models | **implemented** | `Track`, `Album`, `Artist`, `Playlist`, `PlaylistItem`, `LibrarySnapshot` |
| `MusicPlatformAdapter` contract | **implemented** | Read/mutating/destructive classification |
| Platform registry | **implemented** | TIDAL registered; the rest refuse loudly |
| Capability negotiation | **implemented** | `PlatformCapabilities` with `require()` / `supports()` |
| Job lifecycle state machine | **implemented** | 13 statuses, table-driven transitions |
| Item lifecycle state machine | **implemented** | 9 statuses, explicit failure meanings |
| Export / normalize / match | **implemented** | ISRC → direct id → exact → normalized → fuzzy |
| Planning (read-only) | **implemented** | `ReadOnlyAdapter` enforcement |
| Execution + checkpointing | **implemented** | Per-item, idempotent, cancellable |
| Verification | **implemented** | Membership **and** order, multiset-aware |
| Resume / retry | **implemented** | Item-level; no cursor |
| Ordering abstraction | **implemented** | Logical order vs write order |
| JSON persistence | **implemented** | Atomic writes, `chmod 600` |
| Keyring-only credentials | **implemented** | No password path exists in the code |
| Structured redacted logging | **implemented** | `key=value` + `SecretRedactionFilter` |
| i18n (en / ru) | **implemented** | 292 keys each, parity and error-code coverage tested |
| CLI | **implemented** | `python -m music_transfer` |
| Error taxonomy + codes | **implemented** | Stable `code` attributes, localizable |
| TIDAL adapter | **implemented** | The first real adapter |
| Legacy `tidal_manager/` | **experimental** | Preserved byte-for-byte; superseded |
| Queue port | **experimental** | `JobQueue` interface + `InlineQueue`; no Redis/Celery |
| Unit of Work | **experimental** | `NullUnitOfWork` only |
| Telegram | **planned** | Seam ready: `interfaces/`, DTOs, i18n, cancellation token |
| Spotify / Apple / Deezer / YouTube | **planned** | Enum members + capability declarations only |
| PostgreSQL | **planned** | Schema sketched in §5 |
| Billing, multi-tenancy, web UI | **not planned** | Out of scope |

---

## 3. Next phase: Telegram interface

The seam is already in place. `TransferService` and `AccountService` know
nothing about chat ids, keyboards, or message formatting, and `CliContext`
(`interfaces/cli/context.py`) is the template for a `TelegramContext`.

Work items:

1. `interfaces/telegram/` — handlers, keyboards, and a session store.
2. Progress: map `TransferProgress` onto message edits, throttled.
3. Cancellation: a "Cancel" button sets the same `CancellationToken` the CLI
   uses.
4. Long transfers must move off the request path → the queue port (§4).
5. Reuse `locales/` verbatim; `LocalizationManager.error(code, **values)` is
   already designed for it.

Known constraint: a Telegram handler must not block. Synchronous adapters are
bridged with `asyncio.to_thread` (`to_async` / `_AsyncBridge` in
`core/ports/platform.py`).

---

## 4. Workers and queues

```
Telegram / CLI  →  TransferService.create_job()  →  JobQueue.enqueue(job_id)
                                                            │
                                                          worker
                                                            ▼
                                              TransferService.execute(...)
```

- `JobQueue` (`core/ports/queue.py`) declares `enqueue` / `dequeue` / `ack`.
- `InlineQueue` runs in-process — correct for the CLI, not a fake.
- A Redis / Celery / Dramatiq implementation satisfies the same interface.
- The worker resumes by `job_id` only, so a crashed worker costs at most the
  item it was writing.

Not in this phase: retries with backoff at the queue level, priority queues,
dead-letter handling.

---

## 5. PostgreSQL schema (planned)

The JSON repositories implement the same ports, so this migration touches
`infrastructure/persistence/` only.

```sql
CREATE TABLE users (
    id              TEXT PRIMARY KEY,
    interface       TEXT NOT NULL,          -- 'cli' | 'telegram' | 'web'
    interface_id    TEXT NOT NULL,          -- platform-specific user key
    language        TEXT NOT NULL DEFAULT 'en',
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (interface, interface_id)
);

CREATE TABLE platform_accounts (
    id                  TEXT PRIMARY KEY,
    user_id             TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    platform            TEXT NOT NULL,       -- Platform enum value
    platform_account_id TEXT NOT NULL,       -- id on the music service
    display_name        TEXT,
    auth_reference      TEXT NOT NULL,       -- keyring pointer; NEVER a token
    connected_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (platform, platform_account_id, user_id)
);

CREATE INDEX platform_accounts_user_idx ON platform_accounts (user_id);

CREATE TABLE transfer_jobs (
    id                     TEXT PRIMARY KEY,
    user_id                TEXT REFERENCES users(id) ON DELETE SET NULL,
    source_platform        TEXT NOT NULL,
    destination_platform   TEXT NOT NULL,
    source_account_id      TEXT REFERENCES platform_accounts(id),
    destination_account_id TEXT REFERENCES platform_accounts(id),
    status                 TEXT NOT NULL,    -- JobStatus enum value
    requested_content      TEXT[] NOT NULL,
    settings               JSONB NOT NULL DEFAULT '{}'::jsonb,
    metadata               JSONB NOT NULL DEFAULT '{}'::jsonb,
    total_items            INTEGER NOT NULL DEFAULT 0,
    processed_items        INTEGER NOT NULL DEFAULT 0,
    successful_items       INTEGER NOT NULL DEFAULT 0,
    failed_items           INTEGER NOT NULL DEFAULT 0,
    skipped_items          INTEGER NOT NULL DEFAULT 0,
    cancellation_requested BOOLEAN NOT NULL DEFAULT false,
    error_code             TEXT,
    started_at             TIMESTAMPTZ,
    finished_at            TIMESTAMPTZ,
    created_at             TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at             TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX transfer_jobs_user_idx   ON transfer_jobs (user_id, created_at DESC);
CREATE INDEX transfer_jobs_status_idx ON transfer_jobs (status);
-- Drives "resumable jobs" without a table scan.
CREATE INDEX transfer_jobs_resumable_idx ON transfer_jobs (status)
    WHERE status IN ('paused', 'importing');

CREATE TABLE transfer_items (
    id                        TEXT PRIMARY KEY,
    job_id                    TEXT NOT NULL REFERENCES transfer_jobs(id) ON DELETE CASCADE,
    entity_type               TEXT NOT NULL,   -- EntityType enum value
    source_platform           TEXT NOT NULL,
    source_id                 TEXT NOT NULL,
    destination_platform      TEXT NOT NULL,
    destination_id            TEXT,
    status                    TEXT NOT NULL,   -- ItemStatus enum value
    original_position         INTEGER NOT NULL DEFAULT 0,
    container_source_id       TEXT,            -- parent playlist, source side
    container_destination_id  TEXT,            -- parent playlist, destination side
    match_method              TEXT,
    match_score               REAL,
    attempt_count             INTEGER NOT NULL DEFAULT 0,
    last_failure_kind         TEXT,
    last_error                TEXT,
    source_metadata           JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at                TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at                TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- The resume hot path: "give me everything still pending for this job".
CREATE INDEX transfer_items_job_idx     ON transfer_items (job_id, original_position);
CREATE INDEX transfer_items_pending_idx ON transfer_items (job_id)
    WHERE status NOT IN ('transferred', 'already_exists', 'skipped');
CREATE UNIQUE INDEX transfer_items_identity_idx
    ON transfer_items (job_id, entity_type, source_id, original_position);

CREATE TABLE transfer_plans (
    job_id                TEXT PRIMARY KEY REFERENCES transfer_jobs(id) ON DELETE CASCADE,
    source_platform       TEXT NOT NULL,
    destination_platform  TEXT NOT NULL,
    summary               JSONB NOT NULL,
    warnings              JSONB NOT NULL DEFAULT '[]'::jsonb,
    source_incomplete     BOOLEAN NOT NULL DEFAULT false,
    destination_incomplete BOOLEAN NOT NULL DEFAULT false,
    created_at            TIMESTAMPTZ NOT NULL DEFAULT now()
);
```

Notes:

- `auth_reference` is a **pointer**, never a token. The repository port
  documents this and `Account` has no credential field to violate it with.
- `transfer_items.attempt_count` and `status` together give retry its input;
  no extra bookkeeping table is needed.
- The partial unique index on `transfer_items` is what makes a replayed plan
  idempotent at the database level too.

---

## 6. Additional platform adapters

Order suggested by effort, not by value:

1. **Spotify** — OAuth PKCE, Web API, ISRC exposed, `APPEND` insertion,
   no playlist duplicates.
2. **Deezer** — OAuth, ISRC exposed, straightforward catalogue.
3. **YouTube Music** — unofficial API; expect brittle matching and no ISRC,
   which the scoring layer already handles.
4. **Apple Music** — MusicKit tokens, no public write API on non-Apple
   platforms; likely read-only for a long time.

Each is ~4 files plus a registry line. See
[platform-adapters.md](platform-adapters.md) §5.

---

## 7. Known limitations

Documented openly; see the implementation report for the authoritative list.

- **No live network tests.** Everything runs against a stateful in-memory fake.
  The TIDAL adapter is exercised only by import and construction.
- **JSON persistence rewrites the whole item document per checkpoint.** Fine
  for thousands of items; the database implementation updates one row.
- **Videos and mixes** are modelled and plannable but not verified (the
  verifier covers tracks, albums, artists, and playlists).
- **Playlist folders** are read and created, but not re-linked on transfer.
- **Cleanup is not wired to the CLI.** The destructive operations exist on the
  TIDAL adapter and are correctly classified; no command drives them yet.
- **No rate-limit coordination** between concurrent workers.
