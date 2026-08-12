# Checkpoint 5 proposal: human-readable, read-only project inspection

Status: **Proposed for review; not accepted and not implemented.**

Baseline: `3f5f21a20f3401fb8d680b02984c26ac3d9db264`

## 1. Purpose and boundary

Checkpoint 5 adds two public, read-only inspection seams:

- `status()` answers “what state is this project in?” by presenting the existing
  Activation Review in stable project-oriented categories.
- `doctor()` answers “can this project be safely activated on this platform?” by
  combining `status()` with non-mutating platform capability inspection.

The checkpoint adds deterministic text rendering for these two seams. JSON remains
the default CLI format and the existing JSON envelope remains unchanged.

Both seams are observers. They may read and hash files, inspect metadata and links,
open existing objects read-only, and run the existing validation and Activation
Review logic. They must not create, replace, remove, rename, relink, normalize,
repair, initialize, deactivate, fetch, update, or otherwise mutate anything.

## 2. Candidate vocabulary

These terms are proposed here and must not be added to `CONTEXT.md` until this
contract is accepted.

**Project Status**: An immutable, deterministic projection of an Activation Review
for a project. It reports observed state; it is neither an Activation Plan nor
authorization to mutate.

**Doctor Report**: A Project Status plus inspection of the platform capabilities
already required by safe Activation. It is not a probe transaction and does not
certify that a later Activation will succeed after concurrent state changes.

**Guidance**: Stable presentation metadata attached to an existing issue. Guidance
does not reinterpret whether an issue blocks Activation and does not repair it.

## 3. Public Python seam

The following contract shows the new public exports from `skill_collection` and,
where marked, the one shared internal probe result that is deliberately not
exported:

```python
StatusCategory = Literal["blocked", "inactive", "active", "drifted"]
DoctorCategory = Literal["ok", "attention", "blocked"]
CapabilityOutcome = Literal["supported", "unsupported", "not-inspected"]

# Shared internal result used by Doctor and Activation preflight; not a public export.
CapabilityProbeResult = Literal[
    "supported",
    "unsupported",
    "target-unavailable",
]

@dataclass(frozen=True, slots=True)
class Guidance:
    id: str
    text: str

@dataclass(frozen=True, slots=True)
class GuidedIssue:
    issue: ValidationIssue
    guidance: Guidance

@dataclass(frozen=True, slots=True)
class RecommendedCommand:
    id: str
    command: str
    description: str

@dataclass(frozen=True, slots=True)
class ProjectStatus:
    category: StatusCategory
    profile: str | None
    activation_id: str | None
    plan_id: str | None
    pending_action_count: int
    unchanged_link_count: int
    issues: tuple[GuidedIssue, ...]
    recommended_commands: tuple[RecommendedCommand, ...]

@dataclass(frozen=True, slots=True)
class CapabilityCheck:
    id: Literal[
        "safe-project-containment",
        "project-directory-fsync",
        "binding-file-fsync",
    ]
    outcome: CapabilityOutcome
    summary: str
    issue: GuidedIssue | None

@dataclass(frozen=True, slots=True)
class DoctorReport:
    category: DoctorCategory
    status: ProjectStatus
    capabilities: tuple[CapabilityCheck, ...]
    recommended_commands: tuple[RecommendedCommand, ...]

def status(
    collection_root: str | Path,
    project_root: str | Path,
) -> ProjectStatus: ...

def doctor(
    collection_root: str | Path,
    project_root: str | Path,
) -> DoctorReport: ...
```

All public result objects are value objects: frozen, slotted dataclasses containing
only immutable scalars, existing immutable value objects, or tuples. No result
contains a live `Path`, file descriptor, callable, exception, or mutable collection.

`project_root` is deliberately required. Collection-only inspection remains the
job of the existing `scan()` and `validate()` seams.

## 4. Exact Project Status categories

`status()` calls `prepare_activation(collection_root, project_root)` exactly once
and maps the returned `ActivationReview` mechanically:

