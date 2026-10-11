"""The NibePi Lovelace dashboard, built from a key -> entity id mapping.

Plain Python, no Home Assistant imports. ``build_dashboard`` returns the
dashboard as a dict (a ``sections`` view per area, built-in cards only);
a card whose entities are not in the mapping is left out, and a section or
view left without cards is dropped. ``build_apexcharts`` returns the
optional chart dashboard for the HACS apexcharts-card. ``dump_yaml`` is a
small deterministic YAML emitter for the dashboard dicts, used for the
committed ``dashboards/*.yaml`` (the ``nibepi.get_dashboard`` action uses
Home Assistant's own dumper).

The keys are the integration's translation keys (``planned_offset_s1`` for
a per-system entity) and, for the gateway's discovered register sensors,
the names in ``GATEWAY_REGISTERS`` / ``SYSTEM_REGISTERS``. ``indoor_s1`` /
``indoor_s2`` is the indoor sensor the integration uses (the configured
entity or BT50) and ``bus_health`` an optional sensor of the gateway's
``<prefix>bus_health`` topic.
"""

from __future__ import annotations

import json
import re
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

SYSTEMS = ("s1", "s2")

# Integration entities: key -> platform. Unique id ``<entry id>_<key>``.
SHARED_ENTITIES: Tuple[Tuple[str, str], ...] = (
    ("gateway_online", "binary_sensor"),
    ("price_level", "sensor"),
    ("optimiser_savings_today", "sensor"),
    ("heat_pump_cost_today", "sensor"),
    ("hw_planned_action", "sensor"),
    ("hw_draw_profile", "sensor"),
    ("hw_optimiser_plan", "sensor"),
    ("hw_comfort_lease_state", "sensor"),
    ("hw_period_lease_state", "sensor"),
    ("hw_temp_lux_lease_state", "sensor"),
    ("hw_mode", "select"),
    ("hw_learning", "switch"),
    ("hw_vv_control", "switch"),
    ("hw_autoluxury", "switch"),
    ("hw_priority", "switch"),
    ("hw_price_comfort", "switch"),
    ("hw_optimiser", "switch"),
)

# Per system: key -> platform. Unique id ``<entry id>_<system>_<key>``,
# dashboard key ``<key>_<system>``.
SYSTEM_ENTITIES: Tuple[Tuple[str, str], ...] = (
    ("mode", "select"),
    ("strategy", "select"),
    ("indoor_enabled", "switch"),
    ("price_enabled", "switch"),
    ("weather_enabled", "switch"),
    ("rmu_feed", "switch"),
    ("planned_offset", "sensor"),
    ("indoor_offset", "sensor"),
    ("price_offset", "sensor"),
    ("weather_offset", "sensor"),
    ("pump_offset", "sensor"),
    ("lease_state", "sensor"),
    ("rmu_lease_state", "sensor"),
    ("optimiser_plan", "sensor"),
    ("house_model", "sensor"),
)

# The gateway's MQTT-discovered register sensors (unique id
# ``nibepi_<id>_<register>``).
GATEWAY_REGISTERS: Dict[str, int] = {
    "outdoor": 40004,  # BT1
    "bt3_return": 40012,
    "bt6_hw_load": 40014,
    "bt7_hw_top": 40013,
    "compressor_frequency": 43136,
    "compressor_power": 43141,
    "degree_minutes": 43005,
    "priority": 43086,
}
SYSTEM_REGISTERS: Dict[str, Dict[str, int]] = {
    "bt50": {"s1": 40033, "s2": 40032},
    "supply": {"s1": 40008, "s2": 40007},  # BT2 / EP21-BT2
}

COMPONENT_LABELS = (("indoor", "Indoor"), ("price", "Price"), ("weather", "Forecast"))
HW_FEATURES = ("vv_control", "optimiser", "learning", "autoluxury", "priority", "price_comfort")

CARD_TYPES = frozenset(
    {
        "tile",
        "heading",
        "entities",
        "glance",
        "gauge",
        "history-graph",
        "statistics-graph",
        "markdown",
        "conditional",
        "grid",
    }
)


def integration_unique_ids(entry_id: str, systems: Iterable[str]) -> Dict[str, Tuple[str, str]]:
    """Dashboard key -> (platform, unique id) of the integration's entities."""
    found = {key: (domain, "%s_%s" % (entry_id, key)) for key, domain in SHARED_ENTITIES}
    for system in systems:
        for key, domain in SYSTEM_ENTITIES:
            found["%s_%s" % (key, system)] = (domain, "%s_%s_%s" % (entry_id, system, key))
    return found


def gateway_registers(systems: Iterable[str]) -> Dict[str, int]:
    """Dashboard key -> register of the gateway's discovered sensors."""
    found = dict(GATEWAY_REGISTERS)
    for system in systems:
        for name, regs in SYSTEM_REGISTERS.items():
            found["%s_%s" % (name, system)] = regs[system]
    return found


