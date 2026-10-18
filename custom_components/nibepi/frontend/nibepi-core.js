// NibePi cards: entity resolution and data shaping. Pure functions, no DOM,
// so they run under node:test (tests/js). nibepi-cards.js renders them.
// Helpers that produce text take ``loc``: a language code ("sv", "en") or
// a locale context from localeOf(hass); without it, English and 24 h.
// nibepi-i18n.js is imported with this module's own ?v= query, like this
// module is by the cards, so all three update together.

const i18n = await import(new URL(`./nibepi-i18n.js${new URL(import.meta.url).search}`, import.meta.url).href);
const { clockText, hasKey, numberText, t: tr, toLoc } = i18n;

/** The text of ``key`` in the language of ``loc``. */
function t(loc, key, vars) {
  return tr(toLoc(loc).lang, key, vars);
}

/** The text of ``key``, or ``fallback`` for a key without a text (a state
 * the cards do not know yet). */
function tOr(loc, key, fallback) {
  return hasKey(key) ? t(loc, key) : fallback;
}

export const DOMAIN = "nibepi";
export const SYSTEMS = ["s1", "s2"];

// Translation keys of the integration's per-system entities. Their card key
// is "<key>_<system>"; shared entities use the translation key alone.
export const SYSTEM_KEYS = [
  "mode",
  "strategy",
  "indoor_enabled",
  "price_enabled",
  "weather_enabled",
  "rmu_feed",
  "planned_offset",
  "indoor_offset",
  "price_offset",
  "weather_offset",
  "pump_offset",
  "lease_state",
  "rmu_lease_state",
  "optimiser_plan",
  "house_model",
  "supply",
  "bt50",
];

export const COMPONENTS = ["indoor", "price", "weather"];

export const LEASES = [
  { key: "lease_state_s1", label: "Curve offset S1", register: 47011 },
  { key: "lease_state_s2", label: "Curve offset S2", register: 47010 },
  { key: "rmu_lease_state_s1", label: "RMU40 S1", register: 10060 },
  { key: "rmu_lease_state_s2", label: "RMU40 S2", register: 10160 },
  { key: "hw_comfort_lease_state", label: "Hot water comfort", register: 47041 },
  { key: "hw_period_lease_state", label: "Hot water period", register: 47134 },
  { key: "hw_temp_lux_lease_state", label: "Temporary lux", register: 48132 },
  { key: "hw_lux_stop_lease_state", label: "Luxury stop temperature", register: 47047 },
];

export const HW_LEASES = LEASES.slice(4);

/** A lease's label in the language of ``loc``. */
export function leaseLabel(lease, loc) {
  return t(loc, `lease.${lease.key}`);
}

const MISSING = new Set(["unknown", "unavailable", "none", ""]);

// --------------------------------------------------------------------------
// Entity resolution
// --------------------------------------------------------------------------

/** The integration's Gateway entities: [{entity_id, device_id, entries, name}]. */
export function gatewayEntities(hass) {
  const found = [];
  for (const entry of Object.values(hass?.entities || {})) {
    if (entry.platform !== DOMAIN || entry.translation_key !== "gateway_online") continue;
    const device = entry.device_id ? hass.devices?.[entry.device_id] : undefined;
    found.push({
      entity_id: entry.entity_id,
      device_id: entry.device_id || null,
      entries: device?.config_entries || [],
      name: device?.name_by_user || device?.name || entry.entity_id,
    });
  }
  return found.sort((a, b) => a.entity_id.localeCompare(b.entity_id));
}

/** The NibePi config entries with a Gateway entity: [{entry_id, name}]. */
export function nibepiEntries(hass) {
  const seen = new Map();
  for (const gw of gatewayEntities(hass)) {
    for (const id of gw.entries) if (!seen.has(id)) seen.set(id, { entry_id: id, name: gw.name });
  }
  return [...seen.values()];
}

/** Card keys from the entity registry alone: the fallback when the Gateway
 * entity's map is missing (disabled, or not written yet). Per-system
 * entities are told apart by the "_s1" / "_s2" their default entity id
 * ends with, so a renamed one may be missed. */
