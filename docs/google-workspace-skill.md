# Removed Google Workspace skill transport

The external `workspace-skill` backend is no longer supported or shipped. Finix
no longer executes the skill's Python helper, discovers its installation, or
opens its token store. The Google client libraries used only by that backend
are not Finix dependencies.

## Supported storage

- Google Drive uses only the owning profile's Hermes-managed `finix:google`
  OAuth declaration and the public credentialed provider request API. Run
  `hermes auth add finix:google` in the selected profile, then explicitly check
  and apply **Google Drive**. The declared scope is `drive.file`, not full Drive.
- rclone remains supported, including its explicit owner-approved authorization
  subprocess and named remote configuration. This does not reuse a Workspace
  skill grant.
- Local encrypted archives remain available for explicitly selected recovery.

## Existing skill-backed data

`storage: workspace` with `workspace_transport: skill` fails closed with
`WORKSPACE_SKILL_TRANSPORT_REMOVED`. Unknown transport values also fail closed.
Missing transport selectors retain the existing `finix` default; the literal
`skill` selector is never rewritten or silently treated as `finix`.

The removed backend's settings, account pin, pending journals, committed journals,
backup grants and ciphertext are not copied, adopted, deleted or migrated.
Finix does not list old skill-backed objects as active cloud recovery sources.
Preserve the original recovery key and retrieve old ciphertext using the original
owner-controlled provider access, outside this removed transport. Select that
ciphertext explicitly in local recovery. Do not delete the old journal or grant
until the owner has independently verified recovery and decided how to retain it.

Authorizing `finix:google`, even for the same Google account, does **not** grant
access to objects created by the old application's grant. New backups require an
explicit supported connection selection; this is not a migration of old objects.

## Verification boundary

Removal regression tests cover rejected legacy configuration, unchanged local
artifacts, supported connection choices, and authority isolation. They are not
live OAuth or cloud recovery acceptance. The compiled Desktop bundle must be
rebuilt and exercised in CI; source edits alone do not update the shipped bundle.
