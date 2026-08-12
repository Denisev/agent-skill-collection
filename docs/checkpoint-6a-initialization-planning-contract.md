# Checkpoint 6A contract: read-only project initialization planning

Status: **Accepted for Checkpoint 6A.**

Baseline: `d9255fe4a1855d8f2bde27e5c309040d608e3c10`

## 1. Purpose and boundary

Checkpoint 6A adds one public, read-only seam that answers:

> What exact project Binding could be created for this validated collection and
> Profile, and what would prevent that initialization?

It also adds a dry-run-only CLI command:

```text
skill-collection init-project --project-root PROJECT --profile PROFILE
```

The command plans creation of `project:skill-collection.toml`. It does not perform
that creation and has no `--apply` mode. A ready result contains the exact canonical
TOML text and one proposed creation action. A blocked result contains issues and no
action, content, digest, revision, Profile selection, or `plan_id`.

This checkpoint is entirely read-only. It may inspect existing objects and metadata,
read and hash files, and run existing collection validation, discovery, and Profile
resolution. It must not create, replace,
remove, rename, relink, normalize, repair, initialize, activate, fetch, or update
anything. In particular, it must never create the Binding, directories, links,
Activation Records, temporary files, lock files, probe objects, or cache files.

`skill-collection.toml` is an exclusive destination. If that path already exists in
any form—including a regular file, directory, symlink to an existing or missing
target, looping symlink, FIFO, socket, or device—the plan is blocked. Existing bytes
are never parsed, adopted, compared, normalized, or overwritten by this seam.

## 2. Accepted vocabulary

These accepted terms are recorded in `CONTEXT.md`.

**Project Initialization**: The deliberate creation of the first project Binding
for one validated collection revision and Profile. It establishes project intent;
it is not Activation and creates no generated skill state.

**Initialization Plan**: An immutable, deterministic, read-only description of the
one Binding creation that Project Initialization would require. A blocked
Initialization Plan contains issues and no proposed action or Binding preview.

**Binding Destination Observation**: The read-only classification of
`project:skill-collection.toml` used as the proposed action's no-overwrite
precondition. It is observation, not ownership proof or authorization to mutate.

**Collection URL**: The canonical, portable network location committed in the
Catalog for identifying the collection in a Binding. It is collection metadata,
not a local Git remote, checkout path, credential source, or reachability claim.

## 3. Public Python seam

The following names are new public exports from `skill_collection`:

```python
InitializationStatus = Literal["ready", "blocked"]

@dataclass(frozen=True, slots=True)
class BindingDestinationObservation:
    location: Location
    kind: FilesystemKind

@dataclass(frozen=True, slots=True)
class CreateBindingAction:
    action_id: str
    kind: Literal["create-binding"]
    location: Location
    precondition: Literal["absent"]
    content_sha256: str

ProposedInitializationAction = CreateBindingAction

@dataclass(frozen=True, slots=True)
class InitializationPlan:
    status: InitializationStatus
    plan_id: str | None
    profile: str | None
    collection_revision: str | None
    collection_url: str | None
    binding_location: Location
    binding_observation: BindingDestinationObservation
    binding_content: str | None
    binding_digest: str | None
    actions: tuple[ProposedInitializationAction, ...]
    blocking_issues: tuple[ValidationIssue, ...]

def plan_project_initialization(
    collection_root: str | Path,
    project_root: str | Path,
    profile: str,
) -> InitializationPlan: ...
```

All result objects are value objects: frozen, slotted dataclasses containing only
immutable scalars, existing immutable value objects, or tuples. No result contains
a live `Path`, file descriptor, callable, exception, bytes object, set, mapping, or
mutable collection.

The Binding location is always exactly:

```python
Location("project", "skill-collection.toml")
```

`BindingDestinationObservation.kind` reuses the public `FilesystemKind` values
already used by Activation review:

```text
absent, directory, regular-file, symlink, broken-symlink, looping-symlink,
fifo, socket, block-device, character-device, unreadable
```

This checkpoint must share the existing object-classification implementation or
extract a common read-only classifier. It must not introduce a second, divergent
definition of filesystem kinds.

### Ready and blocked invariants

A ready plan has all of the following:

- `status == "ready"`;
- a non-`None` `plan_id`, `profile`, `collection_revision`, `collection_url`,
  `binding_content`, and `binding_digest`;
- `binding_observation.kind == "absent"`;
- exactly one action, a `CreateBindingAction` at `binding_location` with
  `precondition == "absent"`;