export function registryIds(hass, deviceIds) {
  const ids = {};
  for (const entry of Object.values(hass?.entities || {})) {
    if (entry.platform !== DOMAIN || !entry.translation_key) continue;
    if (deviceIds && !deviceIds.has(entry.device_id)) continue;
    const key = entry.translation_key;
    if (SYSTEM_KEYS.includes(key)) {
      const match = /_(s[12])(?:_\d+)?$/.exec(entry.entity_id);
      if (match) ids[`${key}_${match[1]}`] = entry.entity_id;
    } else if (!(key in ids)) {
      ids[key] = entry.entity_id;
    }
  }
  // The indoor sensor the integration uses is only in the map; BT50 is
  // the default one.
  for (const s of SYSTEMS) {
    if (!ids[`indoor_${s}`] && ids[`bt50_${s}`]) ids[`indoor_${s}`] = ids[`bt50_${s}`];
  }
  return ids;
}

/**
 * Card key -> entity id for the configured entry.
 * Returns {ids, systems, gateway, problem}; problem is null, "no_entry"
 * (no NibePi Gateway entity found), "unknown_entry" (entry_id matches
 * none) or "several_entries" (more than one and no entry_id: the first is
 * used).
 */
export function resolveEntities(hass, config = {}) {
  let gateways = gatewayEntities(hass);
  let problem = null;
  if (config.entry_id) {
    gateways = gateways.filter((gw) => gw.entries.includes(config.entry_id));
    if (!gateways.length) problem = "unknown_entry";
  } else if (gateways.length > 1) {
    problem = "several_entries";
  }
  const gw = gateways[0];
  if (!gw) {
    const ids = config.entry_id ? {} : registryIds(hass);
    return { ids, systems: systemsIn(ids), gateway: null, problem: problem || "no_entry" };
  }
  const state = hass.states?.[gw.entity_id];
  const map = state?.attributes?.entities;
  let ids;
  if (map && typeof map === "object") {
    ids = { ...map };
  } else {
    ids = registryIds(hass, gw.device_id ? new Set([gw.device_id]) : undefined);
  }
  ids.gateway_online = gw.entity_id;
  const systems = Array.isArray(state?.attributes?.systems) ? state.attributes.systems : systemsIn(ids);
  return { ids, systems, gateway: gw.entity_id, problem };
}

/** The systems with at least one per-system entity. */
export function systemsIn(ids) {
  return SYSTEMS.filter((s) => SYSTEM_KEYS.some((k) => `${k}_${s}` in ids));
}

export function sysKey(key, system) {
  return `${key}_${system}`;
}

/** The state object of a card key, or undefined. */
export function stateOf(hass, ids, key) {
  const id = ids?.[key];
  return id ? hass?.states?.[id] : undefined;
}

export function isMissing(stateObj) {
  return !stateObj || MISSING.has(String(stateObj.state));
}

/** A numeric state, or null. */
export function num(stateObj) {
  if (isMissing(stateObj)) return null;
  const value = Number(stateObj.state);
  return Number.isFinite(value) ? value : null;
}

export function toNumber(value) {
  if (value === null || value === undefined || value === "") return null;
  const n = Number(value);
  return Number.isFinite(n) ? n : null;
}

// --------------------------------------------------------------------------
// Formatting
// --------------------------------------------------------------------------

/** "21.5" ("21,5" in Swedish), "–" without a number. */
export function formatNumber(value, digits = 1, loc) {
  const n = toNumber(value);
  return n === null ? "–" : numberText(n, digits, loc);
}

/** "+2", "−1.5" ("−1,5"), "0". */
export function formatSigned(value, digits = 0, loc) {
  const n = toNumber(value);
  if (n === null) return "–";
  const abs = Math.abs(n);
  if (Number(abs.toFixed(digits)) === 0) return numberText(0, digits, loc);
  return (n > 0 ? "+" : "−") + numberText(abs, digits, loc);
}

/** Milliseconds as "45 s", "4 min 05 s", "1 h 02 min", "2 d 3 h";
 * ``coarse`` drops the seconds from a minute on ("4 min"). */
