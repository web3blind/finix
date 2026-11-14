# Publication status — experimental beta

## Accepted development workflow, 2026-11-14

This document supersedes older local-only and Windows-pending milestone prose,
including historical status notes in the installation quickstarts. It describes
actual development acceptance, not an independent maintainer audit, universal SDK
compatibility or permission to bypass any runtime gate.

### Version and package identity

- Finix source version: `0.1.0`, reviewed runtime build **B11**. The runtime was
  subsequently published in source commit
  [`d4cc0eb3adcd8f00946215c8179a0b89f2e967da`](https://github.com/web3blind/finix/commit/d4cc0eb3adcd8f00946215c8179a0b89f2e967da).
  This documentation update does not change runtime code.
- Hermes SDK on both hosts: `0.23.0`, source
  `2bd378ed1b8d6bf2192cb26e0a55ff3091e6bd19`.
- Tested native ZIP SHA-256:
  `d973d4c479d4dd053cf0128d2e8713e5573357de0ada56a736c8ed485fb02b3a`.
- Tested immutable source-map SHA-256:
  `566c8da3106b8fd8b59531980680576c0f6ac535d80103334b8d9e476ea78698`.
  A fresh source checkout may normalize line endings; the package digest identifies
  the exact tested native bytes. Documentation commits are not new runtime builds.
- Cloud transport used for these runs: **Google Drive through the explicitly
  selected pinned Google Workspace skill bridge**, with the owning profile's grant.
  These results must not be relabelled as `finix:google` acceptance.

### Native Windows Desktop

On an isolated synthetic genuine SDK default on local NTFS, the actual Desktop
workflow completed encrypted backup and full remote ciphertext readback/hash
verification. An explicitly selected cloud copy was downloaded/decrypted; a fresh
sealed PREVIEW and separate one-shot Apply replaced the physical default, restarted
its owner and reached `ready` without rollback in the final run.

The Windows cloud archive was created and readback-verified by the earlier 0.1.0
candidate B7, then reused unchanged for final B11 recovery (ciphertext SHA-256
`9c24b45166699a29ddeb62262a7019f51d6849333a0bb4103a3c095be3381fda`).
This is compatibility with that actual prior cloud copy, not a fresh B11 Windows
backup claim. Linux, in contrast, created its new backup with B11.

Verification found the original **3 conversations / 12 messages**, the memory note,
the garden note and the original garden SQLite task rows. SQLite integrity was
`ok`; post-backup file/conversation/database mutations were removed. The original
garden conversation was opened in the reopened native Desktop. Current SDK
coordination lock-file identities were retained, not replaced by archived markers.
Earlier failed/rolled-back attempts remain historical failures, not success evidence.

### Ubuntu Linux CLI

The ordinary registered CLI, under an unprivileged Linux account and normal OS
home, completed a new encrypted cloud backup with zero omissions, complete remote
readback and SHA-256 verification. After deliberate data mutations, a nonlocal cloud
candidate was explicitly selected. Download/decryption, fresh PREVIEW, separate
Apply and physical-default restart reached `ready`.

Verification found **6 conversations / 12 messages**, the saved note, the original
Linux SQLite proof, and the garden note/database rows. Post-backup changes were
removed; SQLite integrity was `ok`. Stock SDK stored-session listing, history and
resume succeeded. No AI prompt/inference was submitted.

### Evidence and test boundaries

The development operator retained exact-job status, package/source identities,
cloud-object bindings, full readback hashes and data-verifier results outside the
public repository. Private evidence/profiles/grants are not included here, and
maintainers have not independently rerun these live workflows by virtue of this
text. In particular, Linux success is not substituted for native Windows evidence.

Builds 11 and 12 were byte-identical. Affected component regressions on each
platform reported **30 passed / 1 platform skip / 0 failures or errors**; a real
Windows base-image/venv-launcher regression and installed controller-freezer
admission also passed. Isolated wheel checks on Windows/Linux verified 49 namespace
Python files, 79 embedded native members, `pip check` and CLI help. These narrow
checks are not a new full-suite, all-platform or optional HTTP-import claim.

An 84-second English demonstration was assembled from actual post-run Windows
screens and privacy-filtered Linux CLI recordings, retaining all seven original
narration paragraphs. Unfilmed Windows start/confirmation/restart actions were not
staged; verified notes/SQLite results are supported by acceptance checks, not
invented terminal output. A video is not a replacement for these evidence boundaries.

## Authorization and outstanding SDK-only requirement

The default independent Google path already declares Hermes-managed `finix:google`
with `drive.file` and uses public credentialed-provider requests. It is **partially
implemented, not absent**, but the complete authorization lifecycle and the same
Windows/Linux cloud/default recovery workflow still need acceptance with the
Workspace skill absent.

The optional Workspace bridge uses the external skill's pinned
`build_service('drive', 'v3')` helper. It is **not a stable Hermes SDK provider API
and does not satisfy the SDK-only review requirement**. Retaining this working
compatibility route is not a maintainer-approved exception. The skill owns loading
and refreshing its grant; Finix does not copy its token files, silently start broad
Workspace consent or fall back to another profile/account. Some skill releases
request broader scopes than `drive.file`; inspect the actual consent screen.

Before replacing the bridge, validate client provisioning/consent, accessible
Desktop/CLI and remote-browser onboarding, cancellation/callback ownership, offline
refresh, expiry/revocation/reauthorization/disconnect, account/profile isolation and
normal dependency-manager installation. Google client dependencies are currently
unconditional. Migration must explicitly address object access and separate pending
journals: the same Google account alone does not authorize another OAuth client to
access old objects under `drive.file` or adopt their provenance.
See [the compatibility contract](google-workspace-skill.md).

## Remaining restrictions and risk disclosure

- New-profile import/activation still refuses with
  `HOST_IMPORT_CONTRACT_UNSUPPORTED`. The accepted physical-default replacement
  is a separate operation, not proof of safe new-profile publication.
- Active shared autoruns, supervising owners and sibling-serving multiplexers are
  unsupported by default Apply. Ownership, no-follow/ACL checks, CURRENT source
  admission, SQLite-family fences and root/plan-bound explicit consent remain.
- Preparation and Apply are distinct. Interrupted legacy operations remain blocked;
  never discard journals/locks or replay Apply to repair a lost status indicator.
- After successful READY, a deliberately separate gateway stop/restart changed the
  owner. The saved Desktop maintenance hint then required manual reconciliation.
  Restored data and native conversation access were verified, but fully seamless
  historical status and a new maintenance intent after arbitrary owner changes are
  not claimed. No hint/journal rewrite or old Apply replay was used.
- The README's cooperative same-OS-user/shared-root condition remains; this is not
  atomic defense against hostile concurrent target replacement, a whole-plugin
  security certification, all-SDK support, or AI-turn-readiness verification.

Finix reads selected profile content outside its plugin directory, creates encrypted
archives and private staging, and can replace profile data through admitted explicit
maintenance. Backup content may include configuration and credentials: encryption
is not permission to activate imported grants, executable plugins or autoruns.
External age/age-keygen processes are required; configured cloud helpers make
third-party network requests. Scheduling and recovery can run in the background.
Keep independent backups and private recovery keys outside the backup host.
No SDK, password manager, demo profiles, private evidence or recovery material is
included in this source. This publication does not enable telemetry.

Public author: [Denis Skripnik](https://github.com/denis-skripnik),
[X](https://x.com/denis_skripnik), [blinddev.xyz](https://blinddev.xyz).
