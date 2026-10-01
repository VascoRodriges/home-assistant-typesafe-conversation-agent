"""Closed-set JEV decisions compiled locally; no generated services or numbers."""

import re

from .extraction import media_chunks
from .household_policy import PlanError, PolicyError, probability

NUMBER_WORDS = {
    "ноль": 0,
    "нуль": 0,
    "один": 1,
    "одна": 1,
    "одну": 1,
    "два": 2,
    "две": 2,
    "три": 3,
    "четыре": 4,
    "пять": 5,
    "шесть": 6,
    "семь": 7,
    "восемь": 8,
    "девять": 9,
    "десять": 10,
    "одиннадцать": 11,
    "двенадцать": 12,
    "тринадцать": 13,
    "четырнадцать": 14,
    "пятнадцать": 15,
    "шестнадцать": 16,
    "семнадцать": 17,
    "восемнадцать": 18,
    "девятнадцать": 19,
    "двадцать": 20,
    "тридцать": 30,
    "сорок": 40,
    "пятьдесят": 50,
    "шестьдесят": 60,
    "семьдесят": 70,
    "восемьдесят": 80,
    "девяносто": 90,
    "сто": 100,
}


def number_candidates(text):
    """Copy literal spans, never estimate. Jev decides which field a span belongs to."""
    found = {}
    for match in re.finditer(r"(?<![\w.])-?\d+(?:[.,]\d+)?(?![\w.])", text):
        value = float(match[0].replace(",", "."))
        if value.is_integer() and 0 <= value <= 1440:
            found[match[0]] = int(value)
    words = list(re.finditer(r"[а-яё]+", text.lower()))
    index = 0
    while index < len(words):
        word = words[index][0]
        if word not in NUMBER_WORDS:
            index += 1
            continue
        value = NUMBER_WORDS[word]
        end = index
        if value >= 20 and value % 10 == 0 and index + 1 < len(words):
            next_word = words[index + 1][0]
            if next_word in NUMBER_WORDS and 0 < NUMBER_WORDS[next_word] < 10:
                value += NUMBER_WORDS[next_word]
                end += 1
        found[text[words[index].start() : words[end].end()]] = value
        index = end + 1
    if re.search(r"половин|наполовину|half", text, re.I):
        found["половина / half"] = 50
    return {
        f"value_{i}": {"span": span, "value": value}
        for i, (span, value) in enumerate(found.items())
    }


def choice(instructions, criteria):
    return {"type": "choice", "instructions": instructions, "criteria": criteria}


def read_choice(answer, criteria):
    if (
        not isinstance(answer, dict)
        or answer.get("type") != "choice"
        or answer.get("choice") not in criteria
    ):
        raise PolicyError("Invalid closed-set decision")
    distribution = answer.get("probabilities", {})
    if set(distribution) != set(criteria):
        raise PolicyError("Incomplete closed-set distribution")
    values = sorted(
        (probability({"type": "noul", "noul": p}) for p in distribution.values()),
        reverse=True,
    )
    confidence = probability({"type": "noul", "noul": answer.get("confidence")})
    return (
        answer["choice"],
        confidence,
        values[0] - (values[1] if len(values) > 1 else 0),
    )


