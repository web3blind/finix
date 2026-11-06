# Finix: owner-controlled encrypted Hermes backups

Standalone Python CLI, native Hermes plugin, Desktop pane and offline browser key generator.
Requires **Linux**, Python 3.11–3.14, `age` and `age-keygen` on PATH. The local CLI has no runtime Python dependencies. Optional Google login additionally uses the owning Hermes runtime's `google-auth-oauthlib` and Google API libraries; optional rclone login uses a reviewed `rclone` on PATH. Run from this checkout, or install the package in a separate virtual environment. Never install it into a running Hermes environment for testing.

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

All regular files below the explicitly selected `--source` profile are included by default: unknown root files, third-party plugin stores, arbitrary project folders (including `ai-projects/` **inside** the profile), dotfiles, credentials and project `.git/` history. There is no file-extension filter; an owner's `.log` file outside the excluded root `logs/` is included. Configured external paths and sibling projects outside the profile are never followed.

The small, explicit policy is defined once by `ROOT_EXCLUSIONS`, `TREE_EXCLUSIONS` and `excluded()` in `finix/core.py` and enforced both during discovery and v2 restore:

- **At the profile root only:** `cache/`, `logs/`, `tmp/`, `temp/`, `backups/`, `profiles/`, `hermes-agent/`. These are cache/log/temp/backup trees, other profiles, and the regenerable Hermes installation checkout. A nested project `backups/`, `tmp/` or `logs/` is not excluded by these root-only names.
- **At every depth:** `__pycache__/`, `.cache/`, `.pytest_cache/`, `node_modules/`, `.venv/`, `venv/`. Recreate these named cache/dependency environments after recovery. These exact names are reserved even if used as regular files; no broader extension or generated-project filter applies.

The default/main profile naturally includes its root `shared/` directory (including shared authorization state). It excludes `profiles/**`. A named profile backs up only its captured home, never its parent or the main profile's `shared/`, even when the gateway was launched from the default profile. An external `HERMES_SHARED_AUTH_DIR` override is **not** an additional source. Restoring a main-profile copy restores `shared/` under the fresh target, not into a live common directory.

Empty directories, original ownership/modes/timestamps and filesystem links are not preserved: included symlinks and special files fail the whole backup rather than being followed or silently lost. Excluded trees are not traversed. Skills and plugins are inert archive bytes, never imported during backup/restore. Every SQLite file is detected by its header at any location/extension and snapshotted through the backup API, incorporating committed WAL contents; validated raw WAL/SHM/journal companions are omitted only for actual SQLite databases. Unrelated suffix-named files are retained. This is a **selected-profile file backup**, not a server image.

New archives use `finix-2`, with tar metadata at `manifest.json` and profile payloads under `profile/`; a user's own root `manifest.json` is therefore preserved. Restore still accepts `finix-1` archives with their original frozen allowlist. Arbitrary safe relative paths are admitted only by v2. Older Finix readers cannot restore v2 archives.

The archive includes a manifest with per-file SHA-256 and sizes, then gzip/tar encrypted by the standard `age` executable. Native age recipients and one native age identity are supported, not SSH identities or age plugins. Production backup and recovery stream file/archive/ciphertext payloads; there is no fixed 128 MiB archive or per-file cap. Metadata count/depth/JSON limits remain security controls, not archive-size limits. Provider quotas, free local space and filesystem limits still apply. Unsupported paths, unsafe file types, invalid metadata or readback errors fail rather than silently omit data; failure cannot authorize pruning.

### Large SQLite databases: private temporary snapshots

With the owner's explicit approval, SQLite consistency snapshots may be **plaintext temporary files** on the backup host, as with the existing cron workflow. Snapshot directories are private mode 0700 and files mode 0600; they are cleaned on normal completion, cancellation and handled errors. A process kill, host crash or power loss can leave a temporary plaintext snapshot behind. Disk encryption is recommended but is not a mandatory Finix readiness gate. The final archive is always encrypted by age before publication or cloud upload. No existing production cron job is modified by this setting.

```sh
python -m finix backup --source /profiles/example \
  --output /backups/example --state /backup-state/example \
  --recipient age1... --sqlite-temp-dir /PRIVATE/finix-temp
```

