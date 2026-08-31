# Stackforge My Assets Bridge

This Editor-only Unity package exports the signed-in account's minimal My
Assets metadata for local Stackforge search. It does not export credentials,
page content, images, package files, or asset contents.

## Use

1. In Unity Package Manager, choose **Add package from disk** and select this
   directory's `package.json`.
2. Sign in to Unity Hub / Unity Editor.
3. Open **Tools > Stackforge > My Assets Sync**.
4. Leave the default periodic sync enabled (6 hours), or change the visible
   interval. Click **My Assetsを同期** when a new purchase must appear immediately.
5. Stackforge imports a changed export during the current or next UI/MCP run;
   no second Catalog sync is normally required.

The package uses a runtime reflection adapter over Unity's undocumented
internal Package Manager service. If Unity changes that service, Stackforge
will fail closed and show an error instead of attempting to access credentials
directly.