def systems_in(entities: Mapping[str, Any]) -> List[str]:
    return [s for s in SYSTEMS if any(k.endswith("_" + s) and k[: -len(s) - 1] in _SYSTEM_KEYS for k in entities)]


_SYSTEM_KEYS = frozenset(key for key, _ in SYSTEM_ENTITIES)


# --------------------------------------------------------------------------
# Card helpers: each returns None when an entity it needs is missing.
# --------------------------------------------------------------------------


class _Ids:
    def __init__(self, entities: Mapping[str, Optional[str]]) -> None:
        self.map = {k: v for k, v in entities.items() if isinstance(v, str) and v}

    def __call__(self, key: str) -> Optional[str]:
        return self.map.get(key)

    def has(self, *keys: str) -> bool:
        return all(key in self.map for key in keys)


def _compact(items: Iterable[Any]) -> List[Any]:
    return [item for item in items if item is not None]


def _tpl(text: str, **entities: str) -> str:
    """Put entity ids into a Jinja template at ``<<name>>``."""
    for name, entity_id in entities.items():
        text = text.replace("<<%s>>" % name, entity_id)
    if "<<" in text:
        raise ValueError("Unfilled template placeholder in %r" % text[:60])
    return text.strip("\n")


def heading(text: str, icon: Optional[str] = None, style: str = "title") -> Dict[str, Any]:
    card: Dict[str, Any] = {"type": "heading", "heading": text, "heading_style": style}
    if icon:
        card["icon"] = icon
    return card


def tile(ids: _Ids, key: str, name: Optional[str] = None, **extra: Any) -> Optional[Dict[str, Any]]:
    entity_id = ids(key)
    if entity_id is None:
        return None
    card: Dict[str, Any] = {"type": "tile", "entity": entity_id}
    if name:
        card["name"] = name
    card.update(extra)
    return card


def select_tile(ids: _Ids, key: str, name: Optional[str] = None) -> Optional[Dict[str, Any]]:
    return tile(ids, key, name, features=[{"type": "select-options"}], grid_options={"columns": 12})


def row(ids: _Ids, key: str, name: Optional[str] = None) -> Optional[Dict[str, Any]]:
    entity_id = ids(key)
    if entity_id is None:
        return None
    item: Dict[str, Any] = {"entity": entity_id}
    if name:
        item["name"] = name
    return item


def attr_row(
    ids: _Ids, key: str, attribute: str, name: str, suffix: Optional[str] = None
) -> Optional[Dict[str, Any]]:
    entity_id = ids(key)
    if entity_id is None:
        return None
    item: Dict[str, Any] = {"type": "attribute", "entity": entity_id, "attribute": attribute, "name": name}
    if suffix:
        item["suffix"] = suffix
    return item


def entities_card(title: Optional[str], rows: Iterable[Any]) -> Optional[Dict[str, Any]]:
    items = _compact(rows)
    if not items:
        return None
    card: Dict[str, Any] = {"type": "entities"}
    if title:
        card["title"] = title
    card["entities"] = items
    return card


def history(ids: _Ids, title: str, keys: Sequence[Any], hours: int = 24) -> Optional[Dict[str, Any]]:
    """``keys``: keys or (key, name) pairs."""
    items = []
    for key in keys:
        key, name = key if isinstance(key, tuple) else (key, None)
        item = row(ids, key, name)
        if item is not None:
            items.append(item)
    if not items:
        return None
    return {
        "type": "history-graph",
        "title": title,
        "hours_to_show": hours,
        "entities": items,
        "grid_options": {"columns": "full"},
    }


def markdown(content: str, title: Optional[str] = None) -> Dict[str, Any]:
    card: Dict[str, Any] = {"type": "markdown"}
    if title:
        card["title"] = title
    card["content"] = content
    card["grid_options"] = {"columns": "full"}
    return card


def alert(conditions: List[Dict[str, Any]], kind: str, text: str) -> Dict[str, Any]:
    return {
        "type": "conditional",
        "conditions": conditions,
        "card": {"type": "markdown", "content": '<ha-alert alert-type="%s">%s</ha-alert>' % (kind, text)},
    }


def state_is(entity_id: str, state: Any) -> Dict[str, Any]:
    return {"condition": "state", "entity": entity_id, "state": state}


def state_not(entity_id: str, state: Any) -> Dict[str, Any]:
    return {"condition": "state", "entity": entity_id, "state_not": state}


def section(cards: Iterable[Any], span: Optional[int] = None) -> Optional[Dict[str, Any]]:
    """A grid section; dropped when it has nothing but headings."""
    items = _compact(cards)
    if not [card for card in items if card.get("type") != "heading"]:
        return None
    result: Dict[str, Any] = {"type": "grid"}
    if span:
        result["column_span"] = span
    result["cards"] = items
    return result


def badge(ids: _Ids, key: str, name: Optional[str] = None) -> Optional[Dict[str, Any]]:
    entity_id = ids(key)
    if entity_id is None:
        return None
    item: Dict[str, Any] = {"type": "entity", "entity": entity_id, "show_name": True}
    if name:
        item["name"] = name
    return item


