"""Turn Jev's answers into a plan, using nothing but the answers.

``route`` is a pure function: answers in, plan out. No hass, no network, no
clock. That is what makes the routing logic testable as a table of fixtures,
which matters because the thresholds here are the part most likely to need
tuning.

Two axes decide whether a branch is trustworthy, because they fail differently:

* ``confidence`` - how peaked the distribution is overall.
* ``margin`` - p(top) - p(second). A distribution can look confident while the
  top two options remain effectively tied.

A branch is "solid" only when both clear their bar. Nouls carry no confidence,
so they get a dead band instead: between the low and high thresholds we treat
the answer as "no" and log it, because a Noul near 0.5 means the model splits
yes/no, not that the condition half-holds.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from . import questions as Q
from .actions import INCREASING_ACTIONS, ActionSpec, spec_for
from .const import (
    CONF_ACT_EXPLICIT,
    CONF_ACT_TERSE,
    CONF_CLARIFY_FLOOR,
    LOGGER,
    MAGNITUDE_STEPS,
    MIN_MARGIN,
    NOUL_COMPOUND_HIGH,
    NOUL_COMPOUND_LOW,
    NOUL_HERE_RELATIVE,
    NOUL_RISKY,
    RISKY_ACTIONS,
    T_ACTION_PROBABILITY,
    T_AREA,
    T_CATEGORY_CANCEL,
    T_CATEGORY_COMPOUND,
    T_CATEGORY_INFORMATION,
    T_CATEGORY_QUERY,
    T_CATEGORY_UNCLEAR,
    T_DOMAIN,
    T_ENTITY,
    T_LLM_LEAN,
    T_QUERY_KIND,
    T_RISKY_ACTION,
    T_RISKY_TARGET,
    T_SCOPE,
    T_SCOPE_WHOLE_HOUSE,
)
from .entities import CatalogEntity
from .extraction import COLOR_TEMP_PRESETS, Extraction
from .system_one import ChoiceAnswer, SystemOneResponse


class Route(StrEnum):
    """What the agent decided to do with the utterance."""

    COMMAND = "command"
    QUERY = "query"
    COMPOUND = "compound"
    INFORMATION = "information"
    CANCEL = "cancel"
    CLARIFY = "clarify"
    CONFIRM = "confirm"
    FALLBACK = "fallback"


@dataclass(slots=True)
class Target:
    """Who the action applies to, as intent slots would express it."""

    entity: CatalogEntity | None = None
    area_id: str | None = None
    floor_name: str | None = None
    domain: str | None = None
    whole_house: bool = False

    @property
    def described(self) -> str:
        if self.entity is not None:
            return self.entity.name
        if self.whole_house:
            return "everything"
        return "them"


@dataclass(slots=True)
class Plan:
    """The router's decision. ``execute`` turns this into intent calls."""

    route: Route
    reason: str = ""

    # command / confirm
    domain: str | None = None
    action: str | None = None
    spec: ActionSpec | None = None
    target: Target = field(default_factory=Target)
    value: float | None = None
    value_unit: str | None = None
    relative_step: int | None = None
    text_slot: tuple[str, str] | None = None
    """(slot name, value) for the free-text slots we can fill."""
    color_temp_kelvin: int | None = None

    # query
    query_kind: str | None = None

    # clarify / confirm
    options: tuple[tuple[str, str], ...] = ()
    """(entity_id, friendly name) pairs for a disambiguation question."""
    speech: str | None = None

    # presentation
    name_target_in_speech: bool = False
    """True in the middle confidence band: act, but say what we acted on."""

    # diagnostics, always populated
    trace: dict[str, Any] = field(default_factory=dict)


def _solid(answer: ChoiceAnswer | None, threshold: float) -> bool:
    """Confident enough *and* not a near-tie."""
    return (
        answer is not None
        and answer.confidence >= threshold
        and answer.margin >= MIN_MARGIN
    )


def _top_options(
    answer: ChoiceAnswer, exclude: set[str], limit: int = 2
) -> tuple[str, ...]:
    ranked = sorted(answer.probabilities.items(), key=lambda kv: -kv[1])
    return tuple(name for name, _ in ranked if name not in exclude)[:limit]


