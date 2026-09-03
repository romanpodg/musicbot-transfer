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
  - Mandatory confirmed non-empty folder name check (`title`); rejects missing/whitespace name and never falls back to `source_id`.
  - Resolves parent destination ID via `resolved_parent_destination_id` (parent must be confirmed `TRANSFERRED`; fails closed if missing or parent is failed/ambiguous).
  - Dedicated recovery branch for persisted `IN_FLIGHT`: executes ONLY reconciliation and NEVER reaches `destination.create_folder()`.
  - Durably records `mutation_state = MutationState.IN_FLIGHT` before calling destination.
  - Calls `destination.create_folder(folder_name, parent_destination_id)`.
  - Records created `destination_id`, marks `TRANSFERRED`, and propagates runtime container destination ID to in-memory child folders and playlists.
  - On timeout or ambiguous error, reconciles against destination state matching exact `(parent_id, name)`.
  - Explicit reconciliation outcomes: `FolderReconciliationOutcome.RECOVERED` (1 match) or `FolderReconciliationOutcome.INCONCLUSIVE` (0 or >1 matches, incomplete readback, or readback error).
  - Zero destination matches does NOT authoritatively prove failure of a previous non-idempotent create; item transitions to `AMBIGUOUS` with 0 replay creates.
- Failure cascade isolation across restarts:
  - `blocked_containers` pre-seeds from persisted items with `status in (FAILED, AMBIGUOUS)` before execution begins.
  - All descendants (subfolders, playlists, and playlist items) are skipped immediately with `AMBIGUOUS` status and `container_blocked` (or `playlist_sequence_blocked` for entries).
  - Foldered playlists never fall back to root. Sibling subtrees continue normally.

### 4. Post-Transfer Verification
- Post-execution verification re-reads destination with `sections=("folders", "playlists")`.
- Transferred folders are verified for existence, matching title, and matching parent folder ID.
- Transferred playlists are verified for correct folder placement (`folder_id == container_destination_id`). Mismatch yields `playlist_folder_mismatch` and fails verification.
- Incomplete destination sections yield `VerificationStatus.PARTIAL`.
- Exact playlist item sequence and multiset duplicates verification preserved.

### 5. Platform-Neutral Core and Provider Adapter Alignment
- Universal root representation is strictly `None`; generic core logic (`planner`, `executor`, `verifier`) contains zero provider root sentinels (`"root"`, `""`). Legitimate identifiers named `"root"` remain ordinary identifiers.
- `can_reuse_identifier` strictly allows catalog entities (`TRACK`, `ALBUM`, `ARTIST`, `VIDEO`, `MIX`), returning `False` for `FOLDER`, `PLAYLIST`, and `PLAYLIST_ITEM`.
- Adapter translates universal `None` root to TIDAL SDK `"root"` parameter.
- Client and mapper normalize `"root"` or `""` to `None`.

## Verification Results
- 525 unit and legacy tests pass (30 Phase 1.5C tests in `tests/unit/test_playlist_folder_hierarchy.py` + 15 Phase 1.5C.1 tests in `tests/unit/test_folder_resume_safety.py`).
- 0 ruff errors.
- 0 platform conditionals in `core/` or `app/`.
