# Platform Adapters

An adapter is the **only** part of the system that knows how a specific music
service works. Everything above it speaks in universal domain objects and asks
about *capabilities*, never about platform names.

> **Invariant I.** An adapter must never claim success for something it did not
> do. Unsupported operations raise — they do not return `True`, `0`, or `[]`.

---

## 1. The contract

`MusicPlatformAdapter` lives in `music_transfer/core/ports/platform.py`. It is a
plain class whose methods raise `UnsupportedCapabilityError` by default, so a
new adapter implements only what its platform genuinely supports.

### Identity

| Member | Purpose |
|---|---|
| `platform` | The `Platform` enum member. |
| `capabilities` | The `PlatformCapabilities` declaration. |
| `get_profile()` | `AccountProfile` — display name and platform account id. |
| `as_account(owner_user_id=)` | Persistable `Account`. Holds **no** credentials. |

### Reads

| Member | Returns |
|---|---|
| `export_library(sections=, progress=)` | `LibrarySnapshot` |
| `get_liked_tracks(progress=)` | `list[Track]` |
| `get_saved_albums(progress=)` | `list[Album]` |
| `get_followed_artists(progress=)` | `list[Artist]` |
| `get_playlists(progress=)` | `list[Playlist]` |
| `get_playlist_items(playlist_id, progress=)` | `list[PlaylistItem]` |
| `get_destination_state(sections=)` | `DestinationState` (for resume reconciliation) |
| `playlist_item_ids(playlist_id)` | `list[str]` in playlist order |
| `search_track(track, limit=)` | `list[Track]` candidates |
| `search_album` / `search_artist` | candidates |

`DestinationState` carries a `is_trustworthy(section)` flag. If a platform
cannot cheaply list everything it holds, the adapter says so, and the engine
treats reconciliation as best-effort rather than authoritative.

### Writes (mutating, non-destructive)

`save_track`, `save_album`, `follow_artist`, `create_playlist`,
`add_playlist_item`, `add_playlist_items`, `create_folder`.

`create_playlist` **must** return the new identifier, or raise. It must not
return an empty string on an unconfirmed write — that is an
`AmbiguousOperationError`.

### Destructive operations (library management only)

`remove_track`, `remove_album`, `unfollow_artist`, `remove_video`,
`remove_mix`, `delete_playlist`, `delete_folder`.

These are **never** invoked by the transfer engine. They exist for the separate
cleanup workflow and are classified `DESTRUCTIVE`, so the confirmation gate in
the application layer always triggers for them.

### Identifier portability

```python
def can_reuse_identifier(self, entity_type: EntityType, source: Platform) -> bool:
```

Return `True` only when a source identifier is valid on this destination.

- Catalogue entities (tracks, albums, artists) are usually portable **within one
  platform**, so `TIDAL → TIDAL` reuse is safe and avoids a pointless search.
- **Playlists are account-owned.** A playlist id is never portable, even between
  two TIDAL accounts. The adapter returns `False` and the planner creates a new
  container.

  > This was a real bug found by the resume tests: `can_reuse_identifier`
  > returned `True` for playlists, so a resumed run skipped playlist creation
  > and then wrote entries into a container that did not exist.

---

## 2. Capability declaration

```python
class PlatformCapabilities:
    platform: Platform

    # reads
    read_liked_tracks: bool = False
    read_saved_albums: bool = False
    read_followed_artists: bool = False
    read_playlists: bool = False
    read_videos: bool = False
    read_mixes: bool = False
    read_folders: bool = False

    # non-destructive writes
    write_liked_tracks: bool = False
    ...

    # destructive operations (library management only)
    delete_liked_tracks: bool = False
    ...

    # search
    search_tracks: bool = False
    search_albums: bool = False
    search_artists: bool = False

    # behavioural traits
    preserves_custom_added_date: bool = False
    supports_playlist_duplicates: bool = True
    supports_already_exists_detection: bool = True
    supports_batch_playlist_writes: bool = False
    insertion_behavior: InsertionBehavior = InsertionBehavior.APPEND
    distinguishes_region_availability: bool = False
    exposes_isrc: bool = False
```

Two helpers keep the intent explicit at the call site:

```python
capabilities.require("write_playlist_items")   # raises if disabled
capabilities.supports("search_tracks")         # bool
```

### The traits that actually change behaviour

| Trait | Effect when `False` / `PREPEND` |
|---|---|
| `supports_playlist_duplicates` | Planner emits a `destination_deduplicates_playlists:` warning; the plan no longer promises `A, B, A`. |
| `insertion_behavior = PREPEND` | The executor writes in reverse so the **visible** order matches the requested logical order. |
| `exposes_isrc` | ISRC matching is skipped; the engine falls back to metadata scoring. |
| `supports_already_exists_detection` | Items that already exist become `SKIPPED` only after verification, not during planning. |
| `distinguishes_region_availability` | Region locks are reported as `NOT_FOUND` instead of `UNAVAILABLE`. |
| `supports_batch_playlist_writes` | `add_playlist_items` is used instead of N single calls. |

