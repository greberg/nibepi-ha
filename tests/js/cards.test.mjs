// Smoke tests of the card elements with a minimal DOM shim: each card
// renders from a fake hass without throwing, shows its main figures and
// its empty states. Real rendering (ha-card, ha-form, theme) needs a
// Home Assistant frontend.
import test from "node:test";
import assert from "node:assert/strict";

class FakeElement {
  constructor() {
    this.children = [];
    this.events = [];
  }
  attachShadow() {
    this.shadowRoot = { innerHTML: "", addEventListener() {} };
    return this.shadowRoot;
  }
  dispatchEvent(ev) {
    this.events.push(ev);
    return true;
  }
  appendChild(el) {
    this.children.push(el);
  }
  addEventListener() {}
}
const registry = new Map();
globalThis.HTMLElement = FakeElement;
globalThis.customElements = { get: (n) => registry.get(n), define: (n, c) => registry.set(n, c) };
globalThis.window = globalThis;
globalThis.document = {
  createElement(tag) {
    const C = registry.get(tag);
    return C ? new C() : { tag, addEventListener() {} };
  },
};
globalThis.confirm = () => true;
const realInfo = console.info;
console.info = () => {};
await import("../../custom_components/nibepi/frontend/nibepi-cards.js?v=test");
console.info = realInfo;

const H = 3600000;
const now = Date.now();
const iso = (t) => new Date(t).toISOString();

function makeHass() {
  const ids = {
    gateway_online: "binary_sensor.nibe_gateway",
    indoor_s1: "sensor.netatmo",
    outdoor: "sensor.bt1",
    bt7_hw_top: "sensor.bt7",
    bt6_hw_load: "sensor.bt6",
    bus_health: "sensor.bus",
    planned_offset_s1: "sensor.planned_s1",
    pump_offset_s1: "sensor.pump_s1",
    optimiser_plan_s1: "sensor.plan_s1",
    mode_s1: "select.mode_s1",
    strategy_s1: "select.strategy_s1",
    lease_state_s1: "sensor.lease_s1",
    house_model_s1: "sensor.model_s1",
    price_level: "sensor.price_level",
    hw_mode: "select.hw_mode",
    hw_planned_action: "sensor.hw_action",
    hw_draw_profile: "sensor.hw_draw",
    hw_optimiser_plan: "sensor.hw_opt",
    hw_comfort_lease_state: "sensor.hw_comfort_lease",
    heat_pump_cost_today: "sensor.cost",
    optimiser_savings_today: "sensor.savings",
  };
  const states = {
    "binary_sensor.nibe_gateway": {
      entity_id: "binary_sensor.nibe_gateway",
      state: "on",
      attributes: { entities: ids, systems: ["s1"], version: "1.2", core: "up", raw_set: "none", last_data: now - 4000, last_message_at: iso(now - 2000) },
    },
    "sensor.netatmo": { state: "21.3", attributes: { unit_of_measurement: "°C" } },
    "sensor.bt1": { state: "4.2", attributes: {} },
    "sensor.bt7": { state: "51.0", attributes: {} },
    "sensor.bt6": { state: "47.5", attributes: {} },
    "sensor.bus": { state: "0", attributes: { unit_of_measurement: "errors/min" } },
    "sensor.planned_s1": { state: "2", attributes: { source: "rules", components: { indoor: 1, price: 1, weather: 0 }, reasons: { price: "cheap" }, strategy_reason: "rules selected" } },
    "sensor.pump_s1": { state: "2", attributes: {} },
    "sensor.plan_s1": {
      state: "1",
      attributes: {
        comfort: { target: 21, lower: 20.5, upper: 22 },
        plan_24h: [["", 1, 21.1, iso(now - 60000)], ["", -1, 21.4, iso(now + 5 * H)]],
        summary: "Pre-heats before the expensive evening.",
        savings: 3.21,
        savings_pct: 8.5,
        currency: "SEK",
        explanation: ["line <b>1</b>"],
        planned_at: iso(now - 120000),
        applied: false,
      },
    },
    "select.mode_s1": { entity_id: "select.mode_s1", state: "shadow", attributes: { options: ["off", "shadow", "active"], friendly_name: "Mode S1" } },
    "select.strategy_s1": { entity_id: "select.strategy_s1", state: "optimiser", attributes: { options: ["rules", "optimiser"] } },
    "sensor.lease_s1": { state: "active", attributes: { expires: iso(now + 240000), value: 2 } },
    "sensor.model_s1": { state: "low", attributes: {} },
    "sensor.price_level": { state: "cheap", attributes: { levels: { [iso(now)]: "CHEAP", [iso(now + 3 * H)]: "EXPENSIVE" } } },
    "select.hw_mode": { entity_id: "select.hw_mode", state: "active", attributes: { options: ["off", "shadow", "active"] } },
    "sensor.hw_action": { state: "economy", attributes: { reasons: { vv_control: "no draw expected" }, next_heat_start: iso(now + 2 * H), plan_24h: [{ start: iso(now + 2 * H), end: iso(now + 3 * H), mode: "normal" }] } },
    "sensor.hw_draw": { state: iso(now + 6 * H), attributes: { confidence: 0.8 } },
    "sensor.hw_opt": { state: "unknown", attributes: { enabled: false, blocks: [] } },
    "sensor.hw_comfort_lease": { state: "error", attributes: { error: "refused" } },
    "sensor.cost": { state: "12.34", attributes: { unit_of_measurement: "SEK", predicted_cost: 11.5, cost_vs_predicted_pct: 7.3, yesterday: { cost: 20.1, cost_vs_predicted_pct: -2 } } },
    "sensor.savings": { state: "3.5", attributes: { unit_of_measurement: "SEK", s1: 3.0, hot_water: 0.5, day: "x" } },
  };
  const calls = [];
  return {
    calls,
    entities: {
      "binary_sensor.nibe_gateway": { entity_id: "binary_sensor.nibe_gateway", platform: "nibepi", translation_key: "gateway_online", device_id: "d1" },
    },
    devices: { d1: { name: "Nibe F1255", config_entries: ["e1"] } },
    states,
    callService: (...args) => calls.push(args),
    callWS: () => Promise.resolve({ "sensor.cost": [{ start: now - 3 * 86400000, change: 10 }, { start: now - 2 * 86400000, change: 12 }] }),
  };
}