- `action.content_sha256` equal to the raw SHA-256 digest of the UTF-8 bytes of
  `binding_content`, formatted as `sha256:<64 lowercase hexadecimal digits>`; and
- no blocking issues.

A blocked plan has all of the following:

- `status == "blocked"` and `plan_id is None`;
- `profile`, `collection_revision`, `collection_url`, `binding_content`, and
  `binding_digest` are `None`;
- `actions == ()`; and
- one or more normalized blocking issues.

The Binding location and its observation remain present in a blocked result because
they identify the destination that was inspected. No partially resolved Profile,
revision, URL, TOML, digest, or action may escape through a blocked result.

## 4. Inputs and root containment

`collection_root`, `project_root`, and `profile` are required at the Python seam.
String and `Path` roots have identical meaning. The CLI converts both roots to
absolute lexical paths in the same way as existing commands before calling the
public seam.

The seam first requires both roots to denote existing directories, reusing the
existing `root.missing` issue and rooted locations `collection:.` and `project:.`.
The project root is never created. A symlink supplied as the project-root argument
may denote an existing directory; its resolved directory is the containment root,
consistent with existing validation behavior.

When the project root is missing, is not a directory, or cannot be inspected, the
blocked plan uses `binding_observation.kind == "unreadable"`; it does not claim that
the destination is absent. The independent rooted project issue remains
authoritative.

The only Binding destination is the literal single path component
`skill-collection.toml` beneath the project root. Before returning ready, planning
must establish read-only that:

1. the resolved project root is an existing directory;
2. the destination's lexical parent is that project root;
3. resolving a missing destination without requiring it to exist remains within
   the resolved project root; and
4. `lstat` classifies the destination as `absent`.

An existing destination symlink is classified as an existing object before its
target is considered. Its target is never followed to decide that the destination
is available.

Planning reports only the final state it observes. Because a missing directory
entry has no retainable identity, read-only planning cannot guarantee detection of
an object created and removed between observations. It does not claim a stable
absence interval, reserve the name, or authorize later creation. A future
Checkpoint 6B apply operation must re-open the project root and revalidate absence
with descriptor-relative exclusive creation that cannot overwrite an existing
entry.

If containment cannot be established, planning returns
`initialization.binding_outside_project` at
`project:skill-collection.toml`. If the destination cannot be classified safely
because of permissions, an inaccessible parent, an unstable loop, or another
anticipated inspection failure, its kind is `unreadable` and planning returns
`initialization.binding_uninspectable` at the same location.

The plan is only an observation; Checkpoint 6A defines no apply operation and the
observation is not a freshness token. A future mutation checkpoint must re-open and
revalidate the project root, destination, inputs, and plan independently.

## 5. Collection selection orchestration

Checkpoint 6A introduces one shared internal, read-only orchestration seam. It is
not exported from `skill_collection`:

```python
@dataclass(frozen=True, slots=True)
class ValidatedCollectionDocuments:
    # Immutable typed views of the validated Sources, Catalog, Groups, and Profiles.
    sources: tuple[ValidatedSource, ...]
    skills: tuple[ValidatedCatalogSkill, ...]
    groups: tuple[ValidatedGroup, ...]
    profiles: tuple[ValidatedProfile, ...]
    collection_revision: str | None
    collection_url: str | None

@dataclass(frozen=True, slots=True)
class CollectionSelectionReview:
    status: Literal["ready", "blocked"]
    documents: ValidatedCollectionDocuments | None
    discoveries: tuple[DiscoveredSkill, ...]
    profile: str | None
    selected_skill_ids: tuple[str, ...]
    collection_revision: str | None
    collection_url: str | None
    issues: tuple[ValidationIssue, ...]

def _prepare_collection_selection(
    collection_root: str | Path,
    profile: str,
) -> CollectionSelectionReview: ...
```

The `Validated*` names above describe internal immutable typed views, not new public
exports. Their exact class partition may vary, but `documents` must represent the
single parsed and validated document state and must not expose mutable TOML
dictionaries. The orchestration behavior and returned information are normative.

`_prepare_collection_selection()` performs exactly one collection document read and
validation pass and exactly one discovery traversal. It owns correlation,
normalization, and Profile resolution:

1. It invokes the existing collection-only validation core without a project root.
   This validates collection documents, Catalog identity fields, Sources, Groups,
   Profiles, references, cycles, and collisions. It deliberately does not report
   the missing Binding.