| Activation Review | Project Status | Meaning |
| --- | --- | --- |
| `status == "blocked"` | `blocked` | Existing validation, discovery, planning, or ownership review prevents a ready review. |
| `status == "ready"` and `mode == "initial"` | `inactive` | The project has valid intent but no Activation Record yet. |
| `status == "ready"` and `mode == "repeat"` | `active` | The recorded Activation and all managed links already match current intent. |
| `status == "ready"` and `mode == "repair"` | `drifted` | Ownership remains valid, but the existing review found pending actions. This is an observation only. |

No fifth category is permitted in schema version 1. An internally inconsistent
review is an unexpected system failure, not a new status category.

Field mapping is equally mechanical:

- For a ready review, `profile` is
  `review.proposed_activation_record.profile`, `activation_id` is
  `review.activation_id`, `plan_id` is `review.plan_id`,
  `pending_action_count` is exactly `len(review.actions)`, and
  `unchanged_link_count` is exactly `len(review.unchanged_links)`.
- For a blocked review, `profile`, `activation_id`, and `plan_id` are `None`, and
  `pending_action_count` and `unchanged_link_count` are both zero.
- `issues` preserves each `review.blocking_issues` object exactly and only attaches
  presentation guidance. Ready states have no issues.
- Absolute collection and project root paths are never included in a result.

`status()` must not call `validate()`, `scan()`, `plan_activation()`, or private
equivalents separately. `prepare_activation()` remains the sole owner of validation,
discovery, resolution, planning, and ownership rules. This prevents duplicated
domain logic and inconsistent issue ordering. In particular, a blocked result must
not reread the Binding or invoke another domain seam to obtain a profile or counts.

## 5. Exact Doctor categories

`doctor()` obtains one `ProjectStatus` by calling `status()` and appends the three
capability checks in the fixed order shown in the type declaration.

The aggregate Doctor category is:

1. `blocked` when Project Status is `blocked` or any capability is `unsupported`;
2. otherwise `attention` only when at least one capability is `not-inspected`;
3. otherwise `ok`: Project Status is `active`, `inactive`, or `drifted` and every
   capability is `supported`.

These are aggregation categories only. Doctor must not add platform findings to
the Project Status or change its category. The nested Project Status communicates
whether initial Activation or repair remains pending; Doctor does not repeat that
state in its own category.

## 6. Platform capability inspection

Doctor inspects exactly the capabilities enforced by Checkpoint 4B Activation:

| Check id | Supported when | Unsupported issue |
| --- | --- | --- |
| `safe-project-containment` | Both `os.O_NOFOLLOW` and `os.O_DIRECTORY` exist; `os.open`, `os.stat`, `os.readlink`, `os.mkdir`, `os.symlink`, `os.unlink`, `os.rmdir`, and `os.link` are in `os.supports_dir_fd`; and `os.link` and `os.stat` are in `os.supports_follow_symlinks`. | Existing code `activation.containment_unsupported` at `project:.` |
| `project-directory-fsync` | The existing resolved project root can be opened with `O_RDONLY | O_DIRECTORY` and `os.fsync()` succeeds on that descriptor. | Existing code `activation.directory_fsync_unsupported` at `project:.` |
| `binding-file-fsync` | The existing `skill-collection.toml` can be opened with `O_RDONLY | O_NOFOLLOW` and `os.fsync()` succeeds on that descriptor. | Existing code `activation.file_fsync_unsupported` at `project:skill-collection.toml` |

Missing, replaced, unsafe, or inaccessible inspection targets produce
`not-inspected` with `issue = None`; they do not prove a platform limitation and
must not be converted into an invented platform issue. This applies whether the
condition exists before inspection or is observed while opening or verifying the
target. Containment can always be inspected from runtime capability sets because
it has no filesystem target.

Capability summaries are fixed presentation strings:

| Check id | Outcome | Exact summary |
| --- | --- | --- |
| `safe-project-containment` | `supported` | `Required no-follow and directory-relative operations are available.` |
| `safe-project-containment` | `unsupported` | `Required no-follow or directory-relative operations are unavailable.` |
| `project-directory-fsync` | `supported` | `The project filesystem supports directory fsync.` |
| `project-directory-fsync` | `unsupported` | `The project filesystem does not support directory fsync.` |
| `project-directory-fsync` | `not-inspected` | `Directory fsync was not inspected because the project root could not be safely inspected.` |
| `binding-file-fsync` | `supported` | `The project filesystem supports regular-file fsync.` |
| `binding-file-fsync` | `unsupported` | `The project filesystem does not support regular-file fsync.` |
| `binding-file-fsync` | `not-inspected` | `Regular-file fsync was not inspected because the Binding could not be safely inspected.` |

