import test from "node:test";
import assert from "node:assert/strict";

import {
  climateAlerts,
  contributions,
  countdown,
  dailyValues,
  formatAge,
  formatDuration,
  formatSigned,
  hotwaterWindows,
  leaseRows,
  nibepiEntries,
  planChart,
  planPoints,
  priceSlots,
  registryIds,
  resolveEntities,
  savingsModel,
  sparkline,
} from "../../custom_components/nibepi/frontend/nibepi-core.js";

const H = 3600000;
const NOW = Date.parse("2026-10-12T10:05:00Z");
const iso = (t) => new Date(t).toISOString();

function reg(entity_id, translation_key, device_id = "dev1", platform = "nibepi") {
  return { entity_id, translation_key, device_id, platform };
}

function hassWith({ entities = [], states = {}, devices } = {}) {
  return {
    entities: Object.fromEntries(entities.map((e) => [e.entity_id, e])),
    devices: devices || { dev1: { name: "Nibe F1255", config_entries: ["entry1"] } },
    states,
  };
}

test("resolveEntities reads the Gateway entity's map", () => {
  const hass = hassWith({
    entities: [reg("binary_sensor.nibe_gateway", "gateway_online"), reg("sensor.other", "x", "devX", "mqtt")],
    states: {
      "binary_sensor.nibe_gateway": {
        state: "on",
        attributes: { systems: ["s1"], entities: { planned_offset_s1: "sensor.renamed", outdoor: "sensor.bt1" } },
      },
    },
  });
  const r = resolveEntities(hass, {});
  assert.equal(r.problem, null);
  assert.equal(r.gateway, "binary_sensor.nibe_gateway");
  assert.deepEqual(r.systems, ["s1"]);
  assert.equal(r.ids.planned_offset_s1, "sensor.renamed");
  assert.equal(r.ids.outdoor, "sensor.bt1");
  assert.equal(r.ids.gateway_online, "binary_sensor.nibe_gateway");
});

test("resolveEntities falls back to the registry without the map", () => {
  const hass = hassWith({
    entities: [
      reg("binary_sensor.nibe_gateway", "gateway_online"),
      reg("select.nibe_mode_s1", "mode"),
      reg("select.nibe_mode_s2", "mode"),
      reg("sensor.nibe_price_level", "price_level"),
      reg("sensor.foreign_mode_s1", "mode", "dev2"),
    ],
    states: { "binary_sensor.nibe_gateway": { state: "on", attributes: {} } },
  });
  const r = resolveEntities(hass, {});
  assert.equal(r.ids.mode_s1, "select.nibe_mode_s1");
  assert.equal(r.ids.mode_s2, "select.nibe_mode_s2");
  assert.equal(r.ids.price_level, "sensor.nibe_price_level");
  assert.deepEqual(r.systems, ["s1", "s2"]);
});

test("resolveEntities chooses the entry and reports ambiguity", () => {
  const hass = hassWith({
    entities: [reg("binary_sensor.a_gateway", "gateway_online", "dev1"), reg("binary_sensor.b_gateway", "gateway_online", "dev2")],
    devices: { dev1: { name: "A", config_entries: ["e1"] }, dev2: { name: "B", name_by_user: "Barn", config_entries: ["e2"] } },
    states: {},
  });
  assert.equal(resolveEntities(hass, {}).problem, "several_entries");
  assert.equal(resolveEntities(hass, {}).gateway, "binary_sensor.a_gateway");
  const r = resolveEntities(hass, { entry_id: "e2" });
  assert.equal(r.problem, null);
  assert.equal(r.gateway, "binary_sensor.b_gateway");
  assert.equal(resolveEntities(hass, { entry_id: "nope" }).problem, "unknown_entry");
  assert.deepEqual(nibepiEntries(hass), [
    { entry_id: "e1", name: "A" },
    { entry_id: "e2", name: "Barn" },
  ]);
});

test("resolveEntities without any NibePi entity", () => {
  const r = resolveEntities(hassWith(), {});
  assert.equal(r.problem, "no_entry");
  assert.deepEqual(r.systems, []);
  assert.deepEqual(registryIds({}), {});
});

test("durations, countdowns and signs", () => {
  assert.equal(formatDuration(45000), "45 s");
  assert.equal(formatDuration(245000), "4 min 05 s");
  assert.equal(formatDuration(3720000), "1 h 02 min");
  assert.equal(formatDuration(2 * 86400000 + 3 * H), "2 d 3 h");
  assert.equal(formatDuration(null), "");
  assert.deepEqual(countdown(iso(NOW + 90000), NOW), { ms: 90000, expired: false, text: "1 min 30 s" });
  assert.equal(countdown(iso(NOW - 1000), NOW).text, "expired");
  assert.equal(countdown(null, NOW), null);
  assert.equal(countdown((NOW + 5000) / 1000, NOW).text, "5 s"); // epoch seconds
  assert.equal(formatAge(NOW - 12000, NOW), "12 s ago"); // epoch ms
  assert.equal(formatSigned(2), "+2");
  assert.equal(formatSigned(-1.5, 1), "−1.5");
  assert.equal(formatSigned(-0.01, 1), "0.0");
  assert.equal(formatSigned(null), "–");
});

