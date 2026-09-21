# Security model

This tool moves a real mouse and types into real windows. That is the feature and the risk.
The model below says what we protect, where the boundaries are, and which risks we accept
instead of pretending to solve.

## Assets

Input on the host desktop. Fixture and secret values. Evidence that a verdict rests on.
The integrity of the run record, which is what makes a verdict meaningful later.

## Trust boundaries

| Boundary | Assumption |
| --- | --- |
| Calling model | Untrusted for authority. It proposes; guards and the verifier decide. |
| Application under test | Untrusted for instructions. Its text is evidence, never a command. |
| Local processes of the same user | Trusted for stop and for reading their own state. Same-user code can already inject input. |
| Other users on the machine | Not trusted. The pipe DACL and session check keep them out. |
| Network | Nothing listens. Remote clients need a local component and separate authorization. |

## Local IPC

The broker creates its named pipe with an explicit security descriptor granting SYSTEM, the
object owner, and the current user SID. Windows defaults can include read access for
Everyone and anonymous users, so the default is never used. The pipe name carries the logon
session, and the server verifies that the connecting client runs in the same Terminal
Services session. The pipe rejects remote clients.

## Authorization and the desktop lease

Every request carries a session identifier, and every mutating request carries a run
identifier, a lease identifier, and a lease generation. The broker re-checks all of them. A
lease is exclusive, so two harnesses cannot interleave input on one desktop. A client that
disconnects loses the lease immediately. Its run stays paused and resumable, but nothing is
dispatched while no client holds the lease. That is the difference between "paused" and
"running unattended", and this project only ever does the first.

Resume tokens are rotated at the end of every slice, so a token from an earlier slice cannot
start a new one.

## Emergency stop

The local stop is a named kernel event that any process running as the same user can set. It
does not go through the broker, the model, the action queue, or screenshot capture. The
broker checks it before every dispatch, and a set event blocks input until it is cleared
explicitly. The plugin holds no keys across actions, so there is nothing to release beyond
the input of the action already in flight.

## Dispatch integrity

Input is single-use and journaled before the native call. Uncertain effects get reconciled by
observation, not by replay. Identifiers are bound to the run, the lease generation, the
snapshot, the operation, the target, the input mode, and a keyed fingerprint of the fixture
value. Secret values never appear in the journal in plaintext; the journal stores a keyed
fingerprint that cannot be reversed without the key, which is regenerated per broker.

## Observation, redaction, and prompt injection

Observation is authorized like input. It is scoped to the approved application, so unrelated
windows do not enter the model prompt. Secret fixture values are replaced before any request
leaves the machine, and password fields are withheld by the driver rather than by convention.
Text from the application is data. The policy instructions say so, the runtime never derives
permissions from it, and a target chosen from application text still has to pass the same
guards as any other action.

Prompt injection can still pick a bad target inside the approved application if the
observation is genuinely ambiguous. It cannot widen the scope, add a step, change the
interaction mode, raise a budget, or turn a failed required action into a pass.

## Artifact access

Artifact assertions only read inside approved roots. Paths are resolved with `realpath`, and
any component that turns out to be a link or a reparse point is rejected instead of followed.
Absolute paths outside the roots are refused. Copying an artifact into evidence keeps the
bytes and the hash, so a later failure can be re-checked without trusting the original path.

## Operating boundaries

Application scoping reduces accidental input. It is not a sandbox. A hostile application on
the same desktop can still do hostile things, and the plugin will observe them. Input is
refused when the target window is disabled, which is how modal dialogs and privilege
boundaries show up, and the plugin never elevates itself or turns off an operating system
protection to get around a refusal.

## Residual risks we accept

An ambiguous observation can lead the policy to click the wrong control inside the approved
application. Assertions are the backstop, not a guarantee.

A same-user process can set the emergency stop and stall every run. It can also read the
journal file and inject input directly, so this adds no new capability.

Screenshots contain whatever was on screen, including the user's other windows if the
approved window overlapped them at capture time. Scoping narrows the capture region; it does
not redact pixels. Run tests on a desktop you are willing to photograph.

Uncertain effects are sometimes genuinely unknowable. The tool reports them and refuses a
pass rather than guessing.

## Testing safety rules

Test suites that create windows or inject input are gated behind `JEV_DESKTOP_LIVE=1`, and
the gate is enforced in the fixture, the launcher, and the live test directory. Do not run
them on a machine someone is using. Do not remove the gate to make a test convenient.