export function formatDuration(ms, coarse = false) {
  if (ms === null || ms === undefined || !Number.isFinite(ms)) return "";
  const total = Math.max(0, Math.floor(ms / 1000));
  const d = Math.floor(total / 86400);
  const h = Math.floor((total % 86400) / 3600);
  const m = Math.floor((total % 3600) / 60);
  const s = total % 60;
  if (d > 0) return `${d} d ${h} h`;
  if (h > 0) return `${h} h ${String(m).padStart(2, "0")} min`;
  if (m > 0) return coarse ? `${m} min` : `${m} min ${String(s).padStart(2, "0")} s`;
  return `${s} s`;
}

/** An ISO string, epoch milliseconds or epoch seconds as epoch ms, or null. */
export function parseTime(value) {
  if (value === null || value === undefined || value === "") return null;
  if (typeof value === "number") return value > 1e11 ? value : value * 1000;
  const t = Date.parse(value);
  return Number.isFinite(t) ? t : null;
}

/** Time left until ``until``: {ms, text, expired}; null without a time. */
export function countdown(until, now, loc) {
  const at = parseTime(until);
  if (at === null) return null;
  const ms = at - now;
  return { ms, expired: ms <= 0, text: ms <= 0 ? t(loc, "time.expired") : formatDuration(ms) };
}

/** "12 s ago" ("för 12 s sedan"), or "" without a time. */
export function formatAge(since, now, coarse = false, loc) {
  const at = parseTime(since);
  if (at === null) return "";
  const ms = now - at;
  return ms < 0 ? t(loc, "time.just_now") : t(loc, "time.ago", { duration: formatDuration(ms, coarse) });
}

/** "in 2 h" ("om 2 h") until ``until``, or "" without a time. */
export function formatIn(until, now, coarse = true, loc) {
  const at = parseTime(until);
  if (at === null) return "";
  const ms = at - now;
  return ms <= 0 ? t(loc, "time.just_now") : t(loc, "time.in", { duration: formatDuration(ms, coarse) });
}

/** "14:05", or in the locale's (or HA's) 12/24 h format with ``loc``. */
export function formatClock(time, loc) {
  return clockText(time, loc);
}

// --------------------------------------------------------------------------
// Colours (Home Assistant theme variables)
// --------------------------------------------------------------------------

export const PRICE_COLORS = {
  very_cheap: { color: "var(--success-color, #43a047)", opacity: 0.28 },
  cheap: { color: "var(--success-color, #43a047)", opacity: 0.14 },
  normal: { color: "var(--divider-color, #e0e0e0)", opacity: 0 },
  expensive: { color: "var(--warning-color, #ffa600)", opacity: 0.18 },
  very_expensive: { color: "var(--error-color, #db4437)", opacity: 0.22 },
};

export function leaseColor(state) {
  switch (state) {
    case "active":
      return "var(--success-color, #43a047)";
    case "grace":
    case "restoring":
      return "var(--warning-color, #ffa600)";
    case "error":
      return "var(--error-color, #db4437)";
    default:
      return "var(--disabled-color, #bdbdbd)";
  }
}

// --------------------------------------------------------------------------
// Optimiser plan -> chart
// --------------------------------------------------------------------------

function hmMinutes(text) {
  const match = /^(\d{1,2}):(\d{2})/.exec(String(text ?? ""));
  return match ? Number(match[1]) * 60 + Number(match[2]) : null;
}

/** The latest local time at "HH:MM" not after ``ref`` (+1 min slack). */
function anchorBefore(minutes, ref) {
  const d = new Date(ref + 60000);
  d.setHours(Math.floor(minutes / 60), minutes % 60, 0, 0);
  let t = d.getTime();
  if (t > ref + 60000) t -= 86400000;
  return t;
}

/**
 * Plan rows ``[HH:MM, offset, indoor, ISO start?, band lower?, band upper?]``
 * as sorted points {t, offset, tin, lower, upper}. Rows without the ISO
 * start are placed from their local "HH:MM": the first one is the step
 * running now, later ones follow it. The band is per row when Away returns
 * within the plan (null in older rows: the comfort attribute's band).
 */
