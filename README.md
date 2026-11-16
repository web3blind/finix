# Finix — encrypted Hermes profile backups

Finix creates age-encrypted backups of Hermes profiles, uploads them to the
selected storage and verifies the complete cloud object by downloading it back.
It provides a standalone CLI, native Hermes commands, a Desktop setup pane and
an offline recovery-key generator.

**Experimental beta with limited recovery.** Automatic named-profile import,
activation and physical-default replacement are disabled. They return
`PUBLIC_RECOVERY_CONTRACT_UNAVAILABLE` before reading a recovery key. Standalone
offline extraction into an empty directory remains available. This is not an
automatic restoration product.

## Current capabilities

- Encrypted profile backups, including supported conversation-history data,
  configuration and notes. Private staging and archive validation protect the
  capture/extraction boundaries; keep independent backups.
- Local storage, configured rclone storage and Google Drive through the declared
  Hermes-managed `finix:google` OAuth provider with `drive.file`.
- Full downloaded-object verification, durable pending-upload records and
  explicit storage selection. Unknown outcomes do not silently become success.
- Desktop key registration, cloud connection, first-backup verification and
  explicitly enabled scheduling. Existing public recipients cannot be replaced.
- English by default and Russian as an explicit language option.
- Catalog previews and inert offline extraction. Existing recovery journals,
  preimages and provenance are retained but never replayed automatically.

The Google Workspace skill transport and private managed-gateway signing are
removed. Finix does not borrow another tool's login, copy legacy grants or
silently fall back to another account. Old Workspace settings are rejected with
an actionable setup error while existing data is preserved.

## Verified on the released public SDK

The current runtime was tested at source commit
[`684a1e6`](https://github.com/web3blind/finix/commit/684a1e6e8694b5d66a7ee3d6b3acaae490bfd7aa)
with released Hermes **v0.23.1** on an isolated Linux VM and synthetic profile:

- Actual native CLI and Linux Desktop each created and uploaded a new backup
  using its own Hermes-managed Google grant, without the Workspace skill.
- Both objects were independently downloaded, SHA256 matched, and age decryption
  plus full manifest validation recovered **19,846 files per archive**, complete
  with zero omissions.
- The downloaded Desktop archive passed an externally orchestrated cold data
  recovery into an absent physical default-home on the same disposable VM.
  SDK startup/PM regeneration succeeded; all three synthetic conversation exports
  and a note matched, and restored cron stayed paused. Original directories were
  returned after the experiment.

That last experiment used external VM exclusivity and an existing SDK/runtime
outside the restored home. It is **not** automatic Default Apply, a public held
writer lease, fresh-machine recovery or a production-profile test.

[Source CI](https://github.com/web3blind/finix/actions/runs/43751550235) passed all
four Linux/Windows focused and released-SDK jobs. The Linux SDK lane passed 62
history, Drive-protocol and backup/progress fixture tests. These are bounded
checks, not a claim that the entire historical suite passes. Current native
Windows GUI/cloud acceptance has not been rerun; older Windows evidence used a
retired implementation and is not acceptance of this runtime.

See [publication status](docs/PUBLICATION-STATUS.md) and
[test boundaries](docs/TEST-SDK.md) for the exact scope and remaining limitations.

## Install and use

Hermes plugin SDK **0.23.1 or later**, Python **3.11–3.14**, the declared Python
dependencies and separately installed `age`/`age-keygen` are required. Optional
rclone is not bundled. Google uses Hermes' public credentialed-provider API;
client provisioning and consent must be configured for the owning profile.
No plugin runtime downloads executable helpers.

- [Linux installation and setup](docs/INSTALL-LINUX.md).
- [Native Windows installation](docs/INSTALL-WINDOWS.md).
- [Three-stage setup](docs/setup-flow.md).
- [Desktop SDK contract](desktop/SDK-CONTRACT.md).
- [Current recovery boundary and retained legacy data](docs/recovery-catalog-retirement.md).
- [Removed Workspace transport and migration policy](docs/google-workspace-skill.md).

In Desktop, generate and save the private recovery file locally outside the
backup host. Register only its public recipient. Connect and explicitly apply
storage, then run the first backup and wait for readback verification. Enable a
schedule only after a successful check. A lost private key cannot be recovered;
never send it in chat. No private recovery key is required to create a backup.

The native CLI provides `hermes finix status`, `hermes finix check` and
`hermes finix schedule 24h` for an already configured profile. The schedule
command is an explicit opt-in, not a required part of testing a backup.

## Recovery and security boundaries

Finix does not patch Hermes core modules, take private gateway writer locks,
use private activation tickets/pipes, or pin runtime compatibility to hashes of
Hermes core source files. Unsupported automated recovery remains gated off until
a suitable public contract exists and the replacement receives new review.
Stopping a gateway and observing its status are not proof that every writer is
quiescent. Stock import is a manual fallback, not exact transactional replacement.

Backup content can include credentials and executable configuration. Archives are
encrypted before cloud upload; extraction is not permission to activate imported
grants, plugins or autoruns. Use private staging, safe local key storage and
independent backups. Do not delete old journals or replay historical Apply to
repair an interrupted operation. Historical recovery documents are retained as
records, not instructions to enable those retired paths.

Linux and native Windows/NTFS use distinct filesystem protections. WSL is not
native Windows evidence. Broad historical test discovery is not the supported
release gate: see the exact CI files and declared fixture requirements in
[TEST-SDK.md](docs/TEST-SDK.md). Missing fixtures and removed private APIs are not
passing tests and are not suppressed as success.

Finix first-party code is [MIT licensed](LICENSE), copyright 2026 Denis Skripnik.
Third-party notices are preserved in [THIRD-PARTY-NOTICES.md](THIRD-PARTY-NOTICES.md).
