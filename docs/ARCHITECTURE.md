# Architecture

WordPressGenius is a single-user local Streamlit app. A LangGraph crew plans and
runs WordPress / WooCommerce work. A deterministic safety layer sits between the
model and the REST API. This document describes the tree as it exists.

## Request flow

`ui/app_main.py` collects a chat message and calls
`WordPressGeniusOrchestrator.run()` (`core/orchestrator.py`). The orchestrator:

1. Checks the per-user sliding-window rate limit (`core/rate_limit.py`).
2. Runs the prompt guardrails (`core/security.py` `PromptGuard`).
3. Builds `WordPressTools` from the active site's credentials.
4. Compiles and invokes the LangGraph crew (`agents/crew.py`).
5. Writes to the activity log and the hash-chained audit log.

The crew graph is linear:

```text
planner -> researcher -> content_writer -> executor -> reviewer -> communicator
```

State travels in an `AgentState` TypedDict: the request, the plan
(`list[PlannedAction]`), research notes, execution results, approval flags, and
per-agent status lines for the UI.

The Executor node is the gate. It re-evaluates the plan through `SafetyLayer`
with the connected WordPress user's live roles and capabilities. If confirmation
is required and the user has not approved, the graph stops there and returns an
approval summary with before/after previews (`core/preview.py`). Nothing is
written to WordPress until the user approves in the UI and the graph runs again
with `approved=True`.

## agents/

- `agents/crew.py` — the LangGraph `StateGraph`, the six node functions, each
  agent's system prompt, the `OPERATION_GUIDE` the planner can choose from, and
  the rule-based fallback planner (`_plan_actions`) used when no LLM key is
  configured or the model returns nothing usable. LLM plans are parsed and
  validated (`_actions_from_payload`); unknown operations and malformed actions
  are dropped, and plans are capped at four actions.

## core/

- `config.py` — `AppConfig`, a frozen dataclass built from environment
  variables. Secrets resolve from env, `<NAME>_FILE`, or `/run/secrets/`.
  Loads `.env` and `.env.<APP_ENV>` if present.
- `orchestrator.py` — application service described above. Owns the safety
  layer, rate limiter, prompt guard, memory, logs, and the LLM router.
- `safety.py` — `SafetyLayer`. See "SafetyLayer vs LLMRouter" below.
- `llm.py` — `LLMRouter` and `LLMClient`. One API key per provider powers every
  agent. Providers: OpenAI, Anthropic, Groq, Google Gemini (Groq and Gemini via
  OpenAI-compatible base URLs). Routes each agent role to a fast or premium
  model tier and a temperature. Includes the centralized JSON extractor and the
  no-key template copy (`fallback_blog_copy`, `fallback_page_copy`).
- `models.py` — shared pydantic models: `ChangeOperation`, `RiskLevel`,
  `PlannedAction`, `ExecutionResult`, `AgentStatus`, `WordPressCredentials`.
- `memory.py` — `ActivityLog` (JSONL event log) and `SiteMemory` (durable
  conversation/site-event store with a dependency-free TF-IDF search). All
  writes pass through `redact()`.
- `security.py` — `AuthManager` (YAML users, SHA-256 password hashes, app roles
  viewer/editor/admin), `CredentialVault` (Fernet-encrypted WordPress
  credentials per user), `SecurityAuditLog` (hash-chained JSONL with
  `verify()`), `PromptGuard` (Unicode normalization, instruction-override and
  mass-destruction blocking), and Fernet key helpers.
- `sites_store.py` — `SiteVault`: per-user encrypted storage of multiple saved
  WordPress sites, with an active-site pointer.
- `settings_store.py` — `LLMSettingsStore`: encrypted on-disk LLM provider/key/
  model settings, and `resolve_active_settings` merging env config with saved
  UI choices.
- `preview.py` — read-only fetch of the current resource before a gated change,
  rendered as a before/after field diff in the approval summary. Deletions and
  refunds are flagged irreversible.
- `rate_limit.py` — thread-safe sliding-window limiter used by both the
  orchestrator and the UI.
- `cache.py` — small in-process TTL cache for frequent WordPress reads.
- `observability.py` — optional Sentry (PII disabled) and consent-based local
  usage analytics.
- `logging_config.py` — logging setup plus `redact()`, the credential scrubber
  used by memory, audit, observability, and logs.

## tools/

- `wordpress_client.py` — `WordPressClient`: REST client with Application
  Password auth, transient retries with backoff, pagination helpers, read
  caching, friendly error messages, and secret-value redaction.
- `wordpress_tools.py` — `WordPressTools`: the high-level operation surface the
  crew executes against. Maps each `ChangeOperation` to client calls, produces
  `ExecutionResult`s, and triggers backups via `BackupManager`.
- `woocommerce.py` — WooCommerce `wc/v3` reads (products, orders, categories,
  tags, variations, customers with contact details masked, low stock) and
  writes (product CRUD, bulk create, stock, order status, bulk
  price/stock/status).
- `stripe_gateway.py` — Stripe-for-WooCommerce gateway detection, redacted
  settings summaries, read-only transaction listing, and refunds. Works
  entirely through WooCommerce; no Stripe secret key involved.
- `backups.py` — `BackupManager`: JSON snapshots of resources it can read
  before gated writes, rotation (`keep_last`), and `undo_latest` restore for
  posts, pages, settings, WooCommerce products/orders, and plugin activation
  status.