export function planPoints(rows, now) {
  const points = [];
  let prev = null;
  for (const row of Array.isArray(rows) ? rows : []) {
    if (!Array.isArray(row)) continue;
    let t = parseTime(row[3]);
    if (t === null) {
      const minutes = hmMinutes(row[0]);
      if (minutes === null) continue;
      if (prev === null) {
        t = anchorBefore(minutes, now);
      } else {
        const prevMinutes = new Date(prev).getHours() * 60 + new Date(prev).getMinutes();
        t = prev + (((minutes - prevMinutes) % 1440) + 1440) % 1440 * 60000;
      }
    }
    prev = t;
    points.push({ t, offset: toNumber(row[1]), tin: toNumber(row[2]), lower: toNumber(row[4]), upper: toNumber(row[5]) });
  }
  return points.sort((a, b) => a.t - b.t);
}

/** Price levels {ISO hour: LEVEL} as merged slots {start, end, level} in
 * [start, end); a slot lasts until the next one (an hour for the last). */
export function priceSlots(levels, start, end) {
  if (!levels || typeof levels !== "object") return [];
  const items = Object.entries(levels)
    .map(([k, v]) => ({ t: parseTime(k), level: String(v).toLowerCase() }))
    .filter((x) => x.t !== null)
    .sort((a, b) => a.t - b.t);
  const slots = [];
  items.forEach((item, i) => {
    const slotEnd = i + 1 < items.length ? items[i + 1].t : item.t + 3600000;
    const s = Math.max(item.t, start);
    const e = Math.min(slotEnd, end);
    if (e <= s) return;
    const last = slots[slots.length - 1];
    if (last && last.level === item.level && last.end === s) last.end = e;
    else slots.push({ start: s, end: e, level: item.level });
  });
  return slots;
}

function niceRange(lo, hi, pad, minSpan) {
  let a = lo - pad;
  let b = hi + pad;
  if (b - a < minSpan) {
    const mid = (a + b) / 2;
    a = mid - minSpan / 2;
    b = mid + minSpan / 2;
  }
  return [a, b];
}

const r1 = (v) => Math.round(v * 10) / 10;

/**
 * The plan chart's geometry in a ``width`` x ``height`` viewBox.
 * Input: {rows, levels, comfort {lower, upper, target}, now, hours, width,
 * height}. Returns {empty} when no step of the plan is in the window; else
 * the paths, bands, ticks and one hover segment per step.
 */