`--sqlite-temp-dir` is optional; for profile/scheduled backups use `/finix configure sqlite_temp_dir /PRIVATE/finix-temp` to select an existing owner-owned mode-0700 directory outside the source profile. `/finix status` reports `sqlite_temp.ready` and `sqlite_temp.code` using local filesystem checks only, without reading customer SQLite files, inspecting encryption keys or accessing a cloud account. This orchestration setting is outside the profile archive; configure it again on a replacement backup host. Finix does not create encrypted mounts or obtain disk encryption keys. Large databases require enough free temporary disk space; there is no fixed 128 MiB archive/file cap. Cloud transport staging contains only ciphertext.

## Safety and limitations

- Source, state and output must be explicitly specified, disjoint directories with no symlink path components. Included source symlinks and special files are rejected. File descriptors and before/after metadata detect replacements/ordinary file races. A directory advisory lock prevents concurrent **Finix** operations for the same source, not writes by Hermes. For a quiet cross-file snapshot, pause the source application yourself: SQLite is individually consistent, but the profile as a whole is not a database transaction. Finix does not stop services.
- Filesystems and the owner of source/state/output/target-parent directories must be trusted. A hostile process with write access can race filesystem path checks, SQLite's separate WAL opens, or replace metadata. This CLI is not a sandbox against the local account owner. Avoid source writers during backup; retry if change detection rejects a snapshot.
- Whole-archive validation and decryption happen before restore staging. Traversal, absolute paths, duplicate entries, hardlinks, symlinks, special files, checksum mismatches, excluded/out-of-contract files and oversized decompression are rejected. A private sibling staging directory is published using Linux `renameat2(NOREPLACE)`. No existing nonempty target is overwritten. Wrong keys or corrupt archives leave the destination unchanged. New directories are 0700; restored files and identities are 0600. Restore intentionally creates plaintext files in its private stage; an abrupt process/host crash can leave that stage for the owner to remove.
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
The provider's `Location` is an upload capability: ciphertext is sent in bounded
8 MiB raw PUT chunks **without an Authorization header**, with exact `Content-Range`
and contiguous server `Range` acknowledgements. The last chunk must return a final object ID.
Finix neither obtains nor reconstructs a bearer token. URI validation permits only
HTTPS `www.googleapis.com:443`, the exact upload path and the documented
`uploadType=resumable` plus `upload_id` query; foreign hosts, credentials, fragments,
extra/duplicate query keys and redirects fail closed. The session URI stays in memory,
never settings, return values or diagnostics. The raw stdlib HTTP client does not
use environment proxies/netrc or HTTP request logging. No new runtime dependency.

Each backup is one standard `application/octet-stream` Drive file, not base64 in
metadata. Ciphertext is staged in private files and streamed in 8 MiB chunks; there is no Finix whole-archive size cap. Recovery and verification use bounded HTTP Range GET requests and require exact 206 status, Content-Range, byte count and final SHA-256. The owning Hermes JSON-only credential helper buffers each HTTP response before returning it: normal conforming Drive responses are one chunk, but its transport does not expose a pre-buffer body limit for a server that ignores Range. This SDK boundary is not a total-process-RSS guarantee. Raw PUT JSON responses are bounded while reading. Initiation/metadata requests use a 20-second timeout; individual media requests use 120-second inactivity bounds. Creation and ambiguous chunk uploads are not retried. Managed GET/PATCH/DELETE retry transient status codes at most three times. Interrupted sessions are not persisted/resumed across runs; the next backup starts a fresh session. A 308 is accepted only for an intermediate chunk with the exact contiguous Range acknowledgement, never as completed storage. Redirects fail closed.

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

## Managed connector admission (not an available Google storage backend)

`connector-drive` is a **discovery-gated experimental path**, not a claim that
Hermes currently offers a managed googledrive connector. The current public
website catalogue does not establish that availability; a source-code hint is
not evidence. Only the authenticated gateway's dynamic `list_connectors` result
can admit discovery. Local storage remains the default; `drive` above remains
the separate own-OAuth implementation. This is neither Google Workspace nor an
optional MCP/plugin-catalog installation.

