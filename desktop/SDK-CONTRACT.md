# Finix Desktop contract

Build from the repository root:

```sh
node scripts/build-desktop.mjs
FINIX_ELECTRON_PINS=/absolute/path/to/verified-owned-electron-pins.json node --experimental-vm-modules --test --test-concurrency=1 tests-desktop/*.test.mjs
```

`desktop/plugin.js` is the deliverable single ESM bundle. It embeds the shared
`web/keys.mjs` crypto implementation and complete dependency license notices;
its only external modules are the public Hermes SDK and React JSX runtime.
No network script or node runtime API is needed by the bundle.

## Public SDK and local secrets

The plugin contributes a Finix right pane, full `/finix` page, sidebar link and
palette command. It uses SDK `Button`, atoms/useValue, contributions, `ctx.rest`,
`ctx.onDispose`, `ctx.files.requestFolder/writeText`, and `ctx.os.writeClipboard`.
It never queries or patches core Desktop DOM or accesses private bridges/stores.

Keys are generated in the renderer using Web Crypto, even for remote backends.
The private key lives only in the shared key session and the explicit
`ctx.files.writeText` call to a user-picked folder, with `exclusive: true`.
It never enters React props/state, rendered text, clipboard, plugin storage,
backend requests, logs, or a download URL. The user then selects the saved file
through this plugin's file input; shared parsing is bounded to 4096 bytes and
requires its derived recipient to match. A failed/cancelled save cannot enable
verification or registration. The SDK file door writes on the Desktop device,
not the remote backend. Folder grants remain managed/revocable by Hermes.

## Backend namespace

- `ctx.rest('/status')` returns `{ profile_id: string, recipient: string | null }`.
- `ctx.rest('/recipient', { method: 'POST', body: { profile_id, recipient,
  expected_recipient } })` must atomically reject a profile mismatch or a
  changed configured recipient (compare-and-set).
- An existing registered recipient is immutable. No checkbox authorizes replacement;
  generation and registration remain locked after status reports a recipient.
  Registration sends `expected_recipient: null` and fails closed on a concurrent
  registration or profile/route change.
- Success requires a fresh GET readback with exactly the same profile ID and
  generated recipient; a successful POST alone is never displayed as success.
- Absent/incompatible backends fail closed for generation and registration until
  status is known. Existing saved recovery files are unchanged; the separate
  offline HTML key generator remains available.

## Profile/route binding

The key is bound at generation to `host.state.profile.get()` and the documented
SDK `gatewayActivationEpoch()`. The epoch changes on active route selection,
including same-profile source swaps and switch-away/back. Every relevant async
continuation checks the captured binding before continuing or publishing success;
registration also checks immediately before POST, with no intervening await.
Backend profile ID is captured from the route's status response (it need not equal
the source-local profile label on aliased remote routes). The displayed status is
re-read before POST; changed profile ID/recipient aborts without mutating.

Older SDKs without the activation counter may still generate/save/verify, but
registration fails closed. A POST already dispatched to its original route
cannot be revoked by a later profile switch; a switched view will not show it as
success or issue its follow-up status request.

## Cloud connection UI

`desktop/src/cloud.mjs` is bundled into the same shipped ESM. It uses the public
`ctx.os.openExternal` and `ctx.rest` doors, never a private Electron bridge.
`GET /connection` provides a short-lived CSRF ticket and backend binding;
`POST /connection` accepts only closed actions (start/answer/cancel/apply), provider,
folder, consent, local-browser attestation, profile ID and random session ID.
No grant/code/token/config fields are admitted. The auth URL/ticket stay only in
memory, not plugin/browser persistence. The separate owner-only private-chat command
may return only a validated authorization-start URL, never a CSRF ticket. Google/rclone credentials stay in
the credential-owning subprocess. Manual refresh is intentional: no custom poll
loop or hidden account login. Apply requires a read-only verified session and
exact settings CAS; the UI reads its result back. SDK profile/activation epoch
checks fence async results, browser opening and mutations. A backend switch
requires a new refresh and new confirmations. Older SDKs fail closed.

The wizard has profile-scoped English or Russian labels, explicit checkboxes, native select/inputs,
keyboard-operable buttons and a polite live status region. New login requires
loopback reachability: same-machine browser, server browser or user-managed SSH
local forwarding (Google 53683, rclone 53682). Public callback infrastructure
is not provided. Existing Workspace reuse and fresh Google client prerequisites,
rclone OneDrive choice limits and crash leftovers are documented in README.
The built-bundle tests cover consent/local-mode gates, verified-only apply,
readback and route/unload fences. They are not a native browser-opening or
screen-reader test.

