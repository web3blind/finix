# Ordinary Restore security contract — first integration slice

## Current SDK writer coordination files

The exact top-level `.backup.lock` and `.checkpoints.lock` names are CURRENT SDK coordination resources, not incoming profile data. Their installed producers are `writer_fence`/`runtime_state._lock` and `tools.checkpoint_pruning.store_lock_path`. Backup capture and archive verification remain unchanged: archived bytes remain available in the inert copy and are reported as source shared resources, but true-default promotion never installs those archived lock markers.

Retain existing target lock-file identities as opaque current control-plane members. Do not hash or copy their bytes while the actual SDK writer fence is held: native Windows byte-range locks deny even reads of empty locked files through another handle. Keep the fence held through publication/rollback; do not delete, unlock, truncate, chmod/adopt, or replace a CURRENT coordination file to make restoration succeed. Archived markers never confer current lock ownership. This rule also preserves the coordination namespace on POSIX; passing advisory-lock reads is not permission to replace its inode.

This is an exact-name, top-level classification, not a `.lock` wildcard. Ordinary files such as `.user-data.lock`, suffix near-misses, and nested `notes/.backup.lock` retain their existing profile-data behavior. All other root/source/consent/identity, SQLite-family, rollback, and same-UID operating conditions remain unchanged. Opaque kept-member metadata is not a claim that every retained subtree is recursively verified.

## SQLite runtime process-image pin

On Windows, bind the generation producer to the current kernel process image from `GetModuleFileNameW(NULL)`, retaining its exact file identity, size, SHA-256 and Python version. The supported SDK can host the selected environment through the base Python image while a detached controller uses its venv redirector; `sys.executable` then spells different launcher files although the executing image is identical. This is not an executable allowlist or version-only comparison: a different image identity/hash still invalidates the plan. Failure/truncation of native image discovery fails closed. POSIX keeps the existing resolved `sys.executable` policy. No protected-content, schema, user-row, SQLite-family, source, consent or helper/function pin is exempted or rewritten.

## Native Desktop polling and read-only reopen continuity

An already admitted maintenance job may keep its status query active while the
window is hidden. The existing busy/polling, disposal, source, registry, epoch,
key-dispatch and mutation-once fences remain authoritative; enabling the SDK's
background interval does not start idle work or consume Apply consent.

A reopened applied-operation hint grants only an exact status read. After fresh
capabilities and registry authentication, retain a read-only physical-root anchor
from those current responses, never from saved consent. A same-profile/local-source
SDK activation-epoch transition may then reauthenticate that exact root and resume
GET-only status/history verification. A changed root token, registry, profile or
connection still revokes continuation. The key and preview remain absent and
Apply remains consumed; this anchor grants no creation, key, cancellation or Apply
authority and never revives a historical native capability. Only after fresh READY
and exact stock history-route verification, mark the nonsecret UI hint terminal.
Do not edit native journals or receipts. A later fresh client may show a new catalog,
but still needs a newly selected key, Prepare intent and separate Apply consent.

## Native default-journal creation and owned-context census

Create and privately admit `cache/finix/default-recovery` before its per-job child;
Python recursive mkdir does not apply the leaf mode to missing parents. Existing
nonprivate journal directories still refuse without chmod or adoption. The test
uses a separate native child with umask 022, not an agent-wide mask change.
For POSIX owned `memories`/`skills` census only, reuse the existing retained
owner-only profile-root guard: SDK-created 0755 descendants remain unreachable
by other UIDs. The exact private boundary and traversed ownership/no-follow/
nonwritability checks remain required; an unrelated or nonprivate root and
writable descendant refuse. Ordinary inert-copy inventory retains its strict
private-leaf check; native Windows DACL semantics remain unchanged.

## Exact stock SDK config metadata admission (2026-11-14)