Implementation must refactor the Checkpoint 4B capability checks into shared
non-mutating predicates used by both Doctor and Activation preflight. This
refactor is explicitly authorized even though it changes the existing broad
`OSError` classification; it must not change Activation's write boundary or add an
Activation operation. Doctor must not copy a second version of the predicates.

Each shared filesystem predicate returns exactly one `CapabilityProbeResult`:

- `supported`: the intended target was safely opened and verified and its exact
  `fsync` operation succeeded;
- `unsupported`: the intended target was safely opened and verified and `fsync`
  failed with one of the exact unsupported errnos below;
- `target-unavailable`: the intended target could not be safely acquired or did
  not remain the same suitable object long enough to test the capability.

The containment predicate has no target and therefore returns only `supported` or
`unsupported`. Absence of either `os.O_NOFOLLOW` or `os.O_DIRECTORY` returns
`unsupported` with `activation.containment_unsupported`. Doctor maps
`target-unavailable` to public outcome `not-inspected` with no issue. A capability
is `unsupported` only when the shared predicate returns `unsupported`; Doctor must
not map every `OSError` from acquisition, `fsync`, verification, or close handling
to that result.

### Exact exception and errno classification

The classification order is normative:

1. `InterruptedError` from acquisition, inspection, `fsync`, or close propagates
   unchanged. It is never a probe result.
2. During target acquisition or identity/type verification,
   `FileNotFoundError`, `NotADirectoryError`, and `PermissionError`, plus an
   `OSError` whose errno is `ENOENT`, `ENOTDIR`, `EACCES`, `EPERM`, `ELOOP`, or
   `ESTALE`, return `target-unavailable`. A verified identity mismatch, unsafe
   symlink, wrong object type, or unreadable/inaccessible target also returns
   `target-unavailable` without calling `fsync`.
3. After a suitable target has been opened and verified, only the following errnos
   from the `fsync` call return `unsupported`:

   | Predicate | Exact unsupported errnos |
   | --- | --- |
   | project-directory fsync | `EINVAL`, `ENOTSUP`, `EOPNOTSUPP` |
   | regular-file fsync | `EINVAL`, `ENOTSUP`, `EOPNOTSUPP` |

   `ENOTSUP` and `EOPNOTSUPP` may be aliases on a platform; comparison is by their
   integer errno values. A name absent from the platform's `errno` module simply
   contributes no value. No other errno means `unsupported` in schema version 1.
4. Every other exception, including every other `OSError` from acquisition,
   inspection, `fsync`, or close, propagates unchanged to the existing interruption
   or unexpected system-error boundary. It is never converted to a probe result.

An acquisition error in the target-unavailable set is classified there only while
acquiring or verifying the target. The same errno from `fsync` is governed solely
by the fsync table; for example, `EACCES` or `EPERM` from `fsync` propagates as
unexpected rather than becoming `target-unavailable` or `unsupported`.

Target classification and platform capability classification are separate:

| Observation | Outcome |
| --- | --- |
| Target is missing, was replaced, is the wrong object type, traverses an unsafe link, cannot be opened safely, or is inaccessible | `not-inspected`, no issue |
| Shared containment predicate identifies missing `O_NOFOLLOW`, `O_DIRECTORY`, no-follow support, or directory-relative support | `unsupported` with `activation.containment_unsupported` |
| Shared directory-fsync predicate identifies that directory fsync is unsupported for an otherwise safely opened and verified project root | `unsupported` with `activation.directory_fsync_unsupported` |
| Shared file-fsync predicate identifies that regular-file fsync is unsupported for an otherwise safely opened and verified Binding | `unsupported` with `activation.file_fsync_unsupported` |
| Any failure not classified by the preceding target-state or shared capability cases | Propagate to the existing unexpected system-error boundary; do not return a Doctor Report |

