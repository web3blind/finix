# Finix: owner-controlled encrypted Hermes backups

Standalone Python CLI, native Hermes plugin, Desktop pane and offline browser key generator.
Requires **Linux**, Python 3.11–3.14, `age` and `age-keygen` on PATH. No runtime Python dependencies. Run from this checkout, or install the package in a separate virtual environment. Never install it into a running Hermes environment for testing.

```sh
# Run on the owner's device. Only the public recipient is printed.
python -m finix keygen --identity /safe/existing-directory/recovery.key

# Use that public age1… recipient on the backup host. No identity is needed.
python -m finix backup --source /profiles/example \
  --output /backups/example --state /backup-state/example \
  --recipient age1... --keep 5

python -m finix status --output /backups/example --state /backup-state/example

# On the recovery host, with the saved identity file; target must be new or empty.
python -m finix restore --archive /backups/example/finix-...age \
  --identity /safe/existing-directory/recovery.key --target /profiles/restored

python -m unittest discover -s tests -v
```

Use actual public recipients and filenames in place of placeholders. Commands return JSON, nonzero status on failure, and do not print identity contents or source file contents. `keygen` never replaces an existing file. Keep the identity off the backup host; losing it makes recovery impossible. Backups contain credentials, so protect the identity, restored directory and host. Finix does not securely erase process memory or prevent OS swap/core dumps.

## Included data

An explicit allowlist, relative to `--source`:

- Canonical profile settings/auth, session/memory/response and other explicitly named databases;
- `memories/`, `skills/`, `plugins/`, `plugin-data/`, `desktop-plugins/`, `vault/`, `secrets/`, `sessions/`, pairing and selected Kanban state;
- `cron/jobs.json`, cron execution DB and selected gateway state.

The exact authoritative allowlist is `FILE_ROOTS`, `TREE_ROOTS`, `EXACT_FILES` in `finix/core.py`. Configured external paths are not followed.

Everything outside that allowlist is excluded, including logs, caches, nested profiles and working checkouts. Regenerable plugin source dependency/cache trees (`node_modules/`, `.git/`, `__pycache__/`, virtualenvs and similar build caches) are excluded; reinstall dependencies after recovery. Configured external paths are not included. Empty directories are not preserved. Skills and plugins are inert archive bytes, never imported during backup/restore. Included SQLite databases (including nested plugin DBs) use the backup API into RAM, incorporating committed WAL contents; raw WAL/SHM files are not copied. This is an allowlisted **profile backup**, not a server image or a guarantee that every third-party plugin's state is captured.

The archive includes a manifest with per-file SHA-256 and sizes, then gzip/tar encrypted by the standard `age` executable. Native age recipients and one native age identity are supported, not SSH identities or age plugins. Plaintext archives and SQLite snapshots never go to a temporary disk file. Plaintext file payloads are capped at 128 MiB, as are compressed and expanded archive buffers; tar overhead reduces the practical payload limit slightly. Maximum 10,000 files and 10,000 traversed directories, with at most 32 path components. Manifest JSON is capped at 8 MiB, cron JSON at 4 MiB and the local index at 1 MiB. Memory is bounded by a small number of these buffers plus parsed JSON objects, **not** a 128 MiB total RSS guarantee. Encrypted temporary files are mode 0600 and atomically published only after age succeeds; encrypted contents are checksum-read back before indexing and retention.

## Safety and limitations

