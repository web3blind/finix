# Finix — encrypted Hermes profile backups

Standalone Python CLI, native Hermes plugin, Desktop pane and offline browser
recovery-key generator. Linux and **native Windows on NTFS** use distinct
filesystem protections. WSL testing does not establish native Windows support.

**Experimental beta, not a complete restoration product.** Finix first-party
code is [MIT licensed](LICENSE), copyright 2026 Denis Skripnik. All third-party
notices and licenses are preserved in [THIRD-PARTY-NOTICES.md](THIRD-PARTY-NOTICES.md).

## Current verification status

The 2026-11-14 development acceptance completed **private-cloud physical-default
recovery on Windows Desktop and Linux CLI** with Finix 0.1.0, reviewed build B11,
and Hermes SDK 0.23.0 (`2bd378ed1b8d6bf2192cb26e0a55ff3091e6bd19`).
These were isolated synthetic genuine SDK defaults, not personal profiles or
named-profile substitutes. The transport was explicitly selected **Google Drive
through the pinned Google Workspace skill bridge**, not independent `finix:google`.
Windows recovery reused a cloud archive produced and verified by an earlier
0.1.0 candidate; Linux created a fresh B11 backup before its B11 recovery.

| Tested surface | Completed workflow and verified result |
| --- | --- |
| Native Windows Desktop, local NTFS | Encrypted cloud backup/readback, explicit cloud-copy selection, download/decryption, fresh PREVIEW and separate Apply, restart to `ready`; 3 original conversations / 12 messages, memory and garden notes and garden SQLite rows restored. The restored conversation opened in the reopened native Desktop. |
| Ubuntu Linux CLI, unprivileged user | The same cloud/default workflow reached `ready`; 6 conversations / 12 messages, saved note, original SQLite proof and garden note/database restored. Stock SDK history/list/resume worked without an AI request. |

Post-backup mutations disappeared and SQLite integrity checks returned `ok`.
These are development acceptance results for the identified package and host,
not an independent security certification or support for every SDK version.

- **The Workspace bridge does not satisfy the SDK-only review requirement.** It
  invokes a pinned external skill helper. The declared Hermes-managed
  `finix:google` / `drive.file` path exists but still needs its own complete
  authorization-lifecycle and Windows/Linux recovery acceptance with the skill absent.
- New-profile import/activation remains blocked with
  `HOST_IMPORT_CONTRACT_UNSUPPORTED`; the verified in-place default replacement
  does not establish that separate feature.
- Default Apply remains restricted to admitted idle, inert/default-only ownership
  and fresh root/plan-bound consent. Never delete retained state or replay Apply
  after an uncertain acknowledgement.
- A subsequent independent gateway restart can leave a saved historical maintenance
  hint requiring manual reconciliation. Restored data remains available; a fully
  seamless status/next-maintenance experience after owner changes is not claimed.

See [publication status](docs/PUBLICATION-STATUS.md) for exact package identity,
transport boundaries, verification provenance and remaining limitations.
Keep independent backups and test in an isolated environment first.

## Install and use

- [Linux quickstart](docs/INSTALL-LINUX.md)
- [Native Windows quickstart](docs/INSTALL-WINDOWS.md)
- [Backup, restore and cloud safety](docs/SAFETY.md)
- [Hermes three-stage setup](docs/setup-flow.md)
- [Desktop SDK contract](desktop/SDK-CONTRACT.md)
- [Test restore, new-profile import and explicit activation](docs/RESTORE.md)

Python **3.11–3.14**, the declared Python dependencies, and separately installed
`age` and `age-keygen` are required. Optional rclone is not bundled. The default
Google Drive transport uses the declared Finix OAuth client and Hermes' public
credentialed request API. An explicitly selected alternative uses the compatible
[Google Workspace skill as authorization/API transport](docs/google-workspace-skill.md);
Finix still owns encryption, journals, full downloaded-byte verification and restore.
It neither reads nor copies the skill's credentials. Google client libraries for
this optional transport are declared in the package and native plugin manifests.
The skill alternative is a separate compatibility contract, not fulfillment of the
original Finix-OAuth-only review requirement. Nothing downloads executables at
runtime. Test in a separate virtual environment, not a running Hermes installation.

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
consent and inert/default-only gates admit it. The cloud-backed Windows and Linux
physical-default workflows above passed those gates; they do not validate the
separate shared-root/new-profile publication contract.

## Verification boundaries

Do **not** run broad `unittest discover -s tests` with real cloud tools on PATH.
Historical fixtures use POSIX permissions, shebang scripts or HTTP sockets and
are not portable Windows/offline tests. Use `python scripts/release/offline_gate.py`
for the narrow, network-denied packaging gate. See [the test matrix](docs/RELEASE.md).
Native age round trips, Linux permissions, NTFS ACL/link checks, real Desktop
pickers/accessibility and live-provider authorization are separate gates. A build
is not evidence that they passed. English is the default and fallback UI language;
Russian remains an explicit option. The installation documents are English.