Explicit owner settings are `storage=connector-drive`, `connector_account`
(connection ID), `connector_alias`, and `connector_folder`. Use the existing
`/finix configure KEY VALUE` handler. No default/newest account is selected.
`/finix status` and the authenticated status route report `managed_connector`
with `ready`, `code`, and actionable `action` fields. `NO_TOKEN` means there is
no usable profile-local managed authorization; Finix starts no identity/login.
`CONNECTOR_UNAVAILABLE` means the actual catalogue does not advertise enabled
`googledrive`; `NO_SCHEMA` and `UNSUPPORTED_SCHEMA` are hard execution blocks.
The reviewed production schema/codec registry is deliberately empty: **live
Google storage is not implemented/admitted until authenticated schema and output
semantics have been reviewed**. No vendor tool names or input mappings are guessed.

The SDK default auth reader can replace retired guest identities and fall back
to global auth. Finix does not use it. Its supported header-provider seam delegates
to the SDK's read-only token probe only with an explicit profile-local managed
token scope; no bearer is extracted or persisted by the adapter. An auth-store-only
configuration returns `AUTH_POLICY_UNSUPPORTED` rather than borrowing root auth or
refreshing/creating an identity. Do not put tokens in chat. Profile home, secret
scope and multiplex scope are bound together for every adapter operation, including
scheduler calls. This auth-policy limitation is an additional deployment gate.

The exercised synthetic codec uses the actual `ConnectorClient` wire validation,
account lookup, search/schemas, `execute(PlannedCall)`, `stage_file`, `put_file` and
streamed `get_file`. The transfer engine caps ciphertext at the gateway's **20 MiB**,
checks owner/folder/snapshot/size/SHA-256 by remote metadata and binary readback,
and only then journals successful objects and permits retention. Retention is
limited to 1000 journal-owned records in the exact profile/account/alias/folder;
foreign objects and corrupt readbacks block deletion. URL hosts must be pinned by
a reviewed codec; presigned transfer refuses redirects and ambient netrc/proxies.
There are no extra application retries; SDK timeouts/retries are bounded per
request, not a total wall-clock deadline. Crash leftovers are not swept.

Future admitted managed recovery uses a saved public upload record JSON:

```sh
python -m finix.connector_drive status --home /ABSOLUTE/HERMES_PROFILE \
  --profile-id ORIGINAL_ID --account CONNECTION_ID --alias EXACT_ALIAS --folder FOLDER_ID
python -m finix.connector_drive download --home /ABSOLUTE/HERMES_PROFILE \
  --profile-id ORIGINAL_ID --account CONNECTION_ID --alias EXACT_ALIAS --folder FOLDER_ID \
  --object OBJECT_ID --descriptor /safe/public-upload-record.json --output /safe/recovered.age
python -m finix restore --archive /safe/recovered.age \
  --identity /safe/recovery.key --target /safe/new-profile
```

The first commands currently stop at the documented admission gates. Synthetic
SDK tests exercise download and exclusive mode-0600 output, not live Google.
Readback verifies stored ciphertext, not decryptability. Keep the private recovery
key off the server and perform a real restore periodically.

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

## Google Workspace backend (`workspace`)

This is an additional implemented storage path, separate from `drive` (Finix's
own OAuth grant) and the currently fail-closed `connector-drive` scaffold. Existing
local/Drive behavior is unchanged. It reuses the selected Hermes profile's
existing Google Workspace authorization through `scripts/google_api.py`; the
storage adapter never extracts or copies credentials or starts OAuth. A separate
explicit owner connection wizard below can create a new credential-owning grant.
It never revokes a grant or creates a fallback identity. Existing Workspace setup
must already include Drive write permissions. Gmail-only grants are insufficient;
a permission failure asks the owner to review Workspace authorization separately.

```text
/finix configure storage workspace
/finix status
/finix backup
# Retention remains disabled until explicitly configured:
/finix configure retention 5
```