- Source, state and output must be explicitly specified, disjoint directories with no symlink path components. Included source symlinks and special files are rejected. File descriptors and before/after metadata detect replacements/ordinary file races. A directory advisory lock prevents concurrent **Finix** operations for the same source, not writes by Hermes. For a quiet cross-file snapshot, pause the source application yourself: SQLite is individually consistent, but the profile as a whole is not a database transaction. Finix does not stop services.
- Filesystems and the owner of source/state/output/target-parent directories must be trusted. A hostile process with write access can race filesystem path checks, SQLite's separate WAL opens, or replace metadata. This CLI is not a sandbox against the local account owner. Avoid source writers during backup; retry if change detection rejects a snapshot.
- Whole-archive validation and decryption happen before restore staging. Traversal, absolute paths, duplicate entries, hardlinks, symlinks, special files, checksum mismatches, unknown files and oversized decompression are rejected. A private sibling staging directory is published using Linux `renameat2(NOREPLACE)`. No existing nonempty target is overwritten. Wrong keys or corrupt archives leave the destination unchanged. New directories are 0700; restored files and identities are 0600. Restore intentionally creates plaintext files in its private stage; an abrupt process/host crash can leave that stage for the owner to remove.
- Restored supported cron jobs (`{"jobs": [...]}`, bare list, or id-keyed object) have `enabled=false`, `state="paused"`, `next_run_at=null` and a pause marker. Unsupported cron formats fail closed. Review jobs before explicitly enabling them in Hermes.
- Retention is **off unless `--keep N` is given**, with N at least 1. It only deletes checksum-matching files listed in this state directory's random namespace, after a new encrypted output was verified and indexed. Unindexed or foreign files are never pruned. Keep the state directory; losing it loses retention ownership, not decryptability. Errors during retention are reported as failures even if the new copy was saved. Crash leftovers may include encrypted temporary/unindexed files; no automatic cleanup is attempted. File fsync is used, but directory-entry durability across power loss is not guaranteed.
- SHA-256 readback checks local storage consistency, not decryption/recoverability: periodically perform an actual restore with the owner-held key. `status` verifies encrypted checksums; it never decrypts.

## Native plugin installation and ownership

Nothing is activated by this repository. Review the code before installing. For a **local directory install**, copy the payload into the selected
profile’s `plugins/finix` directory without overwriting an existing installation. The native
payload must retain `plugin.yaml`, root `__init__.py`, `finix/`, `dashboard/`,
`desktop/plugin.js` and `dist/keygen.html` together. The source directory is
already a complete native plugin payload. Do not copy only `finix/`.

A wheel built with `python -m build --wheel` contains both the standalone CLI and
the complete payload under `<environment>/share/finix/`. Build/install in a
separate environment managed by Hermes PM, never mutate the running Hermes Python
with raw pip. For example, after isolated wheel installation, copy the complete
`<environment>/share/finix` directory to the intended profile’s `plugins/finix`
using `shutil.copytree` (which refuses an existing target). The current
`hermes plugins install` command documents Git/catalog sources, so no unverified
local-directory argument is assumed here.
`hermes plugins enable finix` opts in to loading. No plugin is enabled by the build.
The package does not install cron, systemd units or modify Hermes source.

In the intended profile's `config.yaml`, set stable transport identity locally:

```yaml
plugins:
  enabled: [finix]  # preserve your other enabled entries
  entries:
    finix:
      settings:
        owner_id: "YOUR_NUMERIC_TELEGRAM_USER_ID"
        owner_platform: telegram
```

All chat operations, even status, default-deny without both settings and matching
adapter-supplied actor/platform. A group participant's name or chat text cannot
grant permission. Local slash calls lacking a transport actor also default-deny;
use the standalone CLI or authenticated Desktop instead. Local OS and authenticated
Hermes dashboard/Desktop access are owner boundaries, not shared-group membership.

```text
/finix setup
/finix status
/finix recipient age1PUBLIC none
/finix recipient age1NEW age1OLD confirm
/finix configure storage local
/finix configure retention 5
/finix configure interval_hours 24
/finix backup
/finix restore
```

`setup` points to the shipped HTML and authenticated
`/api/plugins/finix/keygen.html` download, not a Telegram attachment. Open the saved
HTML on your own device, offline. Never send a private identity to a command or
model. `restore` only gives independent CLI instructions; chat cannot accept an
identity. Replacing a chat recipient needs the exact previous public recipient
and literal `confirm`. Desktop uses authenticated `/status` plus atomic
`POST /recipient {profile_id,recipient,expected_recipient}`. Its UI separately
requires replacement confirmation. Extra fields, arbitrary profile paths, stale
recipient and wrong profile/backend IDs are rejected. The binding is a random
persistent backend ID outside the backed-up profile, **not** the label `default`
or a path hash: identical paths on different servers do not share a binding.

