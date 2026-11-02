# Upstream provenance

Runtime API: Vikunja v2.7.1, official image `vikunja/vikunja:2.7.1`.
Compose pins its multi-platform image index to
`sha256:f7dc8428d10aef17f3075886d3c1c022f566ec456f88985f02c137f4fc534576`,
verified from the Docker registry on 2026-11-02 without starting a container.
Corresponding source: https://github.com/go-vikunja/vikunja/tree/5619c4a9f66ddfc13f7bbba24ada07c3e82ae7a0
License: AGPL-3.0-or-later. A copy is included as LICENSE.

The custom Python services communicate with this API; no modified Go backend is
required for this source snapshot. The customized Vue frontend is separately
published as tasks-frontend. This repository starts with a clean root commit,
without importing deployment history or upstream Git history.

Packaging changes: generic visible branding; explicit bot_username instead of a
hardcoded deployment bot; sanitized examples; local-only Compose; test-only CI;
documentation. Existing processing and notification bugs are intentionally unchanged.
