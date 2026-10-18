"""Electricity price: normalisation, price level and curve offset.

Ports from config_node.js:

- ``fetchTibberPrices`` / ``fetchElprisPrices``: slots are averaged into
  hours before analysis ("stabila timpunkter"). ``hourly_average``.
- ``optimizeElectricityTrading`` with ``findTradingOpportunities``,
  ``findOptimalCombination`` and ``formatResult`` (the "Lokal AI" algorithm
  from energy.anerdins-iot.se): buy/sell pairs within ``time`` hours whose
  spread exceeds ``min_spread`` öre. ``optimize_trading``.
- ``priceLevelsFromTrades``: the cheaper half of the buy points is
  VERY_CHEAP, the rest CHEAP; the cheaper half of the sell points is
  EXPENSIVE, the rest VERY_EXPENSIVE; anything else NORMAL.
  ``levels_from_trades``.
- The "own thresholds" override (``enable_own_setting`` with
  ``verycheap`` / ``veryexpensive`` in öre). ``apply_thresholds``.
- ``priceAdjustCurve`` (heat part): level -> ``heat_<level>_sN`` offset;
  at EXPENSIVE and VERY_EXPENSIVE, with the temperature guard on
  (``enable_temp_sN``), the offset only applies while the indoor
  temperature is above setpoint + ``temp_low_sN``. ``compute_price``.
- ``getPriceList``'s fallback: the last good price list is used for as long
  as it covers the current hour. ``PriceCache``.

Defaults are config_node's: ``time`` 12 h, ``min_spread`` 20 öre, all
level offsets 0 (unset), ``temp_low`` 0, guard off.

Deliberate differences:

- Prices come from Home Assistant instead of Tibber's API /
  elprisetjustnu.se: the Tibber integration's ``tibber.get_prices`` action
  (``normalize_tibber_response``), or an entity's attributes (Nordpool
  ``raw_today``, numeric ``today``/``tomorrow`` lists, or Tibber-style
  ``{startsAt, total}`` lists, ``normalize_prices``).
  Slot length is never assumed: it comes from start/end or from spreading
  the values over the real length of the local day (23/24/25 h).
- Hours are grouped by the real instant, not by the first 13 characters of
  the local time string, which merged the two 02:00 hours on the autumn DST
  day.
- ``unit_factor`` converts the entity's unit to öre (100 for SEK/kWh, the
  default), because min_spread and the thresholds are in öre.
- ``min_spread`` 0 is honoured; config_node's ``|| 20`` turned 0 into 20.
- The threshold override compares the entity's price as is; config_node's
  optional VAT/surcharge (``enable_own_price``) is left out because HA price
  sensors usually include what the user configured.
- When no price covers the current hour the offset is 0 at once;
  config_node kept the last offset for a 30 minute grace period
  (``priceDataMissing``). ``PriceCache`` covers short outages of the entity.
- At NORMAL config_node applied ``heat_normal`` even with heat control
  disabled; here the price component's switch disables all levels.
- Hot water, frequency locking, degree-minute reset and prioritised
  additive from runPrice/priceAdjustCurve are not part of the curve offset
  and come in later phases.
"""

from __future__ import annotations

import math
from collections import Counter
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone, tzinfo
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from .inputs import Decision, PriceSlot, Reading, js_round, to_float, usable
from .texts import msg, term

LEVELS = ("VERY_CHEAP", "CHEAP", "NORMAL", "EXPENSIVE", "VERY_EXPENSIVE")
GUARDED_LEVELS = ("EXPENSIVE", "VERY_EXPENSIVE")

DEFAULT_TIME_WINDOW_H = 12.0
DEFAULT_MIN_SPREAD = 20.0  # öre
DEFAULT_UNIT_FACTOR = 100.0  # SEK/kWh -> öre

_START_KEYS = ("start", "startsAt", "starts_at", "start_time", "time_start", "from")
_END_KEYS = ("end", "endsAt", "ends_at", "end_time", "time_end", "till", "to")
_PRICE_KEYS = ("value", "total", "price", "SEK_per_kWh", "EUR_per_kWh")
_PREFERRED_ATTRS = ("raw_today", "raw_tomorrow", "today", "tomorrow")

# --------------------------------------------------------------------------
# Normalisation
# --------------------------------------------------------------------------