Plugin settings/copies live in the private sibling directory
`<profile-parent>/finix-data/<sha256(canonical-profile-path)>`, outside the source.
Save the public `/finix status` record with the backup location/Drive folder and
original `profile_id`. This orchestration state is intentionally not restored
with profile files. Its loss disables scheduling and changes the backend binding.

## Automatic plugin scheduling

Scheduling invokes no model. `interval_hours=0` disables it; retention `0` disables
pruning. Values are bounded (hours 0–8760, retention 0–1000). After the owner sets
`interval_hours`, the installed native plugin runs a lifecycle-owned worker while
its Hermes host stays running. No separate service is required. Each plugin load
captures its canonical profile and binds both backup paths and managed OAuth to
that profile, independent of later profile switches. Checks run every 60 seconds;
loading the plugin does not immediately start an overdue backup. Unconfigured
plugins do not initialize backup state from the worker.

Unload/disable signals cancellation and waits up to one second. No new checks
start afterward. An already admitted backup drains nonpreemptively: publication
and retention cannot safely be interrupted. If it is still running after the
bounded wait, a warning explicitly reports that it is draining. Network timeouts
are inactivity bounds, not a total job deadline. Source locks fence replacement
workers and manual backups. Do not assume disabling has killed an active backup.

Durable next-run state survives restarts. One completed backup moves next run
forward; missed intervals never cause a catch-up flood. Errors produce a generic
warning and retry no faster than the 60-second poll. Setting interval to zero
prevents future scheduled backups but does not interrupt an active one.

The standalone scheduler remains available when you want scheduling independent
of the plugin host (do not normally run both):

```sh
cd /ABSOLUTE/PROFILE/plugins/finix
HERMES_HOME=/ABSOLUTE/PROFILE /PATH/TO/HERMES/PYTHON -m finix.scheduler
# One due check (safe if schedule is disabled): add --once
```

Run from the installed native payload inside the intended profile's `plugins/`:
Hermes restricts credentialed requests to installed plugin code. Uninstalled
development imports do not start a lifecycle worker or obtain a credential grant.

## Google Drive: standard resumable media adapter

Requires a user-owned Google OAuth client project, Drive API enabled, and appropriate
loopback redirect setup. Provide `FINIX_GOOGLE_CLIENT_ID` and
`FINIX_GOOGLE_CLIENT_SECRET` through Hermes's secure environment setup, then from
the selected profile's owner terminal:

```sh
hermes auth add finix:google
hermes auth status finix:google
```

These are the documented Hermes-managed OAuth commands. Finix never reads auth
files, copies refresh tokens, receives bearer tokens or starts interactive login
from a chat/scheduler. Hermes stores/refreshes this plugin's own grant. The only
scope is `drive.file`; the only credentialed API host is HTTPS
`www.googleapis.com`. The adapter creates a dedicated folder tagged with the
original random Finix profile ID and validates that binding on use. It does not
request broad access to arbitrary Drive folders. Set `/finix configure storage drive`
after signing in. `folder_id` is persisted immediately after creation.

The adapter follows Google's [official resumable-upload flow](https://developers.google.com/workspace/drive/api/guides/manage-uploads):
managed OAuth initiates a JSON metadata POST to `/upload/drive/v3/files?uploadType=resumable`.
The provider's `Location` is an upload capability: the ciphertext is sent with one
raw PUT **without an Authorization header**, as in Google's single-request example.
Finix neither obtains nor reconstructs a bearer token. URI validation permits only
HTTPS `www.googleapis.com:443`, the exact upload path and the documented
`uploadType=resumable` plus `upload_id` query; foreign hosts, credentials, fragments,
extra/duplicate query keys and redirects fail closed. The session URI stays in memory,
never settings, return values or diagnostics. The raw stdlib HTTP client does not
use environment proxies/netrc or HTTP request logging. No new runtime dependency.