`status.workspace` performs **local checks only**: trusted helper presence,
Python client dependency availability and profile-local token **file presence**.
`LOCAL_READY` does not mean valid auth, sufficient scopes, reachable Drive or a
successful live-account test (`live_google_verified` is always false here).
No `setup.py --check`, token refresh or API request is made by status/configure.
The helper is discovered in the selected profile's installed Workspace skill,
then in the Hermes checkout's bundled skill; it is not accepted from a URL or
remote settings. The existing Workspace runtime needs `google.auth` and
`googleapiclient` even if `gws` exists: upload/download use Python media clients.
Finix's small subprocess entry point imports that helper fresh for each call,
forces its Python client path, and extends metadata fields with `trashed` for
retention readback. Credential loading/refresh stays inside the Workspace helper.

The child has an explicit owning `HERMES_HOME` and minimal OS environment (no
ambient credential variables, `PYTHONPATH`, `HERMES_GWS_BIN` or profile dotenv
overlay). Control calls have a 120-second wall-clock limit; upload/download have a separate
configurable runner transfer deadline (default 24 hours), not the short metadata deadline.
Each call has a private process group and 64 KiB combined stdout/stderr limit.
Downloads apply an exact descriptor-sized file-size resource limit, not a fixed archive cap.
Google media clients use explicit 8 MiB upload/download chunks.
Raw provider diagnostics are not returned to chat/logs. Only age ciphertext is
staged on disk, under a private temporary directory. Large or timed-out transfers
fail safely; their remote orphan may require manual owner cleanup.

Each backend identity maintains `workspace/workspace.json` outside the source
profile under its Finix data directory. It records the exact newly-created folder
ID, owner metadata and verified file IDs, sizes and SHA-256 values. There is no
filename/prefix-based ownership, search pagination or adoption of old objects.
Losing the ledger creates a fresh folder namespace and leaves old objects alone.
Changing folder ownership, file parent/MIME/owner/size or ciphertext blocks
rotation. At most 1000 records are admitted; retention zero is disabled.
After uploading, Finix checks fresh metadata and downloads/hashes the full binary
before journaling it. Only ledger-owned older objects can then be moved to trash
(reversible, never permanent), with another fresh metadata/binary readback and
trash-state verification. Each completed prune is journaled so partial failures,
restarts and already-missing/trash objects do not authorize deletion elsewhere.
The newest verified copy is protected. Cloud/upload/readback errors preserve local
ciphertext and prevent local retention; retention errors are still reported.

Every successful profile backup returns `workspace` public recovery metadata and
saves it next to the local ciphertext as `*.age.workspace.json`. Save that JSON
on the recovery device along with the private key (the JSON is not a key and
contains no credentials, but includes file/folder IDs and owner email metadata).
It is essential when the server/ledger is lost. After arranging existing
Workspace access on the recovery host, download independently:

```sh
python -m finix.workspace_drive status --home /ABSOLUTE/HERMES_PROFILE \
  --profile-id ORIGINAL_PROFILE_ID
python -m finix.workspace_drive download --home /ABSOLUTE/HERMES_PROFILE \
  --profile-id ORIGINAL_PROFILE_ID --record /safe/copy.age.workspace.json \
  --output /safe/recovered.age
python -m finix restore --archive /safe/recovered.age \
  --identity /safe/recovery.key --target /safe/new-profile
```

The recovery command needs no original Finix state and exclusively creates a
new mode-0600 output; it never overwrites a file. It verifies metadata, expected
size and SHA-256 before publishing. Recovery uses the explicitly supplied
original profile ID from the saved record, not a newly initialized server's ID.
Ciphertext checks are not a substitute for a periodic full restore.

`tests/test_workspace_drive.py` exercises real age backup → synthetic Workspace
file transport → download → restore, profile-handler wiring, corruption/foreign
metadata, missing ledger, partial retention/restart, minimal profile environment,
timeout/output/file-size bounds, no-network readiness, plus the actual installed
public helper's resumable-upload contract with a fake service. All account data
in these tests is synthetic. **No real Google account smoke, auth, activation or
publication was performed.**

## Подключение облака: Desktop, CLI и личный чат

Откройте **Finix → Подключение облака** в включённом Desktop-плагине.
`/finix connect` показывает команды. В личном чате с настроенным владельцем
можно запустить тот же credential-owning workflow, без Desktop:

```text
/finix connect yandex tunnel confirm
/finix connect status ID
/finix connect choose ID 0
/finix connect apply ID
/finix connect cancel ID
```

Вместо `yandex`: `workspace`, `onedrive`, `dropbox`; вместо `tunnel`: `local`
(браузер на машине backend) или `server-browser` (браузер агента/владельца на
сервере). `confirm` — явное согласие на возможные широкие права диска, включая
чтение/изменение/удаление. Команда возвращает публичный `id`, состояние и, когда
готово, **только проверенную ссылку начала авторизации**: Google provider URL
либо локальный rclone `/auth?state=…`. Она не содержит выданного токена/кода.
`ID` берите из ответа; `choose` нужен только для предложенного выбора диска
OneDrive (индексы `id` начинаются с 0). Старт не ожидает окончания входа: выполните
`status ID` повторно. Только `verified` разрешает отдельный `apply ID`.
Группы, чужие пользователи/платформы, старые handle и дополнительные поля
отклоняются. Не отправляйте callback URL, код, пароль, config или token в чат.
CSRF ticket остаётся приватным в backend, не возвращается chat-командой.

Автономный CLI не требует работающего бота; профиль всегда указывается явно:

```sh
python -m finix connect yandex --home /ABSOLUTE/HERMES_PROFILE --mode tunnel --confirm
# Локальный браузер на этой же машине — только по явному флагу:
python -m finix connect workspace --home /ABSOLUTE/HERMES_PROFILE --mode local --confirm --open-browser
```

CLI печатает безопасные JSON-состояния, автоматически обновляет статус и остаётся
одним процессом до завершения/отмены. В stdin разрешены только `status`,
`choose NUMBER`, `apply`, `cancel` — никаких кодов или URL. Браузер автоматически
не открывается без `--open-browser`; этот флаг запрещён для `tunnel`, поскольку
открыл бы браузер не на том устройстве. EOF/выход отменяет неприменённый candidate.
После `apply` настройки сохраняются, CLI завершается; отдельный процесс не может
продолжить старую незавершённую сессию. Далее доступны `/finix backup` или
профильный scheduler; независимый CLI `backup` выше остаётся локальным архиватором.

1. «Обновить подключение» связывает UI с текущим backend/profile.
2. Выберите **Google Workspace / Яндекс Диск / OneDrive / Dropbox**. Для rclone
   задайте относительную папку (по умолчанию `Finix`); Workspace использует
   существующий профильный folder workflow. Новое подключение не заменяет
   выбранное хранилище автоматически.
3. Подтвердите возможные широкие права (чтение/изменение/удаление всего диска)
   и что браузер достигает loopback backend: на той же машине или через
   собственный уже настроенный SSH-туннель.
4. «Начать подключение» → «Обновить подключение» → «Открыть вход в браузере».
   Введите пароль только на сайте провайдера, не в Finix/чате. Вернитесь и
   обновите статус. OneDrive выбирает обычный `onedrive` route, предлагает
   список дисков; SharePoint/site/search/free-form token questions fail closed.
5. Только после `verified` (read-only cloud probe) нажмите **«Применить
   подключение»**. UI проверяет readback. Затем отдельно настройте public age
   recipient и выполните `/finix backup`: именно backup проверяет передачу
   ciphertext и полный SHA-256 readback. Вход не загружает тестовые данные,
   не удаляет файлы и не включает расписание.

