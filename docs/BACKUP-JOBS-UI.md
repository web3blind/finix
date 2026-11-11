# Desktop backup jobs

The backup setup panel uses the durable `backup-jobs-v1` contract. It never
calls the legacy long-running `POST /setup/check` endpoint.

## Wire contract

`GET /backup/jobs/current` returns:

```json
{
  "code_version": "backup-jobs-v1",
  "profile_id": "public-profile-binding",
  "job": null
}
```

A non-null job contains `id`, `state` (`running`, `succeeded`, `incomplete`,
`failed`, or `interrupted`), `phase`, `progress`, `started_at`, `updated_at`,
`error_code`, `verified`, `complete`, and `omitted_count`. Progress contains
`percent` (number or null), `completed_bytes`, `total_bytes`,
`completed_files`, and `total_files`. Dates are ISO timestamps. Public responses
must not contain filesystem paths, provider stderr, tokens, or secret material.
The UI additionally renders only closed phase labels, numeric counters, parsed
timestamps, and allowlisted error-code remedies.

`POST /backup/jobs` accepts exactly the bound `profile_id` and a client-generated
UUID `request_id`. It must acknowledge quickly with HTTP 202 and the same
envelope, independently of the worker lifetime. The backend must atomically
reattach an active job instead of starting another one. This client alone
cannot guarantee cross-window or cross-process exclusion.

The panel automatically attaches on mount/reopen. It uses the installed SDK's
public `useQuery` options (`queryKey`, `queryFn`, `enabled`, `retry`, and
`refetchInterval`), as the existing recovery panel does. Active or uncertain
jobs and interrupted connection reads are queried every two seconds. Pending
cloud/setup prerequisites are also observed until Start is available. There is
no private timer loop. Queries are scoped by instance, revision, profile,
source connection, and activation epoch; stale callbacks and continuations
cannot dispatch or publish into a new scope. Disposing the plugin removes
subscriptions without cancelling the server job.

A lost Start acknowledgement immediately triggers a read of the current job.
No automatic Start retry occurs. If the result remains unknown or a known
running job disappears, Start stays locked until an actual server terminal
state is observed. Polling network errors retain the last job and phase,
display a temporary connection warning, and retry through SDK Query; recovery
clears the warning, including its polite live announcement.

An absent or outdated capability marker (including an absent route) disables
Start with `BACKUP_BACKEND_UPDATE_REQUIRED`: update the backend and restart
the gateway. There is no legacy fallback.

## Truthful progress and accessibility

The display includes the current phase, phase percentage, actual byte/file
counters, elapsed time, and the server's last-update time. Unknown totals
produce a native indeterminate HTML `progress` element, without `value` or
`aria-valuenow`. Known progress has a Russian accessible label and
`aria-valuenow`. Nonterminal percentages are capped at 99: transfer/readback
completion must not be mistaken for a complete verified archive. The overall
100 is reserved for `succeeded && verified && complete && omitted_count === 0`.

The visible percentage can update on each poll, but the separate atomic polite
live region is bounded to ten-percentage-point buckets and at least ten seconds
between running updates; terminal results and connection recovery are announced
promptly. This is semantic coverage for NVDA, not a claim of a live NVDA session.

A running job's `complete: false` is not reported as a terminal incomplete
archive. Terminal incomplete results explicitly report the missing-file count
and cannot enable scheduling. Period scheduling requires both the complete,
verified terminal job and a fresh verified setup receipt. Saving a period is
an explicit short POST followed by exact profile/interval receipt readback.

## Verification

Run the actual shipped bundle tests serially with Node 22 and `--no-opt`:

```sh
node scripts/build-desktop.mjs
node --no-opt --experimental-vm-modules --test --test-concurrency=1 tests-desktop/*.test.mjs
```

`tests-desktop/setup.test.mjs` evaluates `desktop/plugin.js`, not source mocks.
It includes a twenty-minute fake-clock running worker, quick acknowledgement,
reattachment, lost acknowledgement, temporary polling failures, strict
capability/duplicate/scope gates, complete/readback/schedule gates, and a real
headless Chromium DOM test with synthetic public state and network interception.
Set `FINIX_BROWSER` to a local Chromium-compatible executable when the default
Windows Edge path is unavailable. No cloud, user profile, private key, deployment,
or publication is needed. The simplified three-control recovery UI is unchanged.