## Recovery activation (tested Desktop/SDK 0.23.0)

The installed Desktop package 2026.7411.3516.0 was inspected, including its
actual bundled `host.ensureAgent`, `host.profileRoutes` and `host.requestProfile`.
`ensureAgent(connectionId, name)` selects/dials; it does **not** unpark a gateway.
The recovery UI obtains a durable `recovery-operations-v1` whole-backend receipt,
then selects the new profile. Begin/current/status/key/cancel share one owner;
separate prepare/import/start mutations are not used. It checks both local connection IDs,
profile/activation stores, a unique mode=local route with exact `targetProfile`,
and calls `host.requestProfile(route, 'profiles.describe', {name})`. Both the
route and returned canonical name must equal the requested name. Missing SDK
capabilities fail closed before host start; unknown lifecycle source versions
fail the backend allowlist. No invented `gateway.start` RPC or grant resolver is used.

An incomplete/unknown copy requires its warning-specific Restore button, which
sets `acknowledge_incomplete: true` on that fresh intent. Unexpected incompleteness
stops before import and requires selecting the key again and fresh warning intent.
Rollback stays callable after target-route or description failure, independent of
restored Finix REST availability. No SOURCE REST is issued after ensureAgent.
The controller claims selection ownership before its first registry await;
load/poll cannot each dispatch an ensureAgent while one is pending.

The actual PluginRestOptions has no arbitrary-header field: key multipart POST
uses a canonical nonsecret phase request_id QUERY value. Global ctx.storage is
only a nonsecret hint; durable current/status provides operation authority.
Key generation fences survive terminal cancellation while file reads are pending.

Run shipped contracts serially with Node 22 and `--no-opt` (the VM optimizing
tier has an independently reproduced Windows crash):
`node --no-opt --experimental-vm-modules --test --test-concurrency=1 tests-desktop/*.test.mjs tests-web/*.test.mjs`.

## Profile display language

SDK 0.23.0 publicly exports `useI18n()` with `locale`, `isLoadingConfig`
and `configLoadError`. Its locale context does not carry a profile/source/epoch
binding, so it cannot alone attest a newly selected route. Finix uses this
public hook, plus `GET /ui-language` under the existing authenticated plugin REST
namespace. That endpoint projects only `{ui_language: "en" | "ru"}` from
`hermes_cli.config.load_config_readonly().display.language`, whose installed
cache is keyed by the context-local configuration path. It never returns the
configuration, reads credentials, writes a display preference, or starts work.
An unavailable/unknown preference falls back to English. Russian is used only
when explicitly selected for the active profile.

`desktop/src/messages.mjs` is the sole presentation dictionary. The localized
JSX boundary changes only displayed text and accessible labels, not event
callbacks, values, IDs or request payloads. English covers setup, keys, cloud
consent and remediation, job phases/counters/polite announcements, recovery
warnings, native progress names and terminal controls. Profile/source/connection/
activation-epoch fences reject late locale results. A supported host renders a
neutral busy status while the new route's language is unresolved; previous job
success is not shown during initialization. Only the recovery workflow's own
acknowledged transition retains its originating language, including rollback,
when the restored profile intentionally has Finix integrations disabled.

The primary restore form remains one select, one native file input and one
Restore button. No language controls or additional consent form steps were added.
Run `tests-desktop/locale.test.mjs` and `locale-native.test.mjs` against the shipped
bundle. Native DOM tests require pinned existing Electron runtime and owned
synthetic harness entry via FINIX_ELECTRON_PINS; no Edge/Chrome fallback or silent
skip is permitted. The harness asserts entry, isolated userData/sessionData/root,
blocks external network, and uses a secure in-memory fixture protocol. Its modeled
SDK/REST inputs do not establish actual installed Desktop Restore or cloud access.

## Evidence and limitations

The contract suite imports/evaluates the actual built ESM under Node VM modules,
links only mock public SDK/React modules, renders the contribution tree, clicks
its controls, and uses real bundled crypto. It verifies privacy/failure/race
boundaries, immutable-recipient registration and exact readback. This is not a native Electron
smoke test, not a real host file-picker/clipboard test, and not a live backend
integration test. Native Desktop layout, accessibility-tree behavior and OS file
permissions still need an actual installed Desktop run. No claim of live UI
verification is made.

Public SDK reference fetched successfully during this implementation:
<https://hermes-agent.nousresearch.com/docs/developer-guide/desktop-plugin-sdk>.
Relevant documented constraints: only SDK/React external imports; local granted
folder handles and exclusive writes; gateway state is socket status, not route
identity; activation epoch is the route-selection fence, not a credential or
durable identity; app-level plugins do not follow focused chat profile ownership.
