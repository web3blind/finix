# Publication status — experimental beta

This status supersedes older release-gate prose where it describes the default
feature as disabled. The reviewed runtime enables the guarded default feature;
this documentation change does not alter runtime gates or role checks.

A Linux backup and full readback are verified. Do not equate archive verification
with a successful full live-root replacement. Native Windows archive decryption
passed, but Desktop cold preview timed out at 30 seconds without Plan/Apply or
restored history. Desktop is incomplete; the publication video is Linux-only.
Linux cloud restore has not passed. No complete restore or universal compatibility
claim is authorized by this status. Interrupted legacy operations remain blocked.
Manual reopen/reconciliation may be required after an unknown acknowledgement.

## Risk disclosure

Finix reads selected Hermes profile content outside its own plugin directory and
creates encrypted archives and private recovery staging. Restore may replace
profile content only through explicit consent and reviewed ownership checks.
External age/age-keygen processes are required; optional rclone/Google Drive
integration makes third-party network calls when explicitly configured. Scheduling
and recovery jobs can run in the background. Recovery keys are sensitive: keep
private keys outside the backup host. Google sign-in is through Hermes-managed
OAuth; no vendor credential files are included. No SDK, password manager, demo
data, evidence bundles or private recovery material is included in this source.
Telemetry is not enabled by this publication. Review setup and safety documents.

Public author: [Denis Skripnik](https://github.com/denis-skripnik),
[X](https://x.com/denis_skripnik), [blinddev.xyz](https://blinddev.xyz).