test("plan points use the ISO start, else HH:MM after the first row", () => {
  const rows = [
    ["12:00", 1, 21.0, iso(NOW - 5 * 60000)],
    ["13:00", -2, 21.2, iso(NOW + 55 * 60000)],
  ];
  const pts = planPoints(rows, NOW);
  assert.equal(pts.length, 2);
  assert.equal(pts[1].t - pts[0].t, H);
  assert.equal(pts[1].offset, -2);

  // Without ISO: the first row is the running step, the rest follow it,
  // across midnight.
  const hm = (date) => `${String(date.getHours()).padStart(2, "0")}:${String(date.getMinutes()).padStart(2, "0")}`;
  const first = new Date(NOW - 5 * 60000);
  const legacy = planPoints([[hm(first), 0, 20], [hm(new Date(first.getTime() + 15 * H)), 1, 20]], NOW);
  assert.ok(legacy[0].t <= NOW + 60000 && NOW - legacy[0].t < 86400000);
  assert.equal(legacy[1].t - legacy[0].t, 15 * H);
  assert.equal(planPoints(null, NOW).length, 0);
});

test("price slots merge and clip", () => {
  const levels = {
    [iso(NOW - H)]: "CHEAP",
    [iso(NOW + H)]: "CHEAP",
    [iso(NOW + 2 * H)]: "EXPENSIVE",
  };
  const slots = priceSlots(levels, NOW, NOW + 24 * H);
  assert.deepEqual(slots, [
    { start: NOW, end: NOW + 2 * H, level: "cheap" },
    { start: NOW + 2 * H, end: NOW + 3 * H, level: "expensive" },
  ]);
  assert.deepEqual(priceSlots(undefined, 0, 1), []);
});

test("plan chart geometry", () => {
  const rows = [
    ["", 0, 20.5, iso(NOW - 5 * 60000)],
    ["", 2, 20.8, iso(NOW + 6 * H)],
    ["", -1, 21.4, iso(NOW + 12 * H)],
  ];
  const chart = planChart({ rows, levels: { [iso(NOW)]: "VERY_CHEAP" }, comfort: { lower: 20, upper: 21.5 }, now: NOW, hours: 24, width: 600, height: 220 });
  assert.equal(chart.empty, false);
  assert.equal(chart.nowX, chart.plot.x0);
  assert.equal(chart.segments.length, 3);
  // Steps tile the plot from now to the horizon.
  assert.equal(chart.segments[0].x, chart.plot.x0);
  const last = chart.segments[2];
  assert.equal(Math.round((last.x + last.w) * 10) / 10, chart.plot.x1);
  assert.equal(Math.round(chart.segments[1].x), Math.round(chart.plot.x0 + (chart.plot.x1 - chart.plot.x0) / 4));
  assert.match(chart.offsetPath, /^M34 [\d.]+ H/);
  assert.match(chart.indoorPath, /^M34 /);
  assert.ok(chart.band && chart.band.h > 0);
  assert.equal(chart.priceBands.length, 1);
  assert.equal(chart.priceBands[0].level, "very_cheap");
  assert.ok(chart.timeTicks.length >= 7);
  assert.ok(chart.offsetTicks.some((t) => t.label === "+2"));
  // Higher offsets are drawn higher up.
  const ys = chart.offsetPath.match(/V([\d.]+)/g).map((v) => Number(v.slice(1)));
  assert.ok(ys[0] < ys[1]);
});

test("plan chart empty states", () => {
  assert.equal(planChart({ rows: [], now: NOW }).empty, true);
  assert.equal(planChart({ rows: undefined, now: NOW }).empty, true);
  // A plan that ended before now.
  assert.equal(planChart({ rows: [["", 1, 20, iso(NOW + 30 * H)]], now: NOW, hours: 24 }).empty, true);
  // Without a comfort band or indoor predictions the chart still draws.
  const chart = planChart({ rows: [["", 1, null, iso(NOW)]], now: NOW });
  assert.equal(chart.empty, false);
  assert.equal(chart.band, null);
  assert.equal(chart.indoorPath, "");
});

