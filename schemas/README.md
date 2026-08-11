# Schemas

These JSON Schema Draft 2020-12 documents describe the values produced after TOML
parsing. They intentionally do not prescribe filenames or implement validation.

| TOML document | Schema | Ownership |
| --- | --- | --- |
| Sources | `sources.schema.json` | authored |
| Groups | `groups.schema.json` | authored |
| Profiles | `profiles.schema.json` | authored |
| Binding | `binding.schema.json` | project-authored |
| Catalog | `catalog.schema.json` | generated |
| Activation Record | `activation-record.schema.json` | generated project-local state |

## Cross-document invariants

JSON Schema alone cannot express all collection rules. Future validation must also
enforce:

- every Source identifier, Group name, Profile name, and Skill identity is unique;
- every referenced Skill, Group, Profile, and Source exists;
- Group nesting is acyclic;
- Profile inheritance is acyclic;
- a resolved selection contains at most one Skill per Codex-facing name;
- removals refer to Skills present before removal;
- catalog entries remain within their declared Source roots;
- native external Source paths are initialized, clean Git submodules at their pinned
  parent-repository commit;
- activation targets do not contain broken symlinks; and
- activation never overwrites a project-owned file, directory, or symlink.

Arrays are ordered. Resolution processes inherited Profiles, then Groups, then
explicit Skills, then `add`, and finally `remove`. Repeating an identical Skill
identity is idempotent; selecting different Skill identities with one Codex-facing
name is a Collision.
