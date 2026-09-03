# Phase 1.5C — Playlist Folder Hierarchy Transfer and Runtime Container Binding

## Status
Completed

## Objective
Provide safe, exact, platform-independent transfer of playlist folder hierarchies without introducing a separate user-selectable `ContentType.FOLDERS`. Folders are strictly structural components of `ContentType.PLAYLISTS`.

## Implementation Details

### 1. Structural Dependency and Selective Export
- `TransferContentSpec` augmented with `structural_dependencies: tuple[StructuralContentDependency, ...]`.
- `ContentType.PLAYLISTS` declares dependency on `"folders"`, requiring source `read_folders` and destination `create_folders`.
- `content_sections()` maintains presence-compatible primary sections (`("playlists",)`).
- `source_export_sections(content, source_capabilities)` derives source sections (`("folders", "playlists")` if source supports `read_folders`, else `("playlists",)`).

### 2. Validation and Topological Planning
- `validate_folder_hierarchy(folders, playlists)`:
  - Detects duplicate folder IDs.
  - Detects empty or missing folder IDs.
  - Validates parent references (no missing parents or self-parenting).
  - Detects cycles using graph traversal.
  - Verifies all foldered playlists point to existing folders.
  - Produces deterministic topological ordering (parents before children, ordered by depth).
- `TransferPlanner._plan_playlists()`:
  - Generates `TransferItem` with `operation=CREATE_FOLDER` and `entity_type=EntityType.FOLDER`.
  - Sets `container_source_id` to parent folder's source ID (or `None` for root).
  - Destination ID and `container_destination_id` remain `None` at planning time.
  - Binds playlists to their source folder ID via `container_source_id`.
  - Computes plan hash incorporating `container_source_id`.

### 3. Execution, Topological Ordering, and Runtime Container Binding
- Execution ordering strictly enforces 5 tiers:
  1. Non-container items
  2. Root folders (`container_source_id is None`)
  3. Nested child folders in topological order
  4. Playlists
  5. Playlist items (preserving sequential position)
- Destination folder creation (`_write_folder`):
  - Resolves parent destination ID (`None` for root, or looks up parent folder's runtime `destination_id`).
  - Fails closed if parent destination ID is unresolved.
  - Durably records `mutation_state = MutationState.IN_FLIGHT` before calling destination.
  - Calls `destination.create_folder(folder_name, parent_destination_id)`.
  - Records created `destination_id`, marks `TRANSFERRED`, and propagates runtime container destination ID to in-memory child folders and playlists.
  - On timeout or ambiguous error, reconciles against destination state matching exact `(parent_id, name)`. Single match recovers destination ID; 0 or >1 matches marks `AMBIGUOUS`.
- Failure cascade isolation:
  - When any folder fails or is ambiguous, its source ID and destination ID are added to `blocked_containers`.
  - All descendants (subfolders, playlists, and playlist items) are skipped immediately with `AMBIGUOUS` status and `container_blocked` (or `playlist_sequence_blocked` for entries).
  - Foldered playlists never fall back to root. Sibling subtrees continue normally.

### 4. Post-Transfer Verification
- Post-execution verification re-reads destination with `sections=("folders", "playlists")`.
- Transferred folders are verified for existence, matching title, and matching parent folder ID.
- Transferred playlists are verified for correct folder placement (`folder_id == container_destination_id`). Mismatch yields `playlist_folder_mismatch` and fails verification.
- Incomplete destination sections yield `VerificationStatus.PARTIAL`.
- Exact playlist item sequence and multiset duplicates verification preserved.

### 5. TIDAL Adapter and Mapper Alignment
- `can_reuse_identifier` strictly allows catalog entities (`TRACK`, `ALBUM`, `ARTIST`, `VIDEO`, `MIX`), returning `False` for `FOLDER`, `PLAYLIST`, and `PLAYLIST_ITEM`.
- Adapter translates universal `None` root to TIDAL SDK `"root"` parameter.
- Client and mapper normalize `"root"` or `""` to `None`.

## Verification Results
- 510 unit and legacy tests pass (30 dedicated Phase 1.5C tests in `tests/unit/test_playlist_folder_hierarchy.py`).
- 0 ruff errors.
- 0 platform conditionals in `core/` or `app/`.
