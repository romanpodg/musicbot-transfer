# Implementation Report — Phase 0/1

Architecture foundation and repository refactoring.

Success criterion met: **a tested, platform-independent music-transfer core
where the existing TIDAL implementation is the first real adapter, and where
Spotify and Telegram can be added next without redesigning the project.**

---

## 1. Repository audit

The original project (`tidal_manager/`, ~20 modules) was read end to end and
every concern classified before any code was written.

| Concern | Classification | Where it went |
|---|---|---|
| TIDAL auth, session files | TIDAL-specific | `platforms/tidal/auth.py` |
| TIDAL API calls | TIDAL-specific | `platforms/tidal/client.py` |
| Pagination | **Generic algorithm**, TIDAL parameters | `platforms/tidal/pagination.py` |
| Export / import | Generic | `MusicPlatformAdapter.export_library` + executor |
| Transfer orchestration | Generic | `core/transfer/` |
| Track matching | Generic | `core/matching/` |
| Sorting / ordering | Generic | `core/transfer/ordering.py` |
| Verification | Generic | `core/transfer/verifier.py` |
| Cleanup / deletion | Generic but **destructive** | Adapter `DESTRUCTIVE` methods, not wired to a command |
| Backups, state files | Persistence | `infrastructure/persistence/` |
| Resume state | **Was a cursor → replaced** | `core/transfer/recovery.py` |
| Interactive menu | UI | `interfaces/cli/` |
| Progress bars | UI-driven, generic model | `TransferProgress` + `ProgressRenderer` |
| Localization (en/ru) | UI | `locales/` |
| Logging | Infrastructure | `infrastructure/logging/` |

**Baseline captured before refactoring:** `python -m unittest discover -s tests
-v` → `Ran 15 tests … OK`. Zero pre-existing failures. This baseline is
preserved as `tests/legacy/` and still passes.

Two load-bearing defects were identified in the audit:

1. **Pagination terminated on a short page** (`core/auth.py`):
   `if len(page) < page_size: return values` combined with
   `offset += len(page)`. At `page_size=100`, a 49-item page silently truncated
   the library *and* desynchronised the offset.
2. **Resume state was positional.** A crash could repeat or lose work because
   progress was a single counter rather than per-item status.

---

## 2. Architecture implemented

Ports and adapters, dependencies pointing inward:

```
interfaces  →  app  →  core
platforms   →  core.ports
infrastructure → core.ports
```

`core/` imports the standard library only. Verified mechanically:

```
platform-name conditionals in core/    → none
platform-name conditionals in app/     → none
Telegram imports outside interfaces/   → none
bare `except:`                         → none
exception bodies that are just `pass`  → none
hardcoded token/password literals      → none
```

Guidance: [docs/architecture.md](architecture.md).

---

## 3. Files added

**New package `music_transfer/`** — 66 Python modules (~11 100 lines) plus 2
locale catalogs.

| Area | Modules |
|---|---|
| `core/domain/` | track, album, artist, playlist, account, library, matching, transfer, verification (~1 450 lines) |
| `core/ports/` | platform, repositories, queue (~800) |
| `core/matching/` | normalization, scoring, matcher (~900) |
| `core/transfer/` | lifecycle, planner, executor, verifier, recovery, ordering (~1 700) |
| `core/` | enums (173), errors (244) |
| `platforms/tidal/` | client, mapper, errors, auth, pagination, adapter (~1 490) |
| `platforms/` | registry (99) |
| `infrastructure/` | persistence (json_store, repositories), security/credentials, logging/setup, http (~990) |
| `app/` | transfer_service, account_service, diagnostics, dto/models (~900) |
| `interfaces/cli/` | console, prompts, context, commands, main, `__main__` (~1 140) |
| `locales/` | manager + `en/messages.json` + `ru/messages.json` |

**Tests** — 14 files: `tests/support.py` (stateful fake adapter + builders),
`tests/unit/{test_pagination,test_matching,test_ordering,test_models,test_lifecycle,test_safety,test_resume,test_localization}.py`,
`tests/legacy/` (the original 3 files, relocated).

**Docs** — `README.md`, `docs/architecture.md`, `docs/platform-adapters.md`,
`docs/transfer-lifecycle.md`, `docs/roadmap.md`, and this report.

**Config** — `pyproject.toml` (console script `music-transfer`, ruff/mypy
config), `.env.example` (placeholders only, no credential fields).

---

## 4. Files modified

- `tidal_manager/` — **not modified.** Verified byte-for-byte identical to the
  original with `diff -rq` (the only delta is the nested `.venv` that was
  removed when the tree was copied).