- `plugin_manager.py` — installed-plugin detection, the `KNOWN_PLUGINS`
  capability registry, and REST namespace discovery.
- `plugin_framework.py` — the `(plugin, action)` router. See "PLUGIN_READ vs
  PLUGIN_ACTION" below.
- `woocommerce_advanced.py` — WooCommerce + Stripe handler for the framework,
  including sales reports.
- `elementor.py` — Elementor handler: list/inspect templates, duplicate
  (approval-gated).
- `seo_advanced.py` — Yoast/Rank Math handler: audits, bulk meta, schema.
- `security_plugins.py` — Wordfence/Sucuri/Solid handler: summaries and
  approval-gated scan triggers.
- `forms_plugins.py` — Gravity Forms/WPForms/Contact Form 7 handler: forms,
  entries, troubleshooting.
- `maintenance_plugins.py` — UpdraftPlus and LiteSpeed/WP Rocket handler:
  backup/cache status and approval-gated triggers.
- `acf.py` — Advanced Custom Fields handler: read-only field-group summaries.
- `seo.py` — lightweight SEO title/slug/excerpt helpers.
- `web_search.py` — read-only DuckDuckGo search used by the Researcher only.

## ui/

- `app_main.py` — the chat interface: message history, agent progress, the
  approval card, quick-action prompt library, media attachments, optional voice
  transcription, memory/activity/observability/export sidebar panels, and the
  undo button.
- `setup_page.py` — first-run screen that installs `requirements.txt`, verifies
  imports, and runs readiness tests (`environment_setup.py`).
- `site_select.py` — saved-site picker and the add-site form with connection
  testing.
- `settings_page.py` — LLM provider/model/API-key panel with model detection
  and the agentic-mode toggle.
- `theme.py` — brand constants (product name, page title/icon) and the CSS
  theme with hero, sidebar brand, and footer renderers.

Root files: `launch.py` (starts the UI with only Python installed), `app.py`
(Streamlit entrypoint, relaunches through `launch.py` when run directly),
`environment_setup.py` (dependency install and readiness checks), `health.py`
(FastAPI `/health` and `/ready` for containers).

## SafetyLayer vs LLMRouter

Safety is not the model.

`SafetyLayer.evaluate()` (`core/safety.py`) is deterministic Python. It decides
three things for a plan:

- **Confirmation.** Any operation in `CONFIRMATION_OPERATIONS` (update/delete
  posts and pages, settings, theme, plugin, SEO, bulk products, undo,
  WooCommerce writes, Stripe refunds, bulk SEO, plugin actions) needs explicit
  approval. So does any create with `status: "publish"`, and any plan whose
  highest risk is medium or high. Draft creation is low risk and runs without
  extra approval.
- **Backup.** Operations in `BACKUP_OPERATIONS` snapshot first. The snapshot
  reads the current resource over REST; if it cannot be read, the backup file
  records the error instead of the resource.
- **Permission.** Three layers: the app role (`viewer`/`editor`/`admin` per
  `APP_ROLE_REQUIREMENTS`), the WordPress roles from `users/me`
  (`ROLE_REQUIREMENTS`), and WordPress capabilities when exposed
  (`CAPABILITY_REQUIREMENTS`, e.g. `edit_posts`, `manage_woocommerce`). Any
  miss blocks the plan before anything executes.

`LLMRouter` only proposes. The Planner's model output is validated into
known operations. The Executor's LLM pre-flight can flag a concern but cannot
skip a gate. With no API key, the rule-based planner and template copy take
over and the same safety rules apply unchanged.

## PLUGIN_READ vs PLUGIN_ACTION

The plugin framework (`tools/plugin_framework.py`) routes a `(plugin, action)`
pair to a registered `PluginHandler`. Each handler declares its `read_actions`
and `write_actions`, detects whether its plugin is active (via `/wp/v2/plugins`
and REST namespace discovery), and degrades gracefully — when a plugin exposes
no public REST route, the handler returns a summary plus the exact dashboard
steps instead of failing.

- `PLUGIN_READ` is always read-only. App role `viewer` is enough, and the
  WordPress-side check accepts any read-capable role.
- `PLUGIN_ACTION` is for changes and triggers. It is confirmation-gated,
  backup-gated, requires the `editor` app role, and requires an administrator,
  editor, or shop_manager WordPress role.

Bundled handlers: `woocommerce`, `elementor`, `seo`, `security`, `forms`,
`maintenance`, `acf`. Adding a plugin means writing one handler and registering
it in `build_default_handlers`; the operation enum, safety layer, and agent
graph do not change.

## What .gitignore keeps out

Runtime and secret material never enters git:

- `.env` and `.env.*` — local environment and API keys.
- `.streamlit/secrets.toml` — Streamlit secrets.
- `config/users.yml` — real login users and password hashes
  (`config/users.example.yml` is tracked).
- `data/*.enc` — Fernet-encrypted vaults (credentials, sites, LLM settings).
- `data/site_memory.json` — long-term memory.
- `data/audit_log.jsonl`, `data/activity_log.jsonl`,
  `data/usage_analytics.jsonl` — logs.
- `data/backups/`, `data/uploads/` — JSON snapshots and attached media.
- `data/.environment_ready.json` — first-run marker containing the local Python
  path.
- `logs/` — file logs.
- `.venv/`, `venv/`, `__pycache__/`, and tool caches.
