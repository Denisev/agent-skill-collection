# Agent Skill Collection

This context names the concepts used to collect, compose, bind, and safely activate
agent skills across projects.

## Language

**Source**:
A named, pinned origin that contributes zero or more Skills to the collection. A native external Source is represented by a Git submodule; the collection itself is the origin for collection-owned Skills.
_Avoid_: Repository, upstream

**Skill**:
A discoverable agent capability with a stable collection identity and a Codex-facing name. A Skill belongs to exactly one Source.
_Avoid_: Package, plugin

**Group**:
A named, reusable set of Skill and Group references representing a capability area. A Group cannot contain itself, directly or transitively.
_Avoid_: Bundle, category

**Profile**:
A named composition intended for project use, formed from Groups, Skills, optional inherited Profiles, additions, and removals. Profile inheritance must be acyclic.
_Avoid_: Preset, environment

**Binding**:
A project-owned declaration selecting one Profile and optionally adding or removing Skills. A Binding is portable and records intent, not generated filesystem state.
_Avoid_: Installation, lock file

**Catalog**:
The generated inventory of resolved Skills and their provenance, paths, and Codex-facing names at a specific collection state. It is derived data and is not hand edited.
_Avoid_: Registry, manifest

**Activation**:
The explicit application of a validated plan that exposes selected Skills to one project through collection-owned symlinks. Activation does not copy Skill contents or change global Skills.
_Avoid_: Installation, deployment

**Collision**:
A condition in which two selected Skills claim the same Codex-facing name or an activation target is already project-owned. A Collision prevents activation until resolved.
_Avoid_: Duplicate

**Update**:
A deliberate transition from one pinned collection state to another, followed by catalog regeneration, validation, and a reviewed activation plan. Fetching remote state alone is not an Update.
_Avoid_: Sync, automatic update

**Rollback**:
A deliberate return to a previously recorded, valid collection state and its corresponding project activation. Rollback restores known intent; it is not an ad hoc filesystem repair.
_Avoid_: Undo, reset
