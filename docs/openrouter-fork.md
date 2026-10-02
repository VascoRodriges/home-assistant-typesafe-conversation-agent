# OpenRouter fork — YAML capability mode

Original author: **Sofiane Ghadab (@the-sof)**. Fork maintainer: **VascoRodriges**.
[Original MIT-licensed integration](https://github.com/the-sof/home-assistant-typesafe-conversation-agent).
The native entity catalog/router, intent executor, conversation platform and tests
are retained. The opt-in adapter reuses the upstream typed System One client and
conversation platform to bridge existing YAML scripts.

## Routing and execution

1. One `POST /api/v1/systemone` batch selects request route, domain, timing and
   closed-set candidates. Only fields relevant to that route are read.
2. Eligible single-target light, receiver, playback, literal music-search and
   sensor requests compile directly into typed arguments. Ambiguity, mood
   reformulation, exclusions, schedules and multi-step requests use the planner.
3. One explicitly selected model generates a schema-constrained plan. Local
   validation rejects unknown scripts/fields, out-of-range values, invented
   volume/brightness numbers, protected-target conflicts and unsupported timing.
4. One Jev batch selects typed alternatives for the target, action, literal
   values and room order, plus whole-request coverage. It compares the selected
   field probabilities and margins rather than relying on one opaque global
   confidence. Rejection asks for clarification; there is no replanning loop.
5. All model work and whole-chain service preflight finish before any side
   effect. Each script must return `success: true`; a failed step stops the chain.
   No retry or physical rollback is claimed.

General questions use `models.answer`, without device tools or catalog. Fresh
external questions use `models.web` with one bounded OpenRouter Exa search.
No confirmed search metadata/citations means no purported live facts.

## Synthetic YAML example

### Native Home Assistant settings window

After setup, open **Settings → Devices & services → TypeSafe Conversation →
Configure**. The native options dialog has safety, models/price ceilings, budget,
history, routing/instructions and advanced catalog sections, with Russian labels.
Each section validates the complete proposed configuration before saving and
reloads the same entry. Model price ceilings must exist for every selected model;
catalog edits also validate the file and its allowlisted script definitions.

Saved UI sections override the YAML baseline through `entry.options`. They survive
YAML re-import and restart; YAML files are never silently rewritten. **Restore
YAML** requires confirmation and removes all UI overrides, including execution
permission, without touching expense storage. This can re-enable execution if
the YAML baseline enables it; the separate execution helper must still be on.
Model changes, saving budget limits and restoring YAML never reset accumulated
costs. Existing script definitions and their field/target contracts remain YAML;
the window configures this integration, not an arbitrary script editor.

General toggles other than execution permission apply to the native agent.
Capability-mode models are always selected by its four explicit roles; no router
aliases, API-key copies or budget-reset controls are offered in the window.

Place a package similar to this in your HA configuration. Replace ONLY the
example IDs locally. Your script catalog is never part of this repository.

```yaml
typesafe_conversation:
  name: Home TypeSafe
  provider: openrouter
  model: typesafe/jev-1.13
  openrouter_entry_id: REPLACE_WITH_EXISTING_OPEN_ROUTER_ENTRY_ID
  execution_enabled: false
  local_fallback_enabled: false
  household:
    script_catalog: packages/assist/capabilities.yaml
    execution_switch: input_boolean.assist_execute
    status_sensor: sensor.typesafe_conversation_budget
    models:
      decision: typesafe/jev-1.13
      planner: openai/gpt-4o-mini
      answer: openai/gpt-4o
      web: openai/gpt-4o-mini
    prices:
      typesafe/jev-1.13:
        input_per_million: 0.042
        output_per_million: 0
      openai/gpt-4o-mini:
        input_per_million: 0.15
        output_per_million: 0.60
      openai/gpt-4o:
        input_per_million: 2.50
        output_per_million: 10
    budget:
      request_usd: 0.035
      daily_usd: 0.25
      monthly_usd: 3
    capabilities:
      voice_llm_receiver_power: media
    read_entities:
      sensor.sample_temperature: Example room temperature
    instructions: >-
      Room lighting means MAIN lighting only unless the user names a secondary
      fixture. Relative quieter/brighter uses the script's safe default step.
      Leave unchanged means do not modify that fixture.

input_boolean:
  assist_execute:
    name: Allow Assist device commands
    initial: false
```

Price figures are example configured ceilings, **not automatic price discovery**.
Check current provider pricing before using them. A missing role price blocks
initialization. No router alias, model fallback or alternate role is selected
behind your back. Planner/answer alternatives currently accepted by the adapter
are `google/gemini-3.1-flash-lite` and `openai/gpt-5-mini`; configure their
explicit ceilings before selection. Batch models are unsuitable for live Assist.

Restart HA after YAML changes. Re-import replaces the same entry identified by
name; keep it stable. Imported entries cannot be reconfigured through subentry
UI. Removing YAML alone does not retire the imported entry: disable/remove it.

## Script catalog contract

The file contains a `script:` mapping. Only names explicitly listed in
`capabilities` are callable, classified as `light`, `media` or `vacuum`.
Fields use HA `select`, `number` or `text` selectors; unknown selector types
fail closed. Optional fields are nullable during planning and omitted before
calling scripts. Use script descriptions for semantics, aliases and limitations.
Declare optional safe numeric defaults using each field's `default`. An
unrequested relative step/repeat equal to that local default is omitted before
execution; any other invented value is rejected. Absolute volume/brightness
never gets this exception.

A minimal synthetic catalog (adapt its device ID locally):

```yaml
script:
  voice_llm_receiver_power:
    description: Explicitly switch the example receiver on or off.
    fields:
      power_action:
        required: true
        selector:
          select:
            options: ["on", "off"]
    sequence:
      - action: "media_player.turn_{{ power_action }}"
        target:
          entity_id: media_player.sample_receiver
      - variables:
          result:
            success: true
            message: "Receiver power command accepted by HA."
      - stop: "{{ result.message }}"
        response_variable: result
```

The optimized household profile recognizes these conventional capability names:
`voice_llm_room_lights`, `voice_llm_all_lights_off`,
`voice_llm_play_music`, `voice_llm_music_control`,
`voice_llm_receiver_volume`, `voice_llm_receiver_power`,
`voice_llm_kids_tv_control`, `voice_llm_vacuum_rooms` and
`voice_llm_vacuum_control`. Their field selectors remain the source of truth.

For light scopes, a script's top-level sequence variables supply
`lighting_targets` (`room:fixture` -> entity list), `room_names` and
`fixture_names`. For music, use `players` (destination -> entity) and
`player_names`. Duplicate aliases may point to the same entities; exclusions
are checked by underlying IDs, not just by the alias string. Unknown profiles
can use the bounded planner path; they do not get a guessed fast-path mapping.

Music Assistant integration and playable providers must already be configured.
Scripts perform receiver preparation, search/play and volume commands through
HA/MA services; the model does not send raw DLNA/UPnP requests. Mood search is a
search request, not a guarantee a provider has a matching playlist or track.

The cleaning script persists room order, delay and repeats in HA helpers and
returns immediately. The HA automation owns later execution. The adapter does
not sleep for ten minutes or assume a model will remember a job. Ordered segment
submission is **not proof of strict room-by-room completion on every vacuum**.
Support depends on your vacuum integration; do not claim a guarantee without
checking that integration. Delayed light/media actions are rejected rather than
executed immediately.

## Budget, preview and privacy

### Read-only historical sensor questions

Enable this optional section under `household` (off by default):

```yaml
history:
  enabled: true
  max_days: 7
  max_age_minutes: 120
```

Only `sensor.*` IDs in `read_entities` are readable, subject to the initiating
user's HA read permissions. Uses Recorder's worker, not model-generated SQL.
Recorder must already record those sensors; this integration never changes
retention, exclusions or the database. No raw history is sent to cloud models.

One Jev batch handles confident named periods and statistics; uncertain/custom
dates use a small planner schema plus typed Jev review. Queries resolve dialogue
references without replaying earlier tasks. Current-state fast paths cannot
substitute today's reading for a past question. This branch never calls scripts.
Mixed history/action requests ask to split the messages, rather than silently
dropping an action or changing a device during a history lookup.

Point queries use the last recorded state at or before the requested instant,
within `max_age_minutes`, never a future/nearest sample. Replies show the source
timestamp. Range statistics use time-weighted averages and report valid coverage;
unknown/nonfinite states and stale gaps are not zero or interpolated. A bare
“night” means the last completed 00:00–08:00 in HA's configured time zone, and
the interval is shown in the response. Mixed units are refused.

Windows are bounded by `max_days` (1–31; default 7), a 30,000-record guard and
a 20-second async timeout. This uses retained raw states only, not long-term
hourly statistics. Recorder workers already running may finish after timeout.
Unavailable/old data gets an explicit answer, not guessed history. Generic
dates must be offset-aware; future/out-of-window intervals are rejected locally.

Examples: “what was that temperature yesterday at this time?”, “minimum in the
study last night”, “average humidity yesterday”, “compare with yesterday at
this time”. Multi-sensor comparisons are not yet supported.

Every capability-mode model call reserves a conservative cost ceiling in
`.storage/typesafe_conversation.<entry_id>.budget` before sending. UTC daily
and monthly windows survive restart. Missing usage/timeout retains the
reservation; unexpected cost above it blocks further calls and actuation.
This is a conservative local admission policy, not a guarantee against an
upstream billing error. Do not edit or delete the ledger to clear a limit.

When migrating the former local cascade, set `migrate_cascade_budget: true`
and disable its config entry BEFORE enabling this integration. On first setup
the adapter loads its `home_cascade.budget` balances; a missing previous ledger
or an active previous writer blocks migration instead of resetting spend.

`typesafe_conversation.preview` accepts `text` and optional `entry_id`.
It is billed within the SAME budget but never actuates, regardless of switches.
An optional `candidate_model` compares an explicitly priced candidate planner
ONLY in preview; it cannot change the executing model or decision/answer roles.
`status` is local/read-only. `reload_catalog` atomically refreshes the catalog;
reload HA scripts separately after script implementation changes.
`reload_yaml` is admin-only: it imports the existing YAML agent's merged
package settings without restarting HA. Keep its `name` unchanged. The old
runtime stops accepting turns and drains its budget writer before replacement;
persisted spend is retained. This does not reload code or HA scripts.

Execution requires BOTH `execution_enabled: true` and the configured helper
switch being on. Set that helper's `initial: false` for safety after restart.
The status sensor carries models, budget, last path/calls and the full local
reply for a text dashboard. Treat sensor attributes and traces as private.

**Turn off “Prefer handling commands locally” in the Assist pipeline.** Local
sentence handling can act before a conversation agent sees a request and is
not covered by this adapter's preview/budget switches. A direct
`conversation.process` call must name the intended agent.

Keys are resolved from an existing `open_router` entry only in memory. HTTP
error bodies are not logged. Public examples and tests are synthetic; never
commit home catalogs, entity IDs, credentials or private trace captures.

## Native mode

Omit `household` to use the upstream native-intent router. Optional
`llm_split_model` and `llm_model` then handle splitting and prose respectively.
The household script adapter and its persistent budget are NOT active in native
mode. Native preview still disables actuation; exposed-entity checks remain.

References: [System One protocol](https://openrouter.ai/docs/guides/community/typesafe-sdk),
[server-side web search](https://openrouter.ai/docs/guides/features/server-tools/web-search).
