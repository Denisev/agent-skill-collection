# Checkpoint 6B contract: safe, exclusive Binding creation

Status: **Accepted for Checkpoint 6B — portable POSIX protocol.**

Baseline: `cfe34791f28db8c591546f20d4c4505ac6c43426`

## 1. Purpose and boundary

Checkpoint 6B adds the one mutation that completes Project Initialization:

> Create the exact Binding described by a freshly revalidated Checkpoint 6A
> Initialization Plan, without replacing or adopting any existing object.

The public Python seam is `apply_project_initialization()`. The CLI extends the
existing command with an explicit apply form:

```text
skill-collection init-project --project-root PROJECT --profile PROFILE \
  --apply --plan-id PLAN_ID
```

Planning remains the default and remains entirely read-only. Apply requires the
opaque `plan_id` returned by a ready plan. Apply independently recreates the plan;
the supplied id is review evidence, not a capability, reservation, lock, or
authorization to overwrite.

The only durable Binding effect is creation of the exact canonical file
`project:skill-collection.toml`. A normal `created` result leaves no temporary
file; `created_with_incomplete_cleanup` may additionally retain only its reported
invocation-owned temporary hard link. Apply creates no directories, Skill links,
Activation Record, cache, global state, collection state, Git state, environment
state, credential state, or network state. It has no overwrite, force, merge,
repair, resume, adopt, update, activate, or delete mode.

If `skill-collection.toml` exists in any form when publication is attempted, apply
is blocked and leaves it untouched. Existing objects are never opened, followed,
parsed, compared, normalized, chmodded, removed, or replaced. A failed invocation
may best-effort remove only provisional temporary or final objects whose identity
proves that the same invocation created them. Once the Binding reaches the commit
point defined in section 7, it is never rolled back.

## 2. Vocabulary and ownership

Checkpoint 6B uses the accepted terms in `CONTEXT.md` without changing their
meaning.

**Initialization Application**: One request to create the first project Binding
from an exact, freshly revalidated Initialization Plan. It either publishes the
complete Binding exclusively or reports that it did not complete.

**Initialization Result**: The immutable outcome of one Initialization
Application: `created`, `created_with_incomplete_cleanup`, `blocked`, or
`failed`. Creation fields describe what the invocation observed itself create;
they are not durable ownership records.

**Created with incomplete cleanup**: A successful, synchronized, verified Binding
publication whose only incomplete post-commit work is removal of the
invocation-owned temporary hard link. The Binding is valid and project-owned; the
temporary location is reported for project-owner recovery.

**Initialization Cleanup Report**: The deterministic account of best-effort
removal after an Initialization Application fails or is interrupted. It is not a
Rollback, repair instruction, durable journal, or proof that an absent path stayed
absent.

The Binding is project-owned immediately after successful publication. The
collection does not subsequently own, rewrite, update, or remove it. Project
Initialization therefore has no `unchanged` or repeat-success result: once the
destination exists, every later initialization plan or apply request is blocked.

These three terms must be added to `CONTEXT.md` during implementation. No ADR is
required unless review changes the transaction strategy or ownership boundary.

## 3. Public Python seam

The following names become public exports from `skill_collection`:

```python
InitializationApplyStatus = Literal[
    "created", "created_with_incomplete_cleanup", "blocked", "failed"
]

@dataclass(frozen=True, slots=True)
class InitializationCleanupReport:
    attempted: bool
    removed_binding: bool
    removed_temporary_files: tuple[Location, ...]
    remaining_objects: tuple[Location, ...]
    issues: tuple[ValidationIssue, ...]

@dataclass(frozen=True, slots=True)
class InitializationResult:
    status: InitializationApplyStatus
    plan_id: str | None
    binding_location: Location | None
    binding_digest: str | None
    issues: tuple[ValidationIssue, ...]
    cleanup: InitializationCleanupReport | None

def apply_project_initialization(
    collection_root: str | Path,
    project_root: str | Path,
    profile: str,
    plan_id: str | None,
) -> InitializationResult: ...
```