The inspection must verify with descriptor metadata that an opened target has the
expected identity and type before calling `fsync`, and must verify the path identity
again before accepting the result. A replacement observed before or during
inspection returns `target-unavailable`, which Doctor renders as `not-inspected`.
Every descriptor opened by capability inspection must be closed exactly once on
every success, target-unavailable, unsupported, interruption, and unexpected-
failure path. Close-failure precedence is exact:

- when no earlier exception or interruption is pending, the close failure
  propagates unchanged as unexpected;
- when an earlier exception or interruption is pending, that original failure is
  preserved and propagated; the close failure must not replace it;
- the later close failure is attached internally to the original failure as an
  exception note or chained diagnostic when the runtime permits, without changing
  the original public classification;
- existing CLI interruption and unexpected-error output remains sanitized and
  exposes neither exception detail.

A close failure is never classified as `unsupported` or `target-unavailable`.

Activation preflight uses the same results before any transaction or write:

- `supported` permits Activation to continue;
- `unsupported` returns the corresponding existing
  `activation.*_unsupported` blocking issue;
- `target-unavailable` returns the existing `activation.precondition_changed`
  blocking issue;
- a propagated interruption or unexpected exception reaches the existing CLI
  interruption or system-error boundary.

Thus every result other than confirmed `supported` blocks Activation before
writing. The refactor does not weaken Checkpoint 4B containment, freshness, or
no-overwrite rules.

Checkpoint 5 intentionally refines Checkpoint 4B's broad capability-error
classification. Existing capability tests may be updated only to replace synthetic
errno-less `OSError("unsupported")` failures with a documented unsupported errno
such as `EINVAL`. Their tested outcome and all other assertions must remain
unchanged. No other Checkpoint 1–4B tests may change.

Capability inspection may use `stat`, `lstat`, `readlink`, `resolve`, read-only
`os.open`, `os.close`, and `os.fsync` on an already-existing read-only descriptor.
It must never use `O_WRONLY`, `O_RDWR`, `O_CREAT`, `O_EXCL`, `O_TRUNC`, or
`O_APPEND`; create a probe file or directory; create or replace a link; write bytes;
or remove, rename, chmod, or touch an object. Calling `fsync` on an existing
read-only descriptor is an inspection of the exact durability capability used by
Activation, not a probe write.

No Checkpoint 5 code may invoke Git. Existing read-only Git-backed Source validation
inside `validate()` remains unchanged and may be reached through
`prepare_activation()`; Checkpoint 5 adds no Git command or Git interpretation.

## 7. Stable guidance

Guidance is selected only by issue code. It may explain where a user should look,
but it must not decide blocking, suppress an issue, rewrite its message/location,
or infer a repair. Schema version 1 defines this exhaustive mapping for issues that
can be produced by `prepare_activation()` at the baseline:

| Guidance id | Issue codes | Exact guidance text |
| --- | --- | --- |
| `inspect.root` | `root.missing` | `Provide existing collection and project directories, then inspect again.` |
| `inspect.document` | `document.missing`, `toml.malformed`, `field.required`, `field.invalid`, `field.unexpected`, `field.duplicate` | `Correct the reported TOML document or field, then validate again.` |
| `inspect.source` | `source.duplicate_id`, `source.invalid`, `source.path_outside_collection`, `source.path_symlink`, `source.path_unavailable`, `source.submodule_dirty`, `source.submodule_invalid`, `source.submodule_missing`, `source.submodule_unpinned` | `Correct the reported Source state, then validate and scan again.` |
| `inspect.catalog` | `catalog.skill_not_discovered`, `discovery.ambiguous_catalog`, `discovery.uncataloged`, `discovery.unreadable`, `skill.duplicate_id`, `skill.missing`, `skill.name_collision`, `skill.path_outside_source`, `skill.remove_missing`, `skill.source_missing` | `Correct Catalog or Skill discovery state, then validate and scan again.` |
| `inspect.composition` | `group.cycle`, `group.duplicate_name`, `group.missing`, `profile.duplicate_name`, `profile.inheritance_cycle`, `profile.invalid_selection`, `profile.missing` | `Correct the reported Group or Profile composition, then validate again.` |
| `inspect.binding` | `binding.collection_revision_mismatch`, `binding.target_outside_project` | `Correct the project Binding so it selects the intended pinned collection state, then validate again.` |
| `inspect.activation-ownership` | `activation.broken_symlink`, `activation.owned_object_mismatch`, `activation.record_exists`, `activation.record_intent_mismatch`, `activation.record_invalid`, `activation.record_invalid_type`, `activation.record_noncanonical`, `activation.record_outside_project`, `activation.record_path_owned`, `activation.repair_unowned_directory`, `activation.target_owned`, `activation.unrecorded_object` | `Review the reported project-owned or Activation-owned object; inspection will not change it.` |
| `inspect.platform-containment` | `activation.containment_unsupported` | `Use a platform that provides the no-follow and directory-relative operations required for safe Activation.` |
| `inspect.platform-directory-fsync` | `activation.directory_fsync_unsupported` | `Use a project filesystem that supports directory fsync before applying Activation.` |
| `inspect.platform-file-fsync` | `activation.file_fsync_unsupported` | `Use a project filesystem that supports regular-file fsync before applying Activation.` |

