# Completed Checkpoint 7A migration history

Status: **completed historical evidence; non-authoritative**.

This map records how symbols from the superseded broad 7A design were assigned
when the approved reduced 7A contract was implemented. Names under “Broad 7A”
are historical and do not describe current supported Python or CLI seams. The
authoritative current contract is
`docs/checkpoint-7a-source-update-planning-contract.md`.

## Broad-7A public types and literals

| Historical symbol | Completed destination | Migration note |
|---|---|---|
| `SourceUpdateStatus` | 7A | Narrowed to `ready` and `blocked`; `ready` meant handoff-ready, not object-ready. |
| `SourceRelationship` | 7A | Retained for remote/current comparison only. |
| `SourceRevisionComparison.relationship` | 7A/7B | Reduced 7A used `unchanged` or `unverified`; `fast-forward` was reserved for 7B after acquisition. |
| `SkillChangeKind`, `CatalogChangeKind` | 7C | Local projection outcomes. |
| `ImpactReasonKind` | 7C | Group/Profile projection reasons. |
| `ReactivationConsequence`, `LinkConsequenceKind` | 7C | Project preview outcomes. |
| `SourceUpdateRoot` | 7A/7C | Collection/source roots were retained in 7A; project roots were assigned to 7C. |
| `SourceUpdateLocation` | 7A | Renamed to `InspectionLocation`; a separate projection location type was reserved for 7C. |
| `SourceUpdateIssue` | 7A/7B/7C/7D | Split into checkpoint-owned issue types; rooted redaction rules stayed in 7A. |
| `SourceUpdateRequest` | 7A | Renamed to `RemoteCandidateRequest`; its shape validator became authoritative in 7A. |
| `NetworkAuthorization` | 7A | Retained unchanged in meaning. |
| `SourceRevisionComparison` | 7A | Renamed to `RemoteCandidateComparison`. |
| `CatalogChangePreview`, `SkillChangePreview` | 7C | Catalog/Skill projection. |
| `ImpactReason`, `GroupImpact`, `ProfileImpact` | 7C | Group/Profile projection. |
| `LinkConsequence`, `ReactivationPreview` | 7C | Project preview. |
| `SourceUpdatePlan` | 7A/7B/7C | Split into `RemoteCandidateInspection` (7A), a future acquisition record (7B), and a future projection plan (7C). |
| `plan_source_update` | 7A/7B/7C | Replaced in 7A by `inspect_remote_candidates`; separate acquisition and projection seams were reserved. |

## Broad-7A issue codes

| Historical issue code(s) | Completed destination |
|---|---|
| `source-update.network_not_authorized` | 7A |
| `source-update.request_invalid`, `request_duplicate` | 7A |
| `source-update.source_missing`, `source_not_external` | 7A |
| `source-update.remote_transport_unsupported`, `credentials_required` | 7A |
| `source-update.remote_unavailable`, `remote_response_invalid`, `remote_ref_missing` | 7A |
| `source-update.current_pin_unavailable`, `object_format_unsupported` | 7A, with repository-state refinements in 7B |
| `source-update.candidate_not_commit`, `candidate_not_descendant` | 7B |
| `source-update.candidate_unavailable` | 7B |
| `source-update.candidate_tree_invalid`, `projection_invalid` | 7C |
| `source-update.project_uninspectable` | 7C |
| all Catalog/Skill/Group/Profile/Project consequence errors | 7C |
| `_RemoteOutputInvalid`, `_RemoteCleanupFailure` | 7A internal safety evidence |
| unexpected system/interruption errors | 7A CLI safety; 7B–7D define checkpoint-specific extensions |

## Historical implementation modules and symbols