All new objects are frozen, slotted value objects and have the same immutable-result
constraints as `InitializationPlan`. The cleanup type is deliberately narrower
than `CleanupReport`: initialization cannot create directories, symlinks, or an
Activation Record. The types must not be conflated merely because their output
shapes are similar.

Result invariants are exact:

- `created`: the supplied id matched the final plan; `plan_id`,
  `binding_location`, and `binding_digest` are non-`None`; `binding_location` is
  exactly `Location("project", "skill-collection.toml")`; `issues == ()`; and
  `cleanup is None`.
- `created_with_incomplete_cleanup`: the supplied id matched the final plan;
  `plan_id`, `binding_location`, and `binding_digest` have the same values and
  locations as `created`; `issues` contains exactly
  `initialization.temporary_cleanup_incomplete`; and `cleanup` is non-`None`,
  attempted, and has one or more cleanup issues. When temporary removal did not
  complete safely, it reports that rooted temporary location in
  `remaining_objects`; when only its post-unlink directory synchronization failed,
  `remaining_objects == ()` and `removed_temporary_files` records the removed
  temporary location. `removed_binding` is always `False` for this state.
- `blocked`: no creation syscall for this invocation succeeded; `plan_id`,
  `binding_location`, and `binding_digest` are `None`; one or more normalized
  issues are present; and `cleanup is None`.
- `failed`: at least one creation syscall succeeded but the complete operation did
  not return success; the reviewed `plan_id`, Binding location, and Binding digest
  are retained when they had been established before creation; exactly one primary
  failure issue is present; and `cleanup` is non-`None`.

An expected precondition or capability failure before creation is `blocked`.
An expected operational failure after creation begins is `failed`. Unexpected
exceptions and `KeyboardInterrupt` are re-raised after cleanup is attached as
`initialization_cleanup_report`; they are rendered by the CLI and are not converted
to `InitializationResult`.

## 4. Required review and stale-plan rule

`apply_project_initialization()` converts roots with the same lexical absolute-path
rule used by the CLI, then calls public `plan_project_initialization()` exactly
once for its initial review. It does not separately validate, scan, resolve,
serialize, or derive collection state. Thus it consumes the single collection-
selection orchestration seam established by 6A.

If the plan is blocked, apply returns blocked with exactly its normalized blocking
issues. If the plan is ready but `plan_id` is `None`, malformed, or unequal to the
fresh plan's opaque id, apply returns:

```text
initialization.stale_plan
The supplied plan identifier does not match the current Initialization Plan.
project:skill-collection.toml
```

No capability probe or creation occurs before this comparison.

After non-mutating capability checks and immediately before accepting a transaction
handle, apply calls `plan_project_initialization()` exactly once more. The second
plan must be ready and must equal the first plan as a value, including its id,
content, digests, observation, action, and resolved collection fields. Otherwise
apply returns:

```text
initialization.stale_plan
Filesystem or collection state changed after the reviewed plan was selected.
project:skill-collection.toml
```

The second review is the final pathname-based observation. Apply then captures the
resolved project-root identity and enters the descriptor-relative transaction.
There is no third collection traversal. Inside the transaction, the exact content
and digest already supplied by the final plan are used; neither is reconstructed.

The destination's absence in either plan never authorizes creation. Exclusive
publication is the authoritative no-overwrite check. A competing creator that wins
after planning causes blocked if this invocation has created nothing, or failed
with cleanup if its temporary file already exists. The competing destination is
never removed.

## 5. Platform capability gate

Before any creation, apply requires the existing mutation-containment capability
and directory-`fsync` capability for the existing project root. Capability checks
are read-only and create no probe object. Existing stable Activation capability
codes are not reused because their messages name Activation.

Unsupported capability returns blocked:

| Code | Message | Location |
| --- | --- | --- |
| `initialization.containment_unsupported` | `Safe project-contained Binding creation is not supported on this platform.` | `project:.` |
| `initialization.directory_fsync_unsupported` | `The project filesystem does not support directory fsync required for durable Binding creation.` | `project:.` |

An unavailable or uninspectable root remains governed by the 6A/root issues from
planning. An unexpected capability-probe error propagates to the CLI as a sanitized
system failure.