- `tests/__init__.py` — corrected the layout docstring and documented the
  required `-t .` invocation.
- `tests/unit/test_resume.py` — rewritten to model a real crash (see §13).
- `music_transfer/core/domain/account.py` — added `Account.create()`.
- `music_transfer/core/ports/repositories.py` and
  `infrastructure/persistence/repositories.py` — added
  `AccountRepository.list_all()` / `update()`.
- `music_transfer/app/services/transfer_service.py` — added `jobs` and `items`
  properties so interfaces can reload state without touching private fields.
- `music_transfer/infrastructure/security/credentials.py` — added `available()`.
- `music_transfer/config.py` — `Settings.load` honours
  `MUSIC_TRANSFER_LANGUAGE`.
- `music_transfer/locales/{en,ru}/messages.json` — 30 error codes backfilled
  (see §13).

---

## 5. TIDAL migration

The working logic was **preserved and moved**, not rewritten. `client.py` keeps
the proven API call shapes; what changed is the boundary around them.

| Before | After |
|---|---|
| `core/auth.py` — pagination + session | `platforms/tidal/auth.py` + `pagination.py` |
| `core/transfer.py` — orchestration | `platforms/tidal/adapter.py` (implements the port) |
| `core/sorting.py` | `core/transfer/ordering.py` (generic) |
| `core/verification.py` | `core/transfer/verifier.py` (generic) |
| `core/state.py` | `infrastructure/persistence/` |
| `ui/menu.py`, `ui/prompts.py` | `interfaces/cli/` |
| `localization/` | `locales/` (migrated, then extended) |

**Invariant A** is now encoded in `platforms/tidal/pagination.py`: terminate on
an empty page only, advance the offset by the *requested* page size, detect a
repeated page, and bound work with `max_pages`/`max_items` raising typed errors
instead of truncating silently.

Declared TIDAL capabilities: all reads and writes; destructive operations
present but only for cleanup; `insertion_behavior=PREPEND`; duplicates
supported; region-aware; ISRC exposed; no batch playlist writes; no custom
"date added".

---

## 6. Universal models

`core/domain/` — every model carries `source_platform`, `source_id`, explicit
first-class fields, and a `metadata: dict` for platform-specific extras.

`Track`, `Album`, `Artist`, `Playlist`, `PlaylistItem`, `LibraryRecord`,
`LibrarySnapshot`, `Account`, `AccountProfile`, `TransferJob`, `TransferItem`,
`TransferPlan`, `TransferPlanSummary`, `TransferProgress`, `TransferReport`,
`TransferSettings`, `MatchResult`, `ScoredCandidate`, `VerificationResult`,
`SequenceComparison`.

`Account` has **no credential field** — only `auth_reference`, a pointer into
the keyring. A test asserts no credential material appears in serialization.

The TIDAL mapper round-trips API objects → domain models → dict without loss of
the original values; normalization produces a comparison key and never mutates
the source object (Invariant K).

---

## 7. Platform adapter contract

`MusicPlatformAdapter` (`core/ports/platform.py`) with three operation classes
and a `PlatformCapabilities` declaration. Methods not implemented raise
`UnsupportedCapabilityError` by default.

Exports: `get_profile`, `export_library`, `get_liked_tracks`,
`get_saved_albums`, `get_followed_artists`, `get_playlists`,
`get_playlist_items`, `get_destination_state`, `playlist_item_ids`,
`search_track/album/artist`; writes `save_track`, `save_album`,
`follow_artist`, `create_playlist`, `add_playlist_item(s)`, `create_folder`;
destructive `remove_*`, `delete_*`; plus `can_reuse_identifier`.

`platforms/registry.py` is the **only** place a platform name selects a class.
`registry.create(Platform.SPOTIFY, …)` raises
`UnsupportedCapabilityError("platform_not_registered")`; `registry.unimplemented`
provides a factory that refuses by name. Nothing returns a fake success.

Guidance: [docs/platform-adapters.md](platform-adapters.md).

---

## 8. Transfer lifecycle

```
export → normalize → match → plan → [confirm] → execute → verify → report
```

- **Job machine** — 13 states, table-driven (`TRANSITIONS`); illegal moves raise
  `InvalidStateTransition`.
- **Item machine** — 9 states with explicit failure meanings
  (`NOT_FOUND` / `UNAVAILABLE` / `AMBIGUOUS` / `FAILED` are distinct).
- **Planner** — read-only, enforced by `ReadOnlyAdapter`.
- **Executor** — per-item checkpointing, idempotency via destination
  reconciliation, cooperative cancellation, dry run.