2. It invokes the existing read-only discovery/Catalog correlation core against
   those same validated documents. That core must not invoke collection validation
   again. Existing public `scan()` is refactored to use this shared core so scanning
   and initialization retain one correlation rule and one issue ordering.
3. It selects the requested Profile through the existing identifier validation
   and resolution logic. The exact requested name is used; there is no default,
   fuzzy match, case folding, alias, fallback, or implicit Profile creation.
4. It returns normalized validation and discovery issues once, using the existing
   `normalize_issues()` key. Duplicate equal issues from shared lower-level checks
   occur once. If any issue exists, it returns `blocked`, preserves the available
   normalized discoveries for diagnostics, and clears `documents`, Profile,
   selection, revision, and URL so no partially trusted state is consumable.
5. Otherwise it returns `ready` with the immutable validated documents, normalized
   discoveries, exact requested Profile, selected Skill identities in stable
   identity order, collection revision, and Collection URL.

`plan_project_initialization()` calls `_prepare_collection_selection()` exactly once
and consumes only its result. It must not call `validate()`, `scan()`, document
parsers, discovery walkers, `resolve_profile()`, or private equivalents separately.
It independently inspects only the project root and Binding destination. This makes
the orchestration seam the single source of collection state for initialization and
prevents duplicate validation, duplicate filesystem traversal, or divergent issue
aggregation.

## 6. Collection revision, Collection URL, and Profile rules

Initialization planning reuses existing collection and Profile domain rules.

`catalog.toml#collection_revision` remains authoritative for the pinned collection
revision. The orchestration seam reads it only after validation has established a
valid 40-character lowercase hexadecimal value. The exact value becomes the
Binding revision; Git HEAD, a branch, tag, timestamp, or working-tree state never
substitutes for it.

Checkpoint 6A permits one new optional top-level Catalog field:

```toml
collection_url = "https://example.com/owner/agent-skill-collection.git"
```

The field is optional for the existing general `validate()` and `scan()` seams so
baseline collections and their public behavior remain valid. When present, it is
validated by the shared Catalog validation core. It is mandatory for a ready
Initialization Plan; absence produces existing code `field.required` at
`collection:catalog.toml#collection_url`. This is committed collection metadata,
so equivalent checkouts use the same Binding identity regardless of local remote
names, URL rewrite configuration, checkout paths, or remote aliases.

The only valid Collection URL form is an absolute hierarchical URL satisfying the
following exact predicate. Validation applies `urllib.parse.urlsplit()` to the
unchanged string; `ValueError` from parsing rejects the value. It accepts only when
all rules hold:

- the scheme is exactly lowercase `https`, `ssh`, or `git`;
- the serialized prefix is correspondingly `https://`, `ssh://`, or `git://`;
- `split.scheme` is the allowed scheme, `split.netloc` is non-empty, and
  `split.hostname` is non-`None` and non-empty;
- the host is either a valid IPv4 literal under `ipaddress.IPv4Address`, a valid
  bracketed IPv6 literal under `ipaddress.IPv6Address`, or an ASCII DNS-style name
  of at most 253 characters whose dot-separated labels are 1–63 characters, begin
  and end with an ASCII letter or digit, and otherwise contain only ASCII letters,
  digits, or `-`;
- reading `split.port` does not raise `ValueError`; when a port is written, it
  consists only of ASCII decimal digits and its integer value is in `1..65535`;
- for `https` and `git`, `split.username` and `split.password` are both `None` and
  the authority contains no `@` delimiter;
- for `ssh`, `split.password` is `None`; the authority may contain either no user
  information or exactly one username followed by exactly one `@` delimiter; when
  present, the username is non-empty and matches `[A-Za-z0-9._~-]+`, contains no
  percent escape, and is preserved byte-for-byte;
- `split.path` is non-empty and begins with `/`;
- `split.query` and `split.fragment` are empty;
- every character in the original string is an ASCII unreserved character matching
  `[A-Za-z0-9._~-]`, an ASCII reserved character in `:/?#[]@!$&'()*+,;=`, or `%`;
  and
- every `%` in the original string is followed by exactly two ASCII hexadecimal
  digits as its escape pair.