def parse_time(value: Any, tz: tzinfo) -> Optional[datetime]:
    """datetime or ISO 8601 string -> aware datetime (naive = local ``tz``)."""
    if isinstance(value, datetime):
        result = value
    elif isinstance(value, str):
        text = value.strip()
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        try:
            result = datetime.fromisoformat(text)
        except ValueError:
            return None
    else:
        return None
    if result.tzinfo is None:
        result = result.replace(tzinfo=tz)
    # UTC from here on: arithmetic and comparisons between two datetimes
    # with the same zoneinfo use wall-clock time, which is wrong across DST.
    return result.astimezone(timezone.utc)


def _first(item: Mapping[str, Any], keys: Sequence[str]) -> Any:
    for key in keys:
        if key in item and item[key] is not None:
            return item[key]
    return None


def _is_dict_list(value: Any) -> bool:
    return isinstance(value, (list, tuple)) and any(isinstance(v, Mapping) for v in value)


def _is_number_list(value: Any) -> bool:
    return (
        isinstance(value, (list, tuple))
        and len(value) > 0
        and all(v is None or to_float(v) is not None for v in value)
        and any(v is not None for v in value)
    )


def slots_from_dicts(items: Iterable[Any], tz: tzinfo) -> List[PriceSlot]:
    """``{start, end, value}`` / ``{startsAt, total}`` items -> slots.

    A missing end is the next item's start; the last item without an end
    gets the length of the one before it. A single item without an end is
    dropped: its length cannot be known.
    """
    parsed: List[Tuple[datetime, Optional[datetime], float]] = []
    for item in items:
        if not isinstance(item, Mapping):
            continue
        start = parse_time(_first(item, _START_KEYS), tz)
        price = to_float(_first(item, _PRICE_KEYS))
        if start is None or price is None:
            continue
        end = parse_time(_first(item, _END_KEYS), tz)
        parsed.append((start, end, price))
    parsed.sort(key=lambda p: p[0])
    slots: List[PriceSlot] = []
    for index, (start, end, price) in enumerate(parsed):
        if end is None:
            if index + 1 < len(parsed):
                end = parsed[index + 1][0]
            elif slots:
                end = start + (slots[-1].end - slots[-1].start)
            else:
                continue
        if end <= start:
            continue
        slots.append(PriceSlot(start, end, price))
    return slots


def local_day_bounds(day: date, tz: tzinfo) -> Tuple[datetime, datetime]:
    """Local midnight to next local midnight, as UTC instants.

    Slots are kept in UTC: Python does wall-clock arithmetic between
    datetimes that share a zoneinfo, which is wrong across DST changes.
    """
    start = datetime.combine(day, time(0), tzinfo=tz).astimezone(timezone.utc)
    end = datetime.combine(day + timedelta(days=1), time(0), tzinfo=tz).astimezone(timezone.utc)
    return start, end


def slots_from_numbers(values: Sequence[Any], day: date, tz: tzinfo) -> List[PriceSlot]:
    """Numbers spread evenly over the local day (24, 96, 23, 92, 100, ...).

    The day's real length is used, so 92 quarter-hour values on the spring
    DST day and 100 on the autumn one land on the right instants. ``None``
    entries keep their position but produce no slot.
    """
    if not values:
        return []
    start, end = local_day_bounds(day, tz)
    step = (end - start) / len(values)
    slots = []
    for index, raw in enumerate(values):
        price = to_float(raw)
        if price is None:
            continue
        slot_start = start + step * index
        slots.append(PriceSlot(slot_start, slot_start + step, price))
    return slots


def normalize_prices(attributes: Mapping[str, Any], tz: tzinfo, now: datetime) -> List[PriceSlot]:
    """All price slots found in an entity's attributes, sorted, deduplicated.

    Supported:
    (a) ``raw_today`` / ``raw_tomorrow``: lists of ``{start, end, value}``
        (HACS Nordpool);
    (b) ``today`` / ``tomorrow``: lists of numbers for the local day;
    (c) any attribute holding a list of ``{startsAt, total}`` (Tibber style)
        or similar dicts.
    """
    today = now.astimezone(tz).date()
    found: List[PriceSlot] = []
    keys = list(_PREFERRED_ATTRS) + sorted(k for k in attributes if k not in _PREFERRED_ATTRS)
    for key in keys:
        value = attributes.get(key)
        if _is_dict_list(value):
            found.extend(slots_from_dicts(value, tz))
        elif key in ("today", "tomorrow") and _is_number_list(value):
            day = today if key == "today" else today + timedelta(days=1)
            found.extend(slots_from_numbers(value, day, tz))
    found.sort(key=lambda s: s.start.timestamp())
    result: List[PriceSlot] = []
    seen = set()
    for slot in found:
        stamp = slot.start.timestamp()
        if stamp in seen:
            continue
        seen.add(stamp)
        result.append(slot)
    return result


