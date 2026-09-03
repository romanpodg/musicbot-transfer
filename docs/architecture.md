# Architecture

`music_transfer` is a **ports-and-adapters** (hexagonal) application. Its goal
is that a new music service — Spotify, Apple Music, Deezer, YouTube Music — is
added by writing one new package under `music_transfer/platforms/`, with **no
change to the core**.

---

## 1. Layers

```
┌──────────────────────────────────────────────────────────────┐
│  interfaces/          CLI today; Telegram, web, workers later │
│  cli/  (console, prompts, commands, context)                  │
└───────────────────────────┬──────────────────────────────────┘
                            │  calls
┌───────────────────────────▼──────────────────────────────────┐
│  app/                 application services + DTOs             │
│  AccountService · TransferService · DiagnosticsService        │
│  dto/  view models the interface renders                      │
└───────────────────────────┬──────────────────────────────────┘
                            │  uses
┌───────────────────────────▼──────────────────────────────────┐
│  core/                 domain + engine (no I/O, no SDK)       │
│  domain/    Track, Album, Artist, Playlist, TransferJob, ...  │
│  ports/     MusicPlatformAdapter, repositories, queue         │
│  matching/  normalization → scoring → matcher                 │
│  transfer/  lifecycle · planner · executor · verifier         │
│             · recovery · ordering                             │
│  enums.py   Platform, EntityType, ItemStatus, JobStatus, ...  │
│  errors.py  typed errors with stable `code` attributes        │
└───────────────────────────▲──────────────────────────────────┘
                            │  implements
┌───────────────┬───────────┴───────────┬──────────────────────┐
│ platforms/    │ infrastructure/       │ locales/              │
│ tidal/        │ persistence (JSON)    │ en · ru               │
│ registry.py   │ security (keyring)    │                       │
│               │ logging, http         │                       │
└───────────────┴───────────────────────┴──────────────────────┘
```

### Dependency direction (enforced, not aspirational)

| Layer | May import | Must never import |
|---|---|---|
| `core/` | stdlib only | adapters, Telegram, Redis, any platform SDK |
| `app/` | `core/` | a specific platform, a specific UI |
| `platforms/` | `core.ports` | `app/`, `interfaces/` |
| `infrastructure/` | `core.ports` | `app/`, `interfaces/` |
| `interfaces/` | `app/`, `core/` | a platform SDK |