True-default maintenance admits the optional literal integer `_config_version: 52`
emitted/preserved by the inspected SDK config_defaults/save_config producers.
Other versions, booleans, floats, strings and malformed markers fail closed; the
marker's presence does not admit any additional configuration or migration.
The only admitted `plugins.entries` mapping is exactly
`finix: {allow_tool_override: false}`, with literal boolean false, and only with
the existing trusted CURRENT Finix exemption and exact `enabled: [finix]` list.
This SDK enable-policy producer explicitly denies tool override; its consumer
grants via truthy legacy values or additional capability grants, neither admitted
here. Empty/malformed entries, foreign plugins, unknown fields and other grants
remain refused. Neither marker nor denial grants executable authority. Existing
SDK pins, sibling admission, read-only preflight and config digest binding remain.
No protective metadata is stripped and no SDK/profile is rewritten by admission.

## Scoped PM aliases and selected CURRENT Workspace helper (2026-11-14)

POSIX capture classifies only exact PM direct-root generation aliases as layout
data. Metadata directories reopened from root must match every retained traversal
ancestor; bin aliases additionally match the retained bin parent. Traversal checks
directory membership again on return, including replacement after classification.
No alias target is opened, no metadata grants execution authority, and regular
payload, unknown links/special files and Windows reparse refusal remain unchanged.

True-default replacement may keep only the three existing files of the same-profile,
explicitly selected, compatible CURRENT google-workspace helper: SKILL.md,
scripts/google_api.py and scripts/_hermes_home.py. Existing kept-target records seal
raw hashes, native leaves, ancestor bindings and selection/account coordinates.
Checks precede registration, worker drain, publication, restart and success commit;
changed raw bytes/identities/selection require reprepare, never fresh adoption.
Archived helpers and unrelated current executable skills receive no exemption.
Rollback touches only its sealed ordinary DATA names and keeps CURRENT native
objects; cancellation and post-READY reconciliation retain their existing rules.
These are defined-boundary checks, not an atomic hostile same-UID mutation claim.
No release gate, resource floor or public SDK authority is changed.

## M08 `runtime-attempt-preserve-v1` (plan4/journal3; review gate CLOSED)

Only the initially admitted isolated real-default root with the existing empty/inert
SDK tick-scope admission is supported; active shared autoruns/supervisors are not.
No SDK changes or named-profile-as-default substitution. Existing source/native
root, pins, R1, OWNER_RIGHTS, retained ancestry/FD and unknown-companion guards remain.
Before maintenance consent, seal the exact profile-owned startup-write names
(incoming/replaced/deleted ordinary members and canonical state.db family), the
physical incoming-main identity, included profile `recovery-custody/<plan-id>`
namespace and keepSharedResources policy. Consent permits brief pre-success
startup/user writes and restoring OLD only after durable custody of every variant.
Custody is not cache: flattened immutable manifests/copies/native objects, retained
indefinitely until separate explicit cleanup consent; never auto-delete a sole
variant. Cost is complete old preimages plus complete failed current scope (copies
and retained originals), in addition to existing transaction staging. Existing
custody is opaque kept shared data, never recursively embedded as a new preimage.
Incoming archived custody is separately sealed and copied into the SAME flat
namespace with NoClobber, not dropped as shared data: existing whole namespaces
must match a native-safe complete census for deduplication; mismatches refuse.
Unique partial raw copies stay in the existing transaction cache while their
complete original remains retained; only verified complete blobs enter durable
custody. The sole variant is never relegated to cache.

Custody blobs have a required narrow shared capture boundary: ordinary snapshot
classification would otherwise SQLite-open each preserved main independently of
its archived WAL, altering bytes and losing the intended opaque family. The new
`custody_payload` classifier applies ONLY to exact reserved
`recovery-custody/<32hex>/{old,copies,objects,restore-staging}/<64hex>` paths bound by a valid
versioned execution manifest and exact stable size/hash. It preserves raw bytes
as DATA, never grants SQLite/runtime authority, and rechecks manifest/hash at
capture. All other SQLite discovery, family/owner/link/pin rules stay unchanged;
near misses/invalid manifests do not obtain an opaque bypass. No exclusion or
permission change is introduced. This is the sole necessary shared snapshot
hunk; it must have native exact-byte future age roundtrip and negative controls.