export function planChart({ rows, levels, comfort, now, hours = 24, width = 600, height = 220, loc, clock = (time) => formatClock(time, loc) }) {
  const start = now;
  const end = now + hours * 3600000;
  const points = planPoints(rows, now);
  const steps = [];
  points.forEach((p, i) => {
    const stepEnd = i + 1 < points.length ? points[i + 1].t : end;
    const s = Math.max(p.t, start);
    const e = Math.min(stepEnd, end);
    if (e > s) steps.push({ start: s, end: e, offset: p.offset, tin: p.tin, lower: p.lower, upper: p.upper });
  });
  if (!steps.length) return { empty: true, width, height };

  const plot = { x0: 34, x1: width - 30, y0: 10, y1: height - 22 };
  const x = (t) => r1(plot.x0 + ((t - start) / (end - start)) * (plot.x1 - plot.x0));

  // Indoor (left axis) with the comfort band; offset (right axis).
  const temps = steps.map((s) => s.tin).filter((v) => v !== null);
  const lower = toNumber(comfort?.lower);
  const upper = toNumber(comfort?.upper);
  // The band per step: the row's own (Away's return), else the comfort attribute's.
  for (const s of steps) {
    if (s.lower === null || s.upper === null) {
      s.lower = lower;
      s.upper = upper;
    }
  }
  const bandEdges = steps.flatMap((s) => [s.lower, s.upper]).filter((v) => v !== null);
  const bounds = [...temps, ...bandEdges];
  const [tLo, tHi] = bounds.length ? niceRange(Math.min(...bounds), Math.max(...bounds), 0.3, 2) : [19, 22];
  const yT = (v) => r1(plot.y1 - ((v - tLo) / (tHi - tLo)) * (plot.y1 - plot.y0));
  const offsets = steps.map((s) => s.offset).filter((v) => v !== null);
  const oLo = Math.floor(Math.min(0, ...offsets)) - 1;
  const oHi = Math.ceil(Math.max(0, ...offsets)) + 1;
  const yO = (v) => r1(plot.y1 - ((v - oLo) / (oHi - oLo)) * (plot.y1 - plot.y0));

  let offsetPath = "";
  for (const s of steps) {
    if (s.offset === null) continue;
    offsetPath += offsetPath ? ` V${yO(s.offset)} H${x(s.end)}` : `M${x(s.start)} ${yO(s.offset)} H${x(s.end)}`;
  }
  let indoorPath = "";
  for (const s of steps) {
    if (s.tin === null) continue;
    indoorPath += `${indoorPath ? " L" : "M"}${x(s.start)} ${yT(s.tin)}`;
  }
  const lastStep = steps[steps.length - 1];
  if (indoorPath && lastStep.tin !== null) indoorPath += ` L${x(lastStep.end)} ${yT(lastStep.tin)}`;

  // Comfort band rectangles, one per run of steps with the same band.
  const bands = [];
  for (const s of steps) {
    if (s.lower === null || s.upper === null || !(s.upper > s.lower)) continue;
    const last = bands[bands.length - 1];
    if (last && last.lower === s.lower && last.upper === s.upper && last.end === s.start) {
      last.end = s.end;
    } else {
      bands.push({ start: s.start, end: s.end, lower: s.lower, upper: s.upper });
    }
  }
  for (const b of bands) {
    b.x = x(b.start);
    b.w = r1(x(b.end) - x(b.start));
    b.y = yT(b.upper);
    b.h = r1(yT(b.lower) - yT(b.upper));
  }
  const band = bands[0] || null;

  const priceBands = priceSlots(levels, start, end).map((slot) => ({
    x: x(slot.start),
    w: r1(x(slot.end) - x(slot.start)),
    level: slot.level,
    label: `${clock(slot.start)}–${clock(slot.end)} ${tOr(loc, `price.${slot.level}`, slot.level.replace(/_/g, " "))}`,
  }));

  const tempTicks = [];
  const tStep = tHi - tLo > 4 ? 1 : 0.5;
  for (let v = Math.ceil(tLo / tStep) * tStep; v <= tHi + 1e-9; v += tStep) {
    tempTicks.push({ y: yT(v), label: formatNumber(v, tStep < 1 ? 1 : 0, loc) });
  }
  const offsetTicks = [];
  const oStep = oHi - oLo > 8 ? 2 : 1;
  for (let v = Math.ceil(oLo / oStep) * oStep; v <= oHi; v += oStep) {
    offsetTicks.push({ y: yO(v), label: formatSigned(v, 0, loc) });
  }
  const timeTicks = [];
  const every = hours > 24 || width < 420 ? 6 : 3;
  const first = new Date(start);
  first.setMinutes(0, 0, 0);
  for (let at = first.getTime() + 3600000; at < end; at += 3600000) {
    if (new Date(at).getHours() % every === 0) timeTicks.push({ x: x(at), label: clock(at) });
  }

  return {
    empty: false,
    width,
    height,
    plot,
    offsetPath,
    indoorPath,
    band,
    bands,
    zeroY: yO(0),
    priceBands,
    nowX: x(now),
    tempTicks,
    offsetTicks,
    timeTicks,
    segments: steps.map((s) => ({
      x: x(s.start),
      w: r1(x(s.end) - x(s.start)),
      label: t(loc, "plan.segment", { from: clock(s.start), to: clock(s.end), offset: formatSigned(s.offset, 0, loc), indoor: formatNumber(s.tin, 1, loc) }),
    })),
  };
}

// --------------------------------------------------------------------------
// Hot water
// --------------------------------------------------------------------------

/** Planned hot water windows in [now, now + hours] as fractions of the
 * strip: VV-AI windows (plan_24h, ISO times), the optimiser's charge
 * blocks (start_at / end_at) and a planned or running legionella cycle
 * ({start, end, temperature} in ms, from legionellaModel().window). */
