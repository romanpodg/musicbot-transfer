# music-transfer

A platform-independent music library transfer core. **TIDAL is the first real
adapter**; Spotify, Apple Music, Deezer, YouTube Music, and a Telegram
interface are designed for but deliberately not implemented.

```
python -m music_transfer --help
```

---

## Status

| | |
|---|---|
| Tests | **164 passing** (149 new + 15 legacy) |
| Python | 3.11+ (developed on 3.13) |
| Adapters | TIDAL |

### What is implemented

- Universal domain models — `Track`, `Album`, `Artist`, `Playlist`,
  `PlaylistItem`, `LibrarySnapshot`.
- `MusicPlatformAdapter` contract with read / mutating / destructive
  classification and a `PlatformCapabilities` declaration.
- A platform registry — the **only** place a platform name selects a class.
- Table-driven job (13 states) and item (9 states) state machines.
- Export → normalize → match → plan → confirm → execute → verify → report.
- Matching: ISRC → portable id → exact → normalized → fuzzy, with explicit-vs-clean
  and version penalties, and near-tie downgrade to ambiguous.
- Read-only planning enforced at runtime.
- Per-item checkpointing, idempotent writes, cooperative cancellation.
- Verification that compares membership **and** order, duplicate-aware.
- Item-level resume and retry — no cursor, no repeated confirmed writes.
- JSON persistence (atomic, `0600`), keyring-only credentials, structured
  redacted logging, en/ru localization.
- A working CLI.

### What is planned (a seam exists, nothing is implemented)

- **Telegram interface** — `interfaces/`, DTOs, i18n, and the cancellation token
  are already shaped for it; no handler exists yet.
- **Spotify / Apple Music / Deezer / YouTube Music** — enum members and
  capability declarations only. Asking for one raises
  `UnsupportedCapabilityError`; nothing returns a fake success.
- **PostgreSQL** — the repository ports are in place; the schema is sketched in
  [docs/roadmap.md](docs/roadmap.md#5-postgresql-schema-planned).
- **Queue-backed workers** — `JobQueue` is declared; no Redis/Celery/Dramatiq.

### What is experimental

- `tidal_manager/` — the original project, preserved **byte-for-byte** and
  still passing its own 15 tests. Superseded; do not build on it.
- `InlineQueue` and `NullUnitOfWork` — real in-process implementations, correct
  for the CLI, not substitutes for a broker or a database.

---

## Quick start

```bash
python -m venv .venv
.venv/Scripts/python.exe -m pip install -e .          # Windows
# source .venv/bin/activate && pip install -e .        # macOS / Linux

python -m music_transfer diagnostics     # environment and adapter checks
python -m music_transfer accounts        # connected accounts
python -m music_transfer capabilities tidal
python -m music_transfer plan      --source tidal --destination tidal
python -m music_transfer transfer  <job-id>
python -m music_transfer resume    <job-id>
python -m music_transfer retry     <job-id>
```

Configuration is optional; copy `.env.example` to `.env` to change defaults.
There is no password or token field anywhere in it — credentials live in the
OS keyring.

### Running the tests

```bash
python -m unittest discover -s tests -t .
```

`-t .` matters: without it unittest treats `tests` as the top-level directory
and the `tests.support` import fails.

---

## Why the architecture looks like this

```
interfaces  →  app  →  core
platforms   →  core.ports
infrastructure → core.ports
```

Dependencies point inward. `core/` imports nothing but the standard library:
no platform SDK, no Telegram, no database driver. A new music service is one
new package under `platforms/` plus one registry line — no core change.

The engine asks **what a platform can do**, never **which platform it is**:

```python
if not adapter.capabilities.supports_playlist_duplicates:
    plan.warnings.append(f"destination_deduplicates_playlists:{playlist.source_id}")
```

---

## Two bugs this refactor fixed

**1. Pagination stopped early.** The original code treated a short page as the
end of the data:

```python
if len(page) < page_size:      # a 49-item page at page_size=100 ends the fetch
    return values
offset += len(page)            # and the offset drifts when a page is short
```

Now: terminate only on an **empty** page, advance the offset by the **requested**
page size, detect a repeated page, and bound the work with typed errors. Pinned
by `100 + 49 + 100 = 249`.

**2. Transfer state was a cursor.** A crash at item 900 of 1 000 risked losing
or repeating work. State is now recorded **per item**, checkpointed after every
write, so resume is driven by durable status rather than a position counter.

---

## Documentation

| Document | Contents |
|---|---|
| [docs/architecture.md](docs/architecture.md) | Layers, dependency rules, invariants, validation |
| [docs/platform-adapters.md](docs/platform-adapters.md) | The adapter contract, capabilities, how to add a platform |
| [docs/transfer-lifecycle.md](docs/transfer-lifecycle.md) | State machines, matching, execution, resume, verification |
| [docs/roadmap.md](docs/roadmap.md) | Status by area, Telegram, workers, PostgreSQL schema |

---

## Repository layout

```
music_transfer/
├── core/             domain, ports, matching, transfer engine  (stdlib only)
│   ├── domain/       universal models
│   ├── ports/        MusicPlatformAdapter, repositories, queue
│   ├── matching/     normalization → scoring → matcher
│   └── transfer/     lifecycle · planner · executor · verifier · recovery
├── platforms/
│   ├── registry.py   platform → factory (the only name-based dispatch)
│   └── tidal/        client, mapper, auth, pagination, adapter
├── app/              application services + UI-facing DTOs
├── infrastructure/   JSON persistence, keyring, logging, HTTP
├── interfaces/cli/   argparse CLI, console rendering, prompts
└── locales/          en · ru (292 keys each)

tests/                unit/ (149) · legacy/ (15)
tidal_manager/        the original project, preserved as-is
docs/                 architecture · platform-adapters · transfer-lifecycle · roadmap
```

---

## Safety rules the code enforces

- Planning cannot write — `ReadOnlyAdapter` blocks it at runtime.
- Destructive operations are separately classified and never called by a transfer.
- A timeout is **not** "not applied": unknown outcomes become `AMBIGUOUS` and are
  resolved by re-reading the destination, never retried blindly.
- Unimplemented platforms raise; they never return `True`.
- No token, password, or session blob is ever logged (`SecretRedactionFilter`).
- One untranslated SDK exception fails one item, not the whole library.