Only an in-memory retained newly owned launch may be stopped. Bind that object
to the exact plan before launching and remember observed READY in memory as well
as durably. A raised SDK spawn call with no returned Popen is ambiguous, NOT a
proof of no child: never auto-rollback it or adopt a PID. Only a Finix preflight
failure before the SDK launch was invoked has explicit never-spawned authority;
otherwise missing returned native ownership requires manual preservation. This
narrows handling of a producer exception without inventing a missing conditional
SDK launch contract or modifying the SDK.
After exact child /
Windows retained-runtime exit and SDK exclusive reservations plus existing writer
fence, capture immutable current leaves, parent/root native identities, sizes and
hashes BEFORE any canonical mutation. Changed ordinary atomic replacements within
sealed names are DATA custody, not OLD authority. Main must retain incoming native
identity; unknown auxiliary names, unsafe/hardlinked leaves or uncertain stop /
reservation / identity preserve everything and require manual intervention.
Capture complete opaque main/WAL/SHM/journal without any SQLite open, including hot
journals. Publish copies NoClobber from unique partial names, journal BEFORE every
copy/move/restore, retain original current native objects and OLD physical family.
Only after all copies/native objects are committed may coherent OLD restoration
begin. A newly occupied basename is never overwritten. Crash reopen may continue
only the sealed DATA transaction under absent-owner/exclusive admission; it cannot
adopt a live Popen from a PID record or scan/kill a root. Failed capture never
reports OLD READY. Restart OLD only after custody commit and full restoration;
verify actual SDK default selectors and fresh exact native READY independently.

A separate durable success commit cuts off automatic old-consent rollback. Once
live NEW READY is observed, inability to persist that commit/status requires
reconciliation/preservation, never the launch-failure rollback branch. Post-commit
runtime writes are outside old-consent rollback authority. No claim of continuous
same-UID immutability is made, and ordinary cooperative TARGET nonreplacement does
not waive these M08 checks. Public Apply stays False pending independent native
Windows/Linux acceptance and review.

Status: implementation in progress, no release/deployment or full acceptance. Physical default M08 remains mandatory and `DEFAULT_APPLY_REVIEWED=False`.

## Assets and threat model
Protect SOURCE home and settings, ciphertext identity, generated fixture identities, private staging, immutable archived history and unknown tables, FINAL new target and runtime ownership. Reject client-specified bootstrap paths/principals/booleans, archive executable integrations, stale source epochs, switched native roots, foreign process ACKs and partial history replay. Native ownership/privacy protects other OS principals and unsafe filesystem replacement; it does not promise exclusion from arbitrary same-UID/root attackers, guaranteed erasure after power loss, or atomic exclusion of every same-UID mutation.

## Existing authority and source pins
SDK is immutable source `finix-demo-windows/hermes-agent`, git `2bd378ed1b8d6bf2192cb26e0a55ff3091e6bd19`. Desktop SDK contract is 0.23.0; package metadata 0.0.0 is NOT a version authority. `../finix-integration-evidence/inputs.json` records raw/normalized source hashes and immutable donor maps; `pm-inputs.json` records actual cached PM inputs. Do not change SDK source or install dependencies.

- `RecoveryService.handoff(job, profile_id, activation)` is registered server authority, not an arbitrary directory. D authority/worker lease hooks, copy_context and exact-base guards survive composition.
- H `RecoveryImporter.import_job(history_mode='new-copy')` publishes and reads back a private inert target through pinned stock `profiles.import_profile`, `safe_extract_targz`, `archive_root_dirs`, `get_profile_dir`, `_canon_valid`. HOST_SOURCE_PINS remain unchanged. Only new data ownership is authorized; age encryption and source namespace metadata are NOT sender/old-principal proof.
- Internal bootstrap object capabilities are not HTTP fields. Bind exact service/job, verified handoff, source profile/epoch, generated target name, native directory/root identities, inert digest, original-content digest and SDK pins. Re-admit before launch and after history. No adoption of existing target or foreign READY.
- SDK `spawn_unmanaged_gateway(target, deadline=...)` starts ONLY the generated target with reviewed parked configuration, `gateway.standalone=True`, `multiplex_profiles=False`. Do not rescan/start a root mux, stop SOURCE, change ForegroundSelector or use global PID scans. Retain exact Popen and Windows redirector child process handles via reviewed `_WindowsLaunch`; require exact ready home/roster and kernel pipe/socket peer PID. LIFECYCLE_PINS bind the installed producer contract. SDK legitimately changes files after startup: never recheck the obsolete inert whole-tree digest then.
- `portable_history.complete_target` uses actual `connect_gateway(endpoint)` with protected native ticket, never a manufactured SessionActor. This SDK method defaults to purpose `interactive`; the actual `gateway/runtime_bootstrap.py:_PURPOSE_CAPABILITIES` grants `session:create` and `session:read` for that purpose, and `mint(..., scope='profile')` defaults to the exact profile rather than host reach (source is in the pre-edit SDK map). History RPC is `session.mutate(operation='import', request_id, expected_revision=0, payload={'sessions':[export]})`; every batch must acknowledge exactly one imported segment without skipped/detached/errors. Original DB/census/exports/plan remain durable `.finix-paused/history-original` and future-backup included, not cache.
- `session.list(scope='stored')` counts logical compression roots; native-http tickets authorize actual per-session messages. Compare rich message projection/inactive data using H readback plus logical displayed content and physical count. Do not infer history completion from SDK READY.