| Historical module/symbols | Completed destination |
|---|---|
| `source_update.py`: URL/ref validation, request selection, committed gitlink lookup, remote advertisement parsing, `_run_remote_git`, `_run_bounded_remote`, `_terminate_process_group`, sanitized environment, inspection identity, issue normalization | 7A |
| `source_update.py`: `_candidate_state`, `_projection_objects_available`, candidate object evidence | 7B |
| `source_update.py`: `CatalogChangePreview` through `_skill_tree_digest`, `_discover_skill_trees`, `_project_skill_changes` | 7C |
| `source_update.py`: `_project_groups`, `_project_profiles`, `_project_reactivation`, project validation | 7C |
| future pin/Catalog/Activation writes | Assigned to 7D; reduced 7A authorized none of them |
| `cli.py`: broad `plan-source-update` parser and dispatch | Replaced for 7A by `inspect-source-candidates`; distinct future commands were reserved for 7B/7C/7D |
| `output.py`: broad `_source_update_lines` and JSON envelope | Replaced by 7A inspection output; separate future schemas were reserved for 7B/7C/7D |
| `__init__.py`: broad source-update exports | Replaced by reduced-7A remote-inspection exports; projection exports were removed from the active diff |
| `validation.py`: broad collection/project validation dependency | Removed from remote orchestration; reduced 7A received a selected-Source validation boundary while generic validation remained checkpoint-neutral |
| `CONTEXT.md` broad source-update vocabulary | Rewritten to describe reduced 7A and to label acquisition/projection/application language as future 7B/7C/7D work |

## Historical tests

| Historical test file / exact tests | Completed destination |
|---|---|
| `tests/test_source_update.py::SourceUpdatePublicSeamTests::test_network_authorization_is_required_before_any_git_child`; `test_authorization_value_is_exact_and_immutable` | 7A |
| `tests/test_source_update.py::test_missing_candidate_objects_return_complete_inspection_evidence`; `test_equal_advertised_pin_is_a_complete_unchanged_ready_plan`; `test_every_selected_url_is_validated_before_the_first_remote_call` | 7A, with unavailable-object assertions split to 7B |
| `tests/test_source_update.py::test_dirty_collection_documents_block_before_remote_inspection`; `test_remote_advertisement_requires_one_exact_lowercase_record`; `test_remote_timeout_is_sanitized_as_a_rooted_blocker`; `test_anonymous_authentication_challenge_has_no_remote_detail`; `test_test_owned_git_child_receives_only_exact_command_and_sanitized_environment` | 7A |
| `tests/test_source_update.py::test_timeout_kills_descendant_that_ignores_graceful_termination`; `tests/test_source_update_remote_safety.py` safety tests | 7A |
| `tests/test_source_update.py::test_divergent_commit_blocks_even_when_its_projection_tree_is_missing`; `test_advertised_local_blob_is_not_misreported_as_unavailable`; `test_promisor_repository_is_rejected_before_remote_inspection` | 7B, except repository safety preconditions remain 7A |
| `tests/test_source_update.py::test_local_fast_forward_projects_complete_skill_group_and_profile_changes`; `test_modified_selected_skill_requires_project_reactivation`; `test_equal_content_at_a_new_path_is_removal_plus_unresolved_addition`; `test_source_root_skill_follows_existing_discovery_rules`; `test_authored_profile_removal_still_requires_catalog_resolution` | 7C |
| `tests/test_source_update_projection_matrix.py` all tests | 7C |
| `tests/test_source_update.py::test_project_aliases_deduplicate_before_stable_labels_and_identity` | 7C |
| `tests/test_source_update.py::test_public_results_reject_mutable_nested_collections`; `tests/test_source_update_contract.py` all tests | Split: 7A output/inspection invariants; 7C projection invariants; 7D apply invariants |
| `tests/test_source_update.py::SourceUpdateCliSeamTests` all tests | 7A after command/output rename; new 7B/7C/7D command snapshots belong to those checkpoints |
| `tests/test_source_update_remote_safety.py::test_transport_policy_rejects_tls_scope_expansion_and_credentials`; `test_authentication_failure_is_sanitized_and_does_not_retry`; `test_dirty_collection_is_rejected_before_remote_or_mutation` | 7A |
| `tests/test_source_update.py::test_candidate_*` and all remote-boundary tests | 7A, except local object availability cases move to 7B |

## Preserved checkpoint boundaries

- Reduced 7A retains only remote candidate inspection and its safety/output
  requirements; broad executable fossils are not retained in documentation.
- 7B must not silently inherit 7A network authorization or become an implicit
  fetch/update path; it needs an explicit acquisition contract.
- 7C must consume a recorded 7B acquisition result rather than rediscovering or
  lazily acquiring objects.
- 7D is the only checkpoint allowed to write selected pins and Catalog state.
  It must produce guidance/fresh Activation reviews; existing Activation owns
  its separate explicit plan/apply mutation handshake.
- Future 7B/7C/7D tests are introduced only after the corresponding checkpoint
  contract is approved.