Thus SCP-like syntax such as `git@example.com:owner/repo.git`, `file://` URLs,
local filesystem paths, relative paths, bare hosts, alternate transports, URL user
information on `https` or `git`, SSH passwords, invalid SSH usernames,
newline/control characters, and whitespace are rejected. In particular,
`ssh://git@github.com/owner/repository.git` is valid, while
`ssh://git:secret@github.com/owner/repository.git` is not. Percent escapes, host
spelling, port spelling, path spelling, case outside the required lowercase scheme,
and a trailing slash are otherwise not normalized. An accepted URL, including an
allowed SSH username, is preserved byte-for-byte as the parsed Binding value;
canonical TOML quoting escapes syntax without changing that value.

The allowed-URL predicate belongs to shared Catalog validation and is reused by any
future Binding collection-URL validation; initialization must not define a second
predicate. A present invalid value produces existing code `field.invalid` at
`collection:catalog.toml#collection_url`. Planning does not inspect any Git remote,
Git URL rewrite, environment variable, credential helper, credential file, or
network endpoint. It performs no credential lookup and makes no reachability claim.

### Catalog schema evolution and revision relationship

Checkpoint 6A updates `schemas/catalog.schema.json` while retaining Catalog schema
version `1`. The root schema permits optional property `collection_url` with JSON
type `string`; it remains absent from the root `required` array. The runtime Catalog
allowed-field set is extended by the same name. This is an additive schema
evolution, not Catalog version `2`.

The compatibility rule is exact:

- a version 1 Catalog without `collection_url` remains valid for `validate`,
  `scan`, `plan`, `activate`, `status`, and `doctor`, with their baseline results
  unchanged;
- `init-project` alone requires the field and reports existing code
  `field.required` at `collection:catalog.toml#collection_url` when it is absent;
- a present non-string value or a string rejected by the portable-URL predicate
  reports existing code `field.invalid` at that exact rooted location through every
  seam that performs collection validation; and
- no command, validator, scanner, planner, serializer, or schema migration writes
  the field, fills a default, rewrites its value, or changes
  `catalog.toml#collection_revision`.

The two fields have distinct meanings. `collection_url` is the canonical portable
locator; `collection_revision` is the existing independently assigned pin for the
collection state described by the Catalog. Together they form the Binding's
collection identity, but neither is derived from the other.

A maintainer adding or changing only `collection_url` must deliberately preserve
`collection_revision` when the same pinned collection state is intended and is
available at the new locator. The URL change still changes canonical Binding bytes,
`binding_digest`, and `plan_id`. A maintainer advances `collection_revision` only
when the collection state described by the Catalog changes, using the existing
reviewed Catalog-generation/release process; changing the URL never advances it
implicitly.

`collection_revision` is not a digest of `catalog.toml`, is not computed from
`collection_url`, and is not required to equal the Git commit containing the
Catalog that spells it. Checkpoint 6A therefore creates no self-referential
requirement that a commit contain its own hash. It validates the existing
40-lowercase-hex pin and copies it; assignment, reachability, publication, and
advancement of that pin remain release responsibilities outside this read-only
planning seam.

An invalid Profile argument returns existing code `field.invalid` at
`collection:profiles.toml#selection`. A validly formed name that does not identify
exactly one valid Profile returns existing code `profile.missing` at that location.
If the selected Profile is invalid because its inherited composition is invalid,
the underlying collection issues remain authoritative; no Binding is previewed.

Collection-selection issues and independent project destination issues are combined
and normalized by the existing ordering. A failed prerequisite prevents only its
dependent inspection; it never causes assertions or parsing of invalid data.

## 7. Exact canonical `skill-collection.toml`

For a ready plan, `binding_content` is exactly the following UTF-8 text, with the
three bracketed values replaced by canonical TOML basic strings and with exactly
one final LF:

```toml
version = 1
profile = "<PROFILE>"
target = ".agents/skills"

[collection]
url = "<COLLECTION-URL>"
revision = "<COLLECTION-REVISION>"
```

The angle-bracketed notation above is explanatory and is never emitted. Each
string value is encoded with the existing canonical TOML basic-string encoder used
by `serialize_activation_record()` (`json.dumps(value, ensure_ascii=False)`), so
quotes, backslashes, and control characters are escaped deterministically. Unicode
surrogates and values that do not round-trip through `tomllib` are rejected before
a plan becomes ready.

No optional `add` or `remove` field is emitted. `target` is always explicit and is
exactly `.agents/skills`. Keys, blank lines, spaces around `=`, table placement,
quoting, UTF-8 encoding, and the final LF are normative. There are no comments,
timestamps, absolute roots, environment-derived fields, or trailing whitespace.