Each backup is one standard `application/octet-stream` Drive file, not base64 in
metadata. Maximum ciphertext is the core encrypted-input allowance: **128 MiB +
1 MiB age-overhead allowance**. This does not increase the core's plaintext/archive
limits. The full ciphertext is buffered for upload and managed binary GET readback;
there is no total-RSS guarantee. Adapter response bounds apply after the managed
SDK has buffered the body, not at its transport layer. Raw PUT metadata responses
are bounded while reading. Initiation/metadata requests use a 20-second timeout;
media PUT/GET use 120 seconds (socket/network inactivity bounds, not a wall-clock
job deadline). Creation and raw PUT are not retried after ambiguous errors.
Managed GET/PATCH/DELETE retry transient HTTP errors at most three times with
bounded backoff. Interrupted sessions are not persisted or resumed across runs;
a later backup starts a fresh session. A 308 incomplete upload is a failure, not
successful storage. All requests refuse redirects.

The file starts tagged `pending`. Managed `GET files/id?alt=media` reads binary
bytes, verifies whole-ciphertext length and SHA-256, then a metadata PATCH marks
it `snapshot`; exact committed metadata is read back before retention. This
verifies storage, **not decryption**. A new encrypted local copy is preserved on
cloud failure; no retention runs after an upload/readback/commit failure. Retention
only considers committed snapshots in the owned folder, pins the current upload
regardless of stale ordering, and validates each candidate's ID, MIME type, parent,
owner, snapshot ID and size/hash descriptor before deletion. Inventory is capped
at 1000 snapshots. Unrelated objects are not deleted. A missing candidate or 404
after an owned-file DELETE is safely reconciled; successful deletion is checked
by metadata readback, so partially completed cleanup can restart. Other retention
failures remain errors even when the new backup was already stored. Failed uploads
may leave pending files/sessions; no orphan sweeper is claimed. The former synthetic
`finix-drive-1` metadata protocol is not supported by this adapter.

To recover cloud ciphertext on a fresh machine: install Hermes plus Finix, sign in
to the same Google account/client, then from the installed native payload run:

```sh
python -m finix.drive --profile-id ORIGINAL_PUBLIC_PROFILE_ID \
  --folder SAVED_FOLDER_ID --object SAVED_OBJECT_ID --output /safe/recovered.age
# Then independent restore; Hermes is not needed for this local decrypt step.
python -m finix restore --archive /safe/recovered.age \
  --identity /safe/recovery.key --target /safe/new-profile
```

Only app-created/authorized files visible to this OAuth client can be selected;
knowing an arbitrary folder ID grants nothing. Save folder/object/profile IDs
outside the lost server. Download output must not exist and is created mode 0600.
No real Google-account smoke has been performed: user client setup/sign-in remains
a blocker. Controlled local HTTP tests exercise the real SDK request helper with
only a synthetic grant resolver and redirected test transport, not real OAuth.

## Verification and remaining deployment gates

Run Python tests from this repository with the Hermes checkout available on
`PYTHONPATH` and a disposable `HERMES_HOME` (never your real profile). The standalone
core itself remains importable without Hermes. `tests/test_drive.py` uses a local
HTTP server, actual installed-plugin call-stack attribution, synthetic auth and
real HTTP sockets. `tests/test_integration.py` covers real SDK registration,
FastAPI readback/CAS, owner checks, durable schedule/retention and an actual
synthetic encrypted backup → decrypt/restore round trip.

Browser and Desktop suites/build commands are in `web/` and
`desktop/SDK-CONTRACT.md`. Desktop bundle and offline HTML are shipped, not loaded
from CDN. Native Desktop OS-picker testing, production plugin enablement,
real-account OAuth/Drive verification and a production scheduler are separate
unperformed gates. Nothing has been published or activated in a live profile.
