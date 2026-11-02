# Repository instructions

Keep runtime credentials and data outside Git. Use sanitized examples only.
Use Python 3.13 and the pinned requirements. Run `python tools/run_tests.py`.
New custom routes belong to /api/v2/campus and require caller-scoped authorization.
Keep api/campus.openapi.json aligned with the frontend's schema mirror.
Never start importers, bots, Codex, deployments or SSH in CI. Tests use fake
fixtures and loopback networking only. Do not change production or rotate tokens
as a side effect of source changes. Preserve source and license attribution.