class FastDecisions:
    def __init__(self, catalog, config, text):
        self.catalog, self.config = catalog, config
        self.numbers = number_candidates(text)
        self.music_chunks = {
            f"chunk_{index}": chunk for index, chunk in enumerate(media_chunks(text))
        }
        labels = catalog.labels.get("voice_llm_room_lights", {})
        rooms, fixtures = labels.get("room_names", {}), labels.get("fixture_names", {})
        self.targets = {
            key: f"{rooms.get(key.split(':')[0], key.split(':')[0])}: {fixtures.get(key.split(':')[1], key.split(':')[1])}"
            for key in catalog.lights
        }
        self.targets["unknown"] = "The user did not identify a resolvable light target."
        self.players = catalog.labels.get("voice_llm_play_music", {}).get(
            "player_names", {}
        )
        self.questions = {
            "shape": choice(
                "Can the ENTIRE latest request be handled as one immediate device operation or one sensor reading? Do not silently discard clauses.",
                {
                    "simple": "One immediate target and one operation; several settings of the SAME light are one operation. A literal track/artist search is one operation. No exclusions, future time or other tasks.",
                    "complex": "Several devices/tasks, exclusions, future/conditional actions, music mood/curation needing reformulation, or complex references.",
                    "unknown": "Cannot determine the entire request.",
                },
            ),
            "intent": choice(
                "Select the intent of the latest request, NOT a device mentioned in a hypothetical or question.",
                {
                    "light": "Operate one light or one lighting group.",
                    "all_lights_off": "Turn off all ordinary lights in the whole house, without exceptions.",
                    "receiver_power": "Explicitly turn the receiver on/off; NOT merely play music on it.",
                    "receiver_volume": "Adjust receiver volume.",
                    "playback": "Pause, resume, stop or skip music on a player.",
                    "sensor": "Read one current household temperature or humidity measurement.",
                    "play_music": "Search and play one explicitly named track/artist/album/playlist.",
                    "other": "Vacuum, music recommendation, general question, or unsupported task.",
                },
            ),
            "light_target": choice(
                "Which light target is requested? Room light/brighter means MAIN only; monitor/desk/bed/all require explicit scope. Resolve names and synonyms from household_preferences and target labels.",
                self.targets,
            ),
            "light_action": choice(
                "If operating lights, what is the requested power action?",
                {
                    "on": "Turn on.",
                    "off": "Turn off.",
                    "toggle": "Explicitly toggle.",
                    "adjust": "Change brightness/color without an explicit on/off instruction.",
                    "unknown": "No lighting operation requested.",
                },
            ),
            "brightness": choice(
                "What change to brightness is actually requested?",
                {
                    "unchanged": "No brightness change requested.",
                    "increase": "Brighter relative to current.",
                    "decrease": "Dimmer relative to current.",
                    "set": "Set an explicitly specified absolute brightness.",
                    "unknown": "Unclear.",
                },
            ),
            "color": choice(
                "Which color profile is requested? Choose unchanged if no color change is requested.",
                {
                    "unchanged": "No color change.",
                    "warm": "Warmer / warm white.",
                    "neutral": "Neutral white.",
                    "cool": "Cooler / cold white.",
                    "red": "Red.",
                    "green": "Green.",
                    "blue": "Blue.",
                    "relax": "Relaxing shade.",
                    "night": "Night shade.",
                    "reading": "Reading light.",
                    "focus": "Focus light.",
                    "unknown": "Other/unclear color.",
                },
            ),
            "power": choice(
                "If receiver power is explicitly requested, select it. A request to play music is NOT an explicit power command.",
                {
                    "on": "Turn receiver on.",
                    "off": "Turn receiver off.",
                    "unknown": "Not specified.",
                },
            ),
            "volume": choice(
                "What receiver volume change is requested?",
                {
                    "increase": "Louder relative to current.",
                    "decrease": "Quieter relative to current.",
                    "set": "Explicit absolute volume.",
                    "unknown": "Not specified.",
                },
            ),
            "player": choice(
                "Which playback destination? Room music means Chromecast; receiver and Alice only when named.",
                {**self.players, "unknown": "No resolvable playback destination."},
            ),
            "playback": choice(
                "Which music transport action?",
                {
                    "pause": "Pause.",
                    "play": "Resume existing music, no search.",
                    "stop": "Stop music.",
                    "next": "Next track.",
                    "previous": "Previous track.",
                    "unknown": "Not transport / unclear.",
                },
            ),
            "sensor": choice(
                "Which exact household sensor answers the requested measurement? Room temperature uses room sensor, not clock unless explicitly requested.",
                {
                    **{
                        key: value
                        for key, value in config["read_entities"].items()
                        if key.startswith("sensor.")
                    },
                    "unknown": "No exact sensor / multiple readings.",
                },
            ),
            "amount": choice(
                "Which literal number span gives the requested brightness or volume percentage, or relative step? Do NOT choose a number from an artist/device name.",
                {
                    **{key: value["span"] for key, value in self.numbers.items()},
                    "none": "No explicit numerical adjustment.",
                },
            ),
            "music_query": choice(
                "Which literal fragment is the COMPLETE track/artist/album search? Exclude destinations and control verbs. Never select a partial title.",
                {
                    **self.music_chunks,
                    "unknown": "No complete literal query; needs reformulation or clarification.",
                },
            ),
            "media_kind": choice(
                "Which media kind is explicitly requested? Auto if unspecified.",
                {
                    "auto": "Unspecified.",
                    "track": "Song/track.",
                    "album": "Album.",
                    "artist": "Artist/group.",
                    "playlist": "Named playlist.",
                    "radio": "Radio.",
                    "unknown": "Unclear.",
                },
            ),
        }

    def compile(self, answers, request, groups, snapshot):  # noqa: C901 - typed decision compiler
        """Return a canonical checked plan, or a reason to take the flexible branch."""

        def pick(name):
            result, confidence, margin = read_choice(
                answers.get(name), self.questions[name]["criteria"]
            )
            threshold = (
                self.config.get("fast_read_threshold", 0.85)
                if name == "sensor"
                else self.config.get("fast_control_threshold", 0.90)
            )
            if confidence < threshold or margin < self.config.get("fast_margin", 0.15):
                raise PlanError("low_confidence:" + name)
            if result == "unknown":
                raise PlanError("unresolved:" + name)
            return result

        plan = {
            "operations": [],
            "preserve": [],
            "home_answer": None,
            "home_evidence": [],
            "external_questions": [],
            "clarification": None,
        }
        try:
            route = request["decisions"]["route"]
            _, confidence, margin = read_choice(route, route["probabilities"])
            if confidence < self.config.get(
                "fast_control_threshold", 0.90
            ) or margin < self.config.get("fast_margin", 0.15):
                raise PlanError("low_confidence:route")
            if pick("shape") != "simple":
                raise PlanError("complex_request")
            intent = pick("intent")
            timing = request["timing"]
            if timing.get("confidence", 0) < self.config.get(
                "fast_control_threshold", 0.90
            ):
                raise PlanError("low_confidence:timing")
            if intent == "sensor":
                if route["choice"] != "home_query" or timing["choice"] != "no_control":
                    raise PlanError("not_readonly_sensor")
                entity = pick("sensor")
                state = snapshot.get(entity)
                if not state:
                    raise PlanError("sensor_unavailable")
                value = state["state"]
                label = self.config["read_entities"][entity]
                plan["home_answer"] = (
                    f"{label}: данные недоступны."
                    if value in ("unknown", "unavailable")
                    else f"{label}: {value} {state['attributes'].get('unit_of_measurement', '')}.".strip()
                )
                plan["home_evidence"] = [entity]
            else:
                if route["choice"] != "home_control" or timing["choice"] != "now":
                    raise PlanError("not_immediate_control")
                name, args = None, {}
                if intent == "light":
                    target = pick("light_target")
                    room, fixture = target.split(":")
                    args = {
                        "room": room,
                        "fixture": fixture,
                        "light_action": pick("light_action"),
                        "brightness_action": pick("brightness"),
                        "color_profile": pick("color"),
                    }
                    amount = pick("amount")
                    if args["brightness_action"] == "set":
                        if amount == "none":
                            raise PlanError("missing_brightness")
                        args["brightness_percent"] = self.numbers[amount]["value"]
                    elif (
                        args["brightness_action"] in ("increase", "decrease")
                        and amount != "none"
                    ):
                        args["brightness_step_percent"] = self.numbers[amount]["value"]
                    elif amount != "none":
                        raise PlanError("unmapped_number")
                    name = "voice_llm_room_lights"
                elif intent == "all_lights_off":
                    name = "voice_llm_all_lights_off"
                elif intent == "receiver_power":
                    name, args = (
                        "voice_llm_receiver_power",
                        {"power_action": pick("power")},
                    )
                elif intent == "receiver_volume":
                    name, args = (
                        "voice_llm_receiver_volume",
                        {"volume_action": pick("volume")},
                    )
                    amount = pick("amount")
                    if args["volume_action"] == "set":
                        if amount == "none":
                            raise PlanError("missing_volume")
                        args["volume_percent"] = self.numbers[amount]["value"]
                    elif amount != "none":
                        args["step_percent"] = self.numbers[amount]["value"]
                elif intent == "playback":
                    name, args = (
                        "voice_llm_music_control",
                        {
                            "destination": pick("player"),
                            "playback_action": pick("playback"),
                        },
                    )
                elif intent == "play_music":
                    if pick("amount") != "none":
                        raise PlanError("music_with_numeric_setting")
                    name, args = (
                        "voice_llm_play_music",
                        {
                            "destination": pick("player"),
                            "query": self.music_chunks[pick("music_query")],
                            "media_type": pick("media_kind"),
                        },
                    )
                else:
                    raise PlanError("requires_generation")
                fields = self.catalog.tools[name]["arguments"]["properties"]
                plan["operations"] = [
                    {
                        "capability": name,
                        "arguments": {key: args.get(key) for key in fields},
                    }
                ]
            return self.catalog.checked_plan(
                plan, groups, snapshot
            ), "closed_set_complete"
        except (PlanError, PolicyError, KeyError) as error:
            return None, str(error)


def check_source_numbers(plan, text):
    """Absolute values/explicit steps must exist in THIS request, not a model default."""
    values = {candidate["value"] for candidate in number_candidates(text).values()}
    fields = (
        "volume_percent",
        "brightness_percent",
        "step_percent",
        "brightness_step_percent",
        "repeats",
    )
    for op in plan["operations"]:
        for field in fields:
            value = op["arguments"].get(field)
            if value is not None and value not in values:
                raise PlanError(
                    f"Unrequested numeric field: {field}. Omit it or use the explicitly requested literal value."
                )