- **Verifier** — re-reads the destination; membership **and** order; duplicates
  compared as multisets.
- **Recovery** — `pending_items`, `select_for_retry`, `resolve_ambiguous`.

Guidance: [docs/transfer-lifecycle.md](transfer-lifecycle.md).

---

## 9. Matching foundation

Tiered, cheapest and most reliable first: **ISRC → portable identifier → exact
metadata → normalized → fuzzy**, with `MatchMethod` recorded on every item.

`normalize_text` casefolds, strips diacritics, and collapses whitespace and
punctuation. `split_artists` separates featured artists; `find_version_qualifiers`
detects remaster/live/acoustic/radio-edit markers; `detect_explicit_flag` reads
the explicit marker.

`ScoreWeights` combines title, artists, album, and duration. Penalties:
explicit-vs-clean, version difference, and duration beyond tolerance. Two
candidates within a near-tie margin are reported `AMBIGUOUS` rather than
guessed. Normalization returns a key — the original track is never mutated.

---

## 10. Ordering

Two distinct concepts, deliberately separated:

- **Logical order** — `apply_logical_order(items, mode, …)` with
  `DATE_ADDED_NEWEST_FIRST`, `DATE_ADDED_OLDEST_FIRST`, `ALPHABETICAL`,
  `ARTIST`, `ALBUM`, `SOURCE_ORDER`.
- **Write order** — `to_write_order(…, insertion_behavior, preserve_visible_order=True)`
  reverses for a `PREPEND` destination so the *visible* result matches the
  request. TIDAL favourites are `PREPEND`, which is why this matters.

Items with a missing sort value go **last in both directions**, implemented by
partitioning rather than a flag in the sort key (a `reverse=True` sort would
otherwise promote "date unknown" to "most recently added").

`restore_positions()` rebuilds a playlist's exact source sequence from
`original_position`, which is what lets resume continue a partially written
playlist. A source playlist `A, B, A` is recreated as `A, B, A`.

---

## 11. Persistence and recovery

Ports: `TransferJobRepository`, `TransferItemRepository`,
`TransferPlanRepository`, `AccountRepository`, `UnitOfWork`.

JSON implementation: one file per job, one per job's items, one per plan, plus a
shared accounts document. Writes are atomic (temp file → fsync → `os.replace` →
`chmod 600`). `NullUnitOfWork` provides the transactional boundary today so
callers can be written correctly before a database exists.

**Recovery is item-level.** No cursor. After a crash the engine reads durable
item status, because a write can land while its acknowledgement is lost.
`resolve_ambiguous` re-reads the destination and promotes an `AMBIGUOUS` item to
`TRANSFERRED` only when the identifier is really present.

`create_retry_job` copies retryable items into a **new** job
(`metadata["retry_of"]`), leaving the original auditable.
`select_for_retry` raises `ValueError` if asked for a terminal status.