def route(
    response: SystemOneResponse,
    *,
    entities_by_id: dict[str, CatalogEntity],
    extraction: Extraction,
    speaker_area_id: str | None,
    available_domains: frozenset[str],
    always_confirm_risky: bool = True,
    catalog_floors: dict[str, str] | None = None,
) -> Plan:
    """Decide what to do. Pure - depends only on its arguments."""
    category = response.choice(Q.Q_CATEGORY)
    compound = response.noul(Q.Q_COMPOUND) or 0.0
    trace: dict[str, Any] = {
        "category": _describe(category),
        "compound": round(compound, 3),
        "latency_ms": round(response.latency_ms),
        "input_tokens": response.input_tokens,
    }

    if category is None:
        return Plan(Route.FALLBACK, reason="no category answer", trace=trace)

    # -- 0. the user is calling it off ---------------------------------------
    if category.choice == "cancel" and category.confidence >= T_CATEGORY_CANCEL:
        return Plan(Route.CANCEL, reason="category=cancel", trace=trace)

    # -- 1. more than one thing was asked for --------------------------------
    if (
        compound >= NOUL_COMPOUND_HIGH
        and category.choice == "command"
        and category.confidence >= T_CATEGORY_COMPOUND
    ):
        return Plan(Route.COMPOUND, reason=f"compound={compound:.2f}", trace=trace)
    if NOUL_COMPOUND_LOW <= compound < NOUL_COMPOUND_HIGH:
        # Splitting costs an LLM round trip plus N more Jev calls, and a bad
        # split mangles a request that would have worked as one.
        LOGGER.debug(
            "Compound noul %.2f is in the dead band; treating as a single command",
            compound,
        )

    # -- 2. nothing usable ----------------------------------------------------
    if category.choice == "unclear" and category.confidence >= T_CATEGORY_UNCLEAR:
        return Plan(Route.FALLBACK, reason="category=unclear", trace=trace)

    # -- 3. general knowledge -------------------------------------------------
    if category.choice == "information" and _solid(category, T_CATEGORY_INFORMATION):
        return Plan(Route.INFORMATION, reason="category=information", trace=trace)

    query_kind = response.choice(Q.Q_QUERY_KIND)
    trace["query_kind"] = _describe(query_kind)

    # -- 4. a question about the home ----------------------------------------
    if category.choice == "query" and category.confidence >= T_CATEGORY_QUERY:
        kind = (
            query_kind.choice
            if _solid(query_kind, T_QUERY_KIND)
            else "needs_prose"
        )
        plan = _plan_query(
            response,
            kind=kind,
            entities_by_id=entities_by_id,
            speaker_area_id=speaker_area_id,
            trace=trace,
        )
        return plan

    # -- 5. a command ---------------------------------------------------------
    if category.choice == "command":
        return _plan_command(
            response,
            entities_by_id=entities_by_id,
            extraction=extraction,
            speaker_area_id=speaker_area_id,
            available_domains=available_domains,
            always_confirm_risky=always_confirm_risky,
            catalog_floors=catalog_floors or {},
            trace=trace,
        )

    # Category was something we handle but not confidently enough to act on.
    return Plan(
        Route.FALLBACK,
        reason=f"category={category.choice} conf={category.confidence:.2f}",
        trace=trace,
    )


def _plan_query(
    response: SystemOneResponse,
    *,
    kind: str,
    entities_by_id: dict[str, CatalogEntity],
    speaker_area_id: str | None,
    trace: dict[str, Any],
) -> Plan:
    target_entity = response.choice(Q.Q_TARGET_ENTITY)
    target_area = response.choice(Q.Q_TARGET_AREA)
    target_domain = response.choice(Q.Q_TARGET_DOMAIN)
    trace.update(
        target_entity=_describe(target_entity),
        target_area=_describe(target_area),
        target_domain=_describe(target_domain),
    )

    target = Target()
    if (
        _solid(target_entity, T_ENTITY)
        and target_entity is not None
        and target_entity.choice != Q.NO_SINGLE_ENTITY
    ):
        target.entity = entities_by_id.get(target_entity.choice)
    if (
        _solid(target_area, T_AREA)
        and target_area is not None
        and target_area.choice != Q.NO_AREA
    ):
        target.area_id = target_area.choice
    elif kind == "temperature" and speaker_area_id:
        target.area_id = speaker_area_id
    if (
        _solid(target_domain, T_DOMAIN)
        and target_domain is not None
        and target_domain.choice != Q.NO_DOMAIN
    ):
        target.domain = target_domain.choice

    return Plan(
        Route.QUERY,
        reason=f"query/{kind}",
        query_kind=kind,
        target=target,
        trace=trace,
    )