def view(title: str, path: str, icon: str, sections: Iterable[Any], badges: Iterable[Any] = ()) -> Optional[Dict[str, Any]]:
    items = _compact(sections)
    if not items:
        return None
    result: Dict[str, Any] = {"title": title, "path": path, "icon": icon, "type": "sections", "max_columns": 4}
    marks = _compact(badges)
    if marks:
        result["badges"] = marks
    result["sections"] = items
    return result


# --------------------------------------------------------------------------
# Jinja templates (markdown cards). Attributes may be missing or None.
# --------------------------------------------------------------------------

OFFSET_TEMPLATE = """
{%- set e = '<<planned>>' %}
{%- set comps = state_attr(e, 'components') or {} %}
{%- set reasons = state_attr(e, 'reasons') or {} %}
{%- set total = state_attr(e, 'total') %}
{%- set rules = state_attr(e, 'rules_total') %}
**Planned offset {{ states(e) }}**: sum {{ total if total is not none else '-' }}, rules {{ rules if rules is not none else '-' }}, source {{ state_attr(e, 'source') or '-' }}
{%- if state_attr(e, 'strategy_reason') %}

*{{ state_attr(e, 'strategy_reason') }}*
{%- endif %}
{%- if state_attr(e, 'error') %}

<ha-alert alert-type="error">{{ state_attr(e, 'error') }}</ha-alert>
{%- endif %}

| Component | Offset | Why |
|:--|--:|:--|
{%- for key, label in [('indoor', 'Indoor'), ('price', 'Price'), ('weather', 'Forecast')] %}
| {{ label }} | {{ comps[key] if comps[key] is defined else '-' }} | {{ reasons[key] if reasons[key] is defined else '' }} |
{%- endfor %}
"""

PLAN_TEMPLATE = """
{%- set e = '<<plan>>' %}
{%- set rows = state_attr(e, 'plan_24h') or [] %}
{%- set lines = state_attr(e, 'explanation') or [] %}
{%- set gate = state_attr(e, 'gate') or {} %}
{%- if state_attr(e, 'problem') %}
<ha-alert alert-type="warning">{{ state_attr(e, 'problem') }}</ha-alert>

{% endif %}
{{- state_attr(e, 'summary') or 'No plan yet.' }}
{%- if gate.reason is defined %}

Gate: {{ 'optimiser steers' if gate.use else 'rules steer' }} ({{ gate.reason }}){{ ', applied' if state_attr(e, 'applied') else ', not applied' }}.
{%- endif %}
{%- if lines %}
{% for line in lines %}
- {{ line }}
{%- endfor %}
{%- endif %}
{%- if rows %}

| Time | Offset | Indoor |
|:--|--:|--:|
{%- set ns = namespace(hours=0) %}
{%- for r in rows %}
{%- if r[0][-2:] == '00' %}{% set ns.hours = ns.hours + 1 %}{% endif %}
{%- if ns.hours <= 12 %}
| {{ r[0] }} | {{ '%+d' | format(r[1] | int) }} | {{ r[2] }} °C |
{%- endif %}
{%- endfor %}
{%- endif %}
"""

HW_NOW_TEMPLATE = """
{%- set e = '<<action>>' %}
{%- set reasons = state_attr(e, 'reasons') or {} %}
{%- set nxt = state_attr(e, 'next_heat_start') %}
{%- set closed = state_attr(e, 'closed_until') %}
**{{ states(e) | replace('_', ' ') | capitalize }}** (mode {{ state_attr(e, 'mode') or '-' }})
{%- if state_attr(e, 'error') %}

<ha-alert alert-type="error">{{ state_attr(e, 'error') }}</ha-alert>
{%- endif %}

Next heating: {{ as_timestamp(nxt) | timestamp_custom('%a %H:%M') if nxt else '-' }}
{%- if closed %} · closed until {{ as_timestamp(closed) | timestamp_custom('%a %H:%M') }}{% endif %}
{%- if reasons %}

| Feature | Reason |
|:--|:--|
{%- for key, text in reasons.items() %}
| {{ key | replace('_', ' ') | capitalize }} | {{ text }} |
{%- endfor %}
{%- endif %}
"""

HW_VV_PLAN_TEMPLATE = """
{%- set windows = state_attr('<<action>>', 'plan_24h') or [] %}
{%- if windows %}
| Window | Peak | Mode | Target |
|:--|:--|:--|--:|
{%- for w in windows %}
| {{ (w.start or '')[11:16] }}-{{ (w.end or '')[11:16] }} | {{ (w.peak or '')[11:16] }} | {{ w.mode or '-' }} | {{ w.target if w.target is not none else '-' }} |
{%- endfor %}
{%- else %}
No VV-AI heating windows in the next 24 h.
{%- endif %}
"""