Guidance: [docs/roadmap.md](roadmap.md#5-postgresql-schema-planned) for the
future database schema.

---

## 12. Safety

| Concern | Mechanism |
|---|---|
| Planning writes | `ReadOnlyAdapter` blocks every non-`READ` method at runtime |
| Destructive actions | Classified `DESTRUCTIVE`; only reachable from cleanup, behind confirmation |
| Forgotten confirmation | `ConfirmationRequired` raised by the service, not the UI |
| Unconfirmed writes | `AMBIGUOUS` + destination reconciliation; never a blind retry |
| Untranslated SDK errors | Classified and recorded per item; one bad item cannot abort a library |
| Credentials in logs | `SecretRedactionFilter` installed on every handler |
| Credentials at rest | Keyring only; there is no password code path |
| Runaway reads | `max_pages` / `max_items` raise typed errors instead of truncating |
| Partial exports | Reported as warnings and `snapshot.is_partial`; never hidden |

---

## 13. Tests

**164 tests, all passing** (149 new + 15 legacy).

| Module | Tests | Covers |
|---|---:|---|
| `test_pagination.py` | 15 | Invariant A; `100 + 49 + 100 = 249` and the request grid `[(100,0),(100,100),(100,200),(100,300)]`; bounds; policy defaults |
| `test_matching.py` | 28 | ISRC precedence, feat., Unicode, explicit-vs-clean, remaster, live, duration mismatch, ambiguity, `DIRECT_ID` |
| `test_ordering.py` | 21 | Logical vs write order, `PREPEND` compensation, `A, B, A` duplicates, position restore |
| `test_models.py` | 24 | Universal models, TIDAL mapper round-trip, account identity, no credentials serialized |
| `test_lifecycle.py` | 20 | Full happy path, terminal finality, predicate/behaviour agreement across every pair, cancellation reachability |
| `test_safety.py` | 16 | Zero planner writes, `ReadOnlyAdapter` enforcement, unsupported ops raise, confirmation gate, dry run, log redaction |
| `test_resume.py` | 13 | Crash recovery, per-item state vs cursor, retry selection, playlist idempotency |
| `test_localization.py` | 12 | Catalog parity, English fallback, error-code coverage |
| `tests/legacy/` | 15 | The original suite, unchanged and green |

Run with `python -m unittest discover -s tests -t .`

### Production bugs the tests found

1. `_summarize` mutated a frozen `TransferPlanSummary` (`FrozenInstanceError`).
2. The executor caught only `MusicTransferError`, so one untranslated SDK
   exception aborted the entire job.
3. `can_reuse_identifier` returned `True` for playlists — ids are account-owned,
   so a resumed run skipped creating the container.
4. `_write_order` placed `PLAYLIST_ITEM` entries before their container existed.
5. `normalize_text` did not fold diacritics (`Björk != Bjork`).
6. `apply_logical_order(reverse=True)` promoted missing dates to the front.
7. **30 error codes had no translation.**

For (7), `test_localization.py` walks `core/` and `platforms/` with `ast`,
collects every `raise SomeError("code")` literal, and fails if any lacks an
`errors.<code>` entry in every catalog. The 30 gaps were backfilled with
`tools/add_error_translations.py`; both catalogs now carry 292 leaf keys and 57
error messages.

Resume tests model a crash with `KeyboardInterrupt` — a `BaseException`, which
is the only honest simulation, since ordinary exceptions are handled per item
and never abort a run.

---

## 14. Backward compatibility

- `tidal_manager/` is **byte-for-byte identical** to the original and its 15
  tests still pass. `import main` from the legacy root succeeds.
- The legacy suite runs from its new home via a `sys.path` shim in
  `tests/legacy/__init__.py`; paths were re-pointed from `parents[1]` to
  `parents[2]` plus the package directory.
- The new CLI works alongside it: `python -m music_transfer` (also installable
  as `music-transfer` via `pyproject.toml`).
- No config format migration was required: `Settings` reads the same
  environment variables, with `MUSIC_TRANSFER_LANGUAGE` added for i18n.
- The localization catalogs were migrated from the legacy files and extended;
  every original key is preserved.

---

## 15. Known limitations

- **No live network tests.** Everything runs against a stateful in-memory fake.
  The TIDAL adapter is exercised by construction and mapping only; a real
  account run is still required before production use.
- **JSON persistence rewrites the whole item document per checkpoint.** Fine for
  thousands of items; the database implementation will update one row.
- **Videos and mixes** are declared content types in domain enums, but transfer planning is intentionally rejected as `engine_not_implemented` until dedicated support is added in a future phase.
- **Playlist folders** are read and created, but not re-linked on transfer.
- **Cleanup is not wired to a CLI command.** The destructive operations exist,
  are correctly classified, and are unreachable from transfer; no command drives
  them yet.
- **No rate-limit coordination** between concurrent workers. A single rate-limited
  write becomes `AMBIGUOUS` and is reconciled, not corrupted.
- **No commits yet** — the repository has no history; all files are untracked.
- `InlineQueue` and `NullUnitOfWork` are real in-process implementations, correct
  for the CLI, not substitutes for a broker or a database.

---

## 16. Recommended next phase

**Phase 2 — Spotify adapter.** It is the cheapest real validation of the whole
architecture because it differs from TIDAL in exactly the ways that matter:
`APPEND` instead of `PREPEND`, no playlist duplicates, and a different auth flow
(PKCE). If the core survives that without edits, the design is sound.

Suggested order:

1. **Spotify adapter** — ~4 files plus one registry line. Expect no `core/`
   change; if one is needed, add a capability flag instead.
2. **One end-to-end smoke run against real accounts** for both TIDAL → TIDAL and
   TIDAL → Spotify. This closes the biggest remaining risk (§15, first bullet).
3. **Telegram interface** — the seam exists (`interfaces/`, DTOs, i18n,
   `CancellationToken`). Long transfers must first move behind the `JobQueue`.
4. **PostgreSQL** — implement the existing repository ports against the schema in
   [docs/roadmap.md](roadmap.md#5-postgresql-schema-planned); required before
   multi-user Telegram usage.
5. **Wire the cleanup workflow** to a command, keeping the destructive path
   behind explicit confirmation and a dry run.

Explicitly deferred: billing, multi-tenancy, a web frontend, and YouTube Music /
Apple Music adapters (the latter has no public write API off Apple platforms).
