#!/bin/sh
# Rebuild the injectable Defuddle bundle. Run after `bun update defuddle`.
set -e
cd "$(dirname "$0")/.."
bun build ./vendor/entry.js \
  --outfile ./src/x_bookmarks_rag/vendor/defuddle.bundle.js \
  --format iife --minify
echo "built: src/x_bookmarks_rag/vendor/defuddle.bundle.js"