Implementation must expose one shared canonical Binding serializer and one shared
Binding semantic-payload function. Initialization uses the serializer to produce
the preview, and Activation's existing `_binding_payload()` logic is refactored to
use the shared semantic-payload function. The rendered Binding must parse to the
same document passed to that payload function. Canonical serialization must not
duplicate Binding schema or Profile rules.

## 8. Binding digest, rooted data, action id, and `plan_id`

`binding_digest` has the same meaning and exact value as the `binding_digest` that
the existing Activation Review would compute after the proposed canonical Binding
is created. It is the existing `sha256:` digest of canonical JSON for this semantic
payload:

```json
{
  "binding_schema_version": 1,
  "document": {
    "add": [],
    "collection": {
      "revision": "<COLLECTION-REVISION>",
      "url": "<COLLECTION-URL>"
    },
    "profile": "<PROFILE>",
    "remove": [],
    "target": ".agents/skills",
    "version": 1
  },
  "location": {
    "path": "skill-collection.toml",
    "root": "project"
  }
}
```

Canonical JSON uses the existing digest rules: UTF-8, `ensure_ascii=False`, sorted
object keys, and separators `,` and `:` with no insignificant whitespace.
`binding_digest` is semantic and intentionally differs in purpose from the
action's raw-byte `content_sha256`.

The action id reuses the existing CLI-schema-v1 action-id algorithm with kind
`create-binding` and location `project:skill-collection.toml`. Consumers must treat
it as opaque and compare it as a whole.

For a ready plan, `plan_id` is the existing `sha256:` digest of canonical JSON for:

```json
{
  "initialization_plan_version": 1,
  "binding_digest": "sha256:...",
  "binding_observation": {
    "kind": "absent",
    "location": {"path": "skill-collection.toml", "root": "project"}
  },
  "collection_revision": "...",
  "profile": "...",
  "actions": [
    {
      "content_sha256": "sha256:...",
      "kind": "create-binding",
      "location": {"path": "skill-collection.toml", "root": "project"},
      "precondition": "absent"
    }
  ]
}
```

The displayed key order is non-normative because canonical JSON sorts keys; the
field set and array order are normative. The action id is omitted from identity,
matching existing Activation plan payloads. `plan_id` commits to semantic Binding
intent, exact output bytes through `content_sha256`, rooted destination, observed
absence, revision, Profile, and proposed action. It includes no absolute root,
inode, device, permissions, timestamp, Git working-tree path, or process state.

Repeated calls over unchanged inputs return equal plans and identical ids. Moving
both roots without changing their rooted content, committed Collection URL,
Profile, or destination state does not change the plan. Consumers must not parse
either id.

## 9. Existing destination classification and stable issues

Destination classification is exact:

| Observation at `project:skill-collection.toml` | Kind | Result |
| --- | --- | --- |
| `lstat` reports no directory entry | `absent` | May proceed if every other check succeeds. |
| Ordinary directory | `directory` | Block with `initialization.binding_exists`. |
| Ordinary regular file, regardless of bytes or TOML validity | `regular-file` | Block with `initialization.binding_exists`. |
| Symlink whose target exists | `symlink` | Block with `initialization.binding_exists`; do not adopt or follow it. |
| Symlink whose target is missing | `broken-symlink` | Block with `initialization.binding_exists`. |
| Looping symlink | `looping-symlink` | Block with `initialization.binding_exists`. |
| FIFO, socket, block device, or character device | matching existing kind | Block with `initialization.binding_exists`; never open it. |
| Cannot be classified safely | `unreadable` | Block with `initialization.binding_uninspectable`. |

`initialization.binding_exists` has exact message
`Project Binding destination already exists.` and location
`project:skill-collection.toml`. The issue does not disclose link text, target,
contents, permissions, or absolute paths.

`initialization.binding_uninspectable` has exact message
`Project Binding destination could not be safely inspected.` and the same location.

`initialization.binding_outside_project` has exact message
`Project Binding destination must remain inside the project root.` and the same
location.

These three `initialization.*` codes are stable in CLI schema version 1. Collection,
discovery, field, and Profile failures preserve existing stable issue objects and
codes. An existing Binding never becomes `document.missing`: initialization is the
only seam where its required absence is valid.

## 10. CLI contract

Checkpoint 6A adds:

```text
skill-collection init-project [--collection-root PATH] --project-root PROJECT --profile PROFILE [--format json|text]
```

