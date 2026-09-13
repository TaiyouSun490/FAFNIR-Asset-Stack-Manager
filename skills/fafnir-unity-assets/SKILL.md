---
name: fafnir-unity-assets
description: Use Fafnir MCP to find, evaluate, download, and install Unity Asset Store assets, GitHub repositories, and OpenUPM packages for a concrete game or Unity project. Use when the user asks which Unity assets to use, wants an AI-built implementation stack, or asks to acquire or introduce selected assets. Do not use for ordinary Unity coding that needs no third-party asset discovery.
---

# Fafnir Unity Assets

Use Fafnir as the evidence source and the calling model as the technical director. A local score is a retrieval hint, not a reason to install an item.

## Build the stack

1. Call `fafnir_status` to confirm the MCP connection, ownership sync, catalog counts, RAG state, and public product-detail state. If the tool is unavailable, stop and explain that the configured `fafnir` MCP server is required.
2. Call `retrieve_game_stack_evidence` with the complete game brief, real Unity project path when known, target platform, budget, remote-search preference, and `visual_review` preference (`off`, `quick`, or `detail`). Default to `quick` unless the user asks to avoid image retrieval or wants a deeper gallery review. Do not reduce the request to one keyword.
3. Review every requirement group. Use `search_owned_asset_rag`, `search_unity_assets`, and `get_unity_asset_candidate` when a requirement has weak, ambiguous, or missing candidates. Request `refresh_asset_store_product_details` only when missing public metadata materially blocks a decision.
4. Produce one coherent implementation stack rather than one winner per source. Prefer the smallest non-redundant combination that covers the game.

For every selected candidate state:

- its stable candidate ID and ownership/source status;
- the concrete scene, feature, content, or production task it will serve;
- why its evidence actually supports that use;
- how it connects to the other selected components;
- Unity version, render pipeline, platform, license, and maintenance checks still required;
- whether it is already present, automatically installable, downloadable for inspection, or manual-only.

Never select an item merely because it is owned. Reject generic word matches, unrelated frameworks, duplicate systems, archived repositories, and candidates whose available metadata does not establish a plausible use. Never invent candidate IDs, URLs, package names, ownership, or compatibility.

### Optional product-image review

For `visual_review=off`, do not fetch product images. For `quick`, first build a text-grounded shortlist and then call `review_asset_store_candidate_visuals` with `detail=quick` only for the final Asset Store candidates whose visible style or content coverage can change the decision. For `detail`, call it with `detail=detail` to inspect the extended gallery before final adoption. The tool returns actual image content to the model, not merely image URLs.

Use visual review to judge art direction, apparent asset breadth, readability, and whether screenshots contradict the proposed use. Do not infer Unity compatibility, code quality, performance, license, included source files, or exact package contents from images. Classify the result as `adopt`, `hold for detail`, or `reject`, and state separately which textual or package evidence still controls the technical decision. An unavailable image is not evidence against the asset.

### Image-backed candidate approval

When the user needs to compare alternatives, call `compare_asset_store_candidates` with the saved candidate IDs. It returns identity-labelled cards and actual representative product images; follow `next_offset` for additional pages (default three cards, maximum six per call). The shortlist is not limited to one winner. Use `include_images=false` when visual review is off; that mode makes no product-detail or image requests. Use the single-candidate visual tool for an extended gallery.

Show the user the relevant real product images alongside each stable ID, proposed role, reasons, ownership and remaining unknowns. An internal model-only image review is not a user-visible approval. If the client cannot display the images, say so and offer the official product link or FAFNIR's catalog comparison UI. Never substitute a generated concept image for the actual asset's evidence.

Record the user's choice separately for each candidate: select, hold, or reject. A selection or an AI suitability judgment is not download, import, or scene-adoption approval. Do not transfer approval to a different candidate, version, or scope. Preserve unanswered candidates as pending/held with a message; do not treat a timeout as consent. The Web UI stores latest decisions in that browser only, not in a shared MCP work queue. A calling Host must maintain its own conversation/project approval state.

## Acquire selected components