def slot_at(slots: Sequence[PriceSlot], when: datetime) -> Optional[PriceSlot]:
    for slot in slots:
        if slot.start <= when < slot.end:
            return slot
    return None


def hourly_average(slots: Sequence[PriceSlot]) -> List[PriceSlot]:
    """Average slots into whole hours (by instant, so DST-safe).

    Slots longer than an hour are kept as they are.
    """
    groups: Dict[int, List[PriceSlot]] = {}
    order: List[int] = []
    for slot in slots:
        if slot.end.timestamp() - slot.start.timestamp() > 3600:
            key = int(slot.start.timestamp())
        else:
            key = int(slot.start.timestamp()) // 3600 * 3600
        if key not in groups:
            groups[key] = []
            order.append(key)
        groups[key].append(slot)
    result = []
    for key in sorted(order):
        items = groups[key]
        start = datetime.fromtimestamp(key, timezone.utc)
        end = max(start + timedelta(hours=1), max(s.end for s in items))
        price = sum(s.price for s in items) / len(items)
        result.append(PriceSlot(start, end, price))
    return result


# --------------------------------------------------------------------------
# Tibber: the tibber.get_prices action
# --------------------------------------------------------------------------
#
# Home Assistant's Tibber integration has no price-list attributes; prices
# come from the ``tibber.get_prices`` action (response only):
#
#   request:  {"start": "<datetime>", "end": "<datetime>"}, both optional
#             (default today 00:00 to tomorrow 00:00 local); filtered as
#             start <= start_time < end
#   response: {"prices": {"<home nickname>": [
#                 {"start_time": "2025-10-10T00:00:00.000+02:00",
#                  "price": 1.2345}, ...]}}
#
# ``start_time`` is Tibber's ``startsAt`` as given (pyTibber fetches
# QUARTER_HOURLY, so 96 slots a day; older versions gave 24), ``price`` its
# ``total`` (energy + tax) in currency/kWh, e.g. SEK/kWh. There is no end
# time. Homes are keyed by their Tibber app nickname (address as fallback).

TIBBER_DOMAIN = "tibber"
TIBBER_SERVICE = "get_prices"
TIBBER_MIN_REFRESH = timedelta(minutes=15)
TIBBER_PUBLISH_HOUR = 13  # tomorrow's day-ahead prices come 13:00-14:00 local
# A gap between two starts longer than this (and than the typical slot) is
# a hole in the data, not one long slot.
_MAX_SLOT = timedelta(hours=1)


@dataclass(frozen=True)
class TibberPrices:
    slots: List[PriceSlot]
    home: Optional[str] = None
    homes: List[str] = field(default_factory=list)
    warning: Optional[str] = None


def tibber_request(now: datetime, tz: tzinfo) -> Dict[str, str]:
    """``tibber.get_prices`` data: today 00:00 through tomorrow 24:00 local."""
    day = now.astimezone(tz).date()
    start = datetime.combine(day, time(0), tzinfo=tz)
    end = datetime.combine(day + timedelta(days=2), time(0), tzinfo=tz)
    return {"start": start.isoformat(), "end": end.isoformat()}


def typical_length(starts: Sequence[datetime]) -> Optional[timedelta]:
    """Most common gap between consecutive starts (ties: the shorter)."""
    gaps = Counter(
        b - a for a, b in zip(starts, starts[1:]) if b > a
    )
    if not gaps:
        return None
    return min(gaps, key=lambda gap: (-gaps[gap], gap))