HW_OPT_PLAN_TEMPLATE = """
{%- set e = '<<plan>>' %}
{%- set blocks = state_attr(e, 'blocks') or [] %}
{%- set saved = state_attr(e, 'savings') %}
{%- if state_attr(e, 'problem') %}
<ha-alert alert-type="warning">{{ state_attr(e, 'problem') }}</ha-alert>

{% endif %}
{{- 'In use' if state_attr(e, 'in_use') else ('Enabled, not in use' if state_attr(e, 'enabled') else 'Off (shown for comparison)') }}
{%- if saved is not none %}: saves {{ saved }} {{ state_attr(e, 'currency') or '' }} against the baseline, tank min {{ state_attr(e, 'min_temp') }} °C{% endif %}.
{%- if blocks %}

| Time | Action | Target | Why |
|:--|:--|--:|:--|
{%- for b in blocks %}
| {{ b.start }}-{{ b.end }} | {{ b.action }}{{ ' (boost)' if b.boost else '' }} | {{ b.target if b.target is not none else '-' }} | {{ b.reason or '' }} |
{%- endfor %}
{%- endif %}
"""

LEASES_TEMPLATE = """
| Lease | State | Value | Expires | Error |
|:--|:--|--:|:--|:--|
{%- for name, e in [<<leases>>] %}
{%- set value = state_attr(e, 'value') %}
{%- set exp = state_attr(e, 'expires') %}
| {{ name }} | {{ states(e) }} | {{ value if value is not none else '-' }} | {{ as_timestamp(exp) | timestamp_custom('%H:%M:%S') if exp else '-' }} | {{ state_attr(e, 'error') or '' }} |
{%- endfor %}
"""


# --------------------------------------------------------------------------
# Views
# --------------------------------------------------------------------------


def _label(system: str) -> str:
    return system.upper()


def _alerts(ids: _Ids, systems: Sequence[str]) -> List[Dict[str, Any]]:
    cards: List[Dict[str, Any]] = []
    gateway = ids("gateway_online")
    if gateway:
        cards.append(
            alert(
                [state_is(gateway, "off")],
                "error",
                "The NibePi gateway is offline: nothing reaches the pump, which falls back to its own curve.",
            )
        )
    leases = [("lease_state_" + s, "Curve offset lease " + _label(s)) for s in systems]
    leases += [("rmu_lease_state_" + s, "RMU40 lease " + _label(s)) for s in systems]
    leases += [
        ("hw_comfort_lease_state", "Hot water comfort mode lease"),
        ("hw_period_lease_state", "Hot water period lease"),
        ("hw_temp_lux_lease_state", "Temporary lux lease"),
    ]
    for key, label in leases:
        entity_id = ids(key)
        if entity_id:
            cards.append(
                alert([state_is(entity_id, "error")], "error", "%s: the gateway reports an error." % label)
            )
    for system in systems:
        model, strategy = ids("house_model_" + system), ids("strategy_" + system)
        if model:
            conditions = [state_is(model, ["prior", "low"])]
            if strategy:
                conditions.append(state_is(strategy, "optimiser"))
            cards.append(
                alert(
                    conditions,
                    "info",
                    "House model %s has low confidence: the rules steer until the optimiser's model "
                    "is good enough." % _label(system),
                )
            )
        feed, lease = ids("rmu_feed_" + system), ids("rmu_lease_state_" + system)
        if feed and lease:
            cards.append(
                alert(
                    [state_is(feed, "on"), state_not(lease, ["active", "grace"])],
                    "warning",
                    "RMU40 %s: the room temperature feed is on but its lease is not active." % _label(system),
                )
            )
    return cards


def _overview(ids: _Ids, systems: Sequence[str]) -> Optional[Dict[str, Any]]:
    climate: List[Any] = [heading("Climate", "mdi:home-thermometer")]
    climate.extend(_alerts(ids, systems))
    climate.append(tile(ids, "outdoor", "Outdoor"))
    for system in systems:
        label = _label(system)
        climate.append(tile(ids, "indoor_" + system, "Indoor " + label))
        climate.append(tile(ids, "indoor_offset_" + system, "Target " + label, state_content="setpoint", icon="mdi:target"))
        climate.append(tile(ids, "planned_offset_" + system, "Planned offset " + label, state_content=["state", "source"]))
        climate.append(tile(ids, "pump_offset_" + system, "Pump offset " + label))
        climate.append(select_tile(ids, "strategy_" + system))
        climate.append(select_tile(ids, "mode_" + system))
    money = section(
        [
            heading("Price and cost", "mdi:cash"),
            tile(ids, "price_level"),
            tile(ids, "optimiser_savings_today", "Expected savings"),
            tile(ids, "heat_pump_cost_today", "Heat pump cost"),
            _gauge(ids, "compressor_frequency", "Compressor", 0, 120),
            tile(ids, "compressor_power", "Compressor power"),
            tile(ids, "degree_minutes", "Degree minutes"),
        ]
    )
    hotwater = section(
        [
            heading("Hot water", "mdi:water-boiler"),
            tile(ids, "bt7_hw_top", "Hot water top (BT7)"),
            tile(ids, "hw_planned_action", "Planned action"),
            tile(ids, "hw_draw_profile", "Next draw"),
            tile(ids, "hw_optimiser_plan", "Next optimised charge"),
            select_tile(ids, "hw_mode"),
        ]
    )
    badges = [
        badge(ids, "gateway_online", "Gateway"),
        badge(ids, "priority", "Priority"),
        badge(ids, "compressor_frequency", "Compressor"),
        badge(ids, "degree_minutes", "Degree minutes"),
        badge(ids, "price_level", "Price"),
        badge(ids, "hw_planned_action", "Hot water"),
    ]
    return view("Overview", "overview", "mdi:heat-pump", [section(climate), money, hotwater], badges)


