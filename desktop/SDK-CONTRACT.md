# Finix Desktop contract

Build from the repository root:

```sh
node scripts/build-desktop.mjs
node --experimental-vm-modules --test tests-desktop/*.test.mjs
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
- An existing different recipient requires explicit checkbox confirmation.
- Success requires a fresh GET readback with exactly the same profile ID and
  generated recipient; a successful POST alone is never displayed as success.
- Absent/incompatible backends leave local key generation/save/verification
  usable and explicitly report registration unavailable.

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
memory, not plugin/browser persistence or chat. Google/rclone credentials stay in
the credential-owning subprocess. Manual refresh is intentional: no custom poll
loop or hidden account login. Apply requires a read-only verified session and
exact settings CAS; the UI reads its result back. SDK profile/activation epoch
checks fence async results, browser opening and mutations. A backend switch
requires a new refresh and new confirmations. Older SDKs fail closed.

The wizard has Russian labels, explicit checkboxes, native select/inputs,
keyboard-operable buttons and a polite live status region. New login requires
browser and backend on the same machine; remote/mobile callback infrastructure
is not provided. Existing Workspace reuse and fresh Google client prerequisites,
rclone OneDrive choice limits and crash leftovers are documented in README.
The built-bundle tests cover consent/local-mode gates, verified-only apply,
readback and route/unload fences. They are not a native browser-opening or
screen-reader test.

## Evidence and limitations

The contract suite imports/evaluates the actual built ESM under Node VM modules,
links only mock public SDK/React modules, renders the contribution tree, clicks
its controls, and uses real bundled crypto. It verifies privacy and failure/race
boundaries, replacement CAS, and exact readback. This is not a native Electron
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