def slots_from_starts(items: Iterable[Tuple[datetime, float]]) -> List[PriceSlot]:
    """Start-only prices -> slots.

    A slot ends where the next one starts; the last one, and one followed
    by a hole, gets the typical slot length. With fewer than two distinct
    starts the length cannot be known and nothing is returned.
    """
    by_start: Dict[float, Tuple[datetime, float]] = {}
    for start, price in items:
        by_start.setdefault(start.timestamp(), (start, price))
    ordered = [by_start[key] for key in sorted(by_start)]
    typical = typical_length([start for start, _ in ordered])
    if typical is None:
        return []
    limit = max(typical, _MAX_SLOT)
    slots = []
    for index, (start, price) in enumerate(ordered):
        end = start + typical
        if index + 1 < len(ordered):
            following = ordered[index + 1][0]
            if following - start <= limit:
                end = following
        slots.append(PriceSlot(start, end, price))
    return slots


def _select_home(prices: Mapping[Any, Any], home: Optional[str]) -> Tuple[Any, Optional[str]]:
    """(key in ``prices`` or None, warning)."""
    keys = list(prices)
    names = ", ".join(repr(str(k)) for k in keys)
    if not keys:
        return None, "the response has no Tibber homes"
    wanted = (home or "").strip()
    if wanted:
        for key in keys:
            if str(key) == wanted:
                return key, None
        for key in keys:
            if str(key).strip().casefold() == wanted.casefold():
                return key, None
        return None, "Tibber home %r not in the response (homes: %s)" % (wanted, names)
    if len(keys) > 1:
        return keys[0], (
            "several Tibber homes (%s), using %r; set the Tibber home option to choose"
            % (names, str(keys[0]))
        )
    return keys[0], None


def tibber_homes(response: Any) -> List[str]:
    """Home names in a ``tibber.get_prices`` response (the ``prices`` keys)."""
    prices = response.get("prices") if isinstance(response, Mapping) else None
    if not isinstance(prices, Mapping):
        return []
    return [str(key) for key in prices]


def normalize_tibber_response(
    response: Any, tz: tzinfo, home: Optional[str] = None
) -> TibberPrices:
    """``tibber.get_prices`` response -> slots for one home.

    ``home`` picks a home by nickname (exact, then case-insensitive); empty
    takes the only one, or the first with a warning when there are several.
    Prices stay in the response's unit (currency/kWh); ``compute_level``
    applies ``unit_factor``.
    """
    prices = response.get("prices") if isinstance(response, Mapping) else None
    if not isinstance(prices, Mapping):
        return TibberPrices([], warning="the response has no 'prices'")
    homes = [str(k) for k in prices]
    key, warning = _select_home(prices, home)
    if key is None:
        return TibberPrices([], None, homes, warning)
    items = prices.get(key)
    parsed: List[Tuple[datetime, float]] = []
    for item in items if isinstance(items, (list, tuple)) else []:
        if not isinstance(item, Mapping):
            continue
        start = parse_time(item.get("start_time"), tz)
        price = to_float(item.get("price"))
        if start is not None and price is not None:
            parsed.append((start, price))
    return TibberPrices(slots_from_starts(parsed), str(key), homes, warning)


def tibber_refresh_due(
    now: datetime,
    tz: tzinfo,
    last_attempt: Optional[datetime],
    slots: Sequence[PriceSlot],
) -> bool:
    """Whether to call ``tibber.get_prices`` again.

    - first call, or the clock went backwards;
    - a new local hour since the last call;
    - otherwise at most every 15 minutes, and only while the list does not
      cover now, or from 13:00 local while tomorrow is still missing (the
      day-ahead prices are published 13:00-14:00).
    """
    if last_attempt is None or now < last_attempt:
        return True

    def hour_of(when: datetime) -> datetime:
        local = when.astimezone(tz)
        return local.replace(minute=0, second=0, microsecond=0).astimezone(timezone.utc)

    if hour_of(now) != hour_of(last_attempt):
        return True
    if now - last_attempt < TIBBER_MIN_REFRESH:
        return False
    if slot_at(slots, now) is None:
        return True
    local_now = now.astimezone(tz)
    if local_now.hour >= TIBBER_PUBLISH_HOUR:
        tomorrow, _ = local_day_bounds(local_now.date() + timedelta(days=1), tz)
        if not any(slot.start >= tomorrow for slot in slots):
            return True
    return False


# --------------------------------------------------------------------------
# Level: optimizeElectricityTrading + priceLevelsFromTrades
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class PricePoint:
    ts: float  # epoch seconds
    value: float  # öre


@dataclass(frozen=True)
class Trade:
    buy: PricePoint
    sell: PricePoint
    spread: float