The shorter normative invocation is:

```text
skill-collection init-project --project-root PROJECT --profile PROFILE
```

`--collection-root` defaults to the current directory, consistent with existing
commands. `--project-root` and `--profile` are required. `--format` defaults to
`json`. The only accepted values are `json` and `text`.

There is no `--apply`, `--plan-id`, `--force`, `--overwrite`, `--merge`, URL,
revision, target, add, or remove option. Passing any such option is a usage error.
The returned `plan_id` is opaque review data only and cannot be consumed by any
Checkpoint 6A command.

### Deterministic JSON

JSON uses the existing `json_document()` envelope and serializer:

```json
{
  "command": "init-project",
  "result": {
    "actions": [
      {
        "action_id": "...",
        "content_sha256": "sha256:...",
        "kind": "create-binding",
        "location": {"relative_path": "skill-collection.toml", "root": "project"},
        "precondition": "absent"
      }
    ],
    "binding_content": "version = 1\n...\n",
    "binding_digest": "sha256:...",
    "binding_location": {"relative_path": "skill-collection.toml", "root": "project"},
    "binding_observation": {
      "kind": "absent",
      "location": {"relative_path": "skill-collection.toml", "root": "project"}
    },
    "blocking_issues": [],
    "collection_revision": "...",
    "collection_url": "https://example.com/owner/agent-skill-collection.git",
    "plan_id": "sha256:...",
    "profile": "base",
    "status": "ready"
  },
  "schema_version": 1
}
```

The example abbreviates ids, content, and revision only. Actual JSON contains the
exact complete values.

Serialization remains UTF-8 with `ensure_ascii=False`, two-space indentation,
lexicographically sorted object keys, tuple order preserved as arrays, and exactly
one final LF. Issue order is normalized; action order is fixed. Repeated rendering
of an unchanged result is byte-identical. Output contains rooted locations and the
portable committed Collection URL embedded in canonical content, but never
collection or project absolute paths, exception text, file descriptors,
inode/device values, permission bits, environment values, or timestamps.

### Optional deterministic text

Text renders the same `InitializationPlan` object and must not perform a second
inspection. It uses UTF-8, LF line endings, no ANSI styling, no width-dependent
wrapping, no trailing whitespace, and exactly one final LF.

The exact outer layout is:

```text
Project initialization: <status>
Profile: <profile-or->
Collection revision: <revision-or->
Collection URL: <collection-url-or->
Binding: project:skill-collection.toml
Binding state: <filesystem-kind>
Binding digest: <binding-digest-or->
Plan ID: <plan-id-or->

Proposed actions (<decimal>):
<action blocks or "None.">

Binding content:
<indented canonical content or "None.">

Issues (<decimal>):
<issue blocks or "None.">
```

The one action block is:

```text
1. [create-binding] project:skill-collection.toml
   Precondition: absent
   Content SHA-256: <content-sha256>
   Action ID: <action-id>
```

Each line of canonical Binding content is prefixed with two spaces, including no
extra representation of its final LF. `None.` is unindented. Each issue block uses:

```text
<1-based-index>. [<code>] <message>
   Location: <root>:<relative_path>
   Related: <root>:<relative_path>, <root>:<relative_path>
```

`Related:` is omitted when empty. Embedded carriage return, line feed, and tab in
issue data are escaped as `\\r`, `\\n`, and `\\t`. JSON and text carry the same
status, ids, Profile, revision, observation, action, content, digest, and issues.

### Streams and exit codes

Expected ready and blocked plans are written to stdout in the requested format and
leave stderr empty.

| Condition | Exit code |
| --- | --- |
| Ready Initialization Plan | `0` |
| Blocked Initialization Plan | `1` |
| CLI usage error, including a missing required option or invalid format | `2` |
| Unexpected system failure | `3` |
| Keyboard interruption | `130` |

Usage, unexpected-error, and interruption rendering retain the baseline CLI
behavior. `KeyboardInterrupt` produces no cleanup report because this command
cannot create anything. A format choice never changes planning or exit status.

## 11. Read-only and capability constraints

Initialization planning reuses existing capability and output concepts only where
they apply:

- rooted `Location`, `FilesystemKind`, immutable result objects, normalized issues,
  canonical digest helpers, canonical TOML string encoding, deterministic JSON,
  sanitized errors, and CLI exit conventions are shared;
- no Activation capability probe is run, because directory or file `fsync` and
  mutation containment are irrelevant until a future apply operation exists; and