test("hot water windows", () => {
  const out = hotwaterWindows({
    vvPlan: [{ start: iso(NOW + 2 * H), end: iso(NOW + 3 * H), mode: "normal" }],
    blocks: [
      { action: "charge", start: "11:00", end: "12:00", start_at: iso(NOW - H), end_at: iso(NOW + H), target: 50 },
      { action: "hold", start_at: iso(NOW), end_at: iso(NOW + H) },
      { action: "charge", start_at: iso(NOW + 30 * H), end_at: iso(NOW + 31 * H) },
    ],
    now: NOW,
    hours: 24,
  });
  assert.equal(out.length, 2);
  assert.equal(out[0].kind, "charge");
  assert.equal(out[0].from, 0);
  assert.equal(out[0].to, 1 / 24);
  assert.equal(out[1].kind, "vv");
  assert.deepEqual(hotwaterWindows({ now: NOW }), []);
});

test("contributions: rules components or the optimiser", () => {
  const rules = contributions({ attributes: { source: "rules", components: { indoor: 1.5, price: -3, weather: 0 }, reasons: { price: "expensive" } } });
  assert.deepEqual(rules.map((c) => c.key), ["indoor", "price", "weather"]);
  assert.equal(rules[1].frac, -1);
  assert.equal(rules[1].reason, "expensive");
  const opt = contributions({ attributes: { source: "optimiser", rules_total: 1, strategy_reason: "plan" } }, -2);
  assert.equal(opt[0].key, "optimiser");
  assert.equal(opt[0].value, -2);
  assert.equal(opt[1].value, 1);
  assert.equal(contributions(undefined).length, 3);
});

test("climate alerts", () => {
  const ids = {
    gateway_online: "g",
    lease_state_s1: "l",
    strategy_s1: "st",
    house_model_s1: "hm",
    rmu_feed_s1: "rf",
    rmu_lease_state_s1: "rl",
  };
  const hass = {
    states: {
      g: { state: "off" },
      l: { state: "error", attributes: { error: "write refused" } },
      st: { state: "optimiser" },
      hm: { state: "low" },
      rf: { state: "on" },
      rl: { state: "inactive" },
    },
  };
  const alerts = climateAlerts(hass, ids, "s1");
  assert.deepEqual(alerts.map((a) => a.level), ["error", "error", "warning", "warning"]);
  assert.match(alerts[1].text, /write refused/);
  hass.states = { g: { state: "on" }, l: { state: "active" }, st: { state: "rules" }, hm: { state: "low" }, rf: { state: "off" } };
  assert.deepEqual(climateAlerts(hass, ids, "s1"), []);
});

test("lease rows with countdowns", () => {
  const hass = {
    states: {
      a: { state: "active", attributes: { expires: iso(NOW + 300000), value: 2 } },
      b: { state: "error", attributes: { error: "boom", expires: iso(NOW + 300000) } },
    },
  };
  const rows = leaseRows(hass, { lease_state_s1: "a", hw_comfort_lease_state: "b" }, NOW);
  assert.equal(rows.length, 2);
  assert.equal(rows[0].countdown.text, "5 min 00 s");
  assert.equal(rows[0].value, 2);
  assert.equal(rows[1].countdown, null);
  assert.equal(rows[1].error, "boom");
});

test("savings model, daily values and sparkline", () => {
  const model = savingsModel(
    { state: "12.5", attributes: { unit_of_measurement: "SEK", predicted_cost: 11, cost_vs_predicted_pct: 13.6, yesterday: { cost: 20, predicted_cost: 19, cost_vs_predicted_pct: 5.3, day: "2026-10-11" } } },
    { state: "3.2", attributes: { s1: 2.7, hot_water: 0.5, day: "2026-10-12", unit_of_measurement: "SEK", state_class: "total" } },
  );
  assert.equal(model.costToday, 12.5);
  assert.equal(model.savingsToday, 3.2);
  assert.equal(model.currency, "SEK");
  assert.equal(model.yesterday.cost, 20);
  assert.deepEqual(model.savingsParts.map((p) => p.label), ["S1", "Hot water"]);
  const empty = savingsModel(undefined, { state: "unknown", attributes: {} });
  assert.equal(empty.costToday, null);
  assert.equal(empty.savingsToday, null);
  assert.equal(empty.yesterday, null);

  const values = dailyValues({ "sensor.s": [{ start: NOW, change: 1 }, { start: NOW - 86400000, change: 2 }, { start: NOW, change: null }] }, "sensor.s");
  assert.deepEqual(values.map((v) => v.value), [2, 1]);
  assert.deepEqual(dailyValues({}, "sensor.s"), []);
  assert.equal(sparkline([1]), "");
  assert.match(sparkline([1, 3, 2], 100, 20), /^M0 [\d.]+ L50 2 L100 [\d.]+$/);
});
