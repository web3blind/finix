# Google Drive through the Google Workspace skill

This is an optional, explicitly selected compatibility transport. The default
Google Drive transport still uses Hermes-owned `finix:google` OAuth. Selecting
the skill transport does not migrate that grant, old journals, or other profiles.

## Responsibilities and authorization

Finix creates and encrypts snapshots, maintains its private journals and allocated
object IDs, downloads the complete encrypted object, verifies its SHA-256, and
handles restore. The installed Google Workspace skill supplies authorization and
the Google API service. Finix does not open, copy, write or parse skill token files
and does not implement another Google sign-in flow.

The bridge calls the skill's named Python helper `build_service('drive', 'v3')`
inside an isolated, resource-bounded child. This is a version-pinned external-skill
compatibility contract, **not a documented stable Hermes SDK provider API**.
It differs from the original review requirement to use only `finix:google` and
`credentialed_provider_request`; it must not be described as satisfying that
requirement or as having reviewer approval.

## Prerequisites

- Install the compatible `google-workspace` skill in the **same active profile**.
  Its source must be a regular, trusted local copy beneath that profile's
  `skills` directory. Another profile's installation or grant is not adopted.
- Authorize the skill using its own supported setup instructions. Finix does not
  start consent, create a Google developer application, or copy an existing grant
  to a new test profile. Some installed skill releases request broader Workspace
  scopes than `drive.file`; inspect their actual consent screen. Finix does not
  silently launch broad-scope setup.
- Use the Google client dependencies declared by Finix through the normal Hermes
  plugin dependency manager. Do not modify a managed SDK environment with an
  ad-hoc pip install or substitute an arbitrary Python from PATH.

The supported compatibility contract currently names skill version `1.2.0` and
pins the LF-normalized source hashes of `scripts/google_api.py` and
`scripts/_hermes_home.py` in `finix/workspace_skill_transport.py`. An unreviewed
helper update, missing dependency, unsafe source path, or wrong profile refuses
with a setup error rather than falling back to another account or interpreter.
The installed setup parser may differ from its documentation; Finix does not
assume that `--services` or `--format` is supported.

## Explicit selection

In Desktop, select **Google Drive (Google Workspace SKILL)** in the cloud wizard,
read the permission notice, check the existing connection, and explicitly apply
it. Checking does not start authorization or upload a backup. Applying rechecks
the authenticated requester's account and keeps existing storage unchanged on a
mismatch.

The corresponding local CLI connection is:

```sh
python -m finix connect workspace-skill --home /absolute/owning/profile --mode local --confirm
```

This command uses the Python environment in which Finix is installed. For native
Windows, pass the owning profile's native absolute path. The interactive command
requires an explicit `apply` after verification; EOF cancels it. Remote CLI use
retains the existing explicit connection modes and owner/session checks.

Selection stores `storage: workspace` with `workspace_transport: skill`, an
explicit account digest, and a separate `workspace-skill` state directory. Existing
settings without this transport selector continue to use Hermes OAuth. Unknown
transport values are rejected. Recovery accepts only the selected transport's
exact committed journal and authority namespace.

## Failure and resource boundaries

Every child builds one skill-owned service and reads the authenticated requester's
Drive permission ID before using that same service for an operation. File-owner
metadata is not treated as requester identity. Preallocated Drive object IDs and
Finix's existing pending journal govern reconciliation; names alone do not grant
adoption or deletion authority. Uncertain acknowledgments retain local ciphertext
and pending state. Retention and recovery retain their existing provenance checks.

The worker uses bounded chunks, output sizes, deadlines and OS child-memory limits.
The Google HTTP library may buffer a response before application checks; these
limits are **not** a network pre-body byte cap. Cancellation closes the owned
worker rather than promising that Google rolled back an in-flight request.

## Verification status

The component tests exercise account binding, allocated IDs, paginated metadata,
reversible trash, malformed acknowledgments, binary readback, cancellation and
child cleanup. Offline tests also exercise the real Google client with an explicit
fake HTTP transport. These checks are not proof of live Google authorization,
native Desktop/NVDA acceptance, Linux adoption, or full physical-default recovery.
Those remain separate release gates; a passing component suite is not a completed
cross-platform demonstration.
