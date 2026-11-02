# Tasks backend

Python integration services for Telegram, Gmail and Google Classroom, backed by
Vikunja v2.7.1. The Vue client lives in
[tasks-frontend](https://github.com/stethebeatifulmartyr1stattempt/tasks-frontend).

This repository contains the custom backend code. Vikunja's unmodified Go API
is supplied by the pinned `vikunja/vikunja:2.7.1` image, not copied or rewritten.
See [UPSTREAM.md](UPSTREAM.md) for the exact corresponding source and license.

## Services and state

| Service | Purpose | Private runtime directories |
| --- | --- | --- |
| importer | Read configured sources, queue work, update Vikunja | integration-config, integration-state, integration-spool |
| parser | Isolated Codex text extraction | codex-auth, integration-spool |
| enrollment | Authenticated campus API and course access | enrollment-config, notification-state, campus-state |
| replication | Independent task copies per student | replication-config, replication-state, campus-state |
| notifications | One Telegram Bot API poller, reminders and digests | notification-config, notification-state, campus-state |

Each source is explicitly configured. The reader account reads selected Telegram
groups; the bot sends personal notifications. Gmail correspondence and individual
Classroom work stay private unless explicit sharing rules permit a group task.
Personal completion is not a submission to a university platform.

## Tests without credentials

Use Python 3.13 (production-compatible) and a fresh virtual environment:

```sh
python -m venv .venv
# Activate the virtual environment for your shell.
python -m pip install -r requirements.txt
python tools/run_tests.py
```

The runner permits loopback-only sockets for local HTTP fixtures and blocks
external network access during tests. CI does not run source importers, the bot,
Codex, SSH or deployments. All committed test credentials are synthetic.

## Disposable local stack

Keep both repositories beside each other. Build the frontend first. Copy
`.env.example` to `.env`, generate a fresh local JWT secret, and set it there.

```sh
docker compose up -d
```

Only Vikunja and the local gateway start by default, bound to 127.0.0.1:8080.
This default stack provides the standard task API only. Campus pages and
`/api/v2/campus/` require the enrollment service in the `integrations` profile;
without it, requests to those routes receive a gateway error.
Create disposable users in that local instance. The additional integrations are
an explicit `integrations` profile and require the private configuration described
in [docs/CONFIGURATION.md](docs/CONFIGURATION.md). They must never be enabled with
production credentials during tests. `.env` and `runtime/` are gitignored.

```sh
# After intentionally configuring your own new sources and local service tokens:
docker compose --profile integrations up -d --build
```

This is a development/reproduction configuration, not a production deployment or
migration script. It does not provision user accounts or copy existing data.
No changes were made to the existing production server for this repository split.

## API and compatibility

Vikunja exposes `/api/v1` and `/api/v2`. Custom authenticated routes live under
`/api/v2/campus/`: subjects, Telegram linking, preferences, calendar tasks, variants,
task mute settings and owner-only import review. The sanitized contract is
`api/campus.openapi.json`. Source collection is performed by workers, not a public
ingestion endpoint. The frontend contains a committed generated client.

Preserve legacy `pi42-*` machine markers and stored identity formats when attaching
existing data; renaming them requires a separate reviewed migration.

Known behavior is preserved: OCR/audio and Google attachment extraction are not
implemented; source edits and semester identities have known edge cases; the
notification status writer can fail on prolonged SQLite contention. This split
does not fix those issues or configure production backups/alerts.

See [NOTICE](NOTICE) and [LICENSE](LICENSE). Do not commit private configuration,
OAuth files, Telegram sessions, Codex auth, rosters, messages, databases or logs.