- existing read-only Git-backed Source validation remains reachable. Checkpoint 6A
  adds no Git command and derives no Binding field from local Git configuration.

Permitted filesystem operations are limited to read-only observation such as
`stat`, `lstat`, `readlink`, `resolve`, opening existing regular files with
read-only/no-follow flags, reading, hashing, and closing. Existing Source-validation
Git subprocesses remain read-only and use optional locks disabled.

Checkpoint 6A code must never use a write-capable open mode or flags including
`O_WRONLY`, `O_RDWR`, `O_CREAT`, `O_EXCL`, `O_TRUNC`, or `O_APPEND`; call `mkdir`,
`makedirs`, `write`, `pwrite`, `truncate`, `symlink`, `link`, `unlink`, `remove`,
`rmdir`, `rename`, `replace`, `chmod`, `chown`, `utime`, or a temporary-file API;
or invoke a Git command capable of changing the worktree, index, refs,
configuration, remotes, submodules, or object database.

The seam does not call `prepare_activation()` or `plan_activation()`: both require
an existing Binding and would misclassify its deliberate absence. Shared lower-level
validation, discovery, resolution, serialization, hashing, location, and
classification helpers may be extracted and reused without changing their existing
public behavior.

## 12. Public-seam acceptance tests

Acceptance requires tests at public Python and CLI seams, not only helper tests.

1. **Frozen result graph**: every new dataclass rejects assignment; collection
   fields are tuples; results contain no `Path`, bytes, mapping, set, file
   descriptor, callable, exception, or mutable value.
2. **Exact ready plan**: a validated fixture with committed Collection URL,
   matching Catalog, discoverable Skills, existing project directory, absent
   Binding, and valid Profile returns the exact fields, one action, rooted
   destination, and no issues.
3. **Canonical TOML golden test**: valid ASCII Profile names and allowed ASCII URLs,
   including percent-encoded path data, produce the exact bytes, order, blank line,
   explicit target, and final LF in section 7; the preview round-trips through
   `tomllib` to the intended document.
4. **Digest compatibility**: `content_sha256` matches the exact UTF-8 preview;
   `binding_digest` matches a later public `prepare_activation()` result after the
   test harness—not initialization planning—writes those exact bytes; the shared
   canonical payload is exercised once rather than reimplemented in the test.
5. **Opaque deterministic identities**: two unchanged calls are equal and have
   byte-identical JSON, action ids, digests, and `plan_id`. Moving equivalent roots
   leaves ids unchanged; changing URL, revision, Profile, content, destination
   observation, or action payload changes the appropriate digest or `plan_id`.
6. **Single collection-selection orchestration**: every baseline validation and
   scan issue reachable from collection-only inspection blocks with the same issue
   object and normalized order. Spies prove
   `_prepare_collection_selection()` is called exactly once, performs one document
   validation pass and one discovery traversal, and planning performs no separate
   parsing, validation, scanning, correlation, or Profile resolution. Duplicate
   equal issues occur once.
7. **Revision validation**: missing, malformed, and non-40-lowercase-hex Catalog
   revisions retain existing document/field issues. A valid revision is copied
   exactly to content and result. No Git HEAD, branch, tag, timestamp, or dirty-state
   value substitutes for it.
8. **Profile selection**: invalid syntax, missing name, invalid inherited Profile,
   inheritance cycles, missing references, and a valid inherited composition are
   covered. Selection uses the existing identifier and resolution behavior with no
   fallback, while no project Binding is created for validation.
9. **Committed Collection URL**: each allowed scheme and representative host, port,
   percent escape, and trailing slash is preserved byte-for-byte. HTTPS and Git
   reject all user information. SSH accepts absent usernames and representative
   usernames matching `[A-Za-z0-9._~-]+`, including `git`, while rejecting empty or
   invalid usernames, percent-encoded usernames, and every password. Invalid
   scheme/authority/path, SCP-like syntax, `file://`, local/relative path, query,
   fragment, non-ASCII, whitespace/control character, and backslash yield
   `field.invalid`. Spies prove no Git remote, rewrite configuration, credential
   helper, environment, checkout path, or network lookup influences the result.
