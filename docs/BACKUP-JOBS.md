# Durable backup jobs and local verification

Setup backup uses POST `/backup/jobs` (202) and GET `/backup/jobs/current` with the `backup-jobs-v1` capability marker. HTTP disconnection does not cancel the worker. Reattach before retrying an uncertain acknowledgement. State reads use a short separate lease; an existing immutable profile identity is resolved without waiting for the backup operation lock.

Each worker runs in a fresh copy of the authenticated request ContextVars. It does not change process environment, the selected user profile or native account-home overlays.

## Retained ciphertext

The capture checkpoint records archive basename, SHA-256, exact ciphertext size, completeness and the existing transport namespace. Resume verifies the retained ciphertext without recapture. An existing pending intent always retains its object ID; unknown provider outcomes never authorize a new name. A committed record is read back before receipt publication.

For the checkpoint-before-intent window only, a matching local ownership-index row, exact size/hash/completeness, unchanged owning-profile logical connection and retained namespace, intact bound transport ledger, no pending intent and no committed matching record authorize creation of a new durable pending intent for the SAME ciphertext. Pending intent is persisted before a provider copy; committed ledger is persisted before clearing pending. Missing/replaced namespace, ledger, ciphertext or ownership proof fails closed. Old checkpoints without size/namespace proof require explicit offline reconciliation rather than inferred admission. No automatic live resume is performed by installing or opening this candidate.

## Connection binding and limitations

Bindings include profile identity, recipient/source/settings, explicitly selected account/connection IDs where provided, config path, remote name, remote root and the config parent's filesystem identity. The transport ledger independently validates provider type and the same namespace binding. Scheduling fields do not invalidate their own receipt. Ordinary provider-owned token refresh bytes, file mtime, size and inode do not rebind a job. Existing private-config, no-follow and owner checks remain enforced by the adapter at use.

This is a logical connection binding, NOT cryptographic proof of the provider's authenticated account. Replacing credentials in the SAME configured remote/type/private parent with another account cannot be distinguished without provider-specific stable account identity attestation, which is not implemented here. Finix does not inspect/copy token contents or broaden OAuth scopes to simulate that proof. Explicitly selecting another config/remote/root/account or replacing its private parent is a meaningful binding change and is rejected during retained-ciphertext resume.

## Idempotency resources

Private per-request receipts and per-job terminal records replace the inline 128-request lifetime limit. The current state and individual journal reads remain byte-bounded; old request IDs retain replay semantics across fresh clients/processes. Journal growth consumes filesystem space/inodes and can fail on actual storage/quota exhaustion. There is no fixed lifetime job-count admission cap and no automatic expiration that silently permits a duplicate backup. Concurrent worker slots remain bounded separately.

## Progress and acceptance

First upload, pending reconciliation and committed readback propagate progress and cancellation to the same adapter/runner. Upload counts come from structured rclone stats; readback counts come from ciphertext stdout, with verification completion withheld until exact size and SHA-256 pass. Percentages are phase-local. Success requires commit plus truthful complete status; incomplete results cannot enable scheduling or retention.

The offline composed test uses generated age keys, actual HTTP 202/polling, fresh clients, a simulated twenty-minute barrier while the real root lock is held, real fake-child RcloneRunner pipes, GET-visible bytes, full ciphertext hash readback and commit. It also exercises both real resumed readback branches. These fixtures certify no live provider/account and do not deploy to a running Hermes environment. Existing three-action Restore UI is preserved.