## Transformations and failures

Integration readback exposed the same strict-ASCII comment bug in the direct
`streaming.decrypted_stream`/Windows counterpart used for future-backup proof.
Both direct readers now use strict UTF-8 with one leading BOM compatibility,
retain the existing exact single-token alphabet gate, and send only its canonical
ASCII line to age. Invalid UTF-8 is a closed INVALID_IDENTITY refusal. This is not
a relaxed token policy and does not rewrite the producer or any user's key.

Retained native ancestry must span bootstrap and portable completion. Use D's
reviewed `_PosixAnchor` pool (no second flock on a retained directory) and Windows
native directory handles, not another path-only trust mechanism. SDK history/
ticket module source pins are drawn from the pre-edit raw/normalized input map.
Native identity/digest/root/source-epoch/purpose negatives must refuse before any
SDK startup. Releasing those pins is permitted only after history completion or
interrupted-state publication; it is not a way to bypass a failed admission.
NEW-copy changes owner label to actual authenticated target owner, does not replay original canonical policy/binding receipts, retains original unknown state and archived integrations inertly. Supported history projection is distinct from active metadata identity. Always keep `full_acceptance=False`, `foreground_connected=False`; archive/content/backend-ready flags have separate proof boundaries. Missing model is supported for browsing, not proof of model-turn readiness.

Whole-success requires completion, content readback, unchanged original, fresh exact owned READY, private handoff cleanup and durable backend receipt. Source receipt is written before any future Desktop selection. No target Finix REST is required; recovered plugins are parked.

Cancel is refused during atomic import/start/history. Adapter state locks are short; data/import/ready statuses differ. Cross-process durable full-chain ownership/reconciliation remains the NEXT slice (M11/M12 not GREEN). Per-job pipeline record prevents blind retry in-process; unknown nonempty target or partial helper completion becomes interrupted/preserved, never wipe/retry or reconstructed capability. Failure can stop only exact retained newly owned runtime. No post-success rollback or physical-default replacement is authorized by ordinary Restore.

## Pipeline ownership checkpoint (M11/M12 partial)

Before extending orchestration, the installed PM `environments._recorded_venv`
was read: it resolves both the selected environment and the install's
`environments` directory, admitting only contained generations with pyvenv.cfg.
Sequential fixtures may therefore link their entire generations directory to
ONE new task-owned copy, preserving actual facts and interpreter semantics; no
SDK/cache mutation, hardlinked executable or per-case full copy is needed.
Runner disk admission measures host C backing VHD AND native Linux free bytes,
with tooling size plus 1 GiB transient and 3 GiB retained headroom before copying.

The next authority boundary is a private SOURCE-bound whole backend pipeline
lease. Reuse existing `backup_jobs.file_lease/state_lease` native primitives,
not a JSON/PID pseudo-lock. Bind current record to exact source profile and
activation; no public target path/capability is reconstructed. Separate short
state lease from long worker lease, hold the latter through cleanup, refuse a
second operation after interrupted state, and preserve completed records on
same-job replay without reconstructing runtime authority. Status reports
interrupted when the kernel lease is absent and never adopts a persisted PID.
This checkpoint does NOT extend that lease backward through prepare/decrypt,
make HTTP completion asynchronous, or reconcile partial SDK history. Those
remain required M11/M12 work; physical default remains disabled.

