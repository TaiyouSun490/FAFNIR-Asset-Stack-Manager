# Stackforge My Assets Bridge

This Editor-only Unity package exports the signed-in account's minimal My
Assets metadata for local Stackforge search. It does not export credentials,
page content, images, package files, or asset contents.

## Use

1. In Unity Package Manager, choose **Add package from disk** and select this
   directory's `package.json`.
2. Sign in to Unity Hub / Unity Editor.
3. Open **Tools > Stackforge > My Assets Sync**.
4. Click **My Assetsを同期**.
5. In the Stackforge Catalog, click **所有アセットを同期**.

The package uses a runtime reflection adapter over Unity's undocumented
internal Package Manager service. If Unity changes that service, Stackforge
will fail closed and show an error instead of attempting to access credentials
directly.
