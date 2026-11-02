# Private configuration for a new local installation

The examples contain no accounts or functioning secrets. Numeric IDs are placeholders;
replace them with IDs created in your disposable Vikunja instance. Never point this
stack at production while experimenting. Registration is enabled only in the local
example so disposable users can be created; production access policy is separate.

Create private directories under ignored `runtime/`, owned by container UID 1000
on Linux. Configuration/state files must be writable only by the service owner.
Do not commit runtime files. Use dedicated least-privilege service accounts and
tokens in your own local instance. Keep an independent backup before any migration.

1. `integration-config/config.json`: copy the config example; set project/view IDs,
   your chosen `bot_username`, source allowlists and course routing. For Telegram,
   each chat record needs `id` and `title`, plus `reader_telegram_id` after login.
   All source lists start empty. Do not infer membership or share mail by default.
2. `integration-config/secrets.json`: copy the blank secrets example and supply your
   own service token and authorized source credentials privately. Google OAuth
   files are `google-<account>.json`; never store them in the repository. The setup
   CLI is `python -m uni_tasks.auth --help` inside the appropriate container context.
   Existing CLI setup may perform external authorization and must be run intentionally.
3. `enrollment-config/catalog.json`, `replication-config/catalog.json` and
   `notification-config/catalog.json`: start from the catalog example. Keep course
   and student mappings consistent. A course needs `id`, `title`, `project_id` and
   `view_id`; a student needs `user_id`, `username`, `personal_project_id` and
   `personal_view_id`. Each personal project must belong to that student. Do not
   publish rosters. Set the replication service identity to its actual local user.
4. `enrollment-config/token.txt`: dedicated enrollment token; only the project
   access operations required by the service. `replication-config/token.txt` and
   `notification-config/vikunja-token.txt`: personal-copy service token, with access
   granted only to configured subject and personal projects. `notification-config/
   bot-token.txt`: the notification bot token. These files are never examples in Git.
5. `codex-auth/`: authorize the isolated parser explicitly if you intend to run it.
   It uses the pinned Codex CLI and subscription authorization. Do not put the
   source-service credentials in this directory. Shared usage limits still apply.

The importer uses the v1 read/create API and v2 partial updates. Campus services
use v2. Classroom sharing requires exact `classroom_ids`; Gmail sharing requires
explicit `gmail_rules`. Unknown or personal material stays private. Telegram topic
routes use `telegram_topics`. Custom task markers and state databases are part of
the data model; never discard them to solve a synchronization problem.

No automatic provisioning or production deployment is included. The examples
reproduce the service boundary without packaging working accounts, tokens or data.
