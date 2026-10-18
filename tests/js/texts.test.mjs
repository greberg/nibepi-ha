// The integration's texts in the cards (renderText, attrText in
// frontend/nibepi-i18n.js): the same text as Python renders for every
// code (tests/js/texts-parity.json, written by tools/gen_texts_js.py from
// logic/texts.py; tests/test_texts_js.py keeps it current) and the
// fallbacks for codes the cards do not know.
import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";

import { attrText, isText, renderText } from "../../custom_components/nibepi/frontend/nibepi-i18n.js";
import { TEXTS, TEXT_LANGS } from "../../custom_components/nibepi/frontend/nibepi-texts.js";

const cases = JSON.parse(readFileSync(new URL("./texts-parity.json", import.meta.url), "utf8"));

test("the generated catalogue has both languages", () => {
  assert.deepEqual(TEXT_LANGS, ["en", "sv"]);
  assert.ok(Object.keys(TEXTS).length > 100);
  for (const [code, texts] of Object.entries(TEXTS)) assert.equal(texts.length, 2, code);
});

test("every code renders as Python renders it, in English and Swedish", () => {
  assert.ok(cases.length > Object.keys(TEXTS).length);
  for (const c of cases) {
    const what = JSON.stringify(c.text);
    assert.equal(renderText("en", c.text), c.en, `en ${what}`);
    assert.equal(renderText("sv", c.text), c.sv, `sv ${what}`);
    assert.equal(renderText("sv", c.text, undefined, true), c.sv_nested ?? c.sv, `sv nested ${what}`);
  }
});

test("numbers: Swedish decimal comma and minus, Python's rounding", () => {
  const offset = (value) => ({ c: "weather.lower_blocked", p: { offset: value } });
  assert.equal(renderText("sv", offset(-0.5)), "Sänkning spärrad (−0,50)");
  assert.equal(renderText("en", offset(-0.5)), "lowering blocked (-0.50)");
  // Half to even on the exact value: 0.125 -> 0.12, 0.375 -> 0.38.
  assert.equal(renderText("en", offset(0.125)), "lowering blocked (+0.12)");
  assert.equal(renderText("en", offset(0.375)), "lowering blocked (+0.38)");
  assert.equal(renderText("en", offset(-0.004)), "lowering blocked (-0.00)");
  assert.equal(renderText("sv", { c: "indoor.ok", p: { setpoint: 21, indoor: 20.25, factor: 1e-5 } }), "Börvärde 21,0 °C, inomhus 20,2 °C, faktor 1e−05");
  // An integer spec takes a whole float.
  assert.equal(renderText("en", { c: "opt.plan_old", p: { minutes: 90.7 } }), "plan older than 90 min");
});

test("nested texts, capitals and leading values", () => {
  const reason = { c: "strategy.not_used", p: { reason: { c: "gate.floor", p: { floor: 19.5 } } } };
  assert.equal(renderText("sv", reason), "Optimeraren används inte: under komfortgolvet 19,5 °C: värmer");
  assert.equal(renderText("sv", reason, undefined, true), "optimeraren används inte: under komfortgolvet 19,5 °C: värmer");
  assert.equal(renderText("sv-SE", { c: "meter.not_found", p: { entity: "sensor.m" } }), "sensor.m hittades inte");
  assert.equal(renderText("de", { c: "opt.no_plan" }), "no plan");
  // A nested text that does not render is its code, like in Python.
  assert.equal(renderText("en", { c: "opt.no_plan_why", p: { why: { c: "gate.floor" } } }), "no plan (gate.floor)");
});

test("fallbacks: the attribute's own text, else the code", () => {
  assert.equal(renderText("sv", { c: "from.a.newer.version", p: {} }, "a newer text"), "a newer text");
  assert.equal(renderText("sv", { c: "from.a.newer.version" }), "from.a.newer.version");
  assert.equal(renderText("sv", { c: "priority.over", p: {} }, "BT7 50.0 over 46.0"), "BT7 50.0 over 46.0");
  assert.equal(renderText("sv", null, "plain"), "plain");
  assert.equal(renderText("sv", undefined, undefined), "");
  assert.equal(renderText("sv", "already a text"), "already a text");
  assert.equal(isText({ c: "x" }), true);
  assert.equal(isText(["x"]), false);
  assert.equal(isText(null), false);
});

test("attrText walks the attributes and their i18n side by side", () => {
  const attrs = {
    problem: "indoor temperature missing",
    reasons: { indoor: "setpoint missing", price: "plain" },
    blocks: [{ reason: "waiting for cheaper slots" }, { reason: "" }],
    i18n: {
      problem: { c: "opt.no_indoor" },
      reasons: { indoor: { c: "indoor.setpoint_missing" } },
      blocks: [{ reason: { c: "tank.waiting" } }, null],
    },
  };
  assert.equal(attrText("sv", attrs, "problem"), "Inomhustemperatur saknas");
  assert.equal(attrText("sv", attrs, "problem", true), "inomhustemperatur saknas");
  assert.equal(attrText("en", attrs, "problem"), "indoor temperature missing");
  assert.equal(attrText("sv", attrs, ["reasons", "indoor"]), "Börvärde saknas");
  assert.equal(attrText("sv", attrs, ["reasons", "price"]), "plain");
  assert.equal(attrText("sv", attrs, ["blocks", 0, "reason"]), "Väntar på billigare timmar");
  assert.equal(attrText("sv", attrs, ["blocks", 1, "reason"]), "");
  assert.equal(attrText("sv", attrs, ["nothing", "here"]), "");
  // An older integration without i18n: the attribute as it is.
  assert.equal(attrText("sv", { summary: "Planned 3 kWh" }, "summary"), "Planned 3 kWh");
  assert.equal(attrText("sv", undefined, "summary"), "");
});