## Whole SOURCE operation boundary (M11/M12 implementation)

The existing kernel `backup_jobs.file_lease` uses POSIX flock or Windows one-byte
msvcrt locking over native admitted files. Its `state_lease` is independent and
bounded. A new SOURCE operation uses those unchanged primitives, holding its
worker lease across preparation, key wait, decryption and complete_restore.
Only a short state lease spans record read/write; SDK work never holds it.
Dispatch captures the actual request ContextVars. Durable records contain closed
coordinates, source binding, generated IDs and completion receipts, never keys,
tickets, Python capabilities or process authority. On lease loss a nonterminal
record becomes interrupted and fences fresh requests; reopen does not recreate
handoffs or adopt a runtime. Exact request replay reads its original durable
receipt and mismatched candidate/consent refuses. Key phase replay is identified
by an exact phase request ID, not a persisted key digest. Cancellation is intent
until worker-confirmed cleanup; import/start/history remain noncancellable.
Actual FencedService checks must compare current backend binding on every fence,
not only its stored activation. Legacy mutation adapters must share admission;
public target paths cannot create an alternate owner. Partial history remains
preserved/interrupted until target-issued binding/content reconciliation has a
separate native RED/GREEN; no reconstruction from historical request IDs.

## Verified async operation checkpoint

`recovery_operations.RecoveryOperations` now executes prepare/catalog/private
allocation → transient key dispatch → actual RecoveryService decrypt → existing
complete_restore/import/bootstrap/portable-history/native readback/cleanup →
durable backend receipt under ONE SOURCE operation lease. HTTP operations POST
and key POST ACK independently of blocked SDK import. Registered CLI restore
uses the same class and has `recovery status` and exact `--request-id` replay.
The inner pipeline lease is retained as defense in depth, not used as a substitute
for the earlier owner. A fresh SDK home override is verified at actual HTTP
worker dispatch in the native generated-age loop.

Legacy ordinary `/prepare-auto` requires an explicit request ID and routes to
that owner; `/jobs` status/key/cancel aliases use exact durable operation IDs.
`/prepare`, direct `/import`, and direct `/start` refuse
`OPERATION_PROTOCOL_REQUIRED`, rather than accepting arbitrary paths or separate
mutation ownership. Default-only frozen endpoints retain their original gate.
Old Desktop and old endpoint fixture consumers are NOT migrated/certified yet;
this compatibility refusal is intentional and must not be mistaken for finished
Desktop recovery. Historical synchronous helpers remain unreachable through
ordinary mutation routes pending removal/fixture migration.

Native lost-ACK injection forwards TWO actual target-authenticated SDK history
imports, then drops the second reply. Reentry on the SAME still-live native RPC
reproduces the existing helper's HISTORY_ID_COLLISION. This checkpoint chooses
the permitted controlled-interrupted outcome: two sessions/eight messages,
original DB, target and source are preserved; same-request replay reports
interrupted and a fresh operation is refused. No target wipe/reimport, SQL
rewriting, reconstructed capability, historical process adoption or claim of
successful history reconciliation is made. Backend death also becomes
interrupted using kernel lease absence, with a genuine second-process test.

Key bytes are never persisted in operation state. The transient identity stream
is wiped/closed immediately after data worker completion, before import or any
terminal success. An independent controller's cancellation intent is polled
while waiting for the key and is terminal only after worker confirmation.
Import/bootstrap/history remain noncancellable. No post-selection SOURCE REST,
foreground selection, deployment, physical-default enablement or escrow change
was added.

## Required negative regressions before promotion
Invalid/unregistered capability and purpose; wrong target/digest/root/source epoch before spawn; foreign ACK/peer PID; client path/name/Boolean bootstrap attempts; immutable SOURCE; archived integration traps; nonempty partial history refusal/preservation; duplicate/lost ACK and cancel ownership; all first-party loaded paths from one tree. Current native vertical loop is `tests/test_recovery_pipeline.py`; additional negative cases must accompany new boundaries before release. Existing baseline/Desktop tests are preserved; mode=local consumer RED is evidence only until the next stage, not UI repair authorization by accident.

