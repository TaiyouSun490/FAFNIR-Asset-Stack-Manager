# Image-backed asset comparison and approval

FAFNIR presents actual public product images, not generated concepts or proof of what
an asset will look like in a particular Unity scene. Compatibility, performance, package
contents and placement still need their own checks.

## Web workflow

1. In the catalog, use **比較に追加** on the relevant Asset Store cards. Open
   **画像で候補を比較** in the catalog header. The board shows six candidates per page;
   up to 5,000 distinct saved IDs can be shortlisted, not just one preferred winner.
2. Compare each candidate's images, stable ID, description, ownership evidence,
   catalog version, size and known technical metadata. Enlarge images or step through
   available gallery images. Missing images remain visible as missing, with retry and
   the official product link; they do not count as incompatibility.
3. Record **選択**, **保留**, or **見送り** per candidate. Multiple selections are allowed,
   and choices can be reversed. None of these choices starts a download.
4. Use **この候補の取得内容を確認** for a verified-owned candidate. The separate approval
   dialog displays the real product image and the exact version/size returned by the
   prepared download plan, which can differ from catalog metadata. Its deadline comes
   from that plan. **この1件を取得** authorizes only this package's shared-cache download.
   **保留**, **見送る**, Escape, closing the parent, and expiry do not start it.
5. A cached item is reused without another download. Import remains a separate
   project-specific workflow, including package inspection and Unity's import dialog.
   Selecting download-and-import does not bypass the download decision: cancelling
   download also stops that combined action. Neither action adopts anything in a scene.

Latest per-candidate choices and unanswered acquisition messages live in this browser's
`localStorage` (`fafnir.assetReview.v1`). Pending confirmation is recorded before waiting,
so leaving/reloading the page leaves a hold message. This is not an immutable history,
shared MCP queue, notification service or reusable authorization. No approval nonce is
stored there. Expired/reopened confirmations require a fresh plan and user response;
the browser never executes from saved preferences. Storage failure fails closed.

## MCP / Host workflow

`compare_asset_store_candidates(candidate_ids, offset=0, limit=3, include_images=True)`
accepts 1–5,000 distinct saved Asset Store candidate IDs, with 1–6 per page. Follow
`next_offset` until the desired alternatives have been reviewed.

The unstructured MCP response contains:

- A summary JSON block (`total`, `offset`, `next_offset`, `selection_is_approval=false`).
- A candidate card JSON block, immediately followed by its actual image when available.
  Images include their own source URL and candidate ID in the card; do not infer
  identity from position alone. Each card contains only the comparison projection,
  not the raw catalog metadata or cached-package paths.

One representative image per candidate is attached. Existing public product-details
retrieval is used if image metadata is missing. Use the existing
`review_asset_store_candidate_visuals` for an extended single-product gallery.
Per-image failures leave the other cards usable. Images use the existing restricted
HTTPS Unity CDN fetcher (5 MiB per image), with a 20 MiB aggregate attachment budget.
Smaller pages can retrieve candidates omitted by the image budget.

`include_images=False` uses only saved catalog metadata: no product-detail or image
network requests. The Web route `POST /api/asset-store/compare` with
`{candidate_ids, offset, limit}` also returns only saved, read-only cards; the browser
requests product images separately when displaying the comparison.

The Host should display the returned real images to the user, describe the proposed
role and uncertainty for each candidate, then retain the user's per-candidate decisions
in its own conversation/project workflow. An image delivered only to the model is not
a user-visible approval. If the client cannot render images, disclose that limitation
and offer the official product page or the Web comparison board.

This tool never prepares downloads or issues approval nonces. Acquisition still uses
the existing prepare → user approval → one-use start contract. Approval for candidate A
cannot authorize B, and an AI's suitability judgment is not the user's approval. The
Web browser's local choices are not automatically delivered to an MCP client.

## Isolated verification

Python regression coverage:

```sh
python -m pytest -q
```

Browser tests require Node, Playwright and Microsoft Edge. Start the test-only server
from the checkout, using that checkout as `PYTHONPATH`:

```sh
PYTHONPATH=. python tests/serve_asset_review_ui.py
```

The helper prints a loopback URL and uses a temporary SQLite catalog without the user's
database, Unity bridge or automatic background workers. On PowerShell set
`$env:PYTHONPATH = (Get-Location).Path` before running the Python command. In another
terminal set `FAFNIR_UI_URL` to the printed URL, then run:

```sh
node tests/asset_comparison_browser.cjs
node tests/asset_actions_browser.cjs --mock-only
```

All catalog entries, public images and acquisition endpoints in these runs are fixtures.
Screenshots in `build/asset-*.png` demonstrate the UI, not actual asset quality. The tests
exercise 15 candidates, pagination, missing images, multiple/reversed choices, reload
persistence, exact-plan confirmation, hold/reject/expiry, mismatched product IDs,
cancelled combined import, a single approved download, and narrow-screen layout.
Existing download/import retry and cache-reuse tests also run. They do **not** prove a
real signed-in account download, Unity import, or scene adoption.