def _plan_command(  # noqa: C901 - one decision tree, kept in one place on purpose
    response: SystemOneResponse,
    *,
    entities_by_id: dict[str, CatalogEntity],
    extraction: Extraction,
    speaker_area_id: str | None,
    available_domains: frozenset[str],
    always_confirm_risky: bool,
    catalog_floors: dict[str, str],
    trace: dict[str, Any],
) -> Plan:
    scope = response.choice(Q.Q_SCOPE)
    target_entity = response.choice(Q.Q_TARGET_ENTITY)
    target_area = response.choice(Q.Q_TARGET_AREA)
    target_domain = response.choice(Q.Q_TARGET_DOMAIN)
    here = response.noul(Q.Q_HERE_RELATIVE) or 0.0
    risky = response.noul(Q.Q_RISKY) or 0.0
    trace.update(
        scope=_describe(scope),
        target_entity=_describe(target_entity),
        target_area=_describe(target_area),
        target_domain=_describe(target_domain),
        here_relative=round(here, 3),
        risky=round(risky, 3),
    )

    entity = (
        entities_by_id.get(target_entity.choice)
        if target_entity is not None and target_entity.choice != Q.NO_SINGLE_ENTITY
        else None
    )

    # -- 5a. which kind of thing ---------------------------------------------
    domain: str | None = None
    if _solid(target_domain, T_DOMAIN) and target_domain.choice != Q.NO_DOMAIN:
        domain = target_domain.choice
    # An entity is a stronger constraint than a domain guess: it implies one.
    if entity is not None and _solid(target_entity, T_ENTITY):
        if domain is None or (
            target_domain is not None
            and target_entity.confidence > target_domain.confidence
        ):
            domain = entity.domain

    if domain is None or domain not in available_domains:
        # "turn off everything" legitimately names no domain. Whole-house with
        # no domain would raise IntentHandleError ("cannot target all
        # devices"), so the executor fans out over the controllable domains
        # instead of guessing one.
        if scope is not None and scope.choice == "whole_house" and _solid(
            scope, T_SCOPE_WHOLE_HOUSE
        ):
            return Plan(
                Route.COMMAND,
                reason="whole house, no single domain",
                action=_whole_house_action(response, available_domains),
                target=Target(whole_house=True),
                trace=trace,
            )
        return Plan(
            Route.FALLBACK,
            reason=f"no usable domain ({_describe(target_domain)})",
            trace=trace,
        )

    # -- 5b. what to do, read from that domain's branch only ------------------
    action_answer = response.choice(Q.action_question_id(domain))
    trace["action"] = _describe(action_answer)
    if action_answer is None:
        return Plan(Route.FALLBACK, reason=f"no action answer for {domain}", trace=trace)
    action_probability = action_answer.probabilities.get(action_answer.choice, 0.0)
    if (
        action_answer.choice == Q.NOT_TARGETED
        or action_probability < T_ACTION_PROBABILITY
    ):
        return Plan(
            Route.FALLBACK,
            reason=f"action_{domain}={action_answer.choice} "
            f"p={action_probability:.2f}",
            trace=trace,
        )
    action = action_answer.choice

    spec = spec_for(domain, action)
    if spec is None:
        return Plan(
            Route.FALLBACK, reason=f"no intent for {domain}.{action}", trace=trace
        )

    # -- 5c. who it applies to ------------------------------------------------
    target = Target(domain=domain)
    if scope is not None and scope.choice == "whole_house" and _solid(
        scope, T_SCOPE_WHOLE_HOUSE
    ):
        target.whole_house = True
    elif (
        scope is not None
        and scope.choice == "floor"
        and _solid(scope, T_SCOPE)
        and (floor := _resolve_floor(entity, target_area, catalog_floors)) is not None
    ):
        target.floor_name = floor
    elif entity is not None and _solid(target_entity, T_ENTITY):
        target.entity = entity
        target.area_id = entity.area_id
    elif (
        scope is not None
        and scope.choice == "single"
        and _solid(scope, T_SCOPE)
    ):
        # The user asked for one specific thing and we could not work out
        # which. Widening to the whole area would act on devices they did not
        # mention, so ask instead - see the clarify branch below.
        return _clarify_or_fall_back(
            domain, action, target_entity, entities_by_id, trace,
            reason="scope=single but no confident entity",
        )
    elif (
        _solid(target_area, T_AREA)
        and target_area is not None
        and target_area.choice != Q.NO_AREA
    ):
        target.area_id = target_area.choice
    elif here >= NOUL_HERE_RELATIVE and speaker_area_id:
        target.area_id = speaker_area_id
    elif spec.name_only:
        # This handler has no area slot, so without an entity there is nothing
        # we can legally send.
        return Plan(
            Route.FALLBACK, reason=f"{spec.intent_type} needs a named entity",
            trace=trace,
        )
    else:
        return _clarify_or_fall_back(
            domain, action, target_entity, entities_by_id, trace,
            reason="no usable target",
        )

    plan = Plan(
        Route.COMMAND,
        reason=f"{domain}.{action}",
        domain=domain,
        action=action,
        spec=spec,
        target=target,
        trace=trace,
    )

    # -- 5d. arguments --------------------------------------------------------
    _apply_arguments(plan, response, extraction, trace)

    # -- 5e. do not quietly unlock the house ---------------------------------
    if risky >= NOUL_RISKY and action in RISKY_ACTIONS:
        target_conf = (
            target_entity.confidence
            if target_entity is not None and target.entity is not None
            else (target_area.confidence if target_area is not None else 0.0)
        )
        if (
            always_confirm_risky
            or action_answer.confidence < T_RISKY_ACTION
            or target_conf < T_RISKY_TARGET
        ):
            plan.route = Route.CONFIRM
            plan.reason = f"risky {action}, asking first"
            return plan

    # -- presentation ---------------------------------------------------------
    deciding = min(
        action_probability,
        target_entity.confidence
        if target.entity is not None and target_entity is not None
        else 1.0,
    )
    plan.name_target_in_speech = deciding < CONF_ACT_TERSE
    trace["deciding_confidence"] = round(deciding, 3)
    if deciding < CONF_ACT_EXPLICIT:
        LOGGER.debug(
            "Acting at %.2f confidence, below the explicit band - naming the target",
            deciding,
        )
    return plan


