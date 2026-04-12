# Design Library Gallery Plan

## Current state
Right now the design library stores metadata and references, but it does not yet have a local gallery UI for browsing examples visually.

## What we can support
A local gallery can be built as a lightweight app that:
- reads example JSON files from `design-library/examples/`
- renders cards with thumbnails/screenshots/video previews
- filters by tags, source, status, and design language
- opens source URLs
- groups by pattern or source
- shows annotation details and scores

## Recommended MVP
Build a simple local gallery page with:
- grid of examples
- preview image/thumbnail if available
- title, source, score, status
- tag chips
- filters for source / status / design language
- detail view drawer or page

## Important limitation right now
The current dataset has almost no local visual assets yet:
- no screenshots downloaded
- no thumbnails captured
- mostly metadata and links only

So a gallery can be built now, but without asset capture it will mostly be a metadata browser with outbound links.

## Best next steps
1. add a simple gallery app/page
2. add asset capture workflow for screenshots/thumbnails
3. update ingestion to optionally attach local asset paths
4. later add clustering and similarity browsing

## Suggested implementation path
- Phase 1: metadata gallery
- Phase 2: local screenshots/thumbnails
- Phase 3: richer browsing, search, clustering, and pattern views