`TIDAL` declares `insertion_behavior=PREPEND` because TIDAL favourites put the
newest write on top. This is the whole reason the ordering layer separates
*requested logical order* from *write order*.

---

## 3. Operation classification

Every adapter method belongs to exactly one kind, declared as class-level sets
on `MusicPlatformAdapter`:

```python
OperationKind.READ         # safe, may run during planning
OperationKind.MUTATING     # needs confirmation
OperationKind.DESTRUCTIVE  # needs confirmation and an explicit user request
```

`operation_kind(adapter, "save_track")` resolves a method name to its kind.
`ReadOnlyAdapter` uses it to make writes structurally unreachable:

```python
planner.build(job, snapshot, ReadOnlyAdapter(destination))
# any mutating call → UnsupportedCapabilityError("read_only_adapter_write_blocked")
```

---

## 4. The TIDAL adapter

```
platforms/tidal/
├── client.py      raw tidalapi calls, retries, and pagination
├── mapper.py      tidalapi objects → universal domain models
├── errors.py      tidalapi exceptions → core error taxonomy
├── auth.py        OAuth session persistence; credentials go to the keyring
├── pagination.py  Invariant A: offset paging that does not stop early
└── adapter.py     MusicPlatformAdapter implementation + CAPABILITIES
```

Declared capabilities:

| Group | TIDAL |
|---|---|
| Reads | liked tracks, albums, artists, playlists, videos, mixes, folders — all `True` |
| Writes | all `True` |
| Destructive | all `True` (cleanup only, never transfer) |
| Search | tracks `True`; albums/artists `False` |
| Traits | `PREPEND`, duplicates supported, region-aware, ISRC exposed, no batch writes, no custom added-date |

### Pagination — Invariant A

> **A short non-empty page does NOT mean the end of the data.**

The original code contained this bug:

```python
# WRONG - stops after a 49-item page even though page_size is 100
if len(page) < page_size:
    return values
offset += len(page)        # WRONG - drifts when a page is short
```

`platforms/tidal/pagination.py` replaces it with:

1. Terminate only on an **empty** page (or a documented safe signal).
2. Advance the offset by the **requested** page size, not by `len(page)`.
3. Detect a repeated identical page (fingerprint) and raise instead of looping.
4. Bound the work with `max_pages` and `max_items`, raising a typed error when
   exceeded rather than silently truncating.

Pinned by the canonical regression:

```python
pages = {0: range(0, 100), 100: range(100, 149), 200: range(149, 249), 300: []}
values = fetch_all(getter, policy=PaginationPolicy(page_size=100))
assert len(values) == 249
assert getter.calls == [(100, 0), (100, 100), (100, 200), (100, 300)]
```

Fifteen tests cover this: six termination and ordering cases (the regression
above, alternating short/full pages, full-then-empty, many short pages, a
single short page, and the always-requested page size), five safety-bound
cases (page limit, item limit, repeated page, empty first page, exception
propagation), and three policy defaults.

---

## 5. Adding a new platform

1. **Add the enum member** — `Platform.SPOTIFY` and friends already exist.
2. **`platforms/<name>/client.py`** — thin, honest API client. No domain models
   yet, no retry policy of its own beyond transient-network handling.
3. **`platforms/<name>/mapper.py`** — map API objects to `Track`, `Album`,
   `Artist`, `Playlist`, `PlaylistItem`. Always set `source_platform` and
   `source_id`; put anything platform-specific into `metadata`, never into a
   first-class field.
4. **`platforms/<name>/errors.py`** — translate SDK exceptions into the core
   taxonomy (`core/errors.py`) so `classify_error()` can map them to a retryable
   or permanent failure.
5. **`platforms/<name>/auth.py`** — run the OAuth flow and store the resulting
   session in the keyring via `infrastructure/security/credentials.py`. Never
   accept or log a password.
6. **`platforms/<name>/adapter.py`** — implement the port, declare
   `CAPABILITIES`, and be conservative: `False` until proven.
7. **Register it** in `platforms/registry.py`.

No file under `core/`, `app/`, or `interfaces/` needs to change. If you find
yourself editing the core to add a platform, that is a design bug — add a
capability flag instead.

### Adapter checklist

- [ ] `CAPABILITIES` is conservative and matches reality.
- [ ] Unsupported methods are left raising `UnsupportedCapabilityError`.
- [ ] `create_playlist` returns an id or raises; never returns `""`.
- [ ] `can_reuse_identifier` returns `False` for playlists.
- [ ] Pagination terminates on an empty page only.
- [ ] Every SDK exception is translated; none escapes unclassified.
- [ ] No token, password, or session blob is logged.
- [ ] `export_library` reports partial sections instead of hiding them.

---

## 6. Placeholder platforms

`Platform.SPOTIFY`, `APPLE_MUSIC`, `DEEZER`, and `YOUTUBE_MUSIC` exist as enum
members only. Nothing pretends to implement them:

```
$ music-transfer capabilities spotify
⚠️ This platform is not implemented yet.
```

`registry.unimplemented(platform)` returns a factory that raises
`UnsupportedCapabilityError("platform_not_implemented")` — a deliberate,
loud refusal rather than an absence that looks like a bug.