def find_trading_opportunities(
    sorted_prices: Sequence[PricePoint],
    data: Sequence[PricePoint],
    time_window_h: float,
    min_spread: float,
) -> List[Trade]:
    """``findTradingOpportunities``: every buy/sell pair in the window."""
    slots_in_window = time_window_h
    if len(data) > 1:
        slot_minutes = (data[1].ts - data[0].ts) / 60.0
        if slot_minutes > 0:
            slots_in_window = js_round((time_window_h * 60.0) / slot_minutes)
    slots_in_window = int(slots_in_window)
    index_of: Dict[float, int] = {}
    for index, point in enumerate(data):
        index_of.setdefault(point.ts, index)
    opportunities: List[Trade] = []
    for low in sorted_prices:
        buy_index = index_of.get(low.ts)
        if buy_index is None:
            continue
        end_index = min(buy_index + slots_in_window, len(data))
        for sell_index in range(buy_index + 1, end_index):
            spread = data[sell_index].value - low.value
            if spread > min_spread:
                opportunities.append(Trade(low, data[sell_index], spread))
    # Stable sort, like Array.prototype.sort since ES2019.
    opportunities.sort(key=lambda t: -t.spread)
    return opportunities


def find_optimal_combination(opportunities: Sequence[Trade]) -> List[Trade]:
    """``findOptimalCombination``: greedy non-overlapping sets, best total.

    config_node checked each candidate against every trade in the set; a
    set of used timestamps gives the same answer (a conflict is any shared
    buy/sell timestamp in any role) in O(n²) instead of O(n³).
    """
    best: List[Trade] = []
    best_profit = -math.inf
    for i, first in enumerate(opportunities):
        combination = [first]
        used = {first.buy.ts, first.sell.ts}
        for j, other in enumerate(opportunities):
            if i == j:
                continue
            if other.buy.ts in used or other.sell.ts in used:
                continue
            combination.append(other)
            used.add(other.buy.ts)
            used.add(other.sell.ts)
        profit = sum(t.spread for t in combination)
        if profit > best_profit:
            best_profit = profit
            best = combination
    return best


def optimize_trading(
    points: Sequence[PricePoint],
    time_window_h: float = DEFAULT_TIME_WINDOW_H,
    min_spread: float = DEFAULT_MIN_SPREAD,
) -> Tuple[List[PricePoint], List[PricePoint]]:
    """``optimizeElectricityTrading``: (buy points, sell points)."""
    if not points:
        return [], []
    sorted_by_price = sorted(points, key=lambda p: p.value)
    if sorted_by_price[-1].value - sorted_by_price[0].value <= min_spread:
        return [], []
    opportunities = find_trading_opportunities(sorted_by_price, points, time_window_h, min_spread)
    if not opportunities:
        return [], []
    trades = find_optimal_combination(opportunities)
    return [t.buy for t in trades], [t.sell for t in trades]


def levels_from_trades(
    buys: Sequence[PricePoint], sells: Sequence[PricePoint]
) -> Dict[float, str]:
    """``priceLevelsFromTrades``: level per timestamp that is not NORMAL."""
    levels: Dict[float, str] = {}
    if not buys:
        return levels
    buy_sorted = sorted(buys, key=lambda p: p.value)
    sell_sorted = sorted(sells, key=lambda p: p.value)
    buy_mid = len(buy_sorted) // 2
    sell_mid = len(sell_sorted) // 2
    very_cheap = {p.ts for p in buy_sorted[:buy_mid]}
    cheap = {p.ts for p in buy_sorted[buy_mid:]}
    expensive = {p.ts for p in sell_sorted[:sell_mid]}
    very_expensive = {p.ts for p in sell_sorted[sell_mid:]}
    for ts in very_cheap | cheap | expensive | very_expensive:
        if ts in very_cheap:
            levels[ts] = "VERY_CHEAP"
        elif ts in cheap:
            levels[ts] = "CHEAP"
        elif ts in very_expensive:
            levels[ts] = "VERY_EXPENSIVE"
        elif ts in expensive:
            levels[ts] = "EXPENSIVE"
    return levels