function render(type, config, hass = makeHass()) {
  const card = document.createElement(type);
  card.setConfig({ type: `custom:${type}`, ...config });
  card.hass = hass;
  return { card, html: card.shadowRoot.innerHTML, hass };
}

test("all cards are registered for the card picker", () => {
  const types = window.customCards.map((c) => c.type);
  for (const t of ["nibepi-climate-card", "nibepi-plan-card", "nibepi-hotwater-card", "nibepi-gateway-card", "nibepi-savings-card"]) {
    assert.ok(types.includes(t), t);
    assert.ok(customElements.get(t), t);
    assert.ok(window.customCards.find((c) => c.type === t).preview);
  }
});

test("climate card", () => {
  const { html } = render("nibepi-climate-card", { system: "s1" });
  assert.match(html, /<ha-card>/);
  assert.match(html, /Climate S1/);
  assert.match(html, /21\.3<span class="unit">°C/);
  assert.match(html, /Target<\/span><span class="value ">21\.0 °C/);
  assert.match(html, /4\.2 °C/);
  assert.match(html, /data-select="mode_s1" data-option="active"/);
  assert.match(html, /confidence is low/);
  assert.match(html, /data-more="price_offset_s1"/);
});

test("climate card for a system that is not configured", () => {
  const { html } = render("nibepi-climate-card", { system: "s2" });
  assert.match(html, /S2 is not configured/);
});

test("plan card escapes text and draws the chart", () => {
  const { html } = render("nibepi-plan-card", { system: "s1", hours: 12 });
  assert.match(html, /<svg viewBox="0 0 \d+ \d+"/);
  assert.match(html, /Pre-heats/);
  assert.match(html, /3\.21 SEK/);
  assert.match(html, /line &lt;b&gt;1&lt;\/b&gt;/);
  assert.doesNotMatch(html, /<b>1<\/b>/);
});

test("plan card empty state", () => {
  const hass = makeHass();
  hass.states["sensor.plan_s1"] = { state: "unknown", attributes: { problem: "no prices" } };
  const { html } = render("nibepi-plan-card", { system: "s1" }, hass);
  assert.match(html, /No optimiser plan for the next 24 h yet: no prices/);
});

test("hot water card", () => {
  const { html } = render("nibepi-hotwater-card", {});
  assert.match(html, /51\.0<span class="unit">°C/);
  assert.match(html, /47\.5 °C/);
  assert.match(html, /80 %/);
  assert.match(html, /VV-AI/);
  assert.match(html, /class="seg vv"/);
  assert.match(html, /no draw expected/);
  assert.match(html, /47041/);
});