**Google Workspace:** существующее подключение этого профиля проверяется через
credential-owning Workspace helper и используется без нового grant/сужения
scopes. Первый вход требует Desktop OAuth client JSON владельца: создайте client
в Google Cloud Console, включите Drive API, при Testing добавьте свой аккаунт в
Test users. Сохраните JSON локально через настоящий Workspace
`skills/productivity/google-workspace/scripts/setup.py --client-secret FILE`
в нужном owning profile (явный `HERMES_HOME`). Не отправляйте содержимое JSON
агенту. Мастер требует `google-auth-oauthlib`, `google-auth`,
`google-api-python-client` в Hermes runtime; ничего сам не устанавливает.
Workspace helper и зависимости обязательны до начала входа и повторно проверяются
перед применением; при их исчезновении текущие настройки/токен не меняются.
Новый grant запрашивает Drive scope, не Gmail/Calendar, с `prompt=consent`.
Если Google не вернул refresh token, мастер не подтверждает подключение:
`WORKSPACE_REFRESH_TOKEN_REQUIRED` означает повторить вход с согласием на
автономный доступ; не переносите refresh token из другого grant. Сохранённый
candidate проверяется тем же authorized-user loader, что использует Workspace.
Токен создаётся сначала
в приватном candidate, только явное применение эксклюзивно добавляет
`google_token.json`; существующий токен никогда не перезаписывается.
Legacy `setup.py --auth-url` / `--auth-code CODE` (localhost:1 и code в argv)
**не** используется мастером.

**rclone:** поддержан текущий протокол v1.75.2 `config create --non-interactive`
→ `Option/State` → `config update --continue --state/--result` → пустой State.
`config_auth_no_browser=true` передаётся как config option; флаг
`authorize --auth-no-open-browser` не является флагом `config create`.
У каждого подключения отдельный 0700 candidate/0600 config и ledger namespace;
старые credentials/ledger не перезаписываются. Config и provider stdout/stderr
остаются внутри child process; UI получает только ограниченные статусы,
подтверждённую auth URL и выбор диска. Grants не принимаются как API fields.

**Границы:** API монтируется только существующим authenticated Hermes host
через `/api/plugins/finix/connection`; не запускайте standalone public server.
Read-issued 5-minute UI ticket дополнительно защищает POST от CSRF; profile ID,
random session ID, SDK activation epoch, single active callback and settings CAS
отсекают старые профили/сессии. Ссылка не записывается в localStorage/настройки
или общий status; личный chat может сохранять её в своей истории, поэтому
не пересылайте её. Максимум 5 минут на session; отмена
завершает process group и удаляет неприменённый candidate. Перезапуск инвалидирует
session, child имеет hard timeout. Crash может оставить private candidate:
автоматического credential sweeper нет; не удаляйте config активного подключения.
Авторизация у провайдера после отмены может требовать ручного отзыва grant.

**Удалённый сервер / телефон:** режим `tunnel` даёт точную SSH-команду для порта
текущего провайдера. Выполните её **на устройстве с браузером**, заменив только
`USER@SERVER` своим адресом:

```sh
# Яндекс Диск / OneDrive / Dropbox:
ssh -N -L 127.0.0.1:53682:127.0.0.1:53682 USER@SERVER
# Google Workspace (фиксированный настоящий loopback callback):
ssh -N -L 127.0.0.1:53683:127.0.0.1:53683 USER@SERVER
```

Дождитесь успешного SSH-подключения без ошибки занятого порта, затем откройте
ссылку из `status ID` в браузере этого устройства. Держите SSH до `verified`.
На телефоне нужен SSH-клиент с таким же local forward; без него выберите
`server-browser` и попросите агента открыть безопасную start URL на сервере.
Пароль/2FA вводятся только на сайте провайдера через защищённый Vault, не моделью
из чата. Finix не создаёт SSH/публичный туннель, не открывает сетевой listener
наружу и не требует нового HTTPS callback, helper service или переноса grant.
Порты заняты — соединение отклоняется, случайный порт не подставляется.

**Проверка:** реальные rclone v1.75.2 pre-auth state transitions и старт/отмена
loopback callback для трёх backend без обращения к провайдерам; synthetic child
+ настоящий HTTP callback с wrong-state rejection, private token staging,
explicit apply/CAS/cancel/expiry/restart/profile/CSRF checks. Google использует
реальную google-auth-oauthlib loopback/code exchange с локальным synthetic token
issuer, fake Drive probe; никакого настоящего Google account. Desktop проверяется
исполняемым собранным ESM через public SDK contracts. Live accounts, реальный
Desktop OS browser и телефон не проверялись; cloud permissions/quotas/VPN policies
остаются live-account gates.

## Optional existing rclone connections (`rclone`)

