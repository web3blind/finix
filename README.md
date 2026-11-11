# Finix — encrypted Hermes profile backups

Standalone Python CLI, native Hermes plugin, Desktop pane and offline browser
recovery-key generator. Linux and **native Windows on NTFS** use distinct
filesystem protections. WSL testing does not establish native Windows support.

**Experimental beta, not a complete restoration product.** Finix first-party
code is [MIT licensed](LICENSE), copyright 2026 Denis Skripnik. All third-party
notices and licenses are preserved in [THIRD-PARTY-NOTICES.md](THIRD-PARTY-NOTICES.md).

## Current verification status

- The publication demo is Linux-only. A real Linux encrypted backup and complete
  readback have been verified. This does not prove full live default restoration.
- Native Windows decryption/archive verification succeeded, but the actual Desktop
  cold-preview request timed out before a plan or Apply. No completed Desktop
  replacement, restored-history or restored-note claim is made.
- Default recovery is enabled in this reviewed code, but remains experimental and
  limited to the guarded inert/default-only ownership contract. Feature gates,
  role checks, consent, fail-closed ownership checks and safety fences remain on.
- Unknown preview acknowledgements require manual reopen/reconciliation, not
  automatic replay. Interrupted legacy operations remain blocked; do not discard
  retained source, pending state or recovery artifacts to force a retry.
- Linux cloud restoration has not passed. Neither a universal compatibility claim
  nor all-features-green claim is made. Keep independent backups and test offline.

Native integration targets Hermes plugin SDK **0.23.0**. This is a required SDK
contract, not evidence of compatibility with every Hermes build or OS.
See [publication status](docs/PUBLICATION-STATUS.md) for the current limits.

## Install and use

- [Linux quickstart](docs/INSTALL-LINUX.md)
- [Native Windows quickstart](docs/INSTALL-WINDOWS.md)
- [Backup, restore and cloud safety](docs/SAFETY.md)
- [Hermes three-stage setup](docs/setup-flow.md)
- [Desktop SDK contract](desktop/SDK-CONTRACT.md)
- [Test restore, new-profile import and explicit activation](docs/RESTORE.md)

Python **3.11–3.14**, **PyYAML >=6,<7**, and separately installed `age` and
`age-keygen` are required. Pip installs PyYAML as a runtime dependency. Optional
rclone and Google Workspace dependencies are not bundled. Nothing downloads
executables at runtime. Test in a separate virtual environment, not a running
Hermes installation.

Desktop setup: save and verify the recovery file locally; register only its public
recipient; connect and explicitly apply cloud storage; check the first backup;
then choose a schedule. An existing registered public recipient is immutable.
Keep the private recovery file off the backup host. A lost key cannot be recovered.
Recovery is a separate explicitly consented operation: the selected key is sent
only to the same-machine authenticated local backend as a bounded multipart
upload, then private staging is cleaned. No key is sent during generation/setup.

## Existing shared-root recovery condition

Ordinary recovery through an already-running root multiplexer requires that
another process of the same OS user **does not concurrently replace the new
recovered TARGET profile**. Finix checks source authority, native root/target
identity, root PID generation/instance/epoch and the original served roster at
defined boundaries, including before the serve write and after managed cleanup.
These are not atomic target/name binding through SDK resolution. Changed or
uncertain checks interrupt and preserve the target; they do not stop the shared
root, retry import or grant historical receipts runtime authority. Versioned
receipts state `cooperative-root-boundaries-v1` and `atomic_target_binding:false`.
The Desktop warning appears before choosing a fresh key where the backend knows
this geometry. No extra controls or SDK changes are required. Unknown frontend
routing geometry is still a host-capability limitation, not permission for a
private bridge or an unsafe fallback. Physical default Apply is enabled only when the reviewed ownership,
consent and inert/default-only gates admit it; live end-to-end acceptance is not complete.

## Verification boundaries

Do **not** run broad `unittest discover -s tests` with real cloud tools on PATH.
Historical fixtures use POSIX permissions, shebang scripts or HTTP sockets and
are not portable Windows/offline tests. Use `python scripts/release/offline_gate.py`
for the narrow, network-denied packaging gate. See [the test matrix](docs/RELEASE.md).
Native age round trips, Linux permissions, NTFS ACL/link checks, real Desktop
pickers/accessibility and live-provider authorization are separate gates. A build
is not evidence that they passed. The UI still contains Russian labels; a fully
English UI is not claimed. These installation documents are English.
