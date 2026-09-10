# Fafnir My Assets Bridge

This Editor-only Unity package exports the signed-in account's minimal My
Assets metadata for local Fafnir search. It also accepts bounded,
approval-gated download jobs from the local Fafnir MCP server and passes the
verified product IDs to Unity's authenticated Package Manager downloader.
It does not export credentials, page content, images, or asset contents.

## Use

Fafnir's project-aware installer can embed this pinned package after displaying
a reviewable manifest/file plan. Start with bridge-doctor and bridge-plan,
close the target Editor before an approved bridge-apply, then open the project
and sync below. Diagnose again to verify actual compiler/sync evidence.
The manual installation option below remains available.

1. In Unity Package Manager, choose **Add package from disk** and select this
   directory's `package.json`.
2. Sign in to Unity Hub / Unity Editor.
3. Open **Tools > Fafnir > My Assets Sync**.
4. Leave the default periodic sync enabled (6 hours), or change the visible
   interval. Click **My Assetsを同期** when a new purchase must appear immediately.
5. Fafnir imports a changed export during the current or next UI/MCP run;
   no second Catalog sync is normally required.
6. Keep any Unity project containing this bridge open when an AI requests an
   owned-asset download. Fafnir queues only product IDs verified by the Unity
   ownership sync, while Unity writes progress and completion back locally.

Downloads go to Unity's global Asset Store cache. They do not import files into
the open project. Browser access and browser-session sharing are not required.

The package uses a runtime reflection adapter over Unity's undocumented
internal Package Manager service. If Unity changes that service, Fafnir
will fail closed and show an error instead of attempting to access credentials
directly.

Set the same absolute FAFNIR_BRIDGE_ROOT for both processes to isolate bridge
files and the My Assets export. This does not move Unity's download cache.
Batch-mode Editors do not automatically handle interactive jobs or sync.
