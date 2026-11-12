# Restore and recovered profiles

Finix recovery is local-only. Use the Finix **Recovery** page on the reserved
local Desktop connection. Remote, SSH, cloud and unknown connections are rejected
before the selected key is read or sent. Never attach a recovery key to chat.

## Current import availability

Creating and activating a new recovered profile is currently blocked with
`HOST_IMPORT_CONTRACT_UNSUPPORTED`, before candidate handoff. The public Hermes
import command lacks the required publication guarantees, and Finix no longer
replaces Hermes modules or changes global temporary-directory state to supply
them. The separate guarded [physical-default replacement](default-maintenance-workflow.md)
and inert archive extraction are distinct paths, not a claim that new-profile
activation is available. Existing archives, interrupted jobs and parked profiles
are preserved. The separate-profile UI contract below describes the intended
workflow when a suitable public import contract is available.

## Restore and open a separate profile

1. Choose a backup from the automatically loaded select. Labels show date, source
   and completeness. The newest complete copy is selected when its recorded date
   is known. Without a known date, selection does not imply recency; the label
   honestly says the date is unknown. Unknown completeness has an explicit warning.
2. Select your private recovery-key file with the native file picker (at most
   16 KiB). Never paste it into chat.
3. Press **Восстановить**. Finix prepares a safe staging destination and an unused
   server-generated profile name, restores, imports and requests host startup.

The page states: **Будет создан и открыт восстановленный профиль. Текущий профиль
сохранится.** There are no mode, destination, name or consent fields, no initial
manual-archive picker, and no load, refresh, import or activation buttons.
Mounting/changing scope performs read-only capabilities/catalog requests, not
OAuth, import or startup. An empty catalog reports no available copies without
requiring a filesystem path.

Incomplete copies have an inline warning and **Восстановить неполную копию**.
Unknown completeness uses **Восстановить копию с непроверенной полнотой**. Clicking
that specific button acknowledges the displayed risk for the selected copy.
If a previously complete catalog entry unexpectedly has an incomplete or unknown
manifest, restoration stops before import. Read the new warning, select the key
again and give fresh intent with the newly labelled primary button. There is no
silent acknowledgement or extra checkbox.

Short Russian live announcements describe preparation, key transfer, download,
restore, import and startup. Status is polled through the SDK React Query layer,
not a custom fetch loop. Cancel appears only while busy. Failure clears the key;
select it again and retry with the same primary control. Buffer overwriting is
best effort and cannot promise physical erasure in JavaScript.

## Verified activation and rollback

Recovery imports a separately named inert profile, never overwriting the current
or default profile. Recovered cron, messaging, hooks, MCP servers, auto-loaded
skills and executable plugins remain disabled or parked in `.finix-paused`.
History, memories, credentials and databases are preserved subject to safety
transformations. External bundle extras are not imported by this automatic flow;
keep the original encrypted copy for separately reviewed extras recovery.

The hidden plaintext handoff is an exact server-owned capability inside the
owning profile's excluded `cache/finix/recovered/<random>/bundle`, not a client
path. It remains available through the startup guard, then is identity-checked
and erased after the exact serving ACK. Abandoned imported handoffs have a
600-second cleanup timer while the API process is alive (failed startup can be
retried within that window). A crash stops that timer: owner-private leftovers
may remain and need local, identity-reviewed cleanup; there is no promise of
physical erasure or an unsafe scan of other profiles. Replaced/unsafe paths fail
closed and are not deleted. The separately imported profile is never erased by
handoff cleanup.

Catalog dates come from validated local index `created_at` values or valid
persisted committed-cloud-row `created_at` values. Cloud rows without their own
valid time inherit a known local time only for an identical ciphertext SHA-256
and size. Conflicting/unknown local provenance stays unknown. No filesystem
mtime, opaque identifier, new current-time stamp or remote account scan supplies
a missing date.

The single click authorizes `prepare-auto`, the bounded multipart key upload,
verified-success `import-auto` and the existing separately validated `/start`
request. `acknowledge_incomplete: true` is sent only when the selected copy's
warning-specific primary button supplied that acknowledgement at click time.
Each backend operation revalidates the owning profile. The server independently
chooses and admits safe staging and an unused name.

The UI requires an exact positive serving ACK before `ensureAgent`, then a unique
direct canonical route and route-pinned `profiles.describe` readback. Selection
alone is not success. A parked recovered profile need not expose Finix REST.
After completion or post-activation failure, **Назад к предыдущему профилю** returns
to the previous profile. Other scope transitions abort and clear transient keys.
No gateway killing, integration re-enabling or live-home replacement is implied.

## Compatibility and verification

This candidate targets the inspected installed Desktop SDK **0.23.0** contract:
folders `{id,name,path}`, list entries `{name,path,isDirectory}`, one `file`
upload field, public local registry and profile-route APIs. These shapes were
verified against the installed bundle; automated UI tests use synthetic grants,
not a real native picker. There is no transferable server-side SDK folder grant.

The host import source matches upstream `v0.23.0` and the inspected installed
host after newline normalization. Critical import/path/archive helper source
hashes are pinned and unknown implementations fail closed with
`HOST_UNAVAILABLE`. The shipped Python package's `0.0.0` metadata is not used as
proof of compatibility. Another Desktop/host release needs a separate review,
not a guessed permissive version range.

Local automated evidence uses generated age identities, synthetic committed WAL
SQLite rows, actual JSON/multipart ASGI requests and the real installed host
importer under disposable process and native homes. Provider transports are
mocked exact-journal download clients; no real OAuth, cloud backup, live profile
or installed Desktop activation is claimed. The automatic Node/Chromium response
fixture contains public responses captured from that real test with disposable
synthetic backup data; it is not invented live output. Startup uses an inert
synthetic positive serving ACK and never launches a real model or provider.

Publication/deployment remain closed pending explicit authorization and review.