10. **Catalog schema evolution and revision independence**: the version 1 JSON
    Schema accepts the optional string field and rejects a wrong type. Without the
    field, every baseline seam remains unchanged while `init-project` alone returns
    the exact rooted `field.required`; a present invalid URL returns the exact
    rooted `field.invalid`. Adding or relocating the URL while preserving the same
    collection state leaves `collection_revision` byte-identical but changes the
    Binding bytes, `binding_digest`, and `plan_id`. Tests prove no self-hash, Git
    HEAD, automatic revision rewrite, or Catalog write occurs.
11. **Project-root containment**: missing/file project roots, a symlinked root to an
    existing directory, contained absence, and an inspection that cannot establish
    containment produce the exact rooted outcomes. Absolute roots never appear in
    results or output.
12. **Existing-object matrix**: regular file containing valid or invalid TOML,
    empty file, directory, existing-target symlink, broken symlink, loop, FIFO,
    socket, and available platform device kinds all block. Each known object kind
    is reported exactly; no object is opened, followed for availability, parsed,
    adopted, removed, or changed. Inaccessible or unstable inspection yields
    `unreadable` and the exact uninspectable issue.
13. **Final observation, not concurrency proof**: controlled state changes show the
    result reflects the final destination observation. A transient create/remove
    that is not observed is not promised to block. Tests assert the plan contains
    no reservation or authorization claim and document that a future apply must use
    descriptor-relative exclusive creation after revalidation.
14. **No partial preview**: every blocker produces `plan_id`, Profile, revision,
    Collection URL, content, and digest `None`, empty actions, and one or more
    issues while retaining only the rooted destination and observation.
15. **Read-only snapshots**: byte/type/link-text snapshots of collection and project
    roots, Git status, refs, index metadata, and configured remotes are identical
    before and after two Python calls and both CLI formats. No temporary or lock
    artifact appears, including during injected failure and interruption.
16. **Forbidden-operation spies**: filesystem creation, write-capable opens, byte
    writes, link operations, rename, removal, metadata mutation, temporary-file
    APIs, networking, and mutating Git commands fail the test immediately. Every
    opened read-only descriptor is closed on success, block, failure, and
    interruption paths.
17. **Deterministic JSON**: default format equals explicit JSON; schema version,
    command envelope, key order, array order, Unicode, final LF, and absence of
    private data are golden-tested for ready and every blocked classification.
18. **Deterministic text and parity**: ready and blocked golden outputs follow the
    exact layout; control characters cannot inject lines; text and JSON render the
    same single result object and expose identical semantic data.
19. **Exit and stream behavior**: every exit-code row is tested. Expected plans use
    stdout only; usage and sanitized system errors use the existing stderr boundary;
    interruption creates no cleanup payload.
20. **Existing contract preservation**: all baseline tests remain unchanged and
    pass. Existing `scan`, `validate`, `plan`, `activate`, `status`, and `doctor`
    JSON/text bytes, categories, ids, issues, streams, and exit codes do not change.

## 13. Explicit exclusions

Checkpoint 6A does not add or authorize:

- creation, writing, replacement, merging, normalization, chmod, touching, or
  deletion of `skill-collection.toml` or any other project file;
- creation of directories, symlinks, hard links, Activation Records, ownership
  records, journals, caches, lock files, temporary files, probes, or backups;
- an apply seam, `--apply`, force/overwrite option, approval token, executable
  `plan_id`, transaction, cleanup, rollback, or implicit initialization;
- Activation planning or application, repair, relinking, reconciliation,
  deactivation, migration, Router installation, or global Skill changes;
- edits to collection documents, Catalog generation, Source initialization, Source
  update, submodule changes, dependency installation, or package-manager behavior;
- networking, remote reachability checks, fetch, pull, push, clone, remote mutation,
  credential lookup, authentication, or URL rewriting;
- Git commits, tags, branches, worktrees, index changes, ref changes, hooks, or
  global/local Git configuration changes;
- Profile creation or editing, interactive Profile selection, Binding additions or
  removals, custom Activation target selection, or collection revision override;
- environment mutation, shell configuration, plugins, MCP servers, telemetry,
  timestamps, durable reports, background tasks, or monitors;
- a new domain rule for collection validity other than the additive Catalog
  `collection_url` field and its portable-URL predicate specified here, or any new
  Profile-composition, Binding-schema, canonical-semantic-digest, filesystem-kind,
  capability-support, or output-sanitization rule; or
- changes to existing Validation Issue, Scan Result, Activation Plan, Activation
  Review, Activation Result, Activation Record, Project Status, Doctor Report, or
  capability semantics.

Implementation remains limited to the read-only boundary defined by this contract.