Regular-file `fsync` support cannot be proven read-only at a nonexistent Binding
destination. Apply does not create a probe file and does not reuse the existing
Binding-file probe against a missing path. It performs `fsync` on its exclusively
created temporary regular file as part of the transaction. Unsupported or failed
file synchronization after creation has begun is a failed result, followed by
cleanup.

The supported containment surface must include descriptor-relative `open`, `stat`,
`link`, and `unlink`, `O_NOFOLLOW`, exclusive ordinary-file creation, hard-link
publication that cannot replace a directory entry, and directory `fsync`. If any
required primitive is absent, apply blocks before creation.

## 6. Descriptor-relative containment

All mutation is performed relative to retained directory descriptors. Pathname-
based writes are forbidden.

1. After the final review, choose the root directory of the absolute reviewed path
   as the stable ancestor anchor, open and retain it read-only as a directory, and
   record its `(st_dev, st_ino)` identity. No process working-directory descriptor,
   environment-derived path, or merely immediate-parent descriptor is an anchor.
2. Starting at that anchor, open every existing component of the reviewed canonical
   project path in order with descriptor-relative, directory-only, no-follow
   operations. Record each component name and `(st_dev, st_ino)` identity and retain
   the final project descriptor as the only mutation descriptor. The canonical
   chain contains no symlink component.
3. Before temporary creation, publication, the cleanup decision, and success,
   reopen the complete canonical chain from the retained anchor and compare every
   component identity, including the project root, with the review. Reopened chain
   descriptors are read-only checks and are closed without mutation. The retained
   anchor identity itself is also reverified.
4. If the supplied project-root argument was deliberately accepted as a symlink,
   retain a second lexical-chain review from the same stable anchor: record every
   lexical directory component, then record the final symlink's own no-follow
   identity and exact link text. Separately record the fully resolved canonical
   target chain under rules 1–3. Each reachability check must prove both that the
   lexical chain and final symlink identity/text are unchanged and that resolving
   that unchanged link still denotes the exact reviewed canonical target chain.
   No intermediate lexical symlink is accepted.
5. Retain the canonical project descriptor through publication, verification,
   synchronization, and cleanup. Every destination and temporary operation uses
   only a literal single-component name and this descriptor. Never mutate through
   an anchor, lexical-chain descriptor, or newly reopened check descriptor.
6. A missing, renamed, replaced, identity-changed, or newly symlinked component in
   either required chain is blocked before creation and failed after creation
   begins. Renamed ancestors, a renamed project root, and a changed lexical symlink
   may be addressed through already-retained invocation descriptors only for
   same-invocation cleanup; none can be a successful publication target. No
   replacement path component is ever mutated.
7. Close every descriptor on success, block, failure, exception, and interruption
   under the close-failure rules in sections 9 and 10. No unclassified close
   failure may be ignored or reported as `created`.

The literal final name is `skill-collection.toml`. Temporary names are
`.skill-collection.toml.tmp-` plus 32 lowercase hexadecimal characters from a
cryptographically strong random source. Randomness affects only an ephemeral name,
never canonical Binding bytes, `binding_digest`, `plan_id`, JSON success output, or
project identity. A temporary collision retries with a new name up to 16 times;
exhaustion before creation is blocked with `initialization.temporary_unavailable`.

Neither name may contain `/`, `\\`, NUL, `.`/`..` components, user input, Profile
text, URL text, an absolute root, PID, hostname, timestamp, or environment data.

## 7. Exclusive publication transaction

The normative transaction is:

1. Reverify the stable anchor and every identity in the complete required canonical
   and, when applicable, lexical-symlink chains.
2. Create the temporary ordinary file relative to it with mode `0o600` and flags
   equivalent to `O_WRONLY | O_CREAT | O_EXCL | O_NOFOLLOW`. Immediately record
   its rooted location and `(st_dev, st_ino)` identity in an invocation ledger.
3. Write all exact UTF-8 bytes of the final plan's `binding_content`, handling short
   writes and `EINTR`. Hash the bytes independently and require equality with the
   action's `content_sha256` before publication.