export function hotwaterWindows({ vvPlan, blocks, legionella, now, hours = 24, loc }) {
  const clock = (time) => (time === null ? "" : formatClock(time, loc));
  const end = now + hours * 3600000;
  const span = end - now;
  const out = [];
  const add = (s, e, kind, label) => {
    const a = Math.max(s, now);
    const b = Math.min(e, end);
    if (s === null || e === null || b <= a) return;
    out.push({ kind, start: a, end: b, from: (a - now) / span, to: (b - now) / span, label });
  };
  for (const w of Array.isArray(vvPlan) ? vvPlan : []) {
    const s = parseTime(w?.start);
    const e = parseTime(w?.end);
    const mode = w?.mode ? tOr(loc, `hwmode.${w.mode}`, w.mode) : "";
    add(s, e, "vv", t(loc, "hw.window_vv", { mode, from: clock(s), to: clock(e) }).replace(/\s+/g, " ").trim());
  }
  for (const b of Array.isArray(blocks) ? blocks : []) {
    if (b?.action !== "charge") continue;
    const s = parseTime(b.start_at);
    const e = parseTime(b.end_at);
    const vars = { from: clock(s), to: clock(e), temperature: formatNumber(b.target, Number.isInteger(toNumber(b.target)) ? 0 : 1, loc) };
    const text = t(loc, b.target ? "hw.window_charge_to" : "hw.window_charge", vars);
    // The reason is the optimiser's (English) text.
    add(s, e, "charge", `${text}${b.reason ? `: ${b.reason}` : ""}`);
  }
  if (legionella && legionella.start !== null && legionella.end !== null) {
    const known = legionella.temperature !== null && legionella.temperature !== undefined;
    const vars = { from: clock(legionella.start), to: clock(legionella.end), temperature: formatNumber(legionella.temperature, 1, loc) };
    add(legionella.start, legionella.end, "legionella", t(loc, known ? "hw.window_legionella_to" : "hw.window_legionella", vars));
  }
  return out.sort((a, b) => a.start - b.start);
}

/** The legionella guard from its two sensors (Legionella next due, with
 * the plan in its attributes, and Legionella last done): null without
 * them, else {state, running, lastDone, lastBy, due, temperature,
 * temperatureSource, intervalDays, pumpPeriodic, window, lastResult,
 * reason}. ``window`` ({start, end, temperature}, ms) only while a cycle
 * is planned or running. */
export function legionellaModel(nextDue, lastDone) {
  if (!nextDue && !lastDone) return null;
  const a = nextDue?.attributes || {};
  const state = a.state || null;
  const start = parseTime(a.planned_start);
  const end = parseTime(a.planned_end);
  const temperature = toNumber(a.temperature);
  const planned = state === "planned" || state === "running";
  return {
    state,
    running: state === "running",
    lastDone: isMissing(lastDone) ? null : parseTime(lastDone.state),
    lastBy: lastDone?.attributes?.by || null,
    due: isMissing(nextDue) ? null : parseTime(nextDue.state),
    temperature,
    temperatureSource: a.temperature_source || null,
    intervalDays: toNumber(a.interval_days),
    pumpPeriodic: a.pump_periodic === true,
    window: planned && start !== null && end !== null && end > start ? { start, end, temperature } : null,
    lastResult: a.last_result || null,
    reason: a.reason || null,
  };
}

// --------------------------------------------------------------------------
// Climate
// --------------------------------------------------------------------------

/** The bars under the offset: the rules' components, or the optimiser's
 * offset against the rules' total. {key, label, value, reason, frac}. */
