# ADR 0005: Project Activation is contained and no-overwrite

## Status

Accepted for Checkpoint 4B.

## Context

Activation creates project-local directories, managed symlinks, and an Activation
Record. Path validation alone cannot prevent a concurrent parent replacement from
redirecting a later pathname-based write.

## Decision

Every mutation is relative to previously opened project directory descriptors and
does not follow path components. The project root is reopened and compared with the
identity captured by the final review before the transaction handle is accepted.
Every reviewed parent is likewise identity-checked when reopened. A platform without these containment
operations or directory `fsync` is blocked before creation.

Directories, links, the temporary record, and the final record are created
exclusively. The canonical record is written and synchronized through a temporary
ordinary file, reopened and verified, then published with a same-directory hard
link that cannot replace an existing destination. Parent directories are
synchronized after every creation and removal.

Every successful creating syscall is recorded immediately in an invocation ledger.
For symlinks, the ledger retains the exact parent descriptor before post-creation
verification, so cleanup can address the created object even if that parent is
concurrently renamed. Retained descriptors are closed unconditionally, including
when cleanup cannot reopen the project path.

Each reviewed Skill source is identified by its confined ordinary directory, its
regular non-symlink `SKILL.md`, and the file's content digest. Those facts are
revalidated around link creation, immediately before record publication, and before
success. A source mismatch fails with `activation.source_changed`; invocation-created
objects are cleaned and no mismatching Activation Record is published.
An expected failure returns a failed Activation Result. An unexpected exception or
interruption preserves the original failure and carries its Cleanup Report. Cleanup
removes only matching objects created by the same invocation, in reverse dependency
order. Cleanup errors never replace the original error.

## Consequences

Activation favors containment and explicit failure over availability on weaker
platforms. A process crash can leave unrecorded objects because there is no durable
transaction journal. Later runs treat those objects as project-owned and do not
remove them automatically. This cleanup is not the domain operation Rollback.
