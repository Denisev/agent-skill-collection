# Agent Skill Collection

`agent-skill-collection` is a portable, Git-distributed catalog for composing agent
skills from pinned upstream sources and collection-owned skills. Projects select
skills through committed bindings; activation is a separate, explicit operation.

This repository has completed reduced **Checkpoint 7A: Remote Candidate
Inspection**. It can inspect anonymous HTTPS advertisements for selected Sources
and produce deterministic, read-only ready or blocked evidence. It does not acquire
objects, inspect candidate ancestry or content, project collection changes, update
Source pins or the Catalog, or activate projects. The earlier Checkpoint 6C Binding,
project-inspection, explicit Activation, documentation-closeout, and disposable
canary capabilities remain available. No checkpoint authorizes production
deployment, deactivation, an installer, global mutation, hooks, plugins, MCP
servers, or an automatic update mechanism.

## Design constraints

- Human-authored Sources, Groups, Profiles, and Bindings use TOML.
- External Sources are native Git submodules pinned by the parent repository.
- Collection-owned skills live in this repository.
- Groups express reusable capability sets; Profiles compose Groups and skills.
- Project Bindings select a Profile and may add or remove skills.
- A generated Catalog will describe the resolved, uniquely named skill inventory.
- Planning and validation must precede explicit activation.
- Activation is dry-run by default and requires both `--apply` and `--plan-id`.
- Only the future Router may be installed globally.
- Project activation will use generated, uncommitted symlinks.
- Distribution is plain Git; runtime tooling will use Python 3.11's standard library.

## Repository contract

The canonical vocabulary is in [CONTEXT.md](CONTEXT.md). Architectural decisions
are recorded in [docs/adr](docs/adr), and machine-readable contracts for TOML data
after parsing are in [schemas](schemas).

Expected future configuration files:

```text
sources.toml                 # Source declarations
groups.toml                  # reusable skill sets
profiles.toml                # project-oriented compositions
catalog.toml                 # generated inventory; not hand edited
<project binding>.toml       # committed in a consuming project
```

The schemas constrain the data model, not TOML syntax. The validator first parses
TOML with Python's `tomllib`, then validates the resulting values against these
contracts. Cross-document rules—such as references to missing skills, Group cycles,
Profile inheritance cycles, and Codex-facing name collisions—require catalog-aware
validation and are specified in [schemas/README.md](schemas/README.md).

## Current boundaries

Only an approved project-local Initialization Application may create the first
Binding, and only an approved project-local Activation Application may create
container directories, managed skill symlinks, and one canonical Activation Record.
Nothing may deactivate, update Sources, modify global skills, or mutate a real
project during the disposable canary. The pre-install archive at
`/Users/admin/Documents/codex_projects/bystro/.skill-vault` is outside this
repository and is never a canary input, output, or cleanup target.

## Read-only validation seam

Checkpoint 2 introduces one public Python seam and no CLI:

```python
validate(collection_root, project_root=None) -> list[ValidationIssue]
```

Each issue has a stable `code`, human-readable `message`, primary `location`, and
optional `related_locations`. A location contains a `root` (`collection` or
`project`) and a root-relative path. Anticipated invalid states are returned as
issues; the validator performs no writes, repairs, network access, or Git mutations.

Run the tests with Python 3.11 or newer:

```sh
PYTHONPATH=src python3 -m unittest discover -s tests
```

## Commands

The CLI exposes deterministic read-only inspection and Activation commands:

```sh
PYTHONPATH=src python3 -m skill_collection scan [--collection-root PATH]
PYTHONPATH=src python3 -m skill_collection validate [--collection-root PATH] [--project-root PATH]
PYTHONPATH=src python3 -m skill_collection plan [--collection-root PATH] --project-root PATH
PYTHONPATH=src python3 -m skill_collection activate [--collection-root PATH] --project-root PATH
PYTHONPATH=src python3 -m skill_collection activate [--collection-root PATH] --project-root PATH --apply --plan-id ID
PYTHONPATH=src python3 -m skill_collection status [--collection-root PATH] --project-root PATH [--format json|text]
PYTHONPATH=src python3 -m skill_collection doctor [--collection-root PATH] --project-root PATH [--format json|text]
PYTHONPATH=src python3 -m skill_collection init-project [--collection-root PATH] --project-root PATH --profile PROFILE [--format json|text]
PYTHONPATH=src python3 -m skill_collection init-project [--collection-root PATH] --project-root PATH --profile PROFILE --apply --plan-id ID [--format json|text]
PYTHONPATH=src python3 -m skill_collection inspect-source-candidates [--collection-root PATH] --source SOURCE=refs/heads/BRANCH --allow-network [--format json|text]
```

`init-project` remains read-only by default. Its explicit `--apply --plan-id`
handshake exclusively creates the exact reviewed `skill-collection.toml` and
blocks when that destination already exists in any form. It does not activate
Skills or create any other project state.

`status` maps the existing Activation Review to `blocked`, `inactive`, `active`, or
`drifted`. `doctor` adds non-mutating inspection of the platform capabilities used
by Activation preflight. `inspect-source-candidates` is reduced Checkpoint 7A: it
performs explicit, anonymous-HTTPS remote candidate inspection only—no object
acquisition, local projection, pin update, Catalog change, project change, or
Activation. JSON remains the default; `status`, `doctor`, `init-project`, and
`inspect-source-candidates` support text output. The read-only `status`, `doctor`,
and `inspect-source-candidates` commands never initialize, repair, relink,
deactivate, or otherwise change either root.

Checkpoint 7A verification requires both commands below. In a restricted sandbox
the ordinary suite may skip only the loopback listener test. The same complete
suite must then run with loopback permission and report `OK` with zero skips before
acceptance:

```sh
# Ordinary sandboxed run.
PYTHONPATH=src python3 -m unittest discover -s tests

# Mandatory acceptance run in an environment that permits loopback listeners.
PYTHONPATH=src python3 -m unittest discover -s tests
```

`scan` recursively discovers regular `SKILL.md` files without following directory
symlinks and correlates them exactly by Source and Catalog path. `plan` returns an
`ActivationPlan` containing only proposed directory and symlink creation actions.
It includes an in-memory logical Activation Record preview but defines no persisted
record format and performs no writes.

`prepare_activation(collection_root, project_root)` adds a read-only ownership
review. It distinguishes initial, repeated, and repair intent, produces separate
durable `activation_id` and current-state `plan_id` values, and previews a canonical
project-local Activation Record. Existing records must match the version 1 schema
and canonical TOML bytes exactly.

`apply_activation(collection_root, project_root, plan_id)` and `activate --apply`
apply only a matching current review. Creation uses descriptor-relative,
non-following filesystem operations, exclusive creation, mandatory directory
`fsync`, and no-overwrite record publication. Failures attempt cleanup of only the
objects created by that invocation; later runs never remove unproven leftovers.

Each proposed action has an opaque identifier derived from its action kind and
rooted location. The identifier is stable within CLI schema version 1, but consumers
must compare it as a whole and must not parse it. The logical record preview's
`created_directories` exactly matches the locations of proposed directory actions.
