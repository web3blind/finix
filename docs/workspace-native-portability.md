# Native Google Drive transport boundary

`WorkspaceDrive` and `WorkspaceRunner` are retained implementation names for
Finix's own Google Drive adapter. They do not mean that the Google Workspace
skill is used. The old provider/helper subprocess and its credential-bound child
environment are retired. `workspace_helper.py` refuses execution; it is not a
working provider launcher.

## Current transport

Google authorization belongs to the declared Hermes-managed `finix:google`
provider with `drive.file`. Sign-in and refresh are managed by Hermes; API calls
use its public credentialed-provider request route. Finix does not read or copy
another tool's token file, call a Workspace `build_service` helper, or create a
second full-Drive OAuth flow. No owner-selected Python helper or Google client
library installation is needed for this adapter.

The active implementation validates exact Google endpoints and bounded request,
response and resumable-session metadata. Session Location values are secret
capabilities, not loggable diagnostics. Downloads validate byte ranges and the
expected ciphertext size; complete-object SHA and authenticated archive validation
remain separate from metadata/readiness checks. Unknown outcomes retain the
pending record rather than inventing success or adopting another object.

Linux and native Windows retain their respective private-file/no-follow or
owner/DACL protections. These protections are not a sandbox for arbitrary trusted
same-user code, nor an SDK writer lease for profile replacement.

## Verification scope

Current native Linux CLI and Desktop cloud backup/readback/decryption are recorded
in [publication status](PUBLICATION-STATUS.md). Native Windows SDK validation and
filesystem CI do not establish a fresh Windows GUI/cloud run. Older subprocess,
venv/helper-readiness and process-group tests describe the retired implementation,
not the current public-SDK transport. See [the exact test gates](TEST-SDK.md).