def _gauge(ids: _Ids, key: str, name: str, low: float, high: float) -> Optional[Dict[str, Any]]:
    entity_id = ids(key)
    if entity_id is None:
        return None
    return {"type": "gauge", "entity": entity_id, "name": name, "min": low, "max": high, "needle": True}


def _heating(ids: _Ids, systems: Sequence[str]) -> Optional[Dict[str, Any]]:
    sections: List[Any] = []
    for system in systems:
        label = _label(system)
        s = system
        sections.append(
            section(
                [
                    heading("Control " + label, "mdi:tune"),
                    select_tile(ids, "mode_" + s),
                    select_tile(ids, "strategy_" + s),
                    tile(ids, "indoor_enabled_" + s, "Indoor"),
                    tile(ids, "price_enabled_" + s, "Price"),
                    tile(ids, "weather_enabled_" + s, "Forecast"),
                ]
            )
        )
        offset_cards: List[Any] = [heading("Curve offset " + label, "mdi:chart-bell-curve-cumulative")]
        if ids.has("planned_offset_" + s):
            offset_cards.append(markdown(_tpl(OFFSET_TEMPLATE, planned=ids("planned_offset_" + s))))
        offset_cards.append(
            entities_card(
                None,
                [
                    row(ids, "planned_offset_" + s, "Planned"),
                    row(ids, "pump_offset_" + s, "Pump (read back)"),
                    row(ids, "indoor_offset_" + s, "Indoor"),
                    row(ids, "price_offset_" + s, "Price"),
                    row(ids, "weather_offset_" + s, "Forecast"),
                    attr_row(ids, "indoor_offset_" + s, "setpoint", "Setpoint", "°C"),
                    attr_row(ids, "planned_offset_" + s, "base_adjust", "Base adjustment"),
                ],
            )
        )
        sections.append(section(offset_cards))
        plan_cards: List[Any] = [heading("Optimiser plan " + label, "mdi:chart-timeline-variant-shimmer")]
        plan_cards.append(tile(ids, "optimiser_plan_" + s, "Planned offset now", state_content=["state", "strategy"]))
        if ids.has("optimiser_plan_" + s):
            plan_cards.append(markdown(_tpl(PLAN_TEMPLATE, plan=ids("optimiser_plan_" + s))))
        sections.append(section(plan_cards))
        sections.append(
            section(
                [
                    heading("House model " + label, "mdi:home-analytics"),
                    tile(ids, "house_model_" + s, "Confidence", state_content=["state", "confidence"]),
                    entities_card(
                        None,
                        [
                            attr_row(ids, "house_model_" + s, "heat_loss_w_per_k", "Heat loss", "W/K"),
                            attr_row(ids, "house_model_" + s, "time_constant_h", "Time constant", "h"),
                            attr_row(ids, "house_model_" + s, "capacity_kwh_per_k", "Heat capacity", "kWh/K"),
                            attr_row(ids, "house_model_" + s, "emitter", "Emitters"),
                            attr_row(ids, "house_model_" + s, "emitter_lag_h", "Emitter lag", "h"),
                            attr_row(ids, "house_model_" + s, "slab_temperature", "Slab now", "°C"),
                            attr_row(ids, "house_model_" + s, "sim_rmse", "Fit error (RMSE)", "°C"),
                            attr_row(ids, "house_model_" + s, "data_hours", "Data", "h"),
                            attr_row(ids, "house_model_" + s, "last_fit", "Last fit"),
                        ],
                    ),
                ]
            )
        )
        sections.append(
            section(
                [
                    heading("History " + label, "mdi:chart-line"),
                    history(ids, "Indoor and outdoor", [("indoor_" + s, "Indoor"), ("outdoor", "Outdoor")]),
                    history(
                        ids,
                        "Curve offset",
                        [
                            ("planned_offset_" + s, "Planned"),
                            ("pump_offset_" + s, "Pump"),
                            ("optimiser_plan_" + s, "Optimiser"),
                        ],
                    ),
                    history(
                        ids,
                        "Supply and return",
                        [("supply_" + s, "Supply (BT2)"), ("bt3_return", "Return (BT3)")],
                    ),
                ],
                span=2,
            )
        )
    cost = _statistics(ids)
    sections.append(
        section(
            [
                heading("Cost", "mdi:cash-multiple"),
                tile(ids, "heat_pump_cost_today", "Heat pump cost today"),
                tile(ids, "optimiser_savings_today", "Expected savings today"),
                cost,
            ],
            span=2,
        )
    )
    return view("Heating", "heating", "mdi:radiator", sections)