## Desktop durable-operation boundary (review checkpoint, not release admission)

The installed public PluginRestOptions has method/body/upload/timeoutMs only;
it has NO arbitrary-header or SOURCE-route option. Desktop key dispatch therefore
uses a canonical nonsecret phase request UUID in the key URL query. HTTP accepts
that query (or the old native header), rejects conflicting header/query values,
and retains the existing canonical UUID validator. Keys stay exclusively in the
bounded multipart upload. Capability admission now requires the mounted
`operation_protocol=recovery-operations-v1` marker before key selection.

Desktop consumes begin/current/status/key/cancel only. Exact operation ID, client
request ID, SOURCE backend profile and captured local connection/profile/epoch
fence every continuation. Preparing or key uncertainty retains busy ownership;
cancel means intent until an exact terminal worker projection. Atomic import,
bootstrap, history, verification and cleanup are noncancellable in both the UI
and retained handler. SDK Query owns polling; no production polling timers.

Only a succeeded backend_target_ready receipt with archive/content/history and
paused-integration semantics admits foreground selection. All SOURCE REST ends
BEFORE ensureAgent. A unique exact connection/profile/targetProfile mode=local
SDK-produced descriptor and pinned profiles.describe then establish connection;
read actual profile/connection/activation stores, never require epoch+1, direct
mode or target Finix REST. Pending SDK calls retain ownership. Unknown SDK
selection outcomes retain the Restore lock and offer explicit previous-profile
selection after the request settles, not backend rollback or target deletion.

Public ctx.storage is global plugin storage, not server authority. It contains
only version/source/connection/backend-profile/operation-ID/phase/target hints.
SOURCE reopening reads durable current independently of the hint. TARGET hint
reopening only describes its actual selected local connection and explicitly
DOES NOT claim archive success or issue backend calls. A forged hint can at most
suppress work/display a connection; it cannot mint operation/target authority.
No key, ticket, path, process identity or capability is persisted. Source change
or dispose erases transient key data and fences later secret calls without
cancelling/releasing the server owner. Interrupted history is preserved/manual,
never an automatic duplicate import. Physical default gate remains False.

The original 20 legacy Desktop failures have now been migrated to actual durable
operation response shapes, canonical candidate/operation/request IDs, key-query
transport and mode=local. The full shipped-bundle/controller/isolated-Electron
Node gate has 118 passing executions and no skips. This is synthetic DOM/unit
coverage, not an installed Desktop Restore or fresh native backend acceptance.

## Bounded repairs after the immutable Desktop review checkpoint

- F2: activation claims its synchronous selection owner before awaiting registry;
  polls recheck after their registry await, and consume cannot create a second
  continuation during registry/selection. The original immutable reviewer probe
  reproduced two dispatches; the canonical negative test now requires one while
  the SDK call is pending. No SOURCE REST is admitted after ensureAgent.
- Cancellation invalidates a separate transient-key generation. A terminal cancel
  may clear its intent flag, but cannot revive a file read already cancelled;
  both await boundaries recheck generation and finally erase its bytes.
- Returning to the prior profile suppresses automatic reselection of the same
  completed receipt within this controller. The suppression set conveys no
  backend/target authority and cannot release a nonterminal operation.
- Closed terminal errors are projected only through an explicit message map.
  COMPLETENESS_CHANGED changes the selected copy to the warning-specific fresh
  intent. Backend admits a retry only after worker completion and managed-output
  cleanup. Only the closed pre-import data failures WRONG_KEY/CORRUPT_ARCHIVE/
  TARGET_NOT_EMPTY/COMPLETENESS_CHANGED become failed; unknown errors and all
  import/bootstrap/history/cleanup ambiguity remain interrupted and block fresh
  ownership. No target runtime exists at the new failed boundary.
- F3: registered CLI exact-request replay uses a short state-lease read before
  legacy operation_lock, catalog and identity admission. It validates canonical
  request/candidate/consent, SOURCE binding and historical receipt; returns only
  durable state with activated=False, without key/worker/runtime authority.
  Foreign nonterminal requests remain BUSY and changed intent REQUEST_CONFLICT.

