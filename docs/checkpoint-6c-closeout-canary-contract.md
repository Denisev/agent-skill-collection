# Checkpoint 6C contract: documentation closeout and disposable canary

Status: **Accepted for Checkpoint 6C.**

Baseline: `17c6c0fb4b20b84ce3b3faae1b1d437a2fa0282a`

## 1. Purpose and boundary

Checkpoint 6C closes out the documented 6A/6B workflow and defines one disposable,
local end-to-end canary. It does not add a product command, public Python API,
configuration field, filesystem ownership rule, deployment mechanism, or automatic
recovery behavior.

The canary proves that the existing public CLI sequence works together in fresh
temporary roots:

1. Project Initialization planning;
2. Project Initialization application using its reviewed plan id;
3. Activation planning;
4. Activation application using its reviewed plan id;
5. project `status`; and
6. project `doctor`.

The canary is release evidence only. It must not be represented as a production
canary, a compatibility certification, a repair action, or a guarantee that a later
real-project invocation will succeed.

## 2. Vocabulary

**Disposable Canary** has the meaning defined in `CONTEXT.md`: a bounded local
exercise against newly created temporary collection and project roots. Its roots,
subprocess environment, output captures, and any copied fixture are disposable.

**Canary Collection** is the valid, self-contained collection fixture created or
copied inside the disposable canary root. It is not the developer checkout, a Git
remote, a submodule checkout, or a production collection.

**Canary Project** is the empty project root created inside the same disposable
canary root. It is not a user project, Bystro path, global-skills location, or
reused test fixture directory.

## 3. Isolation requirements

The canary creates one parent directory using a secure temporary-directory API and
places both the Canary Collection and Canary Project below it. All mutation targets
must resolve beneath that parent before a command is executed.

The canary must not:

- read, write, enumerate, copy, or use `/Users/admin/Documents/codex_projects/bystro`,
  its `.skill-vault`, or any other Bystro path;
- read, write, enumerate, install, activate, or otherwise use a global-skills
  location, including any user-level Codex skills directory;
- use a real project root, the repository checkout as the Canary Collection, a
  user home directory, an environment-provided project path, or a path supplied by
  an operator other than the disposable parent it created;
- access the network, run Git fetch/pull/clone operations, install dependencies,
  invoke credential helpers, mutate environment configuration, or create a
  durable log outside the disposable parent; or
- clean up any path that it did not create below the disposable parent.

The fixture-building mechanism must be local and deterministic. It may copy only
the minimum known-valid fixture data into the Canary Collection, or create that
fixture from checked-in test support. It must neither copy a developer collection
nor depend on an external Source checkout.

## 4. Public-command sequence

Every command runs as a subprocess with an explicit working directory under the
disposable parent, explicit `--collection-root` and `--project-root` arguments,
and `PYTHONPATH` pointing only to the checkout under test. JSON is the required
format. The canary captures stdout and stderr for each command in memory or beneath
the disposable parent.

All six commands must execute as genuine child processes of the CLI under test.
Spies may record subprocess arguments or reject a forbidden executable, network
call, or path, but must not replace command execution, fabricate JSON, or return a
mocked command result.

1. Run `init-project` without `--apply` for the Canary Collection, Canary Project,
   and a fixture Profile. Require a ready result with a nonempty opaque `plan_id`.
2. Run `init-project --apply --plan-id <exact-id>`. Require `created`, or permit
   `created_with_incomplete_cleanup` only when the result contains its documented
   cleanup issue and temporary cleanup location. In either outcome, require the
   exact canonical `skill-collection.toml` in the Canary Project. A
   cleanup-attention outcome makes
   the canary **attention-required**, not silently successful.
3. Run `activate` without `--apply`. Require a ready initial Activation Plan with a
   nonempty opaque `plan_id`.
4. Run `activate --apply --plan-id <exact-id>`. Require the documented successful
   Activation result. Do not derive, alter, or reuse a plan id from another step.
5. Run `status`. Require the documented active project status.
6. Run `doctor`. Require its documented successful/`ok` category and no blocking
   capability or project issue.

The canary must parse the command's JSON envelope and assert `schema_version == 1`,
the expected `command`, result status/category, and exact plan-id handoff. It must
not scrape text output, rely on absolute paths in output, inspect private Python
helpers, or infer success only from process exit codes.

## 5. Exit and disposition

The canary exits `0` only when all six steps meet their required outcomes and
teardown succeeds. It exits nonzero for a command failure, malformed output,
unexpected status, failed isolation check, or teardown failure.

`created_with_incomplete_cleanup` is a distinct attention-required disposition:
the canary captures the committed Binding bytes, temporary rooted location, cleanup
issues, command output, and its nonzero disposition before teardown; it skips
Activation and the inspection steps. It must not selectively delete, repair, retry,
or otherwise alter the Binding or reported temporary file, and must not convert the
result to `created`.

After evidence capture, the canary performs ordinary teardown by removing only its
entire disposable parent; this is not selective recovery of the Binding or
temporary file. Before recursive removal, it must re-stat the parent without
following and require the exact recorded `(st_dev, st_ino)` identity from creation.
If the parent is missing, replaced, identity-changed, or otherwise cannot be
verified, it must not delete it; it reports teardown failure and the parent path as
a local operator-cleanup detail, then exits nonzero. It never performs a broader
cleanup.

## 6. Documentation closeout

README must state that 6B supports the explicit `init-project --apply --plan-id`
handshake and must not describe initialization as planning-only. It must distinguish
the Disposable Canary from production deployment and retain the Bystro/global-skills
boundary.

Checkpoint 6C adds no README installation, rollout, deployment, migration, or
global-skills instructions. It may link to this contract and the existing 6A/6B
contracts.

## 7. Acceptance evidence

Implementation must add one end-to-end test or disposable script exercised by a
test that proves the complete public-command sequence in section 4. Its assertions
must additionally prove:

- all roots are descendants of the one temporary parent;
- the Canary Collection and Canary Project differ from the repository checkout,
  Bystro, and global-skills paths;
- the project initially has no Binding and ends with only documented project-local
  Binding and Activation artifacts;
- no command receives a path outside the disposable parent except the checkout's
  read-only Python source path; and
- the attention-required Initialization outcome does not proceed to Activation.

The test may use controlled fixture construction and argument-recording or
forbidden-call subprocess spies, but it must execute every command as a real child
process and observe outcomes through CLI JSON only. No private transaction helper,
private result constructor, fabricated command output, real project, Bystro path,
or global-skills location may be used as a seam. It must prove that teardown
rechecks the disposable parent's original identity and refuses deletion if that
identity changed.

## 8. Explicit exclusions

Checkpoint 6C does not authorize implementation before this contract is reviewed
and accepted. It does not authorize production deployment, a production canary,
CI configuration, release automation, telemetry, mutation outside the disposable
parent, Bystro access, global-skills access, new product commands, or changes to
the semantics of 6A, 6B, Activation, status, or doctor.

## 9. Approval gate

Implementation must not begin while this document is proposed. Review must approve
the fixture isolation boundary, command sequence, attention-required disposition,
cleanup rule, and acceptance evidence before any canary implementation begins.