def _statistics(ids: _Ids) -> Optional[Dict[str, Any]]:
    entities = _compact([ids("heat_pump_cost_today"), ids("optimiser_savings_today")])
    if not entities:
        return None
    return {
        "type": "statistics-graph",
        "title": "Cost per day",
        "entities": entities,
        "stat_types": ["change"],
        "chart_type": "bar",
        "period": "day",
        "days_to_show": 30,
        "grid_options": {"columns": "full"},
    }


def _hotwater(ids: _Ids) -> Optional[Dict[str, Any]]:
    control = section(
        [heading("Control", "mdi:water-boiler-auto"), select_tile(ids, "hw_mode")]
        + [tile(ids, "hw_" + feature) for feature in HW_FEATURES]
    )
    now_cards: List[Any] = [
        heading("Now", "mdi:water-boiler"),
        tile(ids, "bt7_hw_top", "Top (BT7)"),
        tile(ids, "bt6_hw_load", "Load (BT6)"),
        tile(ids, "hw_planned_action", "Planned action"),
    ]
    if ids.has("hw_planned_action"):
        now_cards.append(markdown(_tpl(HW_NOW_TEMPLATE, action=ids("hw_planned_action"))))
    draw = section(
        [
            heading("Draws", "mdi:shower-head"),
            tile(ids, "hw_draw_profile", "Next expected draw"),
            entities_card(
                None,
                [
                    attr_row(ids, "hw_draw_profile", "confidence", "Confidence"),
                    attr_row(ids, "hw_draw_profile", "expected_drop", "Expected drop", "°C"),
                    attr_row(ids, "hw_draw_profile", "slot", "Slot"),
                    attr_row(ids, "hw_draw_profile", "learned_draws", "Learned draws"),
                    attr_row(ids, "hw_draw_profile", "last_learned", "Last learned"),
                ],
            ),
        ]
    )
    plan_cards: List[Any] = [heading("Plan, next 24 h", "mdi:calendar-clock")]
    if ids.has("hw_planned_action"):
        plan_cards.append(markdown(_tpl(HW_VV_PLAN_TEMPLATE, action=ids("hw_planned_action")), "VV-AI windows"))
    plan_cards.append(tile(ids, "hw_optimiser_plan", "Next optimised charge"))
    if ids.has("hw_optimiser_plan"):
        plan_cards.append(markdown(_tpl(HW_OPT_PLAN_TEMPLATE, plan=ids("hw_optimiser_plan")), "Optimiser charges"))
    graphs = section(
        [
            heading("History", "mdi:chart-line"),
            history(ids, "Tank", [("bt7_hw_top", "Top (BT7)"), ("bt6_hw_load", "Load (BT6)")], 48),
            history(ids, "Actions", [("hw_planned_action", "Planned action"), ("priority", "Priority")], 48),
        ],
        span=2,
    )
    leases = section(
        [
            heading("Leases", "mdi:lock-clock"),
            tile(ids, "hw_comfort_lease_state", "Comfort mode (47041)"),
            tile(ids, "hw_period_lease_state", "Period (47134)"),
            tile(ids, "hw_temp_lux_lease_state", "Temporary lux (48132)"),
        ]
    )
    return view(
        "Hot water",
        "hot-water",
        "mdi:water-boiler",
        [control, section(now_cards), draw, section(plan_cards), graphs, leases],
    )


