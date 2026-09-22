#!/usr/bin/env python
"""Pull the real exposed-entity catalog from a live Home Assistant.

Exposure settings are not in the REST API, so this uses the WebSocket API and
the same registries the integration reads, producing a fixture shaped exactly
like what `EntityCatalog.snapshot()` sends to Jev.

    set -a; . ./.env; set +a
    .venv/bin/python scripts/pull_home.py --out tests/fixtures/real_home.json
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
from pathlib import Path
import sys

import aiohttp

ASSISTANT = "conversation"

# Mirrors entities._DOMAIN_ATTRS.
DOMAIN_ATTRS = {
    "light": ("brightness",),
    "cover": ("current_position", "device_class"),
    "fan": ("percentage",),
    "climate": ("current_temperature", "temperature", "temperature_unit"),
    "media_player": ("volume_level", "media_title"),
    "sensor": ("unit_of_measurement", "device_class"),
    "number": ("unit_of_measurement", "device_class"),
    "humidifier": ("humidity", "current_humidity"),
    "binary_sensor": ("device_class",),
    "switch": ("device_class",),
    "water_heater": ("current_temperature", "temperature"),
}


class WS:
    def __init__(self, session, url, token):
        self._session = session
        self._url = url
        self._token = token
        self._id = 0

    async def __aenter__(self):
        self._ws = await self._session.ws_connect(
            f"{self._url}/api/websocket", heartbeat=30
        )
        msg = await self._ws.receive_json()
        assert msg["type"] == "auth_required", msg
        await self._ws.send_json({"type": "auth", "access_token": self._token})
        msg = await self._ws.receive_json()
        if msg["type"] != "auth_ok":
            raise SystemExit(f"Home Assistant rejected the token: {msg}")
        return self

    async def __aexit__(self, *exc):
        await self._ws.close()

    async def cmd(self, type_: str, **kwargs):
        self._id += 1
        await self._ws.send_json({"id": self._id, "type": type_, **kwargs})
        while True:
            msg = await self._ws.receive_json()
            if msg.get("id") == self._id and msg["type"] == "result":
                if not msg["success"]:
                    raise SystemExit(f"{type_} failed: {msg.get('error')}")
                return msg["result"]


def pick_attrs(domain: str, attributes: dict) -> dict:
    out = {}
    for name in DOMAIN_ATTRS.get(domain, ()):
        if (value := attributes.get(name)) is not None:
            out[name] = value
    return out


async def main() -> None:
    parser = argparse.ArgumentParser()
    # Defaults to a path .gitignore already excludes: a catalog pulled from a
    # live instance profiles the home it came from and must not be committed.
    parser.add_argument(
        "--out", type=Path, default=Path("tests/fixtures/real_home.json")
    )
    parser.add_argument(
        "--keep-coordinates",
        action="store_true",
        help="do not redact latitude/longitude out of entity ids and names",
    )
    args = parser.parse_args()

    url = os.environ.get("HA_BASE_URL", "").rstrip("/")
    token = os.environ.get("HA_TOKEN")
    if not url or not token:
        raise SystemExit("HA_BASE_URL and HA_TOKEN must be set (see .env.example)")

    async with aiohttp.ClientSession() as session:
        async with WS(session, url, token) as ws:
            exposed = await ws.cmd("homeassistant/expose_entity/list")
            states = await ws.cmd("get_states")
            entity_reg = await ws.cmd("config/entity_registry/list")
            device_reg = await ws.cmd("config/device_registry/list")
            area_reg = await ws.cmd("config/area_registry/list")
            floor_reg = await ws.cmd("config/floor_registry/list")

    # The websocket API maps each assistant straight to a bool here, unlike
    # the storage format, which nests it under "should_expose".
    def _is_exposed(settings) -> bool:
        value = settings.get(ASSISTANT)
        if isinstance(value, dict):
            return bool(value.get("should_expose"))
        return bool(value)

    exposed_ids = {
        eid
        for eid, settings in exposed["exposed_entities"].items()
        if _is_exposed(settings)
    }
    entity_by_id = {e["entity_id"]: e for e in entity_reg}
    device_by_id = {d["id"]: d for d in device_reg}
    area_by_id = {a["area_id"]: a for a in area_reg}
    floor_by_id = {f["floor_id"]: f for f in floor_reg}

    entities = []
    used_areas: set[str] = set()
    for state in sorted(states, key=lambda s: s["attributes"].get("friendly_name", "")):
        eid = state["entity_id"]
        if eid not in exposed_ids:
            continue
        domain = eid.split(".", 1)[0]
        entry = entity_by_id.get(eid)
        area_id = None
        if entry:
            area_id = entry.get("area_id")
            if not area_id and entry.get("device_id"):
                area_id = (device_by_id.get(entry["device_id"]) or {}).get("area_id")
        if area_id:
            used_areas.add(area_id)

        name = state["attributes"].get("friendly_name") or eid
        aliases = list((entry or {}).get("aliases") or [])
        info = {
            "id": eid,
            "name": name,
            "domain": domain,
            "state": state["state"],
        }
        if aliases:
            info["also"] = ", ".join(aliases)
        if area_id:
            info["area"] = area_id
        if attrs := pick_attrs(domain, state["attributes"]):
            info["attrs"] = attrs
        entities.append(info)

    areas = []
    for area_id in sorted(used_areas):
        area = area_by_id.get(area_id)
        if not area:
            continue
        entry = {"id": area_id, "name": area["name"]}
        if area.get("floor_id") and (floor := floor_by_id.get(area["floor_id"])):
            entry["floor"] = floor["name"]
        areas.append(entry)
    areas.sort(key=lambda a: a["name"])

    home = {"areas": areas, "entities": entities}
    payload = json.dumps(home, indent=2)
    if not args.keep_coordinates:
        # Weather integrations name their entities after the station's
        # latitude and longitude, which pins the home to a few metres. These
        # fixtures get committed, so redact by default.
        before = payload
        payload = re.sub(r"nws_\d+_\d+_\d+_\d+_\w+", "nws_local_station", payload)
        payload = re.sub(
            r"NWS: -?\d+\.\d+, -?\d+\.\d+ \w+", "NWS: Local Station", payload
        )
        payload = re.sub(
            r"-?\d{1,3}\.\d{6,}", "0.0", payload
        )
        if payload != before:
            print("  (redacted coordinates; pass --keep-coordinates to disable)")
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(payload)

    by_domain: dict[str, int] = {}
    for e in entities:
        by_domain[e["domain"]] = by_domain.get(e["domain"], 0) + 1
    print(f"wrote {args.out}")
    print(f"  {len(entities)} exposed entities, {len(areas)} areas")
    print(f"  {len(states)} states total ({len(entities) / max(len(states),1):.0%} exposed)")
    print("  domains: " + ", ".join(f"{d}={n}" for d, n in sorted(by_domain.items())))
    aliased = sum(1 for e in entities if e.get("also"))
    unassigned = sum(1 for e in entities if "area" not in e)
    print(f"  {aliased} with aliases, {unassigned} with no area")
    print(f"  approx state tokens: {len(json.dumps(home)) // 4}")


if __name__ == "__main__":
    asyncio.run(main())