export function contributions(planned, optimiserOffset, loc) {
  const attrs = planned?.attributes || {};
  const source = attrs.source || "rules";
  let items;
  if (source !== "rules") {
    items = [
      {
        key: "optimiser",
        label: t(loc, source === "comfort_floor" ? "part.comfort_floor" : "part.optimiser"),
        value: toNumber(optimiserOffset ?? attrs.total),
        reason: attrs.strategy_reason || "",
      },
      { key: "rules", label: t(loc, "part.rules"), value: toNumber(attrs.rules_total), reason: "" },
    ];
  } else {
    const components = attrs.components || {};
    const reasons = attrs.reasons || {};
    const keys = "away" in components ? [...COMPONENTS, "away"] : COMPONENTS;
    items = keys.map((c) => ({
      key: c,
      label: t(loc, `part.${c}`),
      value: toNumber(components[c]),
      reason: reasons[c] || "",
    }));
  }
  const maxAbs = Math.max(3, ...items.map((i) => Math.abs(i.value ?? 0)));
  return items.map((i) => ({ ...i, frac: i.value === null ? 0 : Math.max(-1, Math.min(1, i.value / maxAbs)) }));
}

/** Alerts for a system's climate card: [{level, text, key}]. */
export function climateAlerts(hass, ids, system, loc) {
  const alerts = [];
  const st = (key) => stateOf(hass, ids, key);
  const gw = st("gateway_online");
  if (gw && (gw.state === "off" || gw.state === "unavailable")) {
    alerts.push({ level: "error", text: t(loc, "alert.gateway_offline"), key: "gateway_online" });
  }
  const lease = st(sysKey("lease_state", system));
  if (lease?.state === "error") {
    const err = lease.attributes?.error;
    alerts.push({ level: "error", text: err ? t(loc, "alert.lease_error_detail", { error: err }) : t(loc, "alert.lease_error"), key: sysKey("lease_state", system) });
  }
  const strategy = st(sysKey("strategy", system));
  const model = st(sysKey("house_model", system));
  if (strategy?.state === "optimiser" && model && ["prior", "low"].includes(model.state)) {
    alerts.push({
      level: "warning",
      text: t(loc, "alert.model_confidence", { confidence: tOr(loc, `model.${model.state}`, model.state) }),
      key: sysKey("house_model", system),
    });
  }
  const away = awayAlert(hass, ids, sysKey("mode", system), t(loc, "alert.heating_mode", { system: system.toUpperCase() }), loc);
  if (away) alerts.push(away);
  const feed = st(sysKey("rmu_feed", system));
  const rmu = st(sysKey("rmu_lease_state", system));
  if (feed?.state === "on" && !["active", "grace"].includes(rmu?.state)) {
    alerts.push({
      level: "warning",
      text: t(loc, "alert.rmu_lease", { state: rmu?.state ? tOr(loc, `lease.state.${rmu.state}`, rmu.state) : t(loc, "state.missing") }),
      key: sysKey("rmu_lease_state", system),
    });
  }
  return alerts;
}

// --------------------------------------------------------------------------
// Away
// --------------------------------------------------------------------------

/** The Away switch for the cards: null without the entity, else {on,
 * entity, until (ms or null), untilText, setback, hotWater, targets}. */
export function awayModel(hass, ids, now, loc) {
  const sw = stateOf(hass, ids, "away");
  if (!sw) return null;
  const a = sw.attributes || {};
  const on = sw.state === "on";
  const untilState = stateOf(hass, ids, "away_until");
  const until = on ? parseTime(a.until ?? (isMissing(untilState) ? null : untilState.state)) : null;
  return {
    on,
    entity: sw.entity_id || ids.away,
    available: !isMissing(sw),
    until,
    countdown: until !== null ? countdown(until, now, loc) : null,
    setback: toNumber(a.setback),
    hotWater: a.hot_water || null,
    targets: a.targets && typeof a.targets === "object" ? a.targets : {},
  };
}

/** A warning when Away is on but the mode select ``modeKey`` does not let
 * anything be written: {level, text, key}, or null. */
export function awayAlert(hass, ids, modeKey, label, loc) {
  const sw = stateOf(hass, ids, "away");
  if (sw?.state !== "on") return null;
  const mode = stateOf(hass, ids, modeKey);
  if (!mode || mode.state === "active") return null;
  return {
    level: "warning",
    text: t(loc, "alert.away_mode", { mode: label, state: tOr(loc, `mode.${mode.state}`, mode.state) }),
    key: modeKey,
  };
}

