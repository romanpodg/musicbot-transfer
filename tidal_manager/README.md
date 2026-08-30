# TIDAL Library Manager

A safety-first, multilingual Python CLI for backing up, transferring, restoring, verifying, and cleaning a TIDAL library.

It uses [`tidalapi`](https://tidalapi.netlify.app/) for browser/device OAuth and library operations. No passwords are requested, saved, logged, or placed in project files.

## Requirements

- Python 3.12 or later
- A TIDAL account that can authorize the required library actions
- A terminal that supports Unicode for the full emoji experience

## Installation

From the `tidal_manager` directory:

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
python main.py
```

On first launch, choose English or Russian. The selection is saved in `config.json`.

If this project was already installed before the reliability update, run the
requirements command again to install `tqdm` for the live progress display.

## Diagnostics and dry runs

Run a local, credential-free readiness check before authorizing an account:

```powershell
python main.py --diagnostics
```

It checks the Python version, installed `tidalapi` client, configuration,
backup presence, writable log path, and local API client availability. It does
not contact TIDAL or attempt OAuth.

To estimate a cleanup without creating a queue or changing a TIDAL library:

```powershell
python main.py --dry-run
```

Choose **Clean library** and a scope. The CLI reads the selected sections and
prints the exact objects that would be removed. Transfer, restore, and cleanup
mutations are blocked while this mode is active.

## Example usage

1. Select **Create backup** and authorize the source account in the browser link shown by the CLI. The backup is written atomically to `data/backups/tidal_backup.json`.
2. Select **Transfer library**, authorize the source and destination accounts independently, choose an order, inspect the account summary, and approve the destination warning.
3. If a transfer is interrupted, start the application again and accept the resume prompt. The retained `data/state/transfer_state.json` contains library metadata and progress only—never OAuth tokens.
4. Select **Clean library** only after reviewing its exact count. The CLI offers a backup first, asks for an explicit yes/no decision, and then requires the exact `DELETE` phrase before it makes a deletion.
5. Cleanup writes an exact deletion queue before the first request. The terminal shows the current item, item count, errors, elapsed time, and bounded retry notices. Pressing `Ctrl+C` saves the queue and checkpoint safely; start the application again to continue.

## Safety and data handling

- OAuth tokens are stored only in the operating-system keyring when a usable keyring backend is available. If it is unavailable, the session is not persisted and the next run asks for OAuth again.
- `config.json`, backups, reports, logs, and transfer state do not contain passwords or OAuth credentials.
- Every remote mutation is confirmation-gated in both the UI and service layer. This includes transfer, restore, created playlists/folders, favorite additions, and cleanup.
- Every upstream request receives a 20-second timeout. Transient network, timeout, rate-limit, and 5xx failures are retried up to three times with exponential backoff. A playlist/folder creation or playlist-item add with an unknown response is not blindly repeated, because that could create a duplicate; its checkpoint remains for safe reconciliation or review.
- Backups contain only library metadata: favorite tracks, albums, artists, videos, mixes/radio, playlist folders, playlist metadata, and playlist media order.
- A backup or source snapshot that has an unreadable section is marked partial. Transfer and cleanup are blocked rather than treating a partial view as complete.
- Logs record event types and exception classes only; a redaction filter removes token-shaped data.

## Files produced at runtime

| File | Purpose |
| --- | --- |
| `data/backups/tidal_backup.json` | Versioned complete library backup when every section is readable |
| `data/state/transfer_state.json` | Atomic, credential-free transfer/restore checkpoint |
| `data/state/delete_state.json` | Atomic cleanup progress checkpoint (`completed`, `failed`, and `remaining`) |
| `data/state/delete_queue.json` | Exact deletion order and per-item `pending` / `processing` / `completed` / `failed` status |
| `data/reports/transfer_report.json` | Successful, failed, unavailable, skipped, and count comparison report |
| `data/logs/tidal_manager.log` | Rotated, sanitized authentication, transfer, cleanup, retry, error, and recovery events |

## Upstream capability notes

The project targets the maintained `tidalapi` 0.8 API. Its device/browser OAuth flow is used directly, and its supported favorites, mixes, playlist folders, and playlist APIs are called through `core/auth.py`. Availability can still vary by country, subscription, ownership, and TIDAL API behavior. Such items are recorded as unavailable or failed in the report; no raw provider response is exposed in the CLI or log.

Run deterministic checks without a TIDAL account:

```powershell
python -m unittest discover -s tests -v
python -m compileall -q .
```

The automated tests use fakes only; no real TIDAL account or library mutation
is needed. A real account smoke test remains optional and should begin with a
small, disposable playlist or the dry-run cleanup mode.
