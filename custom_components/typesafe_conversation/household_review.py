"""Pure field-level decision review; no opaque score for a whole JSON blob."""

from itertools import permutations

from .household_fast import choice, number_candidates, read_choice
from .household_policy import PlanError


class TypedReview:
    """Offer actual alternatives for each generated field, plus whole coverage."""

    def __init__(self, catalog, plan, text):  # noqa: C901 - schema-driven question builder
        self.questions = {}
        self.expected = {}
        self.scopes = {}
        numbers = number_candidates(text)
        for index, operation in enumerate(plan["operations"]):
            name, args = operation["capability"], operation["arguments"]
            prefix = f"operation_{index}"
            self.scopes[prefix] = catalog.describe(operation)
            alternatives = {
                key: tool["description"]
                for key, tool in catalog.tools.items()
                if tool["group"] == catalog.tools[name]["group"]
            }
            self.add(
                prefix + "_capability",
                name,
                alternatives,
                "Which capability fulfills THIS operation in the latest request? "
                "Do not approve preparatory power commands that a music script already owns.",
            )
            if name == "voice_llm_room_lights":
                criteria, canonical = {}, {}
                by_entities = {}
                labels = catalog.labels.get(name, {})
                for key in sorted(
                    catalog.lights, key=lambda key: (key.split(":")[1] != "main", key)
                ):
                    signature = tuple(sorted(catalog.lights[key]))
                    representative = by_entities.setdefault(signature, key)
                    canonical[key] = representative
                    room, fixture = key.split(":")
                    label = f"{labels.get('room_names', {}).get(room, room)} / {labels.get('fixture_names', {}).get(fixture, fixture)}"
                    criteria[representative] = (
                        criteria.get(representative, "") + " " + label
                    )
                target = args["room"] + ":" + args.get("fixture", "main")
                self.add(
                    prefix + "_target",
                    canonical[target],
                    criteria,
                    "Which EXACT light scope is requested for THIS operation? Room defaults "
                    "to MAIN only; secondary lights need explicit scope. Apply household_preferences.",
                )
            for field, value in args.items():
                if name == "voice_llm_room_lights" and field in ("room", "fixture"):
                    continue
                if value == "unchanged":
                    continue  # Not an actuation parameter; coverage catches missing changes.
                schema = catalog.tools[name]["arguments"]["properties"][field]
                schema = schema.get("anyOf", [schema])[0]
                description = schema.get("description", field)
                key = prefix + "_" + field
                if schema["type"] == "string" and "enum" in schema:
                    labels = (
                        catalog.labels.get(name, {}).get("player_names", {})
                        if field == "destination"
                        else {}
                    )
                    self.add(
                        key,
                        value,
                        {
                            option: labels.get(option, option)
                            for option in schema["enum"]
                        },
                        f"For THIS operation, choose the requested {field}: {description}. "
                        "Use latest request and household preferences, not the generated value as authority.",
                    )
                elif schema["type"] == "array" and "enum" in schema["items"]:
                    options = schema["items"]["enum"]
                    if len(options) > 5:
                        raise PlanError(
                            "Ordered selector is too large for bounded review"
                        )
                    labels = catalog.labels.get(name, {}).get("room_names", {})
                    # Every legal ordered subset, not only the planner's proposed set.
                    criteria, wanted = {}, None
                    for count in range(1, len(options) + 1):
                        for values in permutations(options, count):
                            option = f"order_{len(criteria)}"
                            criteria[option] = " -> ".join(
                                labels.get(item, item) for item in values
                            )
                            if list(values) == value:
                                wanted = option
                    self.add(
                        key,
                        wanted,
                        criteria,
                        "Select ALL requested rooms in exact requested order. "
                        "Do not confuse segment submission order with guaranteed serial completion.",
                    )
                elif schema["type"] in ("integer", "number"):
                    criteria = {
                        option: candidate["span"]
                        for option, candidate in numbers.items()
                    }
                    wanted = next(
                        (
                            option
                            for option, candidate in numbers.items()
                            if candidate["value"] == value
                        ),
                        None,
                    )
                    if wanted is None:
                        raise PlanError(
                            "Generated number has no literal source: " + field
                        )
                    self.add(
                        key,
                        wanted,
                        criteria,
                        f"Which literal number specifies THIS {field}: {description}? "
                        "Ignore numbers belonging to another action, title or device name.",
                    )
                elif schema["type"] == "string":
                    self.add(
                        key,
                        "faithful",
                        {
                            "faithful": "The text preserves the requested literal title/artist or faithfully reformulates its requested mood for search.",
                            "different": "The text changes the requested title/artist/mood or invents unrequested constraints.",
                        },
                        f"Is the proposed free-text {field} faithful to THIS requested operation?",
                    )
        if plan["operations"]:
            self.add(
                "coverage",
                "complete",
                {
                    "complete": "Every requested device change is covered; scopes, exclusions and timing are honored. Nothing unrequested changes.",
                    "partial": "A requested change is omitted, or an unrequested/protected scope would change.",
                },
                "Check coverage of ALL latest-request clauses against proposed_actions. "
                "Leaving a fixture unchanged requires NO action on it. Safe omitted defaults belong in scripts.",
            )
        if plan["home_answer"]:
            self.add(
                "grounding",
                "grounded",
                {
                    "grounded": "Every asserted household fact is supported by the supplied state evidence.",
                    "invented": "At least one household fact/status/action success is unsupported.",
                },
                "Ground ONLY the proposed home answer in current states, not device operations.",
            )
        for index, question in enumerate(plan["external_questions"]):
            self.add(
                f"external_{index}_kind",
                question["kind"],
                {
                    "general": "The user asks this stable general-knowledge question; no current lookup is needed.",
                    "web": "The user asks this question requiring current information or explicit internet search.",
                },
                f"Route proposed external question {index} from the latest request, not from the planner's assumptions.",
            )

    def add(self, name, expected, criteria, instructions):
        if expected is None:
            raise PlanError("Requested parameter has no bounded candidate")
        criteria = {
            **criteria,
            "not_requested": "THIS operation/parameter is not requested or cannot be determined.",
        }
        self.expected[name] = expected
        self.questions[name] = choice(
            instructions
            + " The operation_<N> prefix identifies the corresponding proposed operation.",
            criteria,
        )

    def approve(self, answers, threshold, margin):
        verdict = {}
        for name, expected in self.expected.items():
            selected, _confidence, gap = read_choice(
                answers.get(name), self.questions[name]["criteria"]
            )
            # This gate is the probability of the SPECIFIC expected value, not
            # confidence about an unrelated/irrelevant branch of a JSON blob.
            probability = answers[name]["probabilities"][expected]
            verdict[name] = {
                "selected": selected,
                "expected": expected,
                "probability": probability,
                "margin": gap,
            }
        approved = all(
            value["selected"] == value["expected"]
            and value["probability"] >= threshold
            and value["margin"] >= margin
            for value in verdict.values()
        )
        return approved, verdict