4. Synchronize the temporary file with `fsync`, close its write descriptor, reopen
   it read-only/no-follow relative to the retained project descriptor, require a
   regular file with the recorded identity, read it to EOF, and require exact byte
   equality and digest equality. Close the read descriptor.
5. Reverify the retained project descriptor, stable anchor, complete required path
   chains, and all immutable final-plan inputs without another collection
   traversal: exact Binding bytes/digests/action and every reviewed component
   identity must still be the reviewed values.
6. Publish with a descriptor-relative hard link from the temporary name to literal
   `skill-collection.toml`. The call must have no replacement semantics and must not
   follow either name. Record the final entry's identity immediately. It must be a
   regular non-symlink file with the same identity as the temporary file.
7. `fsync` the project directory. Reverify the retained project descriptor, stable
   anchor, and complete required path chains; then reopen and verify the final file
   read-only and no-follow, requiring exact identity, bytes, raw digest, and
   semantic Binding digest.
8. **Commit** after all required path and Binding verification has completed and
   the final entry has been synchronized. This successful, synchronized, verified
   hard-link publication makes the Binding project-owned and irrevocable for this
   invocation.
9. Remove the temporary link after checking its identity, then `fsync` the project
   directory again. Clear it from the ledger only after both operations succeed.
10. Close all handles successfully, and return `created`.

After commit, apply performs no further path or Binding verification. Its only
remaining work is temporary-link cleanup and descriptor closure.

No `created` return is allowed before the Binding's bytes and directory entry are
synchronized and the temporary link is durably removed. At no point is a partial
Binding visible at the final name. If step 9 fails after commit, apply returns
`created_with_incomplete_cleanup`; it retains the valid Binding and reports the
temporary location either as remaining or as removed-but-not-durably-confirmed.
Hard-link publication is required; `rename`, `replace`,
pathname existence checks followed by ordinary creation, and direct writing of the
final path are forbidden.

The requested creation mode is `0o600`, subject to a stricter process `umask`.
Final permissions may therefore be stricter, but must never contain a group or
other permission bit and must never be broadened after creation. Apply neither
changes nor temporarily clears the process `umask`, and does not call `chmod` or
`fchmod`. Ownership is the operating-system ownership naturally assigned to the
creating process. ACL, extended-attribute, label, and ownership normalization are
outside this checkpoint.

## 8. Concurrent objects and precondition classification

Exclusive syscalls decide races; error strings do not. Before this invocation has
created an object:

- any existing final destination, including every 6A filesystem kind, yields the
  same `initialization.binding_exists` issue as planning and a blocked result;
- a transient object that appears and disappears without being observed need not
  be reported;
- a competitor that publishes first is never inspected beyond no-follow metadata
  needed for classification and is never opened or removed; and
- an anchor, canonical-chain, lexical-chain, final-symlink, project-root, or
  capability change yields `initialization.precondition_changed` unless a more
  specific stable issue applies.

After temporary creation, any precondition race or publication collision is a
failed operation because cleanup is required. The primary issue is
`initialization.precondition_changed`; the competing final object is excluded from
the ledger and cleanup.

`initialization.precondition_changed` has message
`A reviewed initialization precondition changed before Binding creation completed.`
and location `project:skill-collection.toml`.

Apply reports the state it actually observes. It does not promise to detect a
create/remove event that leaves no observable object, lock the project against
other tools, or make a successful Binding immutable after return.

### Portable POSIX concurrency boundary

This checkpoint uses a cooperative-concurrency threat model. Descriptor-relative
`link` provides exclusive, no-overwrite Binding publication even when another
cooperating or noncooperating process races to create the final name. Path-chain
checks continue to protect containment exactly as specified in section 6.

Portable POSIX provides no operation that atomically unlinks a directory entry
only when it has a specified `(st_dev, st_ino)` identity. Therefore the identity
check followed by `unlink` is permitted only for an invocation-owned provisional
entry or temporary hard link under this explicit limitation: this checkpoint
excludes a hostile same-authority process that replaces that exact entry in the
final verification-to-unlink interval. Such a process has the authority to alter
the project directory and is outside 6B's portable POSIX guarantee. A detected
missing, type-changed, or identity-changed entry is never removed and is reported
as remaining.