def _gateway(ids: _Ids, systems: Sequence[str]) -> Optional[Dict[str, Any]]:
    gw = section(
        [
            heading("Gateway", "mdi:router-network"),
            tile(ids, "gateway_online", "Gateway"),
            entities_card(
                None,
                [
                    attr_row(ids, "gateway_online", "version", "Version"),
                    attr_row(ids, "gateway_online", "core", "Core"),
                    attr_row(ids, "gateway_online", "raw_set", "Raw writes"),
                    attr_row(ids, "gateway_online", "last_data", "Last data"),
                    attr_row(ids, "gateway_online", "last_message_at", "Last message"),
                    attr_row(ids, "gateway_online", "last_status", "Status"),
                    attr_row(ids, "gateway_online", "status_topic", "Status topic"),
                ],
            ),
        ]
    )
    lease_keys = [("lease_state_" + s, "Curve offset " + _label(s)) for s in systems]
    lease_keys += [("rmu_lease_state_" + s, "RMU40 " + _label(s)) for s in systems]
    lease_keys += [
        ("hw_comfort_lease_state", "Hot water comfort mode"),
        ("hw_period_lease_state", "Hot water period"),
        ("hw_temp_lux_lease_state", "Temporary lux"),
    ]
    present = [(label, ids(key)) for key, label in lease_keys if ids(key)]
    lease_cards: List[Any] = [heading("Leases", "mdi:lock-clock")]
    if present:
        pairs = ", ".join("('%s', '%s')" % (label, entity_id) for label, entity_id in present)
        lease_cards.append(markdown(_tpl(LEASES_TEMPLATE, leases=pairs)))
    lease_cards.extend(tile(ids, key, label) for key, label in lease_keys)
    bus = section(
        [
            heading("RS485 bus", "mdi:serial-port"),
            history(ids, "Bus errors per minute", [("bus_health", "Errors")], 48),
        ],
        span=2,
    )
    sections: List[Any] = [gw, section(lease_cards), bus]
    for system in systems:
        label = _label(system)
        sections.append(
            section(
                [
                    heading("Room temperature to pump (RMU40) " + label, "mdi:home-thermometer"),
                    tile(ids, "rmu_feed_" + system, "Send room temperature"),
                    tile(ids, "rmu_lease_state_" + system, "Lease"),
                    entities_card(
                        None,
                        [
                            attr_row(ids, "rmu_lease_state_" + system, "room_temperature", "Room temperature", "°C"),
                            attr_row(ids, "rmu_lease_state_" + system, "source_entity", "Source"),
                            attr_row(ids, "rmu_lease_state_" + system, "reason", "Reason"),
                            attr_row(ids, "rmu_lease_state_" + system, "last_action", "Last action"),
                        ],
                    ),
                ]
            )
        )
    return view("Gateway and safety", "gateway", "mdi:shield-check", sections)


def build_dashboard(entities: Mapping[str, Optional[str]], options: Optional[Mapping[str, Any]] = None) -> Dict[str, Any]:
    """The dashboard for the entities found; see the module docstring."""
    options = dict(options or {})
    ids = _Ids(entities)
    systems = systems_in(ids.map)
    views = _compact(
        [_overview(ids, systems), _heating(ids, systems), _hotwater(ids), _gateway(ids, systems)]
    )
    return {"title": options.get("title") or "NibePi", "views": views}


# --------------------------------------------------------------------------
# Optional: apexcharts-card (HACS)
# --------------------------------------------------------------------------

PLAN_SERIES_JS = """
// plan_24h rows are [local "HH:MM", offset, predicted indoor °C].
const rows = entity.attributes.plan_24h || [];
const now = new Date();
let prev = null;
return rows.map((r) => {
  const [h, m] = r[0].split(':').map(Number);
  let t = new Date(now);
  t.setHours(h, m, 0, 0);
  if (prev === null && t - now > 12 * 3600e3) t = new Date(t.getTime() - 86400e3);
  while (prev !== null && t <= prev) t = new Date(t.getTime() + 86400e3);
  prev = t;
  return [t.getTime(), r[%d]];
});
"""

PRICE_SERIES_JS = """
// levels: {ISO hour: level}; 1 very cheap ... 5 very expensive.
const rank = {VERY_CHEAP: 1, CHEAP: 2, NORMAL: 3, EXPENSIVE: 4, VERY_EXPENSIVE: 5};
const levels = entity.attributes.levels || {};
return Object.entries(levels).map(([t, l]) => [new Date(t).getTime(), rank[l] || null]);
"""


def _apex_card(ids: _Ids, system: str) -> Optional[Dict[str, Any]]:
    plan = ids("optimiser_plan_" + system)
    if plan is None:
        return None
    series: List[Dict[str, Any]] = [
        {
            "entity": plan,
            "name": "Planned offset",
            "yaxis_id": "offset",
            "type": "line",
            "curve": "stepline",
            "data_generator": PLAN_SERIES_JS.strip("\n") % 1,
        },
        {
            "entity": plan,
            "name": "Predicted indoor",
            "yaxis_id": "temp",
            "type": "line",
            "curve": "smooth",
            "data_generator": PLAN_SERIES_JS.strip("\n") % 2,
        },
    ]
    yaxis: List[Dict[str, Any]] = [
        {"id": "offset", "decimals": 0, "min": -10, "max": 10},
        {"id": "temp", "opposite": True, "decimals": 1},
    ]
    price = ids("price_level")
    if price:
        series.append(
            {
                "entity": price,
                "name": "Price level",
                "yaxis_id": "price",
                "type": "column",
                "opacity": 0.3,
                "data_generator": PRICE_SERIES_JS.strip("\n"),
            }
        )
        yaxis.append({"id": "price", "show": False, "min": 0, "max": 5})
    return {
        "type": "custom:apexcharts-card",
        "header": {"show": True, "title": "Optimiser plan %s, next 24 h" % _label(system)},
        "graph_span": "24h",
        "span": {"start": "minute"},
        "now": {"show": True, "label": "Now"},
        "yaxis": yaxis,
        "series": series,
        "grid_options": {"columns": "full"},
    }


