# ADR 0008: Playlist Folder Hierarchy Transfer and Runtime Container Binding

## Status
Accepted

## Context
Playlists on platforms such as TIDAL can be organized into a nested hierarchy of folders. Previous phases treated playlists as flat collections in the destination account root or lacked support for transferring and reconstructing the folder structure. Furthermore, folder support had to be designed without turning folders into an independent `ContentType` selectable by users, as folders exist solely to organize playlists.

Previous architecture also exposed a vulnerability where `can_reuse_identifier` returned `True` for `EntityType.PLAYLIST` or `EntityType.FOLDER`, or where child items relied on planning-time synthetic destination IDs rather than confirmed runtime destination IDs.

## Decisions

### 1. Folders as Structural Component of PLAYLISTS
`ContentType.FOLDERS` is explicitly **not** added to the system. Folders are structural components of `ContentType.PLAYLISTS`. `EntityType.FOLDER` is the internal executable entity type operated upon via `TransferOperation.CREATE_FOLDER`.
When users request `ContentType.PLAYLISTS`, the transfer engine inspects source platform capabilities (`source.capabilities.read_folders`). If supported, folder hierarchy is exported alongside playlists via selective export (`source_export_sections`).

### 2. Selective Source Export via Structural Content Dependencies
`TransferContentSpec` declares `structural_dependencies`. For `ContentType.PLAYLISTS`, this registers the dependency on the `"folders"` snapshot section, requiring source `read_folders` and destination `create_folders` (only when source contains folders).
`content_sections()` continues to return presence-compatible primary sections (`("playlists",)`), ensuring presence checks are not broken, while `source_export_sections()` derives `("folders", "playlists")` for source reading.

### 3. Universal Core Root (`None`) vs Provider-Specific Root
The core transfer engine uses `None` as the universal root representation for unparented folders and playlists.
Provider-specific root identifiers (such as TIDAL's `"root"` string) are strictly confined to the platform adapter boundary. Mappers normalize provider roots to `None` upon ingress, and adapters translate `None` back to the provider representation on egress. Core, planner, executor, and verifier contain zero provider root strings or platform-name checks.

### 4. Runtime Destination Binding and Topological Order
Destination folder IDs cannot be synthesized at planning time. In confirmed plans, folder items define `container_source_id` pointing to the source parent folder ID, and `destination_id=None`.
Items execute in topological order across 5 tiers:
1. Non-containers (`TRACK`, `ALBUM`, `ARTIST`, `VIDEO`, `MIX`)
2. Root folders (`container_source_id is None`)
3. Child folders in topological depth order (parents before children)
4. Playlists
5. Playlist items (preserving sequential position)

When a parent folder is created on the destination, its runtime destination ID is persisted and propagated to child folders and child playlists in memory. Playlists are created with their resolved parent destination folder ID. Foldered playlists **never** fall back to the root if their parent folder fails.

### 5. Failure Cascade Isolation and Container Blocking
If a folder creation fails or is ambiguous, its source ID and destination ID are added to `blocked_containers`. All downstream child folders, playlists, and playlist items are immediately blocked with status `AMBIGUOUS` and reason `container_blocked` (or `playlist_sequence_blocked` for playlist entries), preventing orphan generation or ordering corruption. Sibling subtrees remain unaffected.

### 6. Durable Intent and Idempotent Folder Reconciliation
Before executing `create_folder`, the item's mutation intent `MutationState.IN_FLIGHT` is durably persisted. If the remote call errors or times out:
- Destination state is inspected via `destination.export_library(sections=("folders",))`.
- Matching by exact `(parent_id, name)`:
  - Exactly 1 match: Recover destination ID, mark `TRANSFERRED`, clear `mutation_state`.
  - 0 or >1 matches: Mark `AMBIGUOUS`, do not blindly retry.

### 7. Reusable Catalog Allowlist for Platform Identifiers
`can_reuse_identifier` strictly employs an explicit allowlist of reusable catalog entities: `{TRACK, ALBUM, ARTIST, VIDEO, MIX}`. Account-owned containers (`FOLDER`, `PLAYLIST`) and position-dependent entries (`PLAYLIST_ITEM`) strictly return `False`.

### 8. Post-Transfer Folder Hierarchy and Placement Verification
Post-execution verification re-reads the destination via `export_library(sections=("folders", "playlists"))`.
- Verification is job-scoped and does not assume an empty destination.
- Each transferred folder is verified for existence, matching title, and matching parent folder ID.
- Each transferred playlist is verified for correct folder placement (`folder_id` matching `container_destination_id`). Any mismatch fails verification with `playlist_folder_mismatch`.
- Incomplete folder or playlist sections aggregate to `VerificationStatus.PARTIAL`.
- Exact sequence and multiset duplicate verification for playlist tracks remains strictly intact.

## Consequences
- Folder hierarchies transfer safely and losslessly across platforms supporting folder creation.
- Non-folder destinations safely accept flat playlists without error.
- Zero platform conditionals exist in `core/` or `app/`.
- Network drops during folder creation never cause duplicate folders on destination.