Linux `O_TMPFILE` plus an appropriate descriptor-to-name publication primitive is
a possible future hardened backend. It is not a Checkpoint 6B requirement and
does not alter the portable result or CLI contract.

## 9. Failure and cleanup

Each successful creating syscall is recorded before another fallible operation.
The ledger contains only rooted temporary/final locations and their exact device/
inode identities. It is memory-only and is never serialized.

Expected failures after creation begins return `failed` with one primary issue:

| Code | Message | Location |
| --- | --- | --- |
| `initialization.content_mismatch` | `The reviewed Binding content did not match its proposed digest.` | Binding |
| `initialization.file_fsync_failed` | `The Binding file could not be synchronized.` | Binding |
| `initialization.directory_fsync_failed` | `The project directory could not be synchronized after Binding creation.` | `project:.` |
| `initialization.binding_verification_failed` | `The created Binding could not be verified.` | Binding |
| `initialization.precondition_changed` | as defined above | Binding |
| `initialization.operation_failed` | `Project Binding creation could not be completed.` | Binding |

Platform-specific exception text, paths, errno names, temporary suffixes, and
partial bytes never appear in public issues or output. Issue selection is by
transaction stage and safe classification, not by disclosing the underlying
exception.

Before commit, cleanup runs in reverse publication order and only through retained
descriptors. After commit, cleanup never removes `skill-collection.toml`; it may
attempt only temporary-file removal:

Before choosing the cleanup path, apply performs the full-chain reachability check
from section 6. Its result controls reporting only: cleanup always addresses
invocation-owned objects through the retained canonical project descriptor and
never traverses or mutates a replacement chain. A failed reachability check cannot
prevent best-effort cleanup of objects already created by this invocation.

1. Before commit, if the final name is ledger-owned, `lstat` it without following,
   require an ordinary file with the exact recorded identity, unlink that name, and
   `fsync` the project directory, subject to the portable POSIX boundary in section
   8.
2. If the temporary name is ledger-owned, perform the same identity check, unlink
   it, and `fsync` the directory, subject to the same boundary.
3. Never remove an entry that is missing, changed identity, changed type, or was not
   recorded by this invocation. Report such an entry in `remaining_objects`.
4. Close retained descriptors even when removal or synchronization fails. A close
   failure during cleanup adds
   `initialization.cleanup_descriptor_close_failed` and never hides the primary
   issue, exception, or interruption.

Cleanup is best effort. Its deterministic issues use stable codes
`initialization.cleanup_identity_changed`, `initialization.cleanup_remove_failed`,
`initialization.cleanup_directory_fsync_failed`, and
`initialization.cleanup_descriptor_close_failed`, rooted at the affected public
location. A temporary location is exposed as
`project:.skill-collection.toml.tmp-<opaque>` only in cleanup output when it
actually remains; the suffix is treated as opaque. Cleanup issues never replace or
reorder the primary failure.

If both hard links exist and cleanup removes only one, the other is reported. Once
commit has occurred, the final Binding is retained even if temporary cleanup or a
descriptor close fails. A temporary removal or directory sync failure after commit
returns `created_with_incomplete_cleanup`, with exactly:

| Code | Message | Location |
| --- | --- | --- |
| `initialization.temporary_cleanup_incomplete` | `Binding creation completed, but temporary-file cleanup could not be confirmed.` | `project:.skill-collection.toml.tmp-<opaque>` |

The result's cleanup report also includes the underlying cleanup issue
(`initialization.cleanup_identity_changed`,
`initialization.cleanup_remove_failed`, or
`initialization.cleanup_directory_fsync_failed`) and lists that temporary rooted
location in `remaining_objects` when removal did not complete safely. If `unlink`
succeeded but its directory `fsync` failed, it instead records the temporary in
`removed_temporary_files` and reports no remaining object. It does not report the
committed Binding as remaining, removed, or recoverable.