For forward compatibility, an unknown issue code receives guidance id
`inspect.unclassified` and exact text
`Review the reported issue, then run status again.` The original issue is preserved.
Adding a known mapping is additive; changing an existing guidance id or its meaning
requires a CLI schema version change.

## 8. Recommended next commands

Commands are presentation data, not execution requests. Checkpoint 5 never runs a
recommended command. Command strings use the stable placeholders
`<collection-root>`, `<project-root>`, and `<plan-id>` and therefore do not disclose
absolute roots.

The schema version 1 command registry is:

| id | Exact command | Description |
| --- | --- | --- |
| `validate` | `skill-collection validate --collection-root <collection-root> --project-root <project-root>` | `Validate collection and project documents.` |
| `scan` | `skill-collection scan --collection-root <collection-root>` | `Inspect Skill discovery and Catalog correlation.` |
| `review-activation` | `skill-collection activate --collection-root <collection-root> --project-root <project-root>` | `Review the current Activation without applying it.` |
| `inspect-doctor` | `skill-collection doctor --collection-root <collection-root> --project-root <project-root>` | `Inspect project state and platform capabilities.` |

Project Status recommends commands in this fixed, duplicate-free order:

- `active`: `inspect-doctor`.
- `inactive` or `drifted`: `review-activation`.
- `blocked`: `validate`; add `scan` after it when any issue uses
  `inspect.source` or `inspect.catalog`; add `inspect-doctor` after those when an
  issue uses platform guidance.

Doctor Report starts with the Project Status commands, removes `inspect-doctor`
because it would recurse, and appends no mutation command. In particular, neither
result invents `init`, `repair`, `relink`, `deactivate`, or an implicit apply.

## 9. CLI contract

Checkpoint 5 adds:

```text
skill-collection status [--collection-root PATH] --project-root PATH [--format json|text]
skill-collection doctor [--collection-root PATH] --project-root PATH [--format json|text]
```

`--collection-root` continues to default to the current directory.
`--project-root` is required. `--format` is local to these two commands and defaults
to `json`. Existing commands and their output contracts do not change.

### JSON

JSON uses the existing envelope and serializer:

```json
{
  "command": "status",
  "result": {
    "activation_id": "sha256:...",
    "category": "inactive",
    "issues": [],
    "pending_action_count": 5,
    "plan_id": "sha256:...",
    "profile": "base",
    "recommended_commands": [
      {
        "command": "skill-collection activate --collection-root <collection-root> --project-root <project-root>",
        "description": "Review the current Activation without applying it.",
        "id": "review-activation"
      }
    ],
    "unchanged_link_count": 0
  },
  "schema_version": 1
}
```

Serialization requirements are exact:

- UTF-8, `ensure_ascii=False`, two-space indentation, lexicographically sorted
  object keys, and exactly one final LF, matching `json_document()`.
- Tuples serialize as arrays in their defined order. No set or filesystem iteration
  order may reach output.
- Issue order is the existing normalized Activation Review order. Capability order
  and recommended-command order are fixed above.
- Repeated inspection of unchanged roots produces byte-identical output, excluding
  genuine concurrent filesystem changes.