Only acquire files when the user asked to download or install them. Resolve the exact target Unity project before project-specific validation or installation; downloading to the shared Asset Store cache does not require a destination project. Keep downloads and inspection clones outside the project until they pass inspection. Do not install every returned candidate; acquire only the approved coherent stack.

### OpenUPM

Call `prepare_candidate_install` with the chosen candidate ID and project path. Show the returned manifest diff, fixed version, license, risks, and rollback limit. Call `apply_reviewed_install` with the returned plan ID and one-time nonce only after the user approves that exact plan. Then wait for Unity Package Manager resolution and verify compilation.

### GitHub

Treat a search result as a repository lead, not an installable package. Inspect the official repository's license, maintenance, `package.json`, Unity layout, dependencies, setup instructions, and a fixed release tag or full commit SHA. Prefer a pinned UPM-compatible dependency. If it is not a valid Unity package, download it to a temporary directory and propose the exact files and destination before copying anything into the project. Do not run downloaded scripts or binaries merely because the repository is popular.

### Unity Asset Store

#### Project bridge setup

MCP connectivity is separate from Unity bridge installation. If the bridge reports offline
and a target project is known, call diagnose_unity_bridge with that exact path.
Do not equate offline with missing or another project's heartbeat with target validation.
For setup/update, call prepare_unity_bridge_install, show its project, fixed version,
file hashes and manifest diff, and call apply_reviewed_unity_bridge_install
only after the user approves that exact plan and closes the target Editor.
Keep the rollback nonce. Call get_unity_bridge_install_status after the user opens
that project and runs Tools > Fafnir > My Assets Sync. Require diagnosis.setup_verified,
then inspect fafnir_status for catalog import; unknown is not Passed.
Use rollback_unity_bridge_install for an authorized rollback. Changed files require
reconciliation, not forced retries. Reusing a connected bridge for global-cache
download alone does not require switching to the target project.

For confirmed-owned items, call `prepare_owned_asset_download` with their exact candidate IDs. Review the returned titles, product IDs, versions, sizes, cache state, and Unity bridge state. If `requires_approval` is false, every selected package is already cached, so do not start another download. Otherwise, if the user's current message explicitly requested those exact products, that is sufficient approval when the plan matches; show the plan and wait for approval in all other cases. Then call `start_reviewed_asset_download` with the returned plan ID and one-time nonce.

Poll `get_asset_store_download_status` until every product is completed or a concrete error is returned. The local Unity Editor bridge performs the authenticated Package Manager download and writes only progress and cache paths back to Fafnir. It downloads to Unity's global cache and does not import into the open project.

Use `bridge.readiness` and `next_action` to minimize user steps. Reuse a connected bridge without requesting a project switch, reopening My Assets, or a fresh login. `signed_in` is an Editor session hint, not proof that an Asset Store request will succeed; `unknown` is not evidence of logout. For `sign_in_required` or job `error_code=unity_authentication_required`, ask only to restore the account session in the bridge Editor via Unity Hub/My Assets. For `busy`, wait instead of restarting Unity. Report `starting` as waiting for Unity, not active transfer. Opening the destination is needed for actual Unity import/compilation, not cache acquisition. An import from a valid cached package needs no new Asset Store login.

Do not take control of a browser, request browser-session sharing, or ask the user to click Download merely to acquire an owned package. If no target project is known and the bridge reports offline, ask which project should host it; do not silently add it to an unrelated project. Fafnir and the bridge must not extract browser data, Unity account credentials, or session values. Unity's internal download adapter must fail closed when the Editor version is unsupported.

If an item is already linked to a cached `.unitypackage`, do not download it again. Call `validate_cached_asset_for_project` before proposing import; enable its temporary compile test only when that extra validation is warranted.

For a purchase-unconfirmed item, recommend it with its expected role and official link, but do not claim ownership, purchase it, or download paid content. Wait until the user confirms acquisition.

### Existing local components

Do not download an item already present in the target project. A locally cached package is evidence of availability, not ownership; keep those states distinct.

## Verify the result

After installation, verify package resolution, compiler status, render-pipeline and input compatibility, required setup steps, and whether the component can perform its stated role in the actual project. Report completed installs, manual Asset Store actions, rejected candidates, and uncovered requirements separately. Preserve the rollback nonce until verification finishes.