A process crash or uncatchable termination may leave a complete final Binding, a
temporary file, or both. There is no durable transaction journal, automatic
recovery, startup scavenging, or orphan adoption. A later run blocks on a final
Binding. It never removes an orphan temporary file; manual project-owner review is
required. For `created_with_incomplete_cleanup`, recovery is: preserve
`skill-collection.toml`; verify its bytes and digest against the reported plan if
desired; then inspect the cleanup report. Only when it lists a temporary location
in `remaining_objects`, confirm that no cooperating initialization is active and
manually inspect and remove that exact temporary location. A directory-sync issue
with no remaining temporary object requires no manual deletion. Random names make
a later collision negligible but do not establish ownership.

## 10. Exceptions and interruption

For an unexpected `Exception` or `KeyboardInterrupt` before creation, close all
descriptors and re-raise without a cleanup report. After creation, perform the same
best-effort cleanup, attach the report to the original object as
`initialization_cleanup_report`, add a generic note only when objects or cleanup
issues remain, and re-raise the original object with its traceback.

Cleanup exceptions never mask the original exception or interruption. Failure to
attach a report or note also never masks it. The CLI recognizes both the existing
Activation cleanup attribute and the initialization attribute without treating
their types as interchangeable.

Descriptor-close behavior is stage-specific:

- while an earlier exception or interruption is pending, preserve that original
  object; record a close failure internally in its attached cleanup report when
  cleanup was required, but never replace the original;
- during an expected failed-result cleanup, add
  `initialization.cleanup_descriptor_close_failed` and retain the primary result
  issue; and
- after commit, any descriptor close failure is an unexpected exception. While
  sufficient retained descriptors remain, attempt only identity-checked cleanup of
  the temporary hard link; never roll back the committed Binding. Attach the
  cleanup report to the close exception and re-raise it. If the failing close has
  already made a descriptor unusable, cleanup uses only other still-valid retained
  descriptors and reports the temporary as remaining when safe removal cannot be
  established.

The last case is never returned as `created` and is never converted to an expected
failed result. Its CLI outcome is exit `3` with sanitized system output and attached
cleanup details only when cleanup is incomplete.

## 11. CLI contract and output

The existing read-only forms and formats are unchanged:

```text
skill-collection init-project [--collection-root PATH] --project-root PROJECT \
  --profile PROFILE [--format json|text]
```

Apply adds only:

```text
--apply --plan-id PLAN_ID
```

`--apply` requires `--plan-id`; `--plan-id` requires `--apply`. Missing or invalid
pairing is exit `2`. There is no confirmation prompt. Root and Profile options have
the same meaning as planning. Apply supports `json` and `text`; formatting occurs
only after the single result exists and never reruns planning or mutation.

Successful JSON is the existing deterministic envelope:

```json
{
  "command": "init-project",
  "result": {
    "binding_digest": "sha256:...",
    "binding_location": {
      "relative_path": "skill-collection.toml",
      "root": "project"
    },
    "cleanup": null,
    "issues": [],
    "plan_id": "sha256:...",
    "status": "created"
  },
  "schema_version": 1
}
```

Blocked, failed, and `created_with_incomplete_cleanup` results contain the same
exact field set, with nulls and issues according to section 3. Cleanup uses the
public fields from section 3 and the existing deterministic location/issue
encodings. JSON remains sorted, UTF-8, two-space indented, and terminated by one
LF.

Text uses the existing inspection renderer conventions and this exact layout:

```text
Project initialization apply: <status>
Binding: <rooted-location-or->
Binding digest: <digest-or->
Plan ID: <plan-id-or->

Issues (<decimal>):
<issue blocks or "None.">

Cleanup:
<cleanup block or "None.">
```

Cleanup rendering reuses the deterministic issue and rooted-location formatting
concepts but names only Binding and temporary files. It never prints absolute paths,
exception text, descriptors, inode/device values, permissions, environment data,
or timestamps.

For `created_with_incomplete_cleanup`, text adds this exact recovery paragraph
after the cleanup block:

```text
Recovery: Keep the Binding. Inspect the Cleanup section before removing any
reported temporary file.
```

Streams and exits are exact:

| Condition | stdout | stderr | Exit |
| --- | --- | --- | --- |
| Created | result | empty | `0` |
| Created with incomplete cleanup | result | empty | `1` |
| Blocked | result | empty | `1` |
| Failed with cleanup result | result | empty | `1` |
| Usage error | empty | baseline usage error | `2` |
| Unexpected failure | empty | sanitized system JSON | `3` |
| Keyboard interruption | empty | empty unless cleanup is incomplete | `130` |

For an unexpected failure or interruption with incomplete initialization cleanup,
stderr uses the baseline error envelope with the attached cleanup report. Its
message is respectively `An unexpected system failure occurred.` or
`Project initialization was interrupted.` No traceback or exception text is
rendered.

## 12. Stable issue codes

Checkpoint 6B adds these stable CLI-schema-v1 codes:

```text
initialization.stale_plan
initialization.containment_unsupported
initialization.directory_fsync_unsupported
initialization.temporary_unavailable
initialization.precondition_changed
initialization.content_mismatch
initialization.file_fsync_failed
initialization.directory_fsync_failed
initialization.binding_verification_failed
initialization.operation_failed
initialization.temporary_cleanup_incomplete
initialization.cleanup_identity_changed
initialization.cleanup_remove_failed
initialization.cleanup_directory_fsync_failed
initialization.cleanup_descriptor_close_failed
```

The existing 6A codes and all collection-selection issues retain their exact
meaning. Apply must prefer an existing specific planning or destination code over a
generic 6B code when that condition is safely observable before creation.

## 13. Public-seam acceptance tests

Acceptance requires tests through public Python and CLI seams, with syscall spies
or controlled fakes for transaction edges.

1. **Frozen result graph and exports**: exact public exports, annotations,
   dataclass immutability, tuple fields, and absence of mutable/live resources.
2. **Review handshake**: blocked initial plans pass through unchanged; missing,
   malformed, wrong, old, URL-changed, revision-changed, Profile-changed,
   content-changed, destination-changed, and equivalent-root plan ids are covered.
   Spies prove exactly one initial and one final plan call on the mutation path and
   no duplicated collection-selection work outside those calls.
3. **Exact creation golden**: apply publishes exactly the 6A canonical bytes with
   requested mode `0o600` subject to the process `umask`; tests under representative
   restrictive masks prove that no group/other permission bit is ever present and
   that permissions are never broadened. Raw and semantic digests match the
   reviewed plan; only the Binding remains after `created`; success JSON/text are
   golden and deterministic. A post-commit temporary cleanup failure instead
   produces `created_with_incomplete_cleanup`, retains both the valid Binding and
   the reported temporary file, emits its exact issue/recovery output, and exits
   `1`.
4. **Root and ancestor matrix**: missing, file-valued, symlinked, replaced,
   renamed, unreadable, and identity-changed roots exercise the declared
   containment behavior. Before temporary creation, publication, cleanup decision,
   and success, spies prove complete component-by-component reopening from the
   stable anchor. Races independently rename, replace, remove, or symlink the
   project root, its immediate parent, and a higher reviewed ancestor before and
   after creation. Renamed originals are used only for cleanup, and no replacement
   component is mutated.
5. **Lexical project-root symlink matrix**: an accepted final symlink remains
   confined to its separately reviewed canonical target chain. Races replace,
   rename, retarget, remove, and recreate the lexical symlink and alter an ancestor
   in either its lexical or target chain. Tests compare the symlink's no-follow
   identity and exact text as well as every target component, and prove that a
   changed symlink cannot become a successful publication path.
6. **Destination matrix**: every public `FilesystemKind`, empty/invalid TOML files,
   symlinks, FIFOs, sockets, and available device kinds block without opening,
   following, changing, or removing the object.
7. **Race matrix**: competitors create/remove before final review, before temporary
   creation, before publication, during publication, after publication, and during
   cleanup. The test proves exclusive publication, final-observation semantics, and
   that competitor objects are never removed outside the explicitly excluded
   same-authority final verification-to-unlink interval in section 8.
8. **Atomic visibility**: observers see no final name before publication and only
   complete verified bytes afterward. Tests reject direct final-path writes,
   `rename`, and replacement-capable operations.