def _resolve_floor(
    entity: CatalogEntity | None,
    target_area: ChoiceAnswer | None,
    catalog_floors: dict[str, str],
) -> str | None:
    """Work out which floor a floor-scoped request means.

    Returns None when we cannot tell, and the caller then falls through to area
    targeting rather than giving up: a home can name an area "Upstairs" without
    ever defining a floor, and "make it cooler upstairs" should still work.
    """
    if entity is not None and entity.floor_name:
        return entity.floor_name
    if target_area is not None and target_area.choice in catalog_floors:
        return catalog_floors[target_area.choice]
    return None


def _clarify_or_fall_back(
    domain: str,
    action: str,
    target_entity: ChoiceAnswer | None,
    entities_by_id: dict[str, CatalogEntity],
    trace: dict[str, Any],
    *,
    reason: str,
) -> Plan:
    """Ask which device was meant, if we have two plausible candidates.

    Clarification is only ever used for *target* ambiguity. The user knows
    which lamp they meant, and the question is short and natural. We never ask
    it about the category or the action - "did you want to turn something on?"
    is a worse experience than simply letting another matcher try.
    """
    if target_entity is not None and target_entity.confidence >= CONF_CLARIFY_FLOOR:
        options = tuple(
            (eid, entities_by_id[eid].name)
            for eid in _top_options(
                target_entity, exclude={Q.NO_SINGLE_ENTITY}, limit=2
            )
            if eid in entities_by_id
        )
        if len(options) >= 2:
            return Plan(
                Route.CLARIFY,
                reason=reason,
                domain=domain,
                action=action,
                options=options,
                trace=trace,
            )
    return Plan(Route.FALLBACK, reason=reason, trace=trace)


