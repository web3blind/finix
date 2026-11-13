# Finix — encrypted Hermes profile backups

Standalone Python CLI, native Hermes plugin, Desktop pane and offline browser
recovery-key generator. Linux and **native Windows on NTFS** use distinct
filesystem protections. WSL testing does not establish native Windows support.

**Experimental beta, not a complete restoration product.** Finix first-party
code is [MIT licensed](LICENSE), copyright 2026 Denis Skripnik. All third-party
notices and licenses are preserved in [THIRD-PARTY-NOTICES.md](THIRD-PARTY-NOTICES.md).

## Current verification status

- **Linux local physical-default recovery is functionally verified** on an isolated,
  synthetic real-default root using the exact reviewed native package. One fresh
  preparation and one Apply reached a new READY owner. The stock session picker
  returned the original three sessions and history returned six messages; the
  saved note was restored and SQLite integrity was `ok`. Old-only memories/skills
  were removed, shared resources remained unchanged, and the existing ciphertext
  was reused without recapture or a package-manager clone.
- Durable bounded preparation is implemented in the registered CLI and shipped
  Desktop protocol. Exact-job polling, cancellation and lost-acknowledgement
  reconciliation do not replay preparation or Apply. This protocol's component
  tests are not native Windows Desktop acceptance.
- Native Windows archive decryption was verified previously; its historical
  Desktop cold preview timed out before Plan/Apply. The new native Windows UI,
  physical-default Apply, restored-history and restored-note workflow remains
  **pending**, not proven fixed by the Linux result.
- New-profile import/activation is currently blocked with
  `HOST_IMPORT_CONTRACT_UNSUPPORTED`: the public SDK importer does not provide
  the required publication contract. Finix does not replace Hermes modules or
  mutate the interpreter's global temporary-directory setting. This restriction
  does not disable inert extraction or the separate guarded in-place default
  replacement; its final integrated acceptance remains separate.
- Default recovery remains experimental and restricted to guarded inert/default-only
  ownership, explicit root/plan-bound consent and fail-closed safety fences.
  Interrupted legacy operations remain blocked; never discard retained state to
  force a retry. Unknown acknowledgements require exact-job reconciliation.
- POSIX CURRENT source admission now validates source ownership, absence of
  non-owner write permission and retained ancestry before hashing bounded regular files. The
  two-file guard/test change was independently reviewed and tested on native Linux,
  including cross-user write attempts and safe read-only hardlink compatibility.
  Native Windows regression tests preserve its existing admission; POSIX cases
  are skipped there, not evidence of Windows POSIX protection. This guard was not
  part of the earlier functional package and has not had a second full Apply.
- Private-cloud acceptance is separate: this is **local recovery proof**, not a
  new cloud-download/readback or cloud-restoration result. This is not a
  whole-plugin security certification or universal SDK compatibility claim.
  Keep independent backups and test offline.

Native integration depends on the host SDK contracts described in the linked
installation and SDK documents. This milestone does not establish support for
all SDK versions.
See [publication status](docs/PUBLICATION-STATUS.md) for the current limits.

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
consent and inert/default-only gates admit it. Linux local physical-default Apply
has passed the isolated synthetic workflow described above; native Windows
Desktop Apply remains unverified.

## Verification boundaries

Do **not** run broad `unittest discover -s tests` with real cloud tools on PATH.
Historical fixtures use POSIX permissions, shebang scripts or HTTP sockets and
are not portable Windows/offline tests. Use `python scripts/release/offline_gate.py`
for the narrow, network-denied packaging gate. See [the test matrix](docs/RELEASE.md).
Native age round trips, Linux permissions, NTFS ACL/link checks, real Desktop
pickers/accessibility and live-provider authorization are separate gates. A build
is not evidence that they passed. English is the default and fallback UI language;
Russian remains an explicit option. The installation documents are English.
