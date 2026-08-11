# ADR 0004: Activation Record ownership is semantic and project-local

## Status

Accepted for Checkpoint 4A.

## Context

Activation needs durable proof of which project objects the collection may later
recognize as its own. Matching names alone would allow the collection to adopt
project-owned objects. Inode identity is not portable and does not survive ordinary
filesystem replacement.

## Decision

A canonical project-local Activation Record is the only durable ownership proof.
It identifies owned objects by rooted relative path, expected object type, and, for
a managed symlink, its exact collection-relative target.

A valid canonical Activation Record is trusted ownership proof. Checkpoint 4A is
not a security boundary against a person or process that can already modify the
project. Deliberate copying, replacement, or forgery of a fully valid record by such
an actor is outside the threat model. The collection therefore uses no signature,
secret, inode identity, machine-specific identity, or external ownership state.

An existing unrecorded directory may be used as a container but is not owned. An
existing unrecorded symlink blocks initial activation even when its target happens
to match. Missing recorded objects may be proposed for repair. An object with a
different type or symlink target blocks.

Ownership does not use inode, device, timestamp, or other filesystem-instance
identity. Replacing an object with a semantically identical object is therefore
indistinguishable and is treated as matching.

Trusting the record does not bypass review. Every record must still be structurally
valid, byte-for-byte canonical, confined to the project, and consistent with the
current Binding, Binding digest, collection revision, Profile, managed-link set,
and activation identity. Every recorded object is also checked against its recorded
path, type, and target. Missing owned objects may be proposed for repair; semantic
mismatches block.

The stable `activation_id` identifies durable intent and ownership. The stored
`applied_plan_id` is historical audit information only and is not ownership proof.
A current review receives a separate `plan_id` covering its mode, actions, and
filesystem preconditions.

Checkpoint 4A reads and reviews records but never writes or repairs them.

## Consequences

Records must pass schema, semantic, containment, and byte-for-byte canonical checks.
Different formatting is rejected rather than normalized. Mutation, no-overwrite
publication, and same-invocation cleanup remain deferred to Checkpoint 4B.

Because records are trusted local state, copying a valid record into another project
with identical intent and matching filesystem objects transfers its ownership
claims. This is intentional within the stated threat model.
