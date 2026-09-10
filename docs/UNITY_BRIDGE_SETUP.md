# Project-specific Unity bridge setup

An available Fafnir MCP server does not prove a Unity project contains the bridge.
An offline heartbeat can mean a closed, frozen or blocked Editor. Setup uses MCP
or the Fafnir CLI, without Unity CLI Loop, Node.js or a developer's source path.

## Normal workflow

1. Resolve the target's absolute path and call diagnose_unity_bridge(project_path).
2. For a missing bridge or unmodified managed older release, call
   prepare_unity_bridge_install(project_path). This saves a local plan without
   changing the Unity project.
3. Show the project, fixed version, MIT license, bundle hash, file list/hashes,
   manifest diff and rollback limit. Obtain approval for that exact plan.
   Close the target Unity Editor normally; setup does not save, stop, kill or launch it.
4. Call apply_reviewed_unity_bridge_install(plan_id, approval_nonce) within ten
   minutes. Keep the returned job ID and rollback nonce.
5. Open that project, wait for compilation, then open
   **Tools > Fafnir > My Assets Sync** and click **My Assetsを同期**.
   Restore the Unity account session only if needed.
6. Call get_unity_bridge_install_status(job_id) or diagnose again.
   The job status applied_waiting_for_unity records file installation only.
   diagnosis.setup_verified requires a fresh heartbeat from this project,
   the expected bundle fingerprint, the Editor compiler error flag reporting
   success, and a successful My Assets sync in this Editor session.
   Check fafnir_status separately for import of that export into the catalog.
7. Proceed to an independently reviewed owned-asset download/import.
   Setup verification is not proof of asset compatibility or import permission.

CLI equivalents (replace the path and returned IDs/tokens):

    fafnir --json bridge-doctor --project "C:\Projects\My Game"
    fafnir --json bridge-plan --project "C:\Projects\My Game"
    fafnir --json bridge-apply PLAN_ID --approval-nonce APPROVAL_NONCE
    fafnir --json bridge-status JOB_ID
    fafnir --json bridge-rollback JOB_ID --rollback-nonce ROLLBACK_NONCE

Rollback requires a closed Editor and the returned nonce within 24 hours.
Its MCP equivalent is rollback_unity_bridge_install(job_id, rollback_nonce).
Never publish nonces or users' local paths in issues or PRs.

## Portable package and safety

The official MIT package is bundled at the version in package.json, including
Editor code, metadata, assembly definition and license. Wheels contain package
resources; source archives include unity_package/com.taiyousun.stackforge.
No network download occurs during setup.

Only these locations can change:

- Packages/com.taiyousun.stackforge/
- Packages/manifest.json (file:com.taiyousun.stackforge dependency)
- ProjectSettings/FafnirBridgeInstall.json (portable file-hash receipt)

Commit these files with the consumer project. The receipt contains no developer
checkout path. Unity 2022.3 is the declared minimum, not a guarantee every newer
Editor supports the internal download adapter. Unsupported APIs fail closed.

Approval is single-use and bound to the exact plan. Setup rechecks project/release
content, uses a project lock and atomic per-file replacements, and restores its
own writes on ordinary I/O failures. Rollback restores exact prior bytes only
while installed files are unchanged; it cannot undo effects of Editor code
that ran after launch. This is not a power-failure-safe multi-file transaction.
An interrupted lock requires inspection; the persisted SQLite job retains prior bytes.

Identical manual packages can acquire a receipt. Different manual copies,
Git/registry dependencies and modified managed packages require explicit migration;
there is no force-overwrite option.

## Diagnosis and recovery

| Result | Meaning / next step |
| --- | --- |
| not_installed | No embedded package or dependency; prepare setup. |
| declared_unverified | A dependency is declared, but resolution is not proven. Inspect it. |
| other_project / unknown_project | The heartbeat does not identify this target; not verified. |
| managed_unmodified with matches_bundle=false | Review a package update. |
| compilation or sync unknown | Missing evidence, not a pass. Inspect Unity. |
| close_target_unity | Close Unity normally; stale Unity locks are never deleted automatically. |
| bridge_plan_changed | Project/release changed after review; prepare another plan. |
| bridge_setup_busy | Inspect the other/interrupted saved job before recovering the lock. |
| rollback_conflict / bridge_recovery_required | Preserve new edits and reconcile manually or through version control; do not blindly retry. |

## Isolation and limitations

The shared default transport directory is unchanged. Set FAFNIR_BRIDGE_ROOT to
the same absolute directory for both Fafnir and Unity before launch to isolate
jobs, heartbeat and My Assets export. It does not relocate the database (use --db)
or Unity's global Asset Store cache. Batch-mode Editors do not automatically poll
interactive jobs, emit heartbeats or sync My Assets.

This does not implement simultaneous-Editor download arbitration: the default
heartbeat remains shared and may be replaced by another Editor. Mismatches are
reported conservatively. Account hints are not proof a download will succeed.
The compiler error flag is version-dependent; unavailable evidence stays unknown.
Setup responses include the reviewed local path, hashes, manifest diff and
one-time tokens, but no account credentials or asset binaries.