/** Alerts for the hot water card. */
export function hotwaterAlerts(hass, ids, loc) {
  const sw = stateOf(hass, ids, "away");
  if (sw?.attributes?.hot_water === "normal") return [];
  const away = awayAlert(hass, ids, "hw_mode", t(loc, "alert.hot_water_mode"), loc);
  return away ? [away] : [];
}

// --------------------------------------------------------------------------
// Gateway and leases
// --------------------------------------------------------------------------

/** "Gateway v2.0.0 (build 20261017-87eb233) · Integration v0.6.0" from the
 * Gateway version sensor, falling back to the Gateway entity's version
 * attribute; the parts that are known, "" without any. */
export function versionLine(versionState, gatewayState, loc) {
  const a = versionState?.attributes || {};
  const gw = !isMissing(versionState) ? versionState.state : gatewayState?.attributes?.version || null;
  const parts = [];
  if (gw) parts.push(t(loc, a.build ? "gw.version_build" : "gw.version_gateway", { version: gw, build: a.build }));
  if (a.integration_version) parts.push(t(loc, "gw.version_integration", { version: a.integration_version }));
  return parts.join(" · ");
}

/** One row per lease entity present: {key, label, register, state,
 * countdown, error, value}. */
export function leaseRows(hass, ids, now, leases = LEASES, loc) {
  const rows = [];
  for (const lease of leases) {
    const s = stateOf(hass, ids, lease.key);
    if (!s) continue;
    const a = s.attributes || {};
    rows.push({
      key: lease.key,
      label: leaseLabel(lease, loc),
      register: a.register ?? lease.register,
      state: s.state,
      countdown: s.state === "active" || s.state === "grace" ? countdown(a.expires, now, loc) : null,
      error: a.error || null,
      value: a.value ?? a.ha_value ?? null,
    });
  }
  return rows;
}

// --------------------------------------------------------------------------
// Savings
// --------------------------------------------------------------------------

/** Daily totals from recorder/statistics_during_period (period "day",
 * types ["change"]): [{start, value}]. */
export function dailyValues(result, statisticId) {
  const rows = result?.[statisticId];
  if (!Array.isArray(rows)) return [];
  return rows
    .map((r) => ({ start: parseTime(r.start), value: toNumber(r.change) }))
    .filter((r) => r.start !== null && r.value !== null)
    .sort((a, b) => a.start - b.start);
}

/** A sparkline path through ``values`` in a ``width`` x ``height`` box;
 * "" with fewer than two values. */
export function sparkline(values, width = 120, height = 32) {
  const vs = values.filter((v) => Number.isFinite(v));
  if (vs.length < 2) return "";
  const lo = Math.min(...vs);
  const hi = Math.max(...vs);
  const span = hi - lo || 1;
  return vs
    .map((v, i) => {
      const px = r1((i / (vs.length - 1)) * width);
      const py = r1(height - 2 - ((v - lo) / span) * (height - 4));
      return `${i ? "L" : "M"}${px} ${py}`;
    })
    .join(" ");
}

/** The savings card's figures from the cost and savings entities. */
export function savingsModel(cost, savings, loc) {
  const ca = cost?.attributes || {};
  const sa = savings?.attributes || {};
  const y = ca.yesterday && typeof ca.yesterday === "object" ? ca.yesterday : null;
  return {
    currency: ca.unit_of_measurement || sa.unit_of_measurement || "",
    costToday: num(cost),
    savingsToday: num(savings),
    predictedCost: toNumber(ca.predicted_cost),
    gapPct: toNumber(ca.cost_vs_predicted_pct),
    hoursMeasured: toNumber(ca.hours_measured),
    savingsParts: Object.entries(sa)
      .filter(([k, v]) => k !== "day" && k !== "unit_of_measurement" && typeof v === "number")
      .filter(([k]) => !["state_class", "last_reset", "device_class", "friendly_name", "icon"].includes(k))
      .map(([k, v]) => ({ key: k, label: k === "hot_water" ? t(loc, "sav.hot_water") : k.toUpperCase(), value: v })),
    yesterday: y
      ? { cost: toNumber(y.cost), predictedCost: toNumber(y.predicted_cost), gapPct: toNumber(y.cost_vs_predicted_pct), day: y.day || null }
      : null,
  };
}
