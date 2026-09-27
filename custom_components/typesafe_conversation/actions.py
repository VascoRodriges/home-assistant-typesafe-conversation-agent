"""How each (domain, action) pair becomes a Home Assistant intent.

Targeting goes through ``intent.async_handle`` rather than calling services
directly. That is not the obvious choice - Jev has already resolved an exact
``entity_id`` - but ``intent._filter_by_name`` accepts an entity id as the
``name`` slot and matches it exactly before it tries any alias, and the handler
then replaces the slot value with the friendly text for its response template.
So we get deterministic targeting *and* Home Assistant's own localized speech,
with the exposure check applied for free.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

# Intent names, verified present in Home Assistant 2026.5.0 (the oldest core
# this integration supports) and unchanged since.
INTENT_TURN_ON: Final = "HassTurnOn"
INTENT_TURN_OFF: Final = "HassTurnOff"
INTENT_TOGGLE: Final = "HassToggle"
INTENT_LIGHT_SET: Final = "HassLightSet"
INTENT_SET_POSITION: Final = "HassSetPosition"
INTENT_STOP_MOVING: Final = "HassStopMoving"
INTENT_FAN_SET_SPEED: Final = "HassFanSetSpeed"
INTENT_CLIMATE_SET_TEMPERATURE: Final = "HassClimateSetTemperature"
INTENT_CLIMATE_GET_TEMPERATURE: Final = "HassClimateGetTemperature"
INTENT_GET_STATE: Final = "HassGetState"
INTENT_NEVERMIND: Final = "HassNevermind"
INTENT_GET_CURRENT_TIME: Final = "HassGetCurrentTime"
INTENT_GET_CURRENT_DATE: Final = "HassGetCurrentDate"
INTENT_MEDIA_PAUSE: Final = "HassMediaPause"
INTENT_MEDIA_UNPAUSE: Final = "HassMediaUnpause"
INTENT_MEDIA_NEXT: Final = "HassMediaNext"
INTENT_MEDIA_PREVIOUS: Final = "HassMediaPrevious"
INTENT_MEDIA_MUTE: Final = "HassMediaPlayerMute"
INTENT_MEDIA_UNMUTE: Final = "HassMediaPlayerUnmute"
INTENT_SET_VOLUME: Final = "HassSetVolume"
INTENT_SET_VOLUME_RELATIVE: Final = "HassSetVolumeRelative"
INTENT_MEDIA_SEARCH_AND_PLAY: Final = "HassMediaSearchAndPlay"
INTENT_HUMIDIFIER_SETPOINT: Final = "HassHumidifierSetpoint"
INTENT_HUMIDIFIER_MODE: Final = "HassHumidifierMode"
INTENT_VACUUM_START: Final = "HassVacuumStart"
INTENT_VACUUM_RETURN_TO_BASE: Final = "HassVacuumReturnToBase"
INTENT_VACUUM_CLEAN_AREA: Final = "HassVacuumCleanArea"
INTENT_LIST_ADD_ITEM: Final = "HassListAddItem"
INTENT_LIST_COMPLETE_ITEM: Final = "HassListCompleteItem"
INTENT_LIST_REMOVE_ITEM: Final = "HassListRemoveItem"


@dataclass(slots=True, frozen=True)
class ActionSpec:
    """What one (domain, action) needs in order to run."""

    intent_type: str

    value_slot: str | None = None
    """Slot carrying a numeric value, when the action takes one."""

    value_kind: str | None = None
    """How to derive that value: percent, temperature, volume, volume_step."""

    relative: bool = False
    """True when the value is a delta rather than an absolute setting."""

    single_target: bool = False
    """The handler refuses to act on more than one entity."""

    name_only: bool = False
    """The handler has no `area` slot, so it must be given a specific entity."""

    needs_text: str | None = None
    """A free-text slot we can only fill from a Jev-selected span."""


# (domain, action) -> spec. Anything missing here is not something we execute;
# the router falls back for it.
ACTIONS: dict[tuple[str, str], ActionSpec] = {
    # -- on/off across every domain that supports it ---------------------------
    **{
        (domain, "turn_on"): ActionSpec(INTENT_TURN_ON)
        for domain in (
            "light", "switch", "fan", "climate", "media_player", "humidifier",
            "water_heater", "input_boolean",
        )
    },
    **{
        (domain, "turn_off"): ActionSpec(INTENT_TURN_OFF)
        for domain in (
            "light", "switch", "fan", "climate", "media_player", "humidifier",
            "water_heater", "input_boolean",
        )
    },
    **{
        (domain, "toggle"): ActionSpec(INTENT_TOGGLE)
        for domain in ("light", "switch", "input_boolean")
    },
    # -- light ----------------------------------------------------------------
    ("light", "set_brightness"): ActionSpec(
        INTENT_LIGHT_SET, value_slot="brightness", value_kind="percent"
    ),
    ("light", "brighter"): ActionSpec(
        INTENT_LIGHT_SET, value_slot="brightness", value_kind="percent",
        relative=True,
    ),
    ("light", "dimmer"): ActionSpec(
        INTENT_LIGHT_SET, value_slot="brightness", value_kind="percent",
        relative=True,
    ),
    ("light", "set_color"): ActionSpec(INTENT_LIGHT_SET, needs_text="color"),
    # -- cover ----------------------------------------------------------------
    ("cover", "open"): ActionSpec(INTENT_TURN_ON),
    ("cover", "close"): ActionSpec(INTENT_TURN_OFF),
    ("cover", "stop"): ActionSpec(INTENT_STOP_MOVING),
    ("cover", "set_position"): ActionSpec(
        INTENT_SET_POSITION, value_slot="position", value_kind="percent"
    ),
    # -- lock -----------------------------------------------------------------
    ("lock", "lock"): ActionSpec(INTENT_TURN_ON),
    ("lock", "unlock"): ActionSpec(INTENT_TURN_OFF),
    # -- fan ------------------------------------------------------------------
    ("fan", "set_speed"): ActionSpec(
        INTENT_FAN_SET_SPEED, value_slot="percentage", value_kind="percent"
    ),
    ("fan", "faster"): ActionSpec(
        INTENT_FAN_SET_SPEED, value_slot="percentage", value_kind="percent",
        relative=True,
    ),
    ("fan", "slower"): ActionSpec(
        INTENT_FAN_SET_SPEED, value_slot="percentage", value_kind="percent",
        relative=True,
    ),
    # -- climate --------------------------------------------------------------
    ("climate", "set_temperature"): ActionSpec(
        INTENT_CLIMATE_SET_TEMPERATURE,
        value_slot="temperature",
        value_kind="temperature",
        single_target=True,
    ),
    ("climate", "warmer"): ActionSpec(
        INTENT_CLIMATE_SET_TEMPERATURE,
        value_slot="temperature",
        value_kind="temperature",
        relative=True,
        single_target=True,
    ),
    ("climate", "cooler"): ActionSpec(
        INTENT_CLIMATE_SET_TEMPERATURE,
        value_slot="temperature",
        value_kind="temperature",
        relative=True,
        single_target=True,
    ),
    # -- media player ---------------------------------------------------------
    ("media_player", "play"): ActionSpec(INTENT_MEDIA_UNPAUSE),
    ("media_player", "pause"): ActionSpec(INTENT_MEDIA_PAUSE),
    # No longer offered as an option (see questions.py), but kept mapped so an
    # answer that still names it routes somewhere sane. It must not be
    # HassTurnOff: a media player is often a software endpoint with no power to
    # cut, so turn_off is rejected by the entity while the matched area still
    # counts as a success.
    ("media_player", "stop"): ActionSpec(INTENT_MEDIA_PAUSE),
    ("media_player", "next"): ActionSpec(INTENT_MEDIA_NEXT),
    ("media_player", "previous"): ActionSpec(INTENT_MEDIA_PREVIOUS),
    ("media_player", "mute"): ActionSpec(INTENT_MEDIA_MUTE),
    ("media_player", "unmute"): ActionSpec(INTENT_MEDIA_UNMUTE),
    ("media_player", "set_volume"): ActionSpec(
        INTENT_SET_VOLUME, value_slot="volume_level", value_kind="volume"
    ),
    ("media_player", "louder"): ActionSpec(
        INTENT_SET_VOLUME_RELATIVE,
        value_slot="volume_step",
        value_kind="volume_step",
        relative=True,
    ),
    ("media_player", "quieter"): ActionSpec(
        INTENT_SET_VOLUME_RELATIVE,
        value_slot="volume_step",
        value_kind="volume_step",
        relative=True,
    ),
    ("media_player", "search_and_play"): ActionSpec(
        INTENT_MEDIA_SEARCH_AND_PLAY,
        needs_text="search_query",
        # MediaSearchAndPlayHandler sets single_target internally. Saying so
        # here matters: the router branches on this flag when an area holds no
        # player, and a single-target handler is the one case where widening
        # to the home is safe.
        single_target=True,
    ),
    # -- simple trigger domains ----------------------------------------------
    ("scene", "activate"): ActionSpec(INTENT_TURN_ON),
    ("script", "run"): ActionSpec(INTENT_TURN_ON),
    ("button", "press"): ActionSpec(INTENT_TURN_ON),
    # -- vacuum ---------------------------------------------------------------
    ("vacuum", "start"): ActionSpec(INTENT_VACUUM_START),
    ("vacuum", "return_to_base"): ActionSpec(INTENT_VACUUM_RETURN_TO_BASE),
    ("vacuum", "clean_area"): ActionSpec(INTENT_VACUUM_CLEAN_AREA),
    # -- humidifier: these handlers have no `area` slot -----------------------
    ("humidifier", "set_humidity"): ActionSpec(
        INTENT_HUMIDIFIER_SETPOINT,
        value_slot="humidity",
        value_kind="percent",
        name_only=True,
    ),
    ("humidifier", "set_mode"): ActionSpec(
        INTENT_HUMIDIFIER_MODE, needs_text="mode", name_only=True
    ),
    # -- to-do lists: item is free text, and the list must be named -----------
    ("todo", "add_item"): ActionSpec(
        INTENT_LIST_ADD_ITEM, needs_text="item", name_only=True
    ),
    ("todo", "complete_item"): ActionSpec(
        INTENT_LIST_COMPLETE_ITEM, needs_text="item", name_only=True
    ),
    ("todo", "remove_item"): ActionSpec(
        INTENT_LIST_REMOVE_ITEM, needs_text="item", name_only=True
    ),
    # -- water heater ---------------------------------------------------------
    # No set_temperature mapping: HassClimateSetTemperature declares
    # platforms={climate}, so it can never match a water_heater entity and the
    # call is a guaranteed MatchFailedError. Falling back is more useful than
    # sending a request we know cannot succeed.
}

# Actions that move a value up rather than down. Used to sign a relative step.
INCREASING_ACTIONS: frozenset[str] = frozenset(
    {"brighter", "faster", "warmer", "louder"}
)

# Human-readable verbs for the speech we compose ourselves.
ACTION_VERBS: dict[str, str] = {
    "turn_on": "Turned on",
    "turn_off": "Turned off",
    "toggle": "Toggled",
    "open": "Opened",
    "close": "Closed",
    "stop": "Stopped",
    "lock": "Locked",
    "unlock": "Unlocked",
    "activate": "Activated",
    "run": "Ran",
    "press": "Pressed",
    "play": "Resumed",
    "pause": "Paused",
    "next": "Skipped ahead on",
    "previous": "Went back on",
    "mute": "Muted",
    "unmute": "Unmuted",
    "start": "Started",
    "return_to_base": "Sent back to the dock:",
    "clean_area": "Started cleaning with",
    "set_brightness": "Set the brightness of",
    "brighter": "Brightened",
    "dimmer": "Dimmed",
    "set_color": "Changed the colour of",
    "set_position": "Moved",
    "set_speed": "Set the speed of",
    "faster": "Sped up",
    "slower": "Slowed down",
    "set_temperature": "Set",
    "warmer": "Turned up",
    "cooler": "Turned down",
    "set_volume": "Set the volume of",
    "louder": "Turned up",
    "quieter": "Turned down",
    "search_and_play": "Playing that on",
    "set_humidity": "Set the humidity of",
    "set_mode": "Changed the mode of",
    "add_item": "Added that to",
    "complete_item": "Ticked that off",
    "remove_item": "Removed that from",
}


def spec_for(domain: str, action: str) -> ActionSpec | None:
    return ACTIONS.get((domain, action))


__all__ = [
    "ACTIONS",
    "ACTION_VERBS",
    "INCREASING_ACTIONS",
    "ActionSpec",
    "spec_for",
]
