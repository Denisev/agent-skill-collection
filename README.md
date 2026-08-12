# Agent Skill Collection

`agent-skill-collection` is a portable, Git-distributed catalog for composing agent
skills from pinned upstream sources and collection-owned skills. Projects select
skills through committed bindings; activation is a separate, explicit operation.

This repository is currently at **Checkpoint 4B: safe project Activation**. It can
apply an explicitly selected, freshly revalidated Activation Review inside one
project. It contains no deactivation, installer, Source update, global mutation,
hooks, plugins, MCP servers, or automatic update mechanism.

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

At this checkpoint, only an approved project-local Activation Plan may create
container directories, managed skill symlinks, and one canonical Activation Record.
Nothing may deactivate, update Sources, or modify global skills. The pre-install archive at
`/Users/admin/Documents/codex_projects/bystro/.skill-vault` remains outside this
repository and must remain untouched until migration is explicitly approved.

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

## Read-only commands

The CLI exposes three commands and emits deterministic JSON:

```sh
PYTHONPATH=src python3 -m skill_collection scan [--collection-root PATH]
PYTHONPATH=src python3 -m skill_collection validate [--collection-root PATH] [--project-root PATH]
PYTHONPATH=src python3 -m skill_collection plan [--collection-root PATH] --project-root PATH
PYTHONPATH=src python3 -m skill_collection activate [--collection-root PATH] --project-root PATH
PYTHONPATH=src python3 -m skill_collection activate [--collection-root PATH] --project-root PATH --apply --plan-id ID
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