Implemented for configured **Yandex Disk (`yandex`), OneDrive Personal
(`onedrive`) and Dropbox (`dropbox`)** remotes. This is a subprocess storage
adapter. You can reuse an already authorized config as described here, or use
the new local connection wizard below. Supply a reviewed rclone executable on
the plugin host's PATH. Nothing downloads or installs rclone at runtime. The
storage adapter does not read token values; the wizard's isolated child owns
OAuth and its private candidate config. Built-in rclone app IDs can be used
where the provider permits them; a custom OAuth application is not inherently
required. Remote/headless authorization is a separate owner setup prerequisite.
See the official [Yandex](https://rclone.org/yandex/),
[OneDrive](https://rclone.org/onedrive/), [Dropbox](https://rclone.org/dropbox/)
and [remote setup](https://rclone.org/remote_setup/) documentation.

Select all three settings explicitly (quotes are supported by the owner-only
slash parser). The config must be an absolute nonsymlink regular file owned by
the service user, mode **0600**, in a service-user-owned **0700** directory,
**outside the backed-up Hermes profile**. Finix inspects filesystem metadata,
not config contents. Never paste config contents or a password in chat.

```text
/finix configure rclone_config /OWNER/PRIVATE/rclone/rclone.conf
/finix configure rclone_remote mydisk
/finix configure rclone_root Backups/Finix
/finix configure storage rclone
/finix status
/finix backup
# Optional, disabled by default; deletion semantics are provider-dependent:
/finix configure retention 5
```

`status.rclone` invokes only `listremotes --json`, locally; `LOCAL_READY` means
that the selected file-defined remote has an admitted backend type, **not** that
its credentials/scopes/account/network work. No cloud listing/probe runs on
status/configure. OneDrive Personal is the tested target contract; Finix does
not parse `drive_type` or validate the account tier from local discovery. Select
the correct existing Personal remote yourself; Business/SharePoint are not
claimed as verified. Missing binaries/config/selection and unsupported backend
types fail without login or fallback. Password-encrypted rclone configs are
currently unsupported for unattended execution: stdin is closed,
`--ask-password=false`, and inherited `RCLONE_CONFIG_PASS`/password commands are
not accepted. A future secure unlock integration must be explicit.

The selected config is a **trusted, credential-owning account boundary**, not
an untrusted input format. Finix binds the owning random profile ID, exact config
path/private parent device+inode, remote name/type and relative root in its
private `rclone/rclone.json` ledger. The config file itself may be atomically
replaced by rclone's normal OAuth refresh. Finix intentionally does not fingerprint
tokens; changing the account behind the same configured name is not detected by
local readiness. Keep that config owner-controlled and dedicated to the intended
account. Changing a ledger-bound selection fails closed; to intentionally start
a new selection, disable scheduling, preserve the old ledger/recovery records
and move the old `rclone` state directory aside before the next manual backup.
Never merge/adopt old objects by filename. A lost ledger starts a new random
namespace and cannot prune previous copies.

Only native age-header ciphertext crosses the storage boundary; there is no fixed
Finix archive-size cap. Upload is fixed-argv `copyto --immutable` to a random UUID object in a
random namespace; exact-file `lsjson --stat` checks name/type/size, then `cat`
reads back the actual complete bytes and verifies size/SHA-256 before journaling
or pruning. SHA-256 is computed by Finix, not inferred from provider hashes.
Only journal-owned older objects may be deleted with `deletefile`, after another
fresh metadata and ciphertext check; deletion is read back and progress persisted.
Missing old objects are reconciled across interrupted retention. There is no
sync, purge, mount, serve, rcd, recursive cleanup, prefix-based deletion or orphan
sweeper. **Deletion may be permanent depending on backend/account policy; no
universal trash/reversibility promise is made.** Remote/root restrictions constrain
Finix's commands, not the scope of the underlying cloud authorization.

Each process uses an absolute nonsymlink owner/root-owned executable without
group/world write access, no shell, a minimal environment without ambient cloud
credentials or `RCLONE_*` overrides, closed stdin, isolated process group,
120-second control deadline and separate configurable runner transfer deadline
(default 24 hours). `cat` streams stdout directly into a private file, stops before
writing bytes beyond the exact descriptor size, and rejects short downloads.
Other output and stderr are capped at 64 KiB each. File hashing/copying uses
1 MiB blocks; neither upload nor download materializes whole ciphertext in RAM. Provider diagnostics are withheld. Upload stages contain
only mode-0600 ciphertext. Local copies remain on upload/readback/retention
failure; remote unindexed orphans can remain after interruption. Retention caps
the private ledger at 1000 objects and protects the latest verified upload.

Save the returned public `rclone` record (also written next to a successful
profile backup as `*.age.rclone.json`) away from the old server. It contains the
original profile ID, remote/root/type, random namespace/object name, size and
SHA-256, **not config path, credentials or recovery key**. On a fresh machine,
arrange your own existing rclone authorization with the same remote name/root,
then run independent recovery without an original Finix ledger or Hermes:

```sh
python -m finix.rclone_storage status --home /SAFE/EXISTING/HOME \
  --profile-id ORIGINAL_PROFILE_ID --config /PRIVATE/rclone/rclone.conf \
  --remote mydisk --root Backups/Finix
python -m finix.rclone_storage download --home /SAFE/EXISTING/HOME \
  --profile-id ORIGINAL_PROFILE_ID --config /PRIVATE/rclone/rclone.conf \
  --remote mydisk --root Backups/Finix --record /safe/copy.age.rclone.json \
  --output /safe/recovered.age
python -m finix restore --archive /safe/recovered.age \
  --identity /safe/recovery.key --target /safe/new-profile
```

Output is exclusively created mode 0600, never overwritten, only after matching
the saved descriptor. Ciphertext readback does not prove decryptability; perform
periodic real restores with the owner-held identity.

### Dependency review and local-only verification

Development used the official **rclone v1.75.2**, source tag commit
`be8d6044eacb843b74d335db0c5fbe09222031fe`, in isolated scratch only. Reviewed
surface: MIT `COPYING`, listremotes JSON schema (name/type/source, no tokens),
config/password handling and private config writes, cat/stat/copy/delete command
semantics and official provider/headless-auth docs. This is **not** a complete
source/binary security audit. To avoid a large source build, the official pinned
Linux amd64 ZIP was downloaded directly, checked against official HTTPS
`SHA256SUMS`, inspected for traversal/symlinks, then only its binary extracted;
no installer, global install, startup hook or automatic update was used.
Checksum agreement is not an independently verified release signature.

- Source: `https://github.com/rclone/rclone/tree/v1.75.2`
- ZIP: `https://downloads.rclone.org/v1.75.2/rclone-v1.75.2-linux-amd64.zip`
- ZIP SHA-256: `349ac8fba6ff65d6247043f1750cdcb518ec5d500ef91463a10d37c0ccdf3702`
- Extracted executable SHA-256: `ed7ab1f17876bb006c3df3949a3044959671afd9112362dacaa5dd1647735bc1`

rclone is an optional external dependency, not bundled in this package. No
credentials, binaries or upstream source checkout are committed. To run the
opt-in real local-only tests with a previously reviewed absolute executable:

```sh
FINIX_RCLONE_BINARY=/ABSOLUTE/REVIEWED/rclone \
  python -W error::ResourceWarning -m unittest discover -s tests -p test_rclone_storage.py -v
```

The smoke uses an isolated synthetic `local` remote and explicitly injected test
transport (production rejects `local`): real age backup → real rclone copy/stat/cat
→ verified download → real age restore with byte comparison, plus real retention.
It also tests real noninteractive encrypted-config failure. Synthetic contracts
cover command selection, ledger loss/binding, foreign files, corruption,
interrupted retention, owner handler, recovery CLI overwrite refusal, limits and
minimal environment. **No real Yandex/OneDrive/Dropbox authorization or account
operation, production activation or publication has been performed.**

Large transport verification additionally covers a real local-only rclone upload/readback/recovery of 129 MiB + 137 bytes, with Python traced allocations below 12 MiB, exact short/oversend rejection, and multi-chunk own-OAuth HTTP upload/Range GET against a synthetic local server. These are not live-provider quota or account tests. The unavailable experimental managed connector retains its actual 20 MiB gateway limit and is not an unlimited backend.