def _whole_house_action(
    response: SystemOneResponse, available_domains: frozenset[str]
) -> str | None:
    """Pick the action for a whole-house command that names no domain.

    We read the action branch of every domain present and take the one the
    model was most sure about, as long as it is an on/off style action - those
    are the only ones that make sense applied to the whole house.
    """
    best: tuple[float, str] | None = None
    for domain in available_domains:
        answer = response.choice(Q.action_question_id(domain))
        if answer is None or answer.choice in (Q.NOT_TARGETED,):
            continue
        if answer.choice not in ("turn_on", "turn_off", "close", "lock"):
            continue
        if best is None or answer.confidence > best[0]:
            best = (answer.confidence, answer.choice)
    return best[1] if best else None


def _apply_arguments(
    plan: Plan,
    response: SystemOneResponse,
    extraction: Extraction,
    trace: dict[str, Any],
) -> None:
    """Fill the numeric and text slots the chosen action needs."""
    spec = plan.spec
    if spec is None:
        return

    if spec.needs_text == "color":
        color = response.choice(Q.Q_COLOR_PICK)
        trace["color_pick"] = _describe(color)
        if color is not None and color.choice != Q.NO_VALUE:
            if (kelvin := COLOR_TEMP_PRESETS.get(color.choice)) is not None:
                plan.color_temp_kelvin = kelvin
            else:
                plan.text_slot = ("color", color.choice)
        return

    if spec.needs_text == "item":
        span = response.choice(Q.Q_LIST_ITEM_SPAN)
        trace["list_item_span"] = _describe(span)
        if span is not None and span.choice != Q.NO_VALUE and span.confidence >= 0.5:
            plan.text_slot = ("item", span.choice)
        else:
            plan.route = Route.FALLBACK
            plan.reason = "could not tell what item was meant"
        return

    if spec.needs_text == "search_query":
        span = response.choice(Q.Q_MEDIA_SPAN)
        trace["media_span"] = _describe(span)
        if span is not None and span.choice != Q.NO_VALUE and span.confidence >= 0.55:
            plan.text_slot = ("search_query", span.choice)
        else:
            # hassil has good patterns for this and, because we advertise
            # CONTROL, the pipeline never let it try. Let it.
            plan.route = Route.FALLBACK
            plan.reason = "media search query not confident"
        return

    if spec.value_slot is None:
        return

    if spec.relative:
        magnitude = response.choice(Q.Q_MAGNITUDE)
        trace["magnitude"] = _describe(magnitude)
        step = MAGNITUDE_STEPS.get(
            magnitude.choice if magnitude is not None else "moderate", 25
        )
        sign = 1 if plan.action in INCREASING_ACTIONS else -1
        plan.relative_step = sign * step
        return

    value_pick = response.choice(Q.Q_VALUE_PICK)
    trace["value_pick"] = _describe(value_pick)
    if value_pick is None or value_pick.choice == Q.NO_VALUE:
        # Absolute action with no value to apply: let hassil or the LLM try.
        plan.route = Route.FALLBACK
        plan.reason = f"{plan.action} needs a value and none was picked"
        return
    candidate = extraction.by_span(value_pick.choice)
    if candidate is None:
        plan.route = Route.FALLBACK
        plan.reason = f"picked span {value_pick.choice!r} is not a known candidate"
        return
    plan.value = candidate.value
    plan.value_unit = candidate.unit


def _describe(answer: ChoiceAnswer | None) -> dict[str, Any] | None:
    """Compact record of one answer, for the debug log and the HA trace."""
    if answer is None:
        return None
    top = sorted(answer.probabilities.items(), key=lambda kv: -kv[1])[:3]
    return {
        "choice": answer.choice,
        "confidence": round(answer.confidence, 3),
        "margin": round(answer.margin, 3),
        "top": {name: round(p, 3) for name, p in top},
    }


def should_try_llm_answer(response: SystemOneResponse) -> bool:
    """Whether a freeform LLM answer is worth trying as a fallback."""
    category = response.choice(Q.Q_CATEGORY)
    if category is None:
        return False
    lean = category.probabilities.get("information", 0.0) + category.probabilities.get(
        "query", 0.0
    )
    return lean >= T_LLM_LEAN


__all__ = ["Plan", "Route", "Target", "route", "should_try_llm_answer"]
