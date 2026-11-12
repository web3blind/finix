# Three-stage setup and native Hermes CLI

The native plugin registers `hermes finix` separately from the owner-only chat
command `/finix`. CLI operations are authorized by access to the local operator
terminal, not by a messaging account ID. Registration captures the owning
profile directory; deferred commands must not select a profile from a worker's
ambient environment.

Run `hermes finix` in an interactive terminal for the numbered menu:

1. **Key** reports the registered public recipient, if present. It never offers
   replacement. If absent, create and save a recovery key on your own device
   using the shipped offline `dist/keygen.html` or the native Desktop client.
   The server receives only its public `age1…` recipient, never the private key.
2. **Cloud** chooses Google Drive (the declared Finix OAuth client), Yandex Disk,
   OneDrive or Dropbox, or explicitly local-only copies. For Google, first use
   Hermes' public `hermes auth add finix:google` flow in the owning profile with
   the configured `FINIX_GOOGLE_CLIENT_ID` and `FINIX_GOOGLE_CLIENT_SECRET`.
   The declared scope is `drive.file`. Finix never reads or writes the Google
   Workspace skill's credentials. Other providers use their explicitly selected
   private connection workers. A connection is applied separately after verification.
   No authorization codes, callback URLs or credentials should be pasted into the
   terminal or chat. Tunnel mode never opens a browser on the server.
3. **Check everything and run first backup** runs the actual encrypted profile
   backup, source validation and selected cloud adapter's complete upload and
   readback. Only a successful operation asks for a schedule period.

Equivalent explicit commands:

```text
hermes finix key
hermes finix key --recipient age1…
hermes finix connect
hermes finix connect local
hermes finix connect yandex --mode tunnel --confirm
hermes finix check
hermes finix schedule 24h
hermes finix schedule 7d
hermes finix status
hermes finix extra list
```

`key --recipient` accepts only a native public recipient. Initial registration
is allowed; once registered, a different recipient is rejected globally,
including old REST/owner-command replacement paths. Repeating the current
recipient with a current compare-and-set expectation remains safe.

Cloud connection requires an interactive terminal, explicit scope consent and
an explicit `local`, `tunnel` or `server-browser` mode. Local-only selection,
status, public-key registration, check and scheduling can run noninteractively.
With no subcommand and no terminal, the command prints JSON state and help
without waiting for input. Noninteractive connection does not start a worker.

Periods are strictly positive whole numbers immediately followed by lowercase
`h` or `d`, such as `24h` and `7d`. Zero, fractions, whitespace, cron/YAML
expressions and intervals exceeding the existing 8760-hour limit are rejected.

`SetupFlow` stores a non-secret verification receipt beside Finix settings,
outside the encrypted profile source. It binds the actual backend profile ID,
recipient, storage settings, canonical configured extra sources, and metadata
of selected credential files where applicable. It stores no credential
contents. Changing a connection or selected sources invalidates that receipt;
`SetupFlow.schedule()` refuses until check succeeds again. Operational period
and next-run changes do not invalidate their own proof. An explicit recheck
pauses the previous period until the owner confirms a new one.

A receipt records a completed backup and transport integrity, **not** ownership
of a private recovery identity, guaranteed future cloud availability or an
owner-key restore test. Local-only receipts explicitly report no cloud
readback. Existing adapter ownership/retention rules remain authoritative.

## Native plugin wiring

The SDK contract is:

```python
from finix.plugin_cli import setup_parser, run
ctx.register_cli_command(
    'finix', 'Finix setup', setup_parser,
    lambda args: run(args, profile_factory=bound_profile),
)
```

The caller must capture `bound_profile` at registration, including its trusted
default root for external-folder authorization. Shared surface methods are
`SetupFlow(profile).view()`, `.key()`, `.check_and_run()` and
`.schedule('24h')`. Callers remain responsible for UI/chat authentication; the
service does not invent a chat-owner identity.
