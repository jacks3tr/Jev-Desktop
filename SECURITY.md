# Security policy

`jev-desktop` observes and controls a live Windows desktop. Security reports are welcome and
taken seriously.

## Reporting a vulnerability

Use GitHub's private vulnerability reporting: **Security -> Report a vulnerability** on this
repository (GitHub Security Advisories). If that is unavailable, contact the maintainers
listed in `CODEOWNERS`.

Please include: affected version or commit, the exact configuration, a minimal reproduction,
and what an attacker gains. Do not open a public issue for anything exploitable.

We aim to acknowledge within 3 working days and to ship a fix or mitigation within 30 days
for confirmed issues. We will credit reporters who want it.

## What is in scope

* Input escaping its scope. A guarded dispatch reaches a window, process, or control
  outside the approved application and snapshot.
* Authorization bypass. A client session, run, or resume token can act on resources it was
  not granted, identifiers cross sessions, or a stale lease dispatches.
* Journal integrity. A duplicate or uncertain action is automatically replayed,
  `dispatching` or `uncertain` state is laundered into `dispatched`, or a keyed fingerprint
  can be inverted to recover fixture or secret values.
* Path escape. Artifact assertions read or execute outside the approved roots, including
  through junctions, symlinks, or reparse points.
* Observation leaks. Secret values, password fields, or unrelated windows appear in policy
  requests, journals, evidence, or transcripts.
* Prompt injection with a real effect. Application content such as accessible names, dialog
  text, or document text causes the runner to change its test, widen permissions, or use a
  different interaction mechanism.
* Emergency stop bypass. The local stop fails to block input, or held inputs are not
  released.
* Privilege escalation. The plugin attempts elevation, or interacts with a higher-integrity
  target instead of refusing.

## What is out of scope

* An attacker who already has code execution as the same user (they can set the emergency
  stop event, read the journal, and inject input directly).
* A compromised calling harness or model provider.
* Physical access to an unlocked session.
* Denial of service by filling the evidence directory under a legitimate run.
* Findings that require disabling the documented guards, such as lowering integrity checks
  or editing the configuration to approve an unintended root. Report those as hardening
  suggestions instead.

## Security model in brief

* One broker per interactive user/logon session, reachable only over a named pipe created
  with an explicit DACL (SYSTEM, owner, current user SID). Peers must be in the same
  Terminal Services session. There is no network listener.
* Every mutation requires an active session, the desktop lease with a matching generation,
  and a run whose immutable specification digest still matches.
* The local emergency stop is a named kernel event any same-user process can set; the broker
  checks it before every dispatch, independently of the model, the queue, or capture work.
* Input is single-use and journaled before it happens. Uncertain effects are reconciled by
  observation, never replayed. Exactly-once GUI effects are not claimed.
* Observation is scoped and redacted: secrets never reach the model or the journal in
  plaintext, password fields are withheld, and artifact paths are confined to approved roots
  with reparse points rejected.
* The plugin is **not a sandbox**. Application scoping reduces accidental input; it does not
  contain a hostile application running on the same desktop.

Details, including the residual risks we accept, are in [docs/security-model.md](docs/security-model.md).
