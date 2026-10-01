# OpenRouter fork: implementation status

This is a fork of `the-sof/home-assistant-typesafe-conversation-agent`, not a
replacement conversation engine written from scratch. The original MIT license,
pure decision router, entity exposure checks, HA intents and upstream tests remain.

## Implemented

- OpenRouter's TypeSafe-compatible `POST /api/v1/systemone` transport, using
  `typesafe/jev-1.13`. This is **not** chat completions or `jev-router`.
- Authenticated, non-generative key validation through `GET /api/v1/key`.
- One attempt per OpenRouter decision request; no duplicate billed retries,
  router aliases, automatic model substitution or HTTP redirects.
- YAML-managed entry; credentials can reference the existing HA `open_router`
  integration and are resolved only in memory. No second persisted key copy.
- Explicit language-model roles: `llm_split_model` splits compound commands;
  `llm_model` answers general questions. Simple commands still use Jev only.
- New entries start with device execution disabled. Preview does not execute
  commands, cancel timers, use the local command fallback, or execute compound
  subcommands. Decisions appear in downloadable integration diagnostics.
- A compound request is fully resolved before any step executes. Invalid or
  oversized split arrays are rejected, not truncated. Execution stops at the
  first failed step and reports partial success; physical rollback is not claimed.
- Relative brightness reads actual entity state. An off light starts from zero;
  missing brightness no longer invents a 50% baseline. Relative room-wide changes
  require a household helper and are not yet supported by this executor.
- Provider error bodies are not included in exceptions. Jev-reported cost is
  preserved in diagnostics; missing cost remains unknown, not zero.

## Example YAML (synthetic, not a real home)

```yaml
typesafe_conversation:
  name: Home Decision Test
  provider: openrouter
  model: typesafe/jev-1.13
  openrouter_entry_id: REPLACE_WITH_EXISTING_OPEN_ROUTER_ENTRY_ID
  execution_enabled: false
  always_confirm_risky: true
  llm_backend: openai_compatible
  llm_base_url: https://openrouter.ai/api
  llm_split_model: openai/gpt-4o-mini
  llm_model: openai/gpt-4o
  llm_timeout: 30
```

Alternatively omit `openrouter_entry_id` and use `api_key: !secret openrouter_key`.
Do not set both. The LLM reuses the decision key only when its base URL is the
exact OpenRouter endpoint above; other endpoints require their own credentials.

Restart HA after changing YAML. Re-import updates the same entry identified by
its name; keep that name stable. Subentry UI reconfiguration is blocked for a
YAML-managed entry. Removing YAML does not remove the imported HA entry: explicitly
remove the integration if retiring it.

**For a test Assist pipeline, turn off “Prefer handling commands locally”.**
HA's pipeline can execute a local sentence before it reaches any conversation
agent. An agent's preview switch cannot guard a command that bypasses it entirely.
Prefer direct `conversation.process` calls addressed to the test agent.

## Migration gates — NOT completed

This branch is not yet a drop-in replacement for an existing household cascade.
Do not select it as the main assistant until these gates pass:

1. Add a declarative YAML capability registry for typed script fields, room
   primary-light defaults and explicit secondary-light targeting.
2. Connect household Music Assistant preparation, receiver power and safe relative
   volume. Generic native HA media intents alone are not proof these workflows work.
3. Bring over persistent local daily/monthly/per-request budget gates without
   resetting already-spent balances. Cost reporting is not a budget limiter.
   Until then, enforce a spending cap on a dedicated OpenRouter key and avoid paid
   live testing on an unrestricted shared key.
4. Route live-data questions through an explicit search capability. Freeform
   answers currently have no internet access and must not invent current weather.
5. Add durable scheduled jobs and ordered vacuum-room completion handling;
   do not interpret “in ten minutes” as permission to start immediately.
6. Run synthetic Russian phrasing and compound-plan regression tests, then
   no-actuation previews against the household catalog before switching the UI.

The current household integration has not been changed or replaced by this branch.
Do not commit real entity IDs, catalogs, keys or trace captures.

Reference: [OpenRouter System One protocol](https://openrouter.ai/docs/guides/community/typesafe-sdk).
