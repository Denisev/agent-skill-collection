# Use a plain Git repository and native pinned Sources

The collection is distributed as a plain Git repository, and external Sources are
native Git submodules pinned by the parent repository. This keeps provenance and
reviewed revisions visible through standard Git operations without introducing a
package manager, plugin runtime, or automatic updater; the trade-off is that users
must clone submodules and advance pins deliberately.
