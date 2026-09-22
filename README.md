# TypeSafe Conversation for Home Assistant

A Home Assistant conversation agent that decides with a
[TypeSafe System One](https://docs.typesafe.ai) model instead of an LLM.

A System One model returns typed, calibrated judgements rather than text. Jev is
the one available today and the default; the integration is not written around
it, so a later model is a config change. Every device command and state query is resolved from those judgements in
ordinary Python, in a single API call, with a median round trip of ~250 ms. An
LLM is used for exactly two things — splitting a request that contains several
commands, and answering a general question — so the slow path is only taken when
something actually has to be written in prose.

Measured against the same utterances, this replaces a 2–8 s LLM turn with a
~250 ms one for anything that controls or inspects the home.

## How it works

```
utterance
   │   Home Assistant's own sentence matcher has already taken the phrasings
   │   it recognises; what reaches us is the fuzzy and the compound.
   ▼
entity catalog  ── cached, rebuilt on registry and exposure changes
   ▼
ONE System One call:  state    = the request + every exposed entity and area
               questions = 8 fixed + 3 built from the catalog
                         + 1 per device kind present + a few conditional ones
   ▼
route()  ── a pure function over the answers
   ├─ command   → intent.async_handle → your services
   ├─ query     → sentence matcher, then HassGetState / HassClimateGetTemperature
   ├─ compound  → LLM splits it → N parallel Jev calls → run in order
   ├─ general   → LLM answers in prose
   └─ unsure    → sentence matcher → LLM → "I'm not sure"
```

### Speculative fan-out

Every question the router might need is asked up front, including the branches
that will turn out to be irrelevant. Measured against `jev-1.13.0`, an extra
question costs about **97 input tokens** and almost no extra latency, while a
second round trip would cost ~250 ms. So we ask "what should happen to the
locks?" on every single request and simply do not read the answer unless the
request turned out to be about locks.

A confident but wrong answer on a branch nobody reads is free. That is the
contract, and there is a test that holds it: `is everything locked up` returns
`action_lock = lock` at 0.99 confidence, and the router correctly treats the
utterance as a question and locks nothing.

### Two axes, not one

A branch is acted on only when **confidence** clears its bar *and* the
**margin** between the top two options is at least 0.15. They fail differently:
a distribution can look confident overall while the top two options remain
effectively tied.

| confidence | behaviour |
| --- | --- |
| ≥ 0.75 | act, brief acknowledgement |
| 0.50–0.75 | act, but name the target out loud, so a wrong guess is correctable |
| 0.30–0.50 (target only) | ask which device was meant |
| below, or a near-tie | hand back to the sentence matcher, then the LLM |

Clarification is only ever used for an ambiguous *target*. "Which lamp?" is a
short, natural question. "Did you want to turn something on?" is not, so a weak
category or action goes to the fallback ladder instead.

### Values

Jev cannot emit text, so it never invents a number. Code finds the candidate
spans in the utterance and Jev picks which one the user meant:

- `"set the living room lights to 30%"` → candidates `["30%"]` → picked → `brightness_pct: 30`
- `"close the blinds halfway"` → `halfway` → `position: 50`
- `"turn the volume down a bit"` → no value in the text, so a direction and a
  magnitude question → `volume_step: -10`
- `"make the lights a bit warmer"` → a closed set of colour names → `2700 K`

Temperature units come from the entity, or failing that from your Home
Assistant configuration. They are never asked of the model.

## Install

Copy `custom_components/typesafe_conversation` into your Home Assistant `config`
directory, restart, then **Settings → Devices & Services → Add Integration →
TypeSafe Conversation**. You will need an API key from
[console.typesafe.ai](https://console.typesafe.ai/).

The LLM step is optional. Without it the agent still handles every command and
query; it just cannot split compound requests or answer general questions.
Ollama and any OpenAI-compatible endpoint (OpenRouter, vLLM, …) are supported.

Then set it as the conversation agent under **Settings → Voice assistants**.

### Leave "prefer handling commands locally" on

The agent advertises `ConversationEntityFeature.CONTROL`. With prefer-local on,
Home Assistant's sentence matcher keeps every phrasing it recognises, and only
`HassGetState`, `HassMediaSearchAndPlay` and everything it *failed* to parse
reach this agent. That is deliberate: there is no point spending an API call
beating hassil at "turn off the kitchen lights", and it concentrates the traffic
on the requests where Jev earns its keep. Set `bypass_local_intents` if you want
to compare the two regimes.

## Development

```sh
python3.14 -m venv .venv        # Home Assistant 2026.7 requires Python 3.14
.venv/bin/pip install "pytest-homeassistant-custom-component==0.13.348" syrupy
.venv/bin/python -m pytest
```

`0.13.348` pins `homeassistant==2026.7.4` exactly; a mismatch produces confusing
import errors.

The routing tests replay **real recorded Jev responses** from
`tests/fixtures/answers/`, so they describe how the model actually behaves
rather than how we imagine it does. To re-record them, or to re-fit the
thresholds in `const.py` against your own home:

```sh
set -a; . ./.env; set +a
.venv/bin/python scripts/calibrate.py --csv out.csv --record tests/fixtures/answers
```

Every threshold is a named constant in `const.py` precisely so it can be
re-fitted rather than argued about.

To pull your own home's catalog and calibrate against it:

```sh
.venv/bin/python scripts/pull_home.py --out my_home.json
.venv/bin/python scripts/calibrate.py --home my_home.json \
    --cases tests/fixtures/scripted_cases.json --area <your-area-id>
```

`pull_home.py` reads exposure settings over the WebSocket API (they are not in
the REST API) and redacts latitude/longitude by default, because weather
integrations name entities after the station's coordinates.

A catalog pulled from a live instance describes the home it came from — room
layout, device brands, which security devices exist — so keep it out of version
control. `.gitignore` already excludes `tests/fixtures/real_*` and
`docs/calibration-real-*.csv` for that reason. The fixtures that ship with this
repo are synthetic.

### A note on confidence

Jev derives confidence as `(n * p_top - 1) / (n - 1)`, so the *same* probability
scores differently depending on how many options the question offered. A
two-option Choice at p=0.66 scores 0.31; a seven-option Choice at the same
probability scores 0.60. The action gate therefore thresholds the chosen
option's **probability**, not its confidence — thresholding confidence made
every script command fall back on a script-heavy home.

## Cost

Input tokens only, at $42/Btok. A 29-entity home costs ~6.5k tokens per request,
about **$0.00027**. State is billed once per request, not once per question —
verified, which is what makes the fan-out cheap.
