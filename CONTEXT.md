# Agent Skill Collection

This context names the concepts used to collect, compose, bind, and safely activate
agent skills across projects.

## Language

**Source**:
A named, pinned origin that contributes zero or more Skills to the collection. A native external Source is represented by a Git submodule; the collection itself is the origin for collection-owned Skills.
_Avoid_: Repository, upstream

**Skill**:
A discoverable agent capability with a stable collection identity and a Codex-facing name. A Skill belongs to exactly one Source, and its valid Catalog entry must be backed by exactly one matching Discovery.
_Avoid_: Package, plugin

**Discovery**:
The read-only observation of a directory containing `SKILL.md` within a Source. A Discovery is matched to the Catalog by its Source identity and collection-relative directory path.
_Avoid_: Installation, registration

**Discovered Skill**:
A Discovery together with its exact Catalog correlation, if one exists. A Discovered Skill without exactly one valid Catalog match is not a Skill.
_Avoid_: Candidate Skill, scanned Skill

**Group**:
A named, reusable set of Skill and Group references representing a capability area. A Group cannot contain itself, directly or transitively.
_Avoid_: Bundle, category

**Profile**:
A named composition intended for project use, formed from Groups, Skills, optional inherited Profiles, additions, and removals. Profile inheritance must be acyclic.
_Avoid_: Preset, environment

**Binding**:
A project-owned declaration selecting one Profile and optionally adding or removing Skills. A Binding is portable and records intent, not generated filesystem state.
_Avoid_: Installation, lock file

**Collection URL**:
The canonical, portable network location committed in the Catalog for identifying the collection in a Binding. It is a locator, not a local Git remote, checkout path, credential source, or reachability claim.
_Avoid_: Origin URL, checkout URL

**Project Initialization**:
The deliberate creation of the first project Binding for one validated collection revision and Profile. It establishes project intent and does not activate Skills.
_Avoid_: Installation, Activation

**Initialization Plan**:
An immutable, deterministic, read-only description of the one Binding creation that Project Initialization would require. A blocked Initialization Plan contains issues and no proposed action or Binding preview.
_Avoid_: Activation Plan, transaction

**Initialization Application**:
One request to create the first project Binding from an exact, freshly revalidated Initialization Plan. It exclusively publishes that Binding or reports that creation did not complete.
_Avoid_: Activation, installation

**Initialization Result**:
The immutable outcome of one Initialization Application: created, blocked, or failed. Creation fields describe only what that invocation observed itself create and are not durable ownership records.
_Avoid_: Activation Result, ownership record

**Initialization Cleanup Report**:
The deterministic account of best-effort removal after an Initialization Application fails or is interrupted. It is not a Rollback, repair instruction, durable journal, or ownership proof.
_Avoid_: Rollback, recovery record

**Catalog**:
The generated inventory of resolved Skills and their provenance, paths, and Codex-facing names at a specific collection state. It is derived data and is not hand edited.
_Avoid_: Registry, manifest

**Activation**:
The explicit application of a validated plan that exposes selected Skills to one project through collection-owned symlinks. Activation does not copy Skill contents or change global Skills.
_Avoid_: Installation, deployment

**Activation Result**:
The immutable outcome of one apply request: applied, unchanged, blocked, or failed. Its creation fields are an invocation history; a Cleanup Report separately states what remained after failure cleanup.
_Avoid_: Transaction log, installation result

**Project Status**:
An immutable, deterministic projection of an Activation Review for one project. It reports observed state and does not authorize mutation.
_Avoid_: Health check, repair plan

**Doctor Report**:
A Project Status together with inspection of the platform capabilities required by safe Activation. It performs no probe writes and does not guarantee that later state will remain unchanged.
_Avoid_: Certification, repair report

**Guidance**:
Stable presentation metadata attached to an existing issue. Guidance explains a next inspection step without changing issue semantics or performing repair.
_Avoid_: Fix, remediation action

**Cleanup Report**:
The deterministic account of best-effort removal performed after a failed invocation. It is not durable ownership proof and is not a Rollback.
_Avoid_: Rollback, repair record

**Activation Plan**:
A deterministic, read-only description of the project-local directories and Skill links that Activation would require. A blocked Activation Plan contains issues and no proposed actions.
_Avoid_: Plan, transaction

**Activation Record**:
A canonical project-local document that durably records the rooted paths, object types, and Skill targets owned by one Activation. It carries a stable activation identity; it does not authorize mutation by itself.
_Avoid_: Lock file, transaction journal

**Collision**:
A condition in which two selected Skills claim the same Codex-facing name or an activation target is already project-owned. A Collision prevents activation until resolved.
_Avoid_: Duplicate

**Update**:
A deliberate transition from one pinned collection state to another, followed by catalog regeneration, validation, and a reviewed activation plan. Fetching remote state alone is not an Update.
_Avoid_: Sync, automatic update

**Rollback**:
A deliberate return to a previously recorded, valid collection state and its corresponding project activation. Rollback restores known intent; it is not an ad hoc filesystem repair.
_Avoid_: Undo, reset
