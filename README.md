# Finix: local encrypted profile backups

Stage 1: a standalone Python CLI/library, not yet an installed Hermes plugin.
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

- `config.yaml`, `.env`, `auth.json`, `auth_state.json`, `state.db`;
- regular files below `memories/` and `skills/`;
- `cron/jobs.json` only.

Everything else is excluded, including logs, caches, plugin installations/code, cron run output, other databases, nested profiles and working checkouts. Empty directories are not preserved. Skills are copied as inert files, never executed. SQLite `state.db` uses the backup API into RAM, incorporating committed WAL contents; raw WAL/SHM files are not copied. This is an allowlisted **profile backup**, not a server image or a guarantee that every third-party plugin's state is captured.

The archive includes a manifest with per-file SHA-256 and sizes, then gzip/tar encrypted by the standard `age` executable. Native age recipients and one native age identity are supported, not SSH identities or age plugins. Plaintext archives and SQLite snapshots never go to a temporary disk file. Plaintext file payloads are capped at 128 MiB, as are compressed and expanded archive buffers; tar overhead reduces the practical payload limit slightly. Maximum 10,000 files and 10,000 traversed directories, with at most 32 path components. Manifest JSON is capped at 8 MiB, cron JSON at 4 MiB and the local index at 1 MiB. Memory is bounded by a small number of these buffers plus parsed JSON objects, **not** a 128 MiB total RSS guarantee. Encrypted temporary files are mode 0600 and atomically published only after age succeeds; encrypted contents are checksum-read back before indexing and retention.

## Safety and limitations

- Source, state and output must be explicitly specified, disjoint directories with no symlink path components. Included source symlinks and special files are rejected. File descriptors and before/after metadata detect replacements/ordinary file races. A directory advisory lock prevents concurrent **Finix** operations for the same source, not writes by Hermes. For a quiet cross-file snapshot, pause the source application yourself: SQLite is individually consistent, but the profile as a whole is not a database transaction. Finix does not stop services.
- Filesystems and the owner of source/state/output/target-parent directories must be trusted. A hostile process with write access can race filesystem path checks, SQLite's separate WAL opens, or replace metadata. This CLI is not a sandbox against the local account owner. Avoid source writers during backup; retry if change detection rejects a snapshot.
- Whole-archive validation and decryption happen before restore staging. Traversal, absolute paths, duplicate entries, hardlinks, symlinks, special files, checksum mismatches, unknown files and oversized decompression are rejected. A private sibling staging directory is published using Linux `renameat2(NOREPLACE)`. No existing nonempty target is overwritten. Wrong keys or corrupt archives leave the destination unchanged. New directories are 0700; restored files and identities are 0600. Restore intentionally creates plaintext files in its private stage; an abrupt process/host crash can leave that stage for the owner to remove.
- Restored cron jobs in the current Hermes `{"jobs": [...]}` JSON format have `enabled=false`, `state="paused"`, `next_run_at=null` and a pause marker. Unsupported cron formats fail closed. Review jobs before explicitly enabling them in Hermes.
- Retention is **off unless `--keep N` is given**, with N at least 1. It only deletes checksum-matching files listed in this state directory's random namespace, after a new encrypted output was verified and indexed. Unindexed or foreign files are never pruned. Keep the state directory; losing it loses retention ownership, not decryptability. Errors during retention are reported as failures even if the new copy was saved. Crash leftovers may include encrypted temporary/unindexed files; no automatic cleanup is attempted. File fsync is used, but directory-entry durability across power loss is not guaranteed.
- SHA-256 readback checks local storage consistency, not decryption/recoverability: periodically perform an actual restore with the owner-held key. `status` verifies encrypted checksums; it never decrypts.

No cloud provider, browser key generator, Desktop UI, scheduling integration, SDK registration or production activation is implemented in this stage.