def apply_thresholds(
    level: str,
    price_ore: float,
    very_cheap_below: Optional[float],
    very_expensive_above: Optional[float],
) -> str:
    """Own thresholds override, active only when both are set."""
    low = to_float(very_cheap_below)
    high = to_float(very_expensive_above)
    if low is None or high is None:
        return level
    if price_ore >= high:
        return "VERY_EXPENSIVE"
    if price_ore <= low:
        return "VERY_CHEAP"
    return "NORMAL"


@dataclass(frozen=True)
class PriceLevel:
    level: str
    price: float  # current hour, öre
    hour_start: datetime
    levels: Dict[str, str] = field(default_factory=dict)  # ISO hour -> level
    reason: str = ""


def compute_level(
    slots: Sequence[PriceSlot],
    now: datetime,
    time_window_h: float = DEFAULT_TIME_WINDOW_H,
    min_spread: float = DEFAULT_MIN_SPREAD,
    unit_factor: float = DEFAULT_UNIT_FACTOR,
    very_cheap_below: Optional[float] = None,
    very_expensive_above: Optional[float] = None,
) -> Optional[PriceLevel]:
    """Price level for the hour containing ``now``, or None without data."""
    hours = hourly_average(slots)
    current = slot_at(hours, now)
    if current is None:
        return None
    window = to_float(time_window_h)
    if window is None or window <= 0:
        window = DEFAULT_TIME_WINDOW_H
    spread = to_float(min_spread)
    if spread is None or spread < 0:
        spread = DEFAULT_MIN_SPREAD
    factor = to_float(unit_factor) or DEFAULT_UNIT_FACTOR
    points = [PricePoint(h.start.timestamp(), h.price * factor) for h in hours]
    buys, sells = optimize_trading(points, window, spread)
    by_ts = levels_from_trades(buys, sells)
    current_ts = current.start.timestamp()
    level = by_ts.get(current_ts, "NORMAL")
    price_now = current.price * factor
    reason = "optimiser"
    overridden = apply_thresholds(level, price_now, very_cheap_below, very_expensive_above)
    if to_float(very_cheap_below) is not None and to_float(very_expensive_above) is not None:
        reason = "own thresholds"
    level = overridden
    levels = {h.start.isoformat(): by_ts.get(h.start.timestamp(), "NORMAL") for h in hours}
    return PriceLevel(level, price_now, current.start, levels, reason)


# --------------------------------------------------------------------------
# Offset: priceAdjustCurve (heat)
# --------------------------------------------------------------------------


def compute_price(
    level: Optional[PriceLevel],
    offsets: Mapping[str, Any],
    now: datetime,
    guard_enabled: bool = False,
    temp_low: Optional[float] = 0.0,
    indoor: Optional[Reading] = None,
    setpoint: Optional[float] = None,
    sensor_timeout_min: Optional[float] = None,
) -> Decision:
    if level is None:
        return Decision.zero(msg("price.no_price"))
    offset = to_float(offsets.get(level.level))
    if offset is None:
        offset = 0.0
    details = {"level": level.level, "price": round(level.price, 2)}
    if level.level in GUARDED_LEVELS and guard_enabled and offset != 0:
        problem = usable(indoor, now, sensor_timeout_min)
        set_value = to_float(setpoint)
        if problem is not None or set_value is None:
            why = msg("indoor.sensor_%s" % problem) if problem else msg("indoor.setpoint_missing")
            return Decision.zero(msg("price.guard_needs", level=term("price_level", level.level), why=why), **details)
        assert indoor is not None
        limit = set_value + (to_float(temp_low) or 0.0)
        details.update({"indoor": float(indoor.value), "guard_limit": limit})
        if not float(indoor.value) > limit:
            return Decision.zero(
                msg("price.guard_blocked", level=term("price_level", level.level), indoor=float(indoor.value),
                    limit=limit),
                inputs_ok=True,
                **details,
            )
    reason = msg("price.level", level=term("price_level", level.level), source=term("price_source", level.reason))
    return Decision(offset, reason, True, details)


class PriceCache:
    """Last price list that covered the current time (``getPriceList``)."""

    def __init__(self) -> None:
        self._slots: List[PriceSlot] = []

    def update(self, slots: Sequence[PriceSlot], now: datetime) -> List[PriceSlot]:
        """Store ``slots`` if they cover ``now``; return what to use."""
        if slot_at(slots, now) is not None:
            self._slots = list(slots)
            return self._slots
        if slot_at(self._slots, now) is not None:
            return self._slots
        return list(slots)