Verified mechanically (see [Validation](#7-validation)): `core/` contains no
`platform == "tidal"`-style conditional, no Telegram import, and no
platform-SDK import.

---

## 2. Why this shape

The previous code (`tidal_manager/`) mixed four concerns in one place:

- TIDAL API calls and pagination,
- transfer/ordering/matching logic that is **not** TIDAL-specific,
- interactive menu rendering,
- state files and backups.

That made "add Spotify" equivalent to "rewrite the project". The refactor
separates them:

- **TIDAL knowledge** → `platforms/tidal/` (client, mapper, auth, pagination).
- **Transfer knowledge** → `core/` (engine, matching, ordering, recovery).
- **Interaction** → `interfaces/cli/`.
- **Storage** → `infrastructure/persistence/`.

---

## 3. Key design decisions

### 3.1 Capabilities, not platform names

The engine never asks "is this TIDAL?". It asks the adapter what it can do:

```python
capabilities = adapter.capabilities
if not capabilities.supports_playlist_duplicates:
    plan.warnings.append(f"destination_deduplicates_playlists:{playlist.source_id}")
```

`PlatformCapabilities` (`core/ports/platform.py`) declares reads, writes,
destructive operations, search support, and behavioural traits such as
`insertion_behavior`, `supports_playlist_duplicates`, and `exposes_isrc`.

`platforms/registry.py` is the **single** place where a platform name maps to a
class. It is a dict lookup, not a chain of conditionals.

### 3.2 No fake implementations

A platform that is not shipped must not silently "succeed":

- `registry.create(Platform.SPOTIFY, ...)` → `UnsupportedCapabilityError("platform_not_registered")`.
- `AccountService.is_implemented(Platform.SPOTIFY)` → `False`.
- A disabled capability → `UnsupportedCapabilityError("capability_unsupported")`.

The CLI renders this honestly:

```
$ music-transfer capabilities spotify
⚠️ This platform is not implemented yet.
```

### 3.3 Read-only by construction

`TransferPlanner` receives a `ReadOnlyAdapter` wrapper
(`core/ports/platform.py`). Any method not classified `READ` raises before it
reaches the network. Planning — the phase that touches an unfamiliar account —
therefore cannot mutate anything, and a unit test asserts it makes zero write
calls.

### 3.4 Failure meanings stay explicit

`ItemStatus` has `NOT_FOUND`, `UNAVAILABLE`, `AMBIGUOUS`, `FAILED`,
`ALREADY_EXISTS`, and `SKIPPED` as distinct values. Collapsing them into one
`ERROR` state would make retry selection impossible: "not in the catalog" is
permanent, "region-locked" is permanent-for-now, "timeout with unknown outcome"
must not be retried blindly, and "rate limited" is worth retrying.

### 3.5 Item-level state, not a cursor

Resume never replays item 1..N from a saved position. Every item carries its own
status, persisted after each write. See [transfer-lifecycle.md](transfer-lifecycle.md).

### 3.6 Declarative Identifier Resolution (Phase 1.5A)

A transfer item is never emitted in an executable state with a missing destination
identifier. The planner resolves identifiers following an explicit order:

1. Direct portable identifier reuse (`can_reuse_identifier`) without unnecessary search calls.
2. Conservative catalog search (`search_tracks`, `search_albums`, `search_artists`) when search capability is declared and supported.
3. Explicit classification as non-executable (`NOT_FOUND` with `destination_resolution_unavailable` or `not_found`, or `AMBIGUOUS` with `ambiguous`).

Destination presence queries and preflight preconditions are evaluated strictly on resolved non-empty destination identifiers.

### 3.7 Videos and Mixes Transfer Support (Phase 1.5B)

The engine supports `VIDEOS` and `MIXES` content transfer end-to-end:
- **Resolution Policy**: `IdentifierResolutionPolicy.REUSE_ONLY`. Videos and mixes rely strictly on portable identifier reuse across accounts (`can_reuse_identifier`). No catalog search is attempted (`search_capability = None`). Cross-platform transfers without reusable identifiers resolve safely to `NOT_FOUND` with reason `destination_resolution_unavailable`.
- **Destination Presence**: Canonical destination state sections include `"videos"` and `"mixes"` alongside `"tracks"`, `"albums"`, `"artists"`, and `"playlists"`. Observed presence semantics (PRESENT, ABSENT, UNKNOWN) govern exact preflight preconditions, already-exists skips, and post-write verification.
- **Adapter Mutation**: Platform adapters declare `read_videos`/`write_videos` and `read_mixes`/`write_mixes` capabilities, executing mutations via `save_video(video_id)` and `save_mix(mix_id)`. `ReadOnlyAdapter` enforces safety by excluding mutating methods during planning.

### 3.8 Playlist Folder Hierarchy Transfer and Runtime Container Binding (Phase 1.5C)

Folders are structural components of `ContentType.PLAYLISTS`, not an independent selectable content type:
- **Structural Content Dependency**: `ContentType.PLAYLISTS` declares a structural dependency on the `"folders"` snapshot section. `source_export_sections()` selectively reads folders before playlists when source supports `read_folders`. Flat playlists never require destination `create_folders`.
- **Universal Root Representation**: The core engine uses `None` for unparented containers. Provider-specific root representations (such as TIDAL's `"root"`) are strictly translated at the platform adapter boundary.
- **Topological Planning & Runtime Binding**: In confirmed plans, folders are planned with `TransferOperation.CREATE_FOLDER`, `container_source_id` bound to source parent folder ID, and `destination_id = None`. Execution runs in 5 topological tiers (non-containers → root folders → child folders → playlists → playlist items). Destination folder IDs are resolved and propagated at runtime to child folders and playlists.
- **Failure Cascade Isolation**: A failed or ambiguous folder creation blocks all downstream child folders, playlists, and playlist items with `container_blocked` (or `playlist_sequence_blocked`), preventing orphan creation and order corruption while allowing sibling subtrees to proceed. Foldered playlists never fall back to the destination root.
- **Durable Intent & Idempotent Reconciliation**: `MutationState.IN_FLIGHT` is persisted before remote folder creation. On timeout or ambiguous error, folder state is reconciled against destination state matching `(parent_id, name)`.
- **Job-Scoped Post-Transfer Verification**: Destination folder existence, title, and parent ID, along with playlist folder placement, are verified post-execution via `export_library(sections=("folders", "playlists"))`.

---

## 4. Package contents

| Path | Lines | Responsibility |
|---|---:|---|
| `core/domain/` | ~1 450 | Universal models; no platform fields leak in |
| `core/ports/` | ~800 | `MusicPlatformAdapter`, repository and queue interfaces |
| `core/matching/` | ~900 | Normalization, weighted scoring, matcher |
| `core/transfer/` | ~1 700 | Lifecycle, planner, executor, verifier, recovery, ordering |
| `core/enums.py`, `core/errors.py` | ~420 | Shared vocabulary and typed errors |
| `platforms/tidal/` | ~1 490 | The first real adapter |
| `platforms/registry.py` | ~100 | Platform → factory, the only name-based dispatch |
| `infrastructure/` | ~990 | JSON persistence, keyring, structured logging, HTTP policy |
| `app/` | ~900 | Application services and UI-facing DTOs |
| `interfaces/cli/` | ~1 140 | Argparse CLI, console rendering, prompts |
| `locales/` | ~230 | `en` and `ru` catalogs, 292 keys each (57 error codes) |

---

## 5. Invariants

These are the rules the test suite enforces. Breaking one fails a test.

| # | Invariant | Enforced in |
|---|---|---|
| A | A short non-empty page does **not** imply end of pagination | `platforms/tidal/pagination.py`, `tests/unit/test_pagination.py` |
| B | A plan performs no destination mutation | `ReadOnlyAdapter`, `tests/unit/test_safety.py` |
| C | Inspection never triggers a destructive action | `OperationKind`, confirmation gate |
| D | Playlist duplicates are preserved | `tests/unit/test_ordering.py`, `test_resume.py` |
| E | Resume never repeats a confirmed operation | `RecoveryService`, `tests/unit/test_resume.py` |
| F | A timeout is not "not applied" | `ItemStatus.AMBIGUOUS` + reconciliation |
| G | Verification is separate from the API ack | `core/transfer/verifier.py` |
| H | The core is independent of Telegram and of any one platform | dependency checks |
| I | Unsupported capabilities are explicit, never silently true | `UnsupportedCapabilityError` |
| J | No token or password ever reaches a log | `SecretRedactionFilter` |
| K | Original metadata survives normalization | `normalize_text` returns a key, never mutates |
| L | Writes require exact durable plan confirmation (`plan_id + revision + plan_hash`) | `TransferService`, `tests/unit/test_plan_identity_and_confirmation.py` |
| M | Re-planning creates a new revision and invalidates old confirmation; destination drift produces zero writes | `TransferService`, `tests/unit/test_plan_identity_and_confirmation.py` |
| N | A transfer item must never become executable without a resolved destination identifier or explicit non-executable classification | `TransferPlanner`, `validate_plan_set_like_items`, `tests/unit/test_identifier_resolution.py` |
| O | Foldered playlists and child folders must never execute or fall back to root when their parent container fails or is unconfirmed | `TransferExecutor`, `tests/unit/test_playlist_folder_hierarchy.py` |
| P | A persisted in-flight folder creation must never be blindly replayed upon resume; zero destination matches must mark the item AMBIGUOUS rather than replaying create_folder | `TransferExecutor._write_folder`, `tests/unit/test_folder_resume_safety.py` |

---

## 6. Adding a platform

See [platform-adapters.md](platform-adapters.md) for the full checklist. In
short:

1. `platforms/<name>/client.py` — raw API calls.
2. `platforms/<name>/mapper.py` — API objects → universal domain models.
3. `platforms/<name>/auth.py` — OAuth/token flow, credentials to keyring.
4. `platforms/<name>/adapter.py` — implement `MusicPlatformAdapter`, declare
   `CAPABILITIES`.
5. Register the factory in `platforms/registry.py`.
6. Add a `Platform` member (already present for the four planned services).

No file under `core/`, `app/`, or `interfaces/` should need to change.

---

## 7. Validation

Run from the repository root:

```bash
python -m unittest discover -s tests -t .
# 164 tests, OK  (149 new + 15 legacy)
```

| Module | Tests | Covers |
|---|---:|---|
| `test_pagination.py` | 15 | Invariant A — `100 + 49 + 100 = 249`, bounds, policy defaults |
| `test_matching.py` | 28 | ISRC precedence, feat./Unicode/explicit/version/duration, ambiguity |
| `test_ordering.py` | 21 | Logical vs write order, `PREPEND` compensation, `A, B, A` |
| `test_models.py` | 24 | Universal models, TIDAL mapper round-trip, no credentials serialized |
| `test_lifecycle.py` | 20 | Full path, terminality, every transition pair, cancellation reachability |
| `test_safety.py` | 16 | Zero planner writes, read-only enforcement, confirmation gate, redaction |
| `test_resume.py` | 13 | Crash recovery, per-item state, retry selection, playlist idempotency |
| `test_localization.py` | 12 | Catalog parity, fallback, error-code coverage |
| `tests/legacy/` | 15 | The original suite, unchanged |

Structural checks performed on the finished tree:

```
platform-name conditionals in core/    → none
platform-name conditionals in app/     → none
Telegram imports outside interfaces/   → none
bare `except:`                         → none
exception bodies that are just `pass`  → none
hardcoded token/password literals      → none
```

---

## 8. Deliberately not built

Per the phase scope, these are **absent**, not stubbed:

- Spotify / Apple Music / Deezer / YouTube Music adapters.
- A production Telegram bot (only the seam: `interfaces/`, i18n, DTOs).
- Redis / Celery / Dramatiq (a `QueuePort` interface exists; no implementation).
- PostgreSQL (a `UnitOfWork` port exists; a `NullUnitOfWork` is provided;
  the future schema is sketched in [roadmap.md](roadmap.md)).
- Billing, multi-tenancy, and a web frontend.