- Output contains rooted `Location` values and placeholders, never absolute roots,
  exception strings, tracebacks, file descriptors, inode numbers, permission bits,
  environment values, or timestamps.

### Text

Text is a rendering of the same immutable result; it must not run a second
inspection. It uses UTF-8, LF line endings, no ANSI styling, no terminal-width
wrapping, and exactly one final LF.

Status text has this exact section and field order:

```text
Project status: <category>
Profile: <profile-or->
Activation ID: <activation-id-or->
Plan ID: <plan-id-or->
Pending actions: <decimal>
Unchanged links: <decimal>

Issues (<decimal>):
<issue blocks or "None.">

Recommended next commands (<decimal>):
<command blocks or "None.">
```

Each issue block is:

```text
<1-based-index>. [<code>] <message>
   Location: <root>:<relative_path>
   Related: <root>:<relative_path>, <root>:<relative_path>
   Guidance: <guidance text>
```

`Related:` is omitted when there are no related locations. Embedded `\r`, `\n`,
and `\t` in issue data are escaped as the two-character sequences `\\r`, `\\n`,
and `\\t` so one value cannot alter the layout.

Each command block is:

```text
<1-based-index>. <command>
   <description>
```

Doctor text uses this exact outer layout:

```text
Doctor: <category>

Capabilities (3):
- <id>: <outcome> — <summary>

Project:
  Status: <status-category>
  Profile: <profile-or->
  Activation ID: <activation-id-or->
  Plan ID: <plan-id-or->
  Pending actions: <decimal>
  Unchanged links: <decimal>

  Issues (<decimal>):
  <issue blocks or "None.">

Recommended next commands (<decimal>):
<command blocks or "None.">
```

The three capability lines occur consecutively in their fixed order. If a
capability has an issue, the issue block immediately follows its capability line,
with every line prefixed by two spaces. Project issue blocks use the Status issue
format with every line prefixed by two spaces. `None.` is therefore rendered as
`  None.` in the Project section. The Status recommended-command section is omitted
from the nested Project rendering; Doctor's own section is rendered once.

JSON and text carry the same categories, counts, issues, guidance, capability
outcomes, and recommended commands. Text may omit only JSON field names that are
made explicit by its fixed labels.

### Streams and exit codes

For either format, an expected inspection result is written to stdout and stderr is
empty.

| Condition | Exit code |
| --- | --- |
| `status` category `active` | `0` |
| `status` category `inactive`, `drifted`, or `blocked` | `1` |
| `doctor` category `ok` | `0` |
| `doctor` category `attention` or `blocked` | `1` |
| CLI usage error, including missing `--project-root` or invalid format | `2` |
| Unexpected system failure | `3` |
| Keyboard interruption | `130` |

Usage and unexpected-error rendering retain the Checkpoint 4B behavior. A format
choice does not change status categorization or exit code.

## 10. Public-seam acceptance tests

Acceptance requires tests at the public Python and CLI seams, not only helper tests.

1. **Frozen results**: every new result dataclass rejects assignment; every
   collection field is a tuple; results contain no `Path` or mutable container.
2. **Exact status mapping**: synthetic or fixture-backed `blocked`, `initial`,
   `repeat`, and `repair` reviews map to `blocked`, `inactive`, `active`, and
   `drifted`. Ready counts equal `len(review.actions)` and
   `len(review.unchanged_links)` exactly. Blocked results have no profile or ids and
   both counts are zero.
3. **Single source of domain truth**: a mocked `prepare_activation()` is called once
   by `status()`; public or private validation/scanning/planning functions are not
   called separately. A blocked review test forbids reopening or rereading the
   Binding. Every blocking `ValidationIssue` remains equal to the issue nested in
   the corresponding `GuidedIssue`.
4. **Read-only status**: byte-and-type snapshots of both roots before and after two
   calls are identical, including symlink text and broken links.
5. **Capability mapping**: supported, exactly unsupported, and `not-inspected`
   fixtures produce the exact capability ids, order, outcomes, issue presence, and
   aggregate Doctor categories. With every capability supported, active, inactive,
   and drifted statuses all produce Doctor `ok`; only a `not-inspected` capability
   produces `attention`; blocked status or exact unsupported capability produces
   `blocked`. Public Doctor tests independently remove `os.O_NOFOLLOW` and
   `os.O_DIRECTORY`; each reports containment `unsupported` with
   `activation.containment_unsupported` and aggregate category `blocked`.