def build_apexcharts(entities: Mapping[str, Optional[str]], options: Optional[Mapping[str, Any]] = None) -> Optional[Dict[str, Any]]:
    """Optional chart dashboard (needs the HACS apexcharts-card)."""
    options = dict(options or {})
    ids = _Ids(entities)
    cards = [_apex_card(ids, system) for system in systems_in(ids.map)]
    chart = view(
        "Optimiser chart",
        "optimiser-chart",
        "mdi:chart-timeline-variant-shimmer",
        [section([heading("Optimiser plan", "mdi:chart-timeline-variant-shimmer")] + cards, span=4)],
    )
    if chart is None:
        return None
    return {"title": "%s charts" % (options.get("title") or "NibePi"), "views": [chart]}


# --------------------------------------------------------------------------
# Inspection (tests) and YAML
# --------------------------------------------------------------------------


def iter_cards(node: Any) -> Iterable[Dict[str, Any]]:
    """Every card in a dashboard, view, section or card, nested ones too."""
    if not isinstance(node, dict):
        return
    for key in ("views", "sections"):
        for child in node.get(key) or []:
            yield from iter_cards(child)
    for child in node.get("cards") or []:
        yield child
        yield from iter_cards(child)
    card = node.get("card")
    if isinstance(card, dict):
        yield card
        yield from iter_cards(card)


ENTITY_ID = re.compile(r"\b(?:sensor|binary_sensor|select|switch|number|input_number)\.[a-z0-9_]+\b")


def referenced_entities(node: Any) -> List[str]:
    """Entity ids referenced anywhere (fields and templates), in order."""
    found: List[str] = []

    def walk(value: Any, key: Optional[str] = None) -> None:
        if isinstance(value, dict):
            for k, v in value.items():
                walk(v, k)
        elif isinstance(value, list):
            for v in value:
                walk(v, key)
        elif isinstance(value, str):
            if key in ("entity", "entities"):
                found.append(value)
            elif key in ("content", "data_generator"):
                found.extend(ENTITY_ID.findall(value))

    walk(node)
    return found


_PLAIN = re.compile(r"^[A-Za-z_/][A-Za-z0-9_ ./()-]*$")
_RESERVED = frozenset({"y", "n", "yes", "no", "on", "off", "true", "false", "null", "~"})


def _scalar(value: Any) -> str:
    if value is None:
        return "null"
    if value is True:
        return "true"
    if value is False:
        return "false"
    if isinstance(value, (int, float)):
        return repr(value)
    text = str(value)
    if (
        _PLAIN.match(text)
        and text.lower() not in _RESERVED
        and not text.endswith(" ")
        and " #" not in text
    ):
        return text
    return json.dumps(text, ensure_ascii=False)


def _literal_ok(text: str) -> bool:
    return "\n" in text and not text.startswith((" ", "\n")) and "\t" not in text and not any(
        line.endswith(" ") for line in text.split("\n")
    )


def _emit(value: Any, indent: int, lines: List[str]) -> None:
    pad = " " * indent
    if isinstance(value, dict):
        for key, item in value.items():
            _emit_pair(pad, str(key), item, indent, lines)
    elif isinstance(value, list):
        for item in value:
            if isinstance(item, dict) and item:
                first = True
                for key, sub in item.items():
                    prefix = pad + ("- " if first else "  ")
                    _emit_pair(prefix, str(key), sub, indent + 2, lines)
                    first = False
            elif isinstance(item, list) and item:
                lines.append(pad + "-")
                _emit(item, indent + 2, lines)
            else:
                lines.append(pad + "- " + _inline(item, indent + 2, lines))
    else:
        lines.append(pad + _scalar(value))


def _inline(value: Any, indent: int, lines: List[str]) -> str:
    if isinstance(value, dict):
        return "{}"
    if isinstance(value, list):
        return "[]"
    return _scalar(value)


def _emit_pair(prefix: str, key: str, value: Any, indent: int, lines: List[str]) -> None:
    head = prefix + _scalar(key) + ":"
    if isinstance(value, dict) and value:
        lines.append(head)
        _emit(value, indent + 2, lines)
    elif isinstance(value, list) and value:
        lines.append(head)
        _emit(value, indent + 2, lines)
    elif isinstance(value, str) and _literal_ok(value):
        lines.append(head + (" |" if value.endswith("\n") and not value.endswith("\n\n") else " |-"))
        body = value[:-1] if value.endswith("\n") else value
        for line in body.split("\n"):
            lines.append((" " * (indent + 2) + line) if line else "")
    else:
        lines.append(head + " " + _inline(value, indent + 2, lines))


def dump_yaml(data: Any, header: str = "") -> str:
    """Block-style YAML for the dashboard dicts (multi-line strings as ``|``)."""
    lines: List[str] = [("# " + line).rstrip() for line in header.splitlines()] if header else []
    _emit(data, 0, lines)
    return "\n".join(lines) + "\n"