9. **Capability gate**: every missing containment primitive and unsupported
   directory `fsync` blocks before any creation. No capability probe creates a file.
   File-`fsync` failures begin only after exclusive temporary creation.
10. **Descriptor discipline**: every mutating operation is descriptor-relative;
   literal names and required no-follow/exclusive flags are asserted; no
   write-capable descriptor targets an existing final object; all descriptors close
   on every return and exception edge. Close failures with a pending primary
   failure, during cleanup, and after otherwise-complete publication exercise the
   three exact behaviors in section 10; none silently returns `created`.
11. **Short I/O and verification**: short writes, `EINTR`, partial reads, digest
    mismatch, changed identity/type, hard-link mismatch, file-sync failure,
    directory-sync failure, and final reread failure produce exact results and
    cleanup.
12. **Failure ledger and cleanup**: inject failure after every creating and
    synchronizing syscall. Snapshot the result and filesystem; only invocation-
    owned matching identities are removed in reverse order before commit,
    incomplete cleanup is reported, and the primary issue is preserved. After
    commit, the Binding is never removed; temporary cleanup failure has the exact
    created-with-incomplete-cleanup result defined in section 9.
13. **Exception/interruption snapshots**: inject `Exception` and
    `KeyboardInterrupt` before and after each creation boundary. Assert original
    identity/traceback preservation, attached cleanup only after creation,
    descriptor closure, stable stderr, exits `3`/`130`, and no traceback leakage.
14. **State noninterference**: snapshot collection and project trees (including
    types, bytes, modes, links, and absence of lock/temp artifacts), Git refs,
    index, worktree, remotes/config, environment, credential-helper calls, network
    calls, and global Skill locations. `created` differs only by the Binding.
    `created_with_incomplete_cleanup` either retains only its reported temporary
    file, which appears in `remaining_objects`, or has removed that temporary file,
    which appears in `removed_temporary_files` while directory synchronization is
    reported as unconfirmed. Block differs by nothing; cleaned pre-commit failure
    differs by nothing.
15. **No orphan adoption**: pre-existing names matching the temporary pattern are
    never opened, removed, or reported as invocation-owned; collision retry is
    bounded and deterministic under a fake random source.
16. **Baseline compatibility**: every pre-6B test remains byte-for-byte unchanged
    and green. Planning output, ids, issue order, 6A read-only syscall prohibitions,
    `validate`, `scan`, `plan`, `activate`, `status`, and `doctor` behavior are
    unchanged.

## 14. Explicit exclusions

Checkpoint 6B does not:

- activate, plan Activation, create `.agents`, create Skill links, or create an
  Activation Record;
- overwrite, merge, compare, adopt, repair, normalize, chmod, update, remove, or
  migrate an existing Binding;
- add Binding `add`/`remove` editing, Profile defaults, target selection, URL or
  revision overrides, or interactive prompting;
- hold a cross-process lock, reserve a name during 6A planning, guarantee detection
  of transient objects, or guarantee immutability after return;
- fetch, clone, pull, push, inspect Git remotes, consult URL rewrites or credential
  helpers, use the network, mutate Git state, or mutate environment/global state;
- create a durable transaction journal, automatically recover from process crash,
  scavenge temporary files, or infer ownership of artifacts from their names;
- change Catalog schema, Binding schema, canonical Binding serialization,
  collection revision rules, Collection URL grammar, Profile resolution, 6A plan
  identity, Activation ownership, or existing output schema version; or
- authorize implementation of Checkpoint 6B before this contract is independently
  reviewed and accepted.

The portable protocol also does not defend against a hostile same-authority process
that races an invocation-owned provisional entry or temporary hard link between its
final identity verification and `unlink`. Linux-only unnamed-temporary-file
hardening is explicitly deferred.

## 15. Approval gate

Implementation must not begin while this document has status `Proposed for review`.
Review must explicitly approve the public result shape, stale-plan handshake,
descriptor-relative containment, hard-link publication, synchronization and
cleanup semantics, capability boundary, stable issues, CLI behavior, and exclusions.

After approval, implementation proceeds test-first. The implementation checkpoint
must stop for independent review without committing unless separately authorized.