6. **Capability failure distinction**: missing, replaced, wrong-type, unsafe-link,
   and each specified inaccessible/acquisition errno return `target-unavailable`,
   rendered as `not-inspected` without an issue. For directory and regular-file
   probes separately, `EINVAL`, `ENOTSUP`, and `EOPNOTSUPP` from `fsync` return
   `unsupported`; aliases and absent platform errno names are covered. Every other
   `OSError` is tested at acquisition, verification, `fsync`, and close and
   propagates to the existing system-error boundary. `InterruptedError` is tested
   at each stage and propagates unchanged.
7. **No probe writes and descriptor closure**: during Doctor, spies fail the test
   on write-capable `os.open` flags or calls to filesystem creation, byte-writing,
   rename, chmod, link creation, unlink, or removal functions. Every opened
   read-only descriptor is closed exactly once on supported, target-unavailable,
   unsupported, interruption, and unexpected-failure paths. A close failure
   propagates as unexpected when it is the first failure. When an original
   `InterruptedError`, `KeyboardInterrupt`, or unexpected exception is pending, a
   later close failure is retained only as an internal note or chained diagnostic;
   the original object and public CLI classification are preserved and sanitized.
8. **Shared Activation preflight**: the same probe implementation is exercised
   through Activation. `supported` may continue, `unsupported` returns the existing
   capability issue, `target-unavailable` returns
   `activation.precondition_changed`, and propagated failures reach the existing
   boundary. Spies prove no Activation write begins for any outcome other than
   `supported`.
9. **No added external behavior**: Checkpoint 5 modules contain no subprocess, Git,
   socket, HTTP, package-manager, environment mutation, or global configuration
   calls. A collection-owned-Source fixture completes with such calls forbidden.
10. **Guidance coverage**: every baseline issue code listed in section 7 maps to the
   exact guidance id and text; an unknown code uses `inspect.unclassified` without
   changing the issue.
11. **Recommended commands**: state- and issue-based commands use only registry
   entries, have fixed deduplicated order, contain placeholders rather than actual
   roots, never recurse from Doctor, and never invent a mutation command.
12. **Deterministic JSON**: two calls over unchanged roots are byte-identical;
    default output equals explicit `--format json`; key order, array order, Unicode,
    final LF, schema version, and command envelope are golden-tested.
13. **Deterministic text**: every category and capability outcome has a golden
    output; control characters cannot inject lines; output has no ANSI escapes,
    width dependence, absolute roots, timestamps, or trailing whitespace.
14. **Format parity**: parsed JSON and parsed/golden text expose identical semantic
    values from the same result object. The renderer is called after one inspection.
15. **Exit and stream behavior**: all rows in the exit-code table are exercised for
    both formats; expected results use stdout only, usage/system errors retain their
    existing stderr behavior.
16. **Existing contract preservation**: all Checkpoint 1–4B tests remain unchanged
    and pass. Existing `scan`, `validate`, `plan`, and `activate` JSON bytes and exit
    codes do not change.

## 11. Explicit exclusions

Checkpoint 5 does not add or change:

- initialization or scaffolding of a collection, Binding, target directory, or
  Activation Record;
- repair, cleanup, rollback, relinking, reconciliation, migration, or normalization;
- deactivation or removal of any managed or project-owned object;
- networking, fetching, Source update, dependency installation, or remote checks;
- Git commands, Git mutations, submodule initialization, commits, tags, branches,
  worktrees, index changes, or global/local Git configuration;
- global Skill changes, Router installation, hooks, plugins, MCP servers, shell
  configuration, environment mutation, or other machine-global state;
- a new Activation mode, action, apply path, implicit apply, approval shortcut, or
  any other activation mutation;
- a durable status cache, report file, telemetry, timestamps, probing artifact, or
  background monitor;
- changes to the existing Validation Issue, Activation Review, Activation Plan,
  Activation Result, Cleanup Report, or Activation Record semantics;
- text output for pre-existing commands.

No part of this proposal authorizes implementation. Acceptance of the contract is
a separate review decision.