test("gateway card with leases and status link", () => {
  const { html } = render("nibepi-gateway-card", { status_url: "http://pi:8080" });
  assert.match(html, /Online/);
  assert.match(html, /href="http:\/\/pi:8080"/);
  assert.match(html, /4 s ago/);
  assert.match(html, /Curve offset S1/);
  assert.match(html, /\d min \d\d s/);
  assert.match(html, /refused/);
  assert.match(html, /errors\/min/);
  const bad = render("nibepi-gateway-card", { status_url: "javascript:alert(1)" }).html;
  assert.doesNotMatch(bad, /javascript:/);
});

test("savings card and its statistics", async () => {
  const { card, hass } = render("nibepi-savings-card", {});
  assert.match(card.shadowRoot.innerHTML, /12\.34<span class="unit">SEK/);
  assert.match(card.shadowRoot.innerHTML, /3\.50<span class="unit">SEK/);
  assert.match(card.shadowRoot.innerHTML, /20\.10 SEK/);
  await new Promise((r) => setTimeout(r, 0));
  assert.match(card.shadowRoot.innerHTML, /Cost per day, 2 days/);
  assert.ok(hass);
});

test("no NibePi entry", () => {
  const hass = { entities: {}, devices: {}, states: {} };
  const { html } = render("nibepi-climate-card", {}, hass);
  assert.match(html, /No NibePi integration found/);
});

test("chips call select.select_option and values open more-info", () => {
  const { card, hass } = render("nibepi-climate-card", { system: "s1" });
  const chip = { dataset: { select: "strategy_s1", option: "rules" } };
  card._onClick({ composedPath: () => [chip] });
  assert.deepEqual(hass.calls[0], ["select", "select_option", { entity_id: "select.strategy_s1", option: "rules" }]);
  card._onClick({ composedPath: () => [{ dataset: { more: "outdoor" } }] });
  const ev = card.events.at(-1);
  assert.equal(ev.type, "hass-more-info");
  assert.equal(ev.detail.entityId, "sensor.bt1");
});

test("invalid system is refused", () => {
  const card = document.createElement("nibepi-climate-card");
  assert.throws(() => card.setConfig({ system: "s3" }), /s1 or s2/);
});

test("editor schema per card", async () => {
  const climate = await customElements.get("nibepi-climate-card").getConfigElement();
  assert.deepEqual(climate.schema.map((s) => s.name), ["system", "entry_id", "title"]);
  const plan = await customElements.get("nibepi-plan-card").getConfigElement();
  assert.deepEqual(plan.schema.map((s) => s.name), ["system", "entry_id", "title", "hours"]);
  const gw = await customElements.get("nibepi-gateway-card").getConfigElement();
  assert.deepEqual(gw.schema.map((s) => s.name), ["entry_id", "title", "status_url"]);
  assert.deepEqual(customElements.get("nibepi-plan-card").getStubConfig(makeHass()), { system: "s1" });
});

test("away chip, lowered target and the shadow-mode alert", () => {
  const hass = makeHass();
  const ids = hass.states["binary_sensor.nibe_gateway"].attributes.entities;
  ids.away = "switch.away";
  ids.away_until = "sensor.away_until";
  hass.states["switch.away"] = {
    entity_id: "switch.away",
    state: "on",
    attributes: { until: iso(now + 30 * H), setback: 3, hot_water: "economy", targets: { s1: 18 } },
  };
  hass.states["sensor.away_until"] = { state: iso(now + 30 * H), attributes: {} };
  hass.states["sensor.plan_s1"].attributes.comfort = { target: 18, lower: 17.6, upper: 22, away: true, normal_target: 21 };
  const { card, html } = render("nibepi-climate-card", { system: "s1" }, hass);
  assert.match(html, /data-toggle="away"/);
  assert.match(html, /Away until/);
  assert.match(html, /18\.0 °C <span class="muted small">away · normal 21\.0/);
  assert.match(html, /Away is on but heating mode S1 is shadow/);
  card._onClick({ composedPath: () => [{ dataset: { toggle: "away" } }] });
  assert.deepEqual(hass.calls.at(-1), ["switch", "turn_off", { entity_id: "switch.away" }]);

  hass.states["select.hw_mode"].state = "shadow";
  const hw = render("nibepi-hotwater-card", {}, hass).html;
  assert.match(hw, /data-toggle="away"/);
  assert.match(hw, /hot water mode is shadow/);

  hass.states["switch.away"] = { entity_id: "switch.away", state: "off", attributes: {} };
  const home = render("nibepi-climate-card", { system: "s1" }, hass);
  assert.match(home.html, />Home<\/button>/);
  assert.doesNotMatch(home.html, /Away is on/);
  home.card._onClick({ composedPath: () => [{ dataset: { toggle: "away" } }] });
  assert.deepEqual(hass.calls.at(-1), ["switch", "turn_on", { entity_id: "switch.away" }]);
});
