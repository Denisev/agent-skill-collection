# Agent Skill Collection

`agent-skill-collection` is a portable, Git-distributed catalog for composing agent
skills from pinned upstream sources and collection-owned skills. Projects select
skills through committed bindings; activation is a separate, explicit operation.

This repository is currently at **Checkpoint 2: read-only validation**. It contains
no CLI, installer, activation tooling, hooks, plugins, MCP servers, or automatic
update mechanism.

## Design constraints

- Human-authored Sources, Groups, Profiles, and Bindings use TOML.
- External Sources are native Git submodules pinned by the parent repository.
- Collection-owned skills live in this repository.
- Groups express reusable capability sets; Profiles compose Groups and skills.
- Project Bindings select a Profile and may add or remove skills.
- A generated Catalog will describe the resolved, uniquely named skill inventory.
- Planning and validation must precede explicit activation.
- Future mutation commands will be dry-run by default and require `--apply`.
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

At this checkpoint, nothing in this repository may install, activate, link, update,
or modify global or project skills. The pre-install archive at
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