Fresh native async/generated-age and native Linux changed-contract runs were
attempted but refused by the unchanged disk-budget guard before tooling copies:
C: free was below the required fixture reserve. Earlier genuine response fixture
remains historical evidence only. No new native Restore/READY claim is made.

## Independent root-shared review repair (R1/R2)

Terminal READY must follow managed-result cleanup. The cleanup producer returns
an exact host-only retirement receipt only after erasure and deregistration;
the registered pipeline binds it to the original handoff, rechecks source,
SDK pins, retained native root/target/name binding, and queries runtime READY
again. Earlier READY dictionaries are not final proof. Late refusal preserves
the target and publishes interrupted state, never an activatable receipt.

## Cooperative root bootstrap contract — `cooperative-root-boundaries-v1`

The user explicitly accepted ordinary existing-root-multiplexer recovery with
ONE narrower operational condition: another process of the same OS user must
not concurrently replace the new TARGET profile. SDK changes remain forbidden.
This supersedes only the strong R2 requirement for atomic hostile-same-UID
TARGET/name binding. It does not waive SOURCE authority, root native identity,
retained kernel PID generation, root instance/authority epoch, original roster,
exact new target, handoff, capability, SDK pins or post-cleanup admission.

Checks are at defined boundaries, NOT an atomic hostile-same-UID target-binding
guarantee. Scope is mapped from explicit admitted root/SOURCE native paths before
protected discovery (no global host_gateway or foreign identify). Before target
config/unpark and immediately before the actual serve write, recheck SOURCE,
pins, handoff, root/target native bindings and root generation/instance/epoch/
original roster. Authenticate the connected kernel peer through a retained
process handle/pidfd before writing. The pinned one-request control protocol
cannot identify then serve on one connection; use separate protected queries,
then recheck local authority on the authenticated mutation connection before its
single write. Do not invent multiple-request support or ignored expected fields.

After serve ACK, after history, and after managed cleanup before terminal
success, recheck exact target/root/source authority and fresh root READY/roster.
A changed/uncertain observation interrupts and preserves the target, with no
activatable receipt, automatic retry/new target, replay authority or root stop.
Shared runtime is never an owned child. The excluded unobservable interval is
same-UID TARGET replacement between the last check and SDK serve-by-name
resolution; no fail-before-wrong-mutation claim is made for that interval. No
other race is declared safe or excluded by this operational condition.

Public capability metadata communicates the versioned condition before fresh
key selection/Restore where backend geometry is known. It is a warning, not
client authority or a force override. Unknown frontend named/default-vs-isolated
routing geometry remains a host-capability limitation, not a private bridge
bypass. One existing Restore consent remains the only action; no new settings.
Receipts identify boundary checks and the same-UID TARGET condition, not atomic
concurrency safety. Public default Apply/M08 stays closed and unchanged.

Historical pre-decision containment rationale (superseded only as above): The public
`serve-profile` handler accepts only a profile name, resolves it inside its own
reconcile lock, then acquires runtime ownership. Neither its name lookup nor
its roster admission honors a retained caller native-target/root-generation
reservation. Holding `ProfileOwnership` externally conflicts with serving;
releasing it before dispatch reopens the gap. Session write authority/claims
are not a profile lifecycle reservation. Same-connection identify/fence/serve
would authenticate the process but would not make SDK name resolution atomic.
No SDK patch, private lifecycle method or ignored `expected_*` field is used.

Before the accepted cooperative decision, known root multiplexer geometry
refused `ROOT_SHARED_BOOTSTRAP_UNSUPPORTED` before mutation. That containment
is now superseded ONLY for the cooperative contract above. Unknown/foreign
geometry still refuses. Named-owner/root-single/absent-owner standalone paths
and exact completed-operation replay remain unchanged.

An atomic hostile-same-UID target-binding claim would still require a public
conditional scoped serve or resolver-honored retained reservation covering root
PID generation, instance/epoch, source/sibling roster and native target/name
through SDK resolution. This host does not provide that contract. It is NOT
claimed by cooperative recovery. Public default Apply remains closed;
historical encrypted Windows/Linux proofs remain checkpoints until rerun.

