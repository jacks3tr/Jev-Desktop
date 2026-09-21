# Architecture

Five components decide things, and each one decides exactly one kind of thing. Keeping that
split clean is the whole design. When it slips, you get a test runner that can be talked into
passing.

| Component | Decides |
| --- | --- |
| Calling model | What to test, which fixtures to use, what counts as success, how to read a failure |
| Jev policy | The next permitted operation, and which observed target it applies to |
| Runtime | Step order, budgets, pauses, retries, assertion timing, resume |
| Native driver | Observation, reference resolution, input, live state validation |
| Verifier | Whether assertions passed, based on evidence |
| Broker | Who owns the desktop, who may act, how work stops |

The broker appears last because it is infrastructure, not judgement. It is the only process
with a native driver, and every other component talks to it rather than to the desktop.

## Process layout

One broker runs per interactive user session, started on demand by the first client. MCP
stdio and the JSON CLI are thin clients. They hold no state that matters. Run identifiers,
snapshot identifiers, and resume tokens all live in the broker, which means a paused run
started by an MCP client can be resumed by the CLI and the other way round.

```
calling model
  |  MCP stdio                  |  JSON CLI
  v                             v
thin client --------------------+
  |  named pipe, explicit DACL
  v
broker
  +-- ownership: sessions, desktop lease, cancellation, emergency stop
  +-- runtime: bounded loop, checkpoints, budgets, assertions
  +-- journal: SQLite WAL, single-use dispatch identities
  +-- evidence: screenshots, artifacts, retention
  +-- driver: UI Automation worker, input, capture, identity
```

## The runtime loop

One slice of work looks like this:

1. Check cancellation, emergency stop, lease generation, and budgets.
2. Observe the approved application. Observation is scoped and cached, and it records how
   long it took.
3. Verify the running build once per run, before any input.
4. Evaluate assertions whose checkpoint is due.
5. Pick the current required step, then build the candidate target list from the observation.
   Only operations the step permits and controls that genuinely support them appear.
6. Ask Jev for one operation and one target. Validate the answer.
7. Persist the dispatch intent, run the guards, dispatch once, record the receipt.
8. Re-observe, allowing the application a bounded moment to react, then evaluate the
   checkpoints that just became due.

A slice ends on completion, or on a pause that carries a reason and a fresh resume token.
The runtime never runs past its slice deadline, because the calling tool has a deadline of
its own and a half-answered call is worse than a checkpoint.

## The journal

Input is the one thing you cannot take back, so it gets the strictest treatment.

```
dispatching     intent is durable, dispatch may or may not have happened
dispatched      native call acknowledged, outcome still unverified
not_dispatched  a guard refused before any native input
uncertain       something failed after the dispatch boundary
```

A repeated request for a completed action returns the original receipt. A request that hits
`dispatching` or `uncertain` is refused, never replayed. After a restart, recovery observes
the application and compares the state with the fingerprint recorded before dispatch, then
reconciles the row. If the journal cannot write, the broker stops mutating and says so. A
run that cannot record what it is about to do must not do it.

We do not claim exactly-once GUI effects. Nobody can, and a tool that pretends otherwise
will eventually double-click something expensive.

## The Windows driver

Observation runs on one COM MTA thread that owns no windows. Nothing else touches UI
Automation objects. Traversal is scoped to the approved process, uses a cache request so
properties and control patterns arrive together, and stops at element and depth caps. When
it skips offscreen content or hits a cap, the snapshot says so and reports partial coverage
instead of quietly looking complete.

Input is `SendInput` for the user path and control patterns for semantic work. Mouse routing
is proven by hit testing the point under the click, since that is what decides which control
receives the input. Keyboard routing is a different mechanism, so typing and hotkeys also
require the target window to hold the foreground. Windows refuses cross-process focus
stealing, so the driver activates an application the way a person does, by clicking it, or it
reports that activation was refused.

Real elements and window handles stay in the driver. Callers see opaque identifiers that are
bound to the snapshot they came from, and stale ones are rejected rather than reused.

## Verification and evidence

Only the verifier sets assertion outcomes. Evaluators read a fresh observation, an artifact
on disk, or an identity report, and every result records expected value, observed value,
checkpoint, evidence references, and an origin that separates application behaviour from
runner and environment faults.

A verdict needs all of this: a verified build, at least one required assertion, every
required assertion passed, the required path completed, and no uncertain effects. A proven
failure survives later cleanup errors. Missing assertions mean no pass, which is the honest
answer when nobody said what success looks like.

## Transports and contracts

Everything that crosses a boundary lives in `contracts.py` with explicit validation and
serialisation. The MCP adapter and the CLI are small on purpose. They translate calls and
format results, and they do not carry authority: the broker re-authorizes every request from
its identifiers.

## Extension points

A second platform driver implements the `Driver` protocol from `contracts.py` and nothing
else changes. A new assertion evaluator goes in `verification.py` with an explicit origin. A
new operation needs a mapping in observation, a guarded dispatch, and live coverage. The
rules for both are in `CONTRIBUTING.md`.

## What this design refuses to do

It will not swap a mouse path for a shortcut to make a test green. It will not treat a toast,
a screenshot, or a model saying done as proof. It will not continue after the client
disconnects, after the emergency stop, or after the journal stops working. Those refusals are
the reason the verdict is worth reading.
