# Checkpoint 7A contract: Remote Candidate Inspection

Status: **approved authoritative contract**. The former broad 7A planning
contract and acceptance audit are archived under
`docs/archive/checkpoint-7a-planning-history/`.

## Purpose and seam

Checkpoint 7A answers only:

> For explicitly selected native Git submodule Sources and exact full branch
> refs, what commit object does the anonymous HTTPS remote advertise, and can
> that advertised object be identified safely without acquiring or projecting
> objects?

The public Python seam is:

```python
inspect_remote_candidates(
    collection_root,
    requests: tuple[RemoteCandidateRequest, ...],
    network_authorization: NetworkAuthorization | None,
) -> RemoteCandidateInspection
```

The CLI seam is `inspect-source-candidates --source SOURCE=refs/heads/BRANCH
--allow-network`, but its output contains only inspection evidence. There are no
project roots and no local projection in 7A.

## In scope

7A validates the collection root, explicit request shape, selected native
submodule Source URL, committed gitlink identity, SHA-1 repository format, and
the anonymous HTTPS boundary. It performs at most one bounded `ls-remote --refs`
advertisement per selected Source, in stable Source order. It parses exactly one
lowercase 40-hex object ID for the exact requested ref and returns immutable,
rooted, deterministic evidence:

- Source ID, exact requested ref, current committed gitlink, advertised candidate;
- relationship only as `unchanged` or `unverified`;
- `ready` or `blocked` status;
- `inspection_id` for valid advertisement evidence; and
- normalized, sanitized rooted issues.

`ready` means every requested ref produced exactly one valid advertised candidate
and the inspection can be handed to 7B. It does not mean the candidate objects
are locally available or that projection is safe. An unchanged candidate is
still `ready`; there is no separate attention state. `blocked` means complete
valid evidence could not be established.

## Safety and portability boundary

Network access requires the exact immutable authorization
`anonymous-https-remote-inspection`. Only validated anonymous HTTPS URLs and
exact full `refs/heads/...` refs are used. Prompts, credentials, proxies,
redirects, alternate trust material, global/system configuration, helper
selection, and environment secrets are excluded. TLS uses the runtime trust
store; no trusted local endpoint is required by acceptance tests. Git executable
resolution uses only the runtime's fixed platform default path and never the
caller's `PATH`.

The planner is read-only: no fetch, clone, pull, push, object write, lazy fetch,
ref/index/worktree/submodule mutation, Catalog/project write, lock/cache/temp
file, hook, staging, commit, or configuration write is permitted.

## 7A→7B handshake

7A persists no inspection record. 7B receives the original request tuple and the
7A `inspection_id`, immediately reruns the complete 7A inspection, and continues
only when the new result is `ready` and its ID exactly matches the supplied ID.
A mismatch is a stale-inspection block. After a match, 7B acquires only the
exact advertised object IDs in that fresh result. It never trusts copied JSON,
caller-supplied object IDs, or a prior result as mutation authority. An
unchanged candidate still passes through this freshness handshake, although it
may require no transfer.

Each cooperative helper starts in the planner-created process group. Cleanup
sends TERM, waits to a fixed deadline, sends KILL, waits again, and probes
`killpg(group, 0)`. Only `ESRCH` proves the group empty. Any other result is
deterministic internal cleanup-failure evidence and a sanitized public failure;
all pipes close in `finally`. Portable Python/POSIX does not claim to prove that
an arbitrary `setsid()` escape later exited. Production prevents user-controlled
helpers/configuration, so escaped helpers are outside the cooperative-child
boundary.

When interruption is the primary failure, exit 130 remains authoritative even
if cleanup cannot be confirmed. Bounded cleanup evidence is retained internally
and no cleanup details, process identifiers, paths, or exception text are
exposed publicly.

## Public result model

7A retains only these concepts, renamed where necessary to remove projection
meaning:

- `SourceUpdateLocation` → `InspectionLocation`;
- `SourceUpdateIssue` → `InspectionIssue`;
- `SourceUpdateRequest` → `RemoteCandidateRequest`;
- `NetworkAuthorization` → `NetworkAuthorization`;
- `SourceRevisionComparison` → `RemoteCandidateComparison`;
- `SourceUpdatePlan` → `RemoteCandidateInspection`.

7A does not return Catalog changes, Skill changes, Group/Profile impacts,
Reactivation Previews, link consequences, project labels, candidate tree
digests, candidate object evidence, or a future collection revision.

All public values are frozen, slotted dataclasses; nested collections are tuples
and contain no Path, URL object, Git handle, process, mapping, set, exception,
environment, or raw output bytes:

```python
@dataclass(frozen=True, slots=True)
class InspectionLocation:
    root: Literal["collection", "source"]
    label: str | None
    relative_path: str

@dataclass(frozen=True, slots=True)
class RemoteCandidateRequest:
    source_id: str
    remote_ref: str

@dataclass(frozen=True, slots=True)
class RemoteCandidateComparison:
    source_id: str
    source_location: InspectionLocation
    remote_ref: str
    current_revision: str       # lowercase 40-hex SHA-1
    candidate_revision: str     # lowercase 40-hex SHA-1
    relationship: Literal["unchanged", "unverified"]

@dataclass(frozen=True, slots=True)
class InspectionIssue:
    code: str
    message: str
    location: InspectionLocation
    related_locations: tuple[InspectionLocation, ...]

@dataclass(frozen=True, slots=True)
class RemoteCandidateInspection:
    status: Literal["ready", "blocked"]
    inspection_id: str | None
    comparisons: tuple[RemoteCandidateComparison, ...]
    issues: tuple[InspectionIssue, ...]

@dataclass(frozen=True, slots=True)
class NetworkAuthorization:
    kind: Literal["anonymous-https-remote-inspection"]
```

For `ready`, `inspection_id` is `sha256:<64 lowercase hex>`, comparisons are
nonempty and ordered by Source ID, and issues are empty. For `blocked`, the ID
is null, comparisons are exactly `()`, and issues contain one or more normalized
issues. A blocked result retains no earlier successful comparisons. Issues are
normalized by code, location, related locations, and message. Comparisons are
normalized by Source ID. The constructor rejects invalid enums, mutable nested
values, invalid locations, and invalid optional-field combinations.

`unchanged` means candidate ID equals the current gitlink. `unverified` means
the IDs differ; 7A does not claim commit type or ancestry. 7B alone may turn an
unverified candidate into a proven fast-forward after acquisition and inspection.

The inspection ID is the SHA-256 digest of canonical JSON containing the
contract version, declared SHA-1 format, collection committed HEAD, and each
selected Source ID, validated URL, exact ref, current gitlink, and advertised
candidate in Source-ID order. It excludes timestamps, process IDs, traversal
order, availability flags, local paths, raw output, and network timing.

## Issue taxonomy

Retained 7A issues are:

`network_not_authorized`, `request_invalid`, `request_duplicate`, `source_missing`,
`source_not_external`, `remote_transport_unsupported`, `current_pin_unavailable`,
`object_format_unsupported`, `remote_unavailable`, `remote_response_invalid`,
`remote_ref_missing`, and `credentials_required`.

`candidate_not_commit`, `candidate_not_descendant`, `candidate_unavailable`,
`candidate_tree_invalid`, `projection_invalid`, `project_uninspectable`, and
all project/profile/catalog/tree-digest issue paths move out of 7A. Acquisition,
projection, and application checkpoints define their own issue namespaces.

## Output and exit behavior

JSON is the default exact envelope, with sorted keys, two-space indentation, and
one trailing LF. This is an exact blocked golden:

```json
{
  "command": "inspect-source-candidates",
  "result": {
    "comparisons": [],
    "inspection_id": null,
    "issues": [
      {
        "code": "source-update.network_not_authorized",
        "location": {
          "label": null,
          "relative_path": ".",
          "root": "collection"
        },
        "message": "Explicit anonymous HTTPS remote inspection authorization is required.",
        "related_locations": []
      }
    ],
    "status": "blocked"
  },
  "schema_version": 1
}
```

This is an exact ready golden:

```json
{
  "command": "inspect-source-candidates",
  "result": {
    "comparisons": [
      {
        "candidate_revision": "2222222222222222222222222222222222222222",
        "current_revision": "1111111111111111111111111111111111111111",
        "relationship": "unverified",
        "remote_ref": "refs/heads/main",
        "source_id": "upstream",
        "source_location": {
          "label": "upstream",
          "relative_path": ".",
          "root": "source"
        }
      }
    ],
    "inspection_id": "sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
    "issues": [],
    "status": "ready"
  },
  "schema_version": 1
}
```

Ready contains ordered comparisons and a non-null inspection ID; blocked contains
no downstream values. Text is headed `Remote candidate inspection: <status>` and
lists Inspection ID, ordered Sources, and Issues only. Ready exits 0; blocked
exits 1; usage errors exit 2; interruption exits 130; unexpected failures exit
3. stdout contains only the selected document and stderr only usage or sanitized
system errors. Acceptance tests must golden ready and blocked JSON/text, all
exit/stream combinations, malformed and duplicate options, and redaction of
secrets, absolute paths, raw Git output, exception text, credentials, headers,
and helper names.

The exact ready text golden for one comparison is:

```text
Remote candidate inspection: ready
Inspection ID: sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa

Sources (1):
- upstream: 1111111111111111111111111111111111111111 -> 2222222222222222222222222222222222222222
  Ref: refs/heads/main
  Relationship: unverified

Issues (0):
None.
```

The exact blocked text golden is:

```text
Remote candidate inspection: blocked
Inspection ID: -

Sources (0):
None.

Issues (1):
1. [source-update.network_not_authorized] Explicit anonymous HTTPS remote inspection authorization is required.
   Location: collection:.
```

Stable issue messages and locations are:

| Code | Message | Primary location | Related locations |
|---|---|---|---|
| `source-update.network_not_authorized` | Explicit anonymous HTTPS remote inspection authorization is required. | `collection:.` | `()` |
| `source-update.request_invalid` | A Source request or full branch ref is malformed. | `collection:sources.toml` | `()` |
| `source-update.request_duplicate` | A Source identity was requested more than once. | `collection:sources.toml` | `()` |
| `source-update.source_missing` | The requested Source does not exist. | `collection:sources.toml` | `()` |
| `source-update.source_not_external` | The requested Source is not a native Git submodule Source. | `collection:sources.toml#sources[i]` | `()` |
| `source-update.remote_transport_unsupported` | The Source URL is outside the anonymous HTTPS transport policy. | `collection:sources.toml#sources[i].url` | `()` |
| `source-update.current_pin_unavailable` | The committed Source gitlink pin cannot be established. | `collection:.` when collection HEAD or gitlink-tree inspection is unavailable; otherwise `collection:sources.toml#sources[i].path` when the selected gitlink is missing or invalid | `()` |
| `source-update.object_format_unsupported` | Checkpoint 7A supports only the SHA-1 Git object format. | `collection:.` when the collection repository format is unsupported; otherwise `collection:sources.toml#sources[i].path` when the selected submodule format is unsupported | `()` |
| `source-update.remote_unavailable` | The exact remote could not be inspected within the required bounds. | `collection:sources.toml#sources[i].url` | `()` |
| `source-update.remote_response_invalid` | The remote advertisement was malformed or ambiguous. | `collection:sources.toml#sources[i]` | `()` |
| `source-update.remote_ref_missing` | The exact requested ref was not advertised. | `collection:sources.toml#sources[i]` | `()` |
| `source-update.credentials_required` | The remote rejected anonymous inspection or required credentials. | `collection:sources.toml#sources[i].url` | `()` |

All messages are sanitized and locations are rooted at collection or selected
Source fields; raw remote detail is never serialized.

## Acceptance test contract

7A acceptance is owned by `tests/test_source_update_remote_safety.py`,
`tests/test_source_update_contract.py`, and the 7A portions of
`tests/test_source_update.py`. Tests must assert: authorization-before-child;
exact URL/ref argv and Source ordering; sanitized environment and credential
rejection; no mutation; committed-gitlink authority; exact advertisement parser
cases; SHA-1/unsupported repository states; process cleanup on success, block,
timeout, interruption, malformed output, unexpected failure, and cleanup
failure; `killpg(group, 0)` returning ESRCH; descriptor closure; canonical
inspection-ID sensitivity/exclusions; immutable value invariants; shuffled-input
determinism; exact ready/blocked JSON/text goldens; all exit/stream combinations;
and complete blocked redaction. Real local HTTPS tests cover untrusted TLS
rejection without alternate trust material; the approved child seam covers
deterministic protocol behavior and is not evidence of trusted TLS.

## Explicit future checkpoints

### Checkpoint 7B — Exact Candidate Object Acquisition

7B separately and explicitly acquires only the exact candidate objects named by
the matched fresh 7A inspection. It owns object transfer, promisor completion,
object availability evidence, acquisition failure issues, and before/after
repository snapshots. It does not project Catalog, Groups, Profiles, or Projects.

### Checkpoint 7C — Local Source Projection

7C consumes a completed 7B acquisition and owns candidate tree inspection,
whole-tree Skill digests, Catalog/Skill outcomes, Group/Profile impacts, and
project reactivation previews. It owns all former projection, digest, and project
preview types and issues. It does not acquire objects or mutate pins/Catalogs.

### Checkpoint 7D — Reviewed Source Update Application

7D consumes reviewed 7C output and owns the deliberate selected-pin transition,
Catalog authoring/regeneration/validation, and all write/apply authorization. It
produces affected-project guidance or fresh explicit Activation reviews; it never
activates projects automatically. Existing Activation retains its separate
explicit plan/apply handshake.

## Review gate

This is the authoritative reduced Checkpoint 7A contract. It remains
uncommitted until its complete acceptance matrix and a full-diff two-axis review
against the stated baseline report no findings.
