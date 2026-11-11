# Backup, recovery and cloud safety

## Scope and consistency

Finix backs up selected profile files, not a whole OS image. It includes unknown
regular files, dotfiles, credentials, nested projects and their Git history.
The selected root's `cache`, `logs`, `tmp`, `temp`, `backups`, `profiles` and
`hermes-agent` trees are excluded. Dependency/cache trees such as `node_modules`,
`.venv`, `__pycache__`, `.cache` and `.pytest_cache` are excluded at every depth.
Exact regenerated runtime coordination records are also excluded; the authoritative
policy is in `finix/core.py`, shared by archive creation and admission. In
particular `skills/.usage.json` remains included. Do not assume a broad extension
filter. External folders require explicit canonical default-profile configuration;
a named profile cannot grant itself that authority.

Ordinary files are captured individually with bounded retries, not under a global
application transaction. Genuine SQLite is validated and copied transactionally,
including committed WAL content, but different databases do not share a transaction.
An ordinary file disappearing or persistently changing may be listed as omitted:
`complete=false` is **not a complete backup**, even if encrypted readback succeeds.
Incomplete/unknown provenance must not authorize deletion or setup scheduling.
Unsafe links/reparse points, corrupt SQLite, permission and I/O failures remain
fatal. Empty directories and original owners/modes/timestamps are not preserved.

Select an existing private `cache/finix/sqlite-temp` beneath the owning profile
for plaintext staging. Large ordinary captures also need this staging directory.
Linux: 0700 directory/0600 files. Windows: owner-only NTFS DACL, not chmod emulation.
Staging needs disk space; process/host crashes can leave plaintext behind. Normal
cleanup is not secure erasure, and the same OS account can read the profile anyway.
Finix does not configure disk encryption or prevent swap/core dumps.

## Recovery and retention

Only public native age recipients go to the backup host. Save and verify the
private recovery file on the owner's device; no private key upload is required.
An existing registered public recipient cannot be replaced through the UI, REST
or owner command, even with a confirmation checkbox. Clearing the renderer's key
session does not delete saved files or change that recipient.

`status` verifies ciphertext integrity, not decryptability or completeness. Perform
periodic real restores with the owner-held key to a new/empty, disjoint target.
Restore validates paths, types and hashes before publishing; abrupt crashes can
leave a private plaintext restore stage. Restored supported cron jobs are paused;
review before enabling. Extra-source v3 archives restore as a bundle containing
`profile/` and `external/<alias>/`, not to their original absolute locations.

Retention is opt-in and namespace/ledger-bound. Keep multiple independent copies.
Do not delete state or adopt unknown old files by name. An upload/readback error
leaves the local ciphertext and must prevent pruning. Interrupted transfers may
leave remote unindexed objects; no automatic orphan sweeper is promised. A new
run may start a fresh upload rather than resume the interrupted session.

## Cloud setup

Desktop's primary workflow and `hermes finix` use explicit connection consent,
read-only verification, separate Apply, then a real backup/readback check before
scheduling. Authenticated host APIs are required; do not expose a standalone
public Finix server. Private owner chat also requires configured stable owner ID
and platform; groups and absent actor identity do not confer ownership.

Supported implementation paths include local storage, Google Workspace (existing
credential-owning helper), own-OAuth Drive, and rclone Yandex Disk/OneDrive/Dropbox.
`connector-drive` is experimental and fails closed until actual authenticated
schema/availability is admitted. OneDrive Business/SharePoint and live provider
compatibility are not certified by this release build.

Workspace needs the target Hermes runtime's trusted Workspace helper and
`google-auth-oauthlib`, `google-auth`, `google-api-python-client`; standalone Finix
pip installation does not provide them. First-time Google login requires an
owner-created Desktop OAuth client, enabled Drive API and test users where
applicable. Set it up through that Workspace integration, never send client JSON
or tokens through chat. Existing grants must already have sufficient Drive scope.
The rclone connection worker expects the reviewed noninteractive config state
protocol; the historical implementation target is v1.75.2, not a newly verified
binary in this packaging run. OAuth may grant broad whole-drive rights. Existing
rclone config is trusted, owner-only and outside the backed-up profile; local
readiness does not prove account identity, network availability or valid tokens.

Choose `local` only with a browser on the backend machine, `server-browser` for
an explicitly available backend browser, or `tunnel` for an owner-managed SSH
local forward. Google callback uses loopback 53683; rclone uses 53682. For a remote
backend run the relevant forward on the device containing the browser:

```sh
ssh -N -L 127.0.0.1:53683:127.0.0.1:53683 USER@SERVER
# rclone instead:
ssh -N -L 127.0.0.1:53682:127.0.0.1:53682 USER@SERVER
```

Finix does not create the SSH tunnel or public callback infrastructure. A phone
needs a compatible forwarding client or an explicitly chosen server browser.
Open only the validated authorization-start URL. Enter passwords/2FA only at the
provider, never paste callback URLs, codes, config or tokens into Finix/chat.
After cancellation/restart an unapplied candidate cannot be resumed. Crashes may
leave owner-private candidate files and provider grants needing manual review;
do not delete an active connection's config.

Save public recovery metadata (profile/folder/object IDs, descriptors, hashes)
off-host alongside your recovery plan. It is not a key but can reveal account
metadata. Keep authorization for the recovery account separately available.
Ciphertext readback and authorization success never replace a real restore test.
