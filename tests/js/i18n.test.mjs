// The cards' translations (frontend/nibepi-i18n.js) and the localised
// core helpers: fallback, placeholders, key coverage, Swedish formats.
import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";

import { STRINGS, clockText, hasKey, langOf, leaseErrorText, localeOf, normLang, registrationLang, t, toLoc } from "../../custom_components/nibepi/frontend/nibepi-i18n.js";
import {
  LEASES,
  climateAlerts,
  contributions,
  countdown,
  formatAge,
  formatIn,
  formatNumber,
  formatSigned,
  hotwaterWindows,
  leaseRows,
  planChart,
  savingsModel,
  versionLine,
} from "../../custom_components/nibepi/frontend/nibepi-core.js";

const H = 3600000;
const NOW = Date.parse("2026-10-12T10:05:00Z");
const iso = (time) => new Date(time).toISOString();
const frontend = new URL("../../custom_components/nibepi/frontend/", import.meta.url);
const source = (name) => readFileSync(new URL(name, frontend), "utf8");

test("t substitutes placeholders and falls back to English, then the key", () => {
  assert.equal(t("en", "climate.title", { system: "S1" }), "Climate S1");
  assert.equal(t("sv", "climate.title", { system: "S1" }), "Klimat S1");
  assert.equal(t("sv", "time.ago", { duration: "5 min" }), "för 5 min sedan");
  assert.equal(t("sv", "time.in", { duration: "2 h" }), "om 2 h");
  // A missing variable keeps its placeholder; 0 is a value.
  assert.equal(t("en", "climate.title"), "Climate {system}");
  assert.equal(t("en", "hw.reasons", { count: 0 }), "Reasons (0)");
  // Unknown language: English. Unknown key: the key.
  assert.equal(t("de", "hw.title"), "Hot water");
  assert.equal(t("sv", "no.such.key"), "no.such.key");
  STRINGS.en["test.only_en"] = "only English";
  try {
    assert.equal(t("sv", "test.only_en"), "only English");
  } finally {
    delete STRINGS.en["test.only_en"];
  }
  assert.equal(hasKey("hw.title"), true);
  assert.equal(hasKey("reason.unknown"), false);
});

test("the language follows Home Assistant's", () => {
  assert.equal(normLang("sv"), "sv");
  assert.equal(normLang("sv-SE"), "sv");
  assert.equal(normLang("SV_fi"), "sv");
  assert.equal(normLang("svenska"), "en");
  assert.equal(normLang("en-GB"), "en");
  assert.equal(normLang(undefined), "en");
  assert.equal(langOf({ locale: { language: "sv" }, language: "en" }), "sv");
  assert.equal(langOf({ language: "sv-SE" }), "sv");
  assert.equal(langOf({ locale: { language: "nb" } }), "en");
  assert.equal(langOf(undefined), "en");
  // The card picker: the page's language, else the browser's.
  const saved = globalThis.document;
  try {
    globalThis.document = { documentElement: { lang: "sv" } };
    assert.equal(registrationLang(), "sv");
    globalThis.document = { documentElement: { lang: "en" } };
    assert.equal(registrationLang(), "en");
  } finally {
    globalThis.document = saved;
  }
});

test("English and Swedish have the same keys, and no HTML in the texts", () => {
  assert.deepEqual(Object.keys(STRINGS.sv).sort(), Object.keys(STRINGS.en).sort());
  for (const [lang, dict] of Object.entries(STRINGS)) {
    for (const [key, text] of Object.entries(dict)) {
      assert.equal(typeof text, "string", `${lang} ${key}`);
      assert.ok(text.length, `${lang} ${key}`);
      assert.doesNotMatch(text, /[<>&"]/, `${lang} ${key}`);
      // Both languages use the same placeholders.
      const vars = (s) => (s.match(/\{\w+\}/g) || []).sort();
      assert.deepEqual(vars(text), vars(STRINGS.en[key]), `${lang} ${key}`);
    }
  }
});

test("every key the cards and helpers use exists", () => {
  const used = new Set();
  for (const file of ["nibepi-cards.js", "nibepi-core.js"]) {
    for (const m of source(file).matchAll(/\bt\(\s*(?:[\w.]+\s*,\s*)?["']([\w.-]+)["']/g)) used.add(m[1]);
    // Keys chosen by a condition: t(loc, x ? "a" : "b"), label("sav.x"), spark(.., "sav.x", ..)
    for (const m of source(file).matchAll(/["']((?:problem|time|away|alert|climate|plan|hw|leg|gw|sav|part|editor)\.[\w.]*\w)["']/g)) used.add(m[1]);
  }
  assert.ok(used.size > 80, `only ${used.size} keys found`);
  for (const key of used) assert.ok(hasKey(key), key);
  // Keys built at run time.
  const families = [
    ...LEASES.map((l) => `lease.${l.key}`),
    ...["away", "price_comfort", "autoluxury", "priority", "optimiser", "vv_control", "legionella"].map((k) => `reason.${k}`),
    ...["very_cheap", "cheap", "normal", "expensive", "very_expensive"].map((k) => `price.${k}`),
    ...["off", "shadow", "active"].map((k) => `mode.${k}`),
    ...["prior", "low", "medium", "high"].map((k) => `model.${k}`),
    ...["active", "grace", "restoring", "inactive", "error"].map((k) => `lease.state.${k}`),
    ...["economy", "normal", "luxury"].map((k) => `hwmode.${k}`),
    ...["rules", "optimiser", "comfort_floor"].map((k) => `source.${k}`),
    ...["indoor", "price", "weather", "away"].map((k) => `part.${k}`),
    ...["no_entry", "unknown_entry"].map((k) => `problem.${k}`),
    ...["ha", "pump", "history"].map((k) => `leg.by.${k}`),
    ...["system", "entry_id", "title", "hours", "status_url"].map((k) => `editor.${k}`),
  ];
  for (const type of ["nibepi-climate-card", "nibepi-plan-card", "nibepi-hotwater-card", "nibepi-gateway-card", "nibepi-savings-card"]) {
    families.push(`card.${type}.name`, `card.${type}.description`);
  }
  for (const key of families) assert.ok(hasKey(key), key);
});

test("Swedish numbers: decimal comma, signs kept", () => {
  assert.equal(formatNumber(21.34, 1, "sv"), "21,3");
  assert.equal(formatNumber(3.5, 2, "sv"), "3,50");
  assert.equal(formatNumber(-0.5, 1, "sv"), "−0,5");
  assert.equal(formatNumber(null, 1, "sv"), "–");
  assert.equal(formatSigned(2, 0, "sv"), "+2");
  assert.equal(formatSigned(-1.5, 1, "sv"), "−1,5");
  assert.equal(formatSigned(-0.01, 1, "sv"), "0,0");
  assert.equal(formatNumber(12345.6, 1, "sv"), "12\u00a0345,6");
  // Without a locale: as before (point, no grouping).
  assert.equal(formatNumber(12345.6), "12345.6");
  assert.equal(formatSigned(-1.5, 1), "−1.5");
  // Home Assistant's number format setting wins over the language.
  assert.equal(formatNumber(1.5, 1, localeOf({ locale: { language: "sv", number_format: "comma_decimal" } })), "1.5");
  assert.equal(formatNumber(1.5, 1, localeOf({ locale: { language: "en", number_format: "decimal_comma" } })), "1,5");
  assert.equal(formatNumber(12345, 0, localeOf({ locale: { language: "en", number_format: "none" } })), "12345");
});

test("Swedish times: 24 h, unless Home Assistant says 12 h", () => {
  const evening = new Date(2026, 9, 12, 18, 5).getTime();
  const morning = new Date(2026, 9, 12, 6, 5).getTime();
  assert.equal(clockText(evening, "sv"), "18:05");
  assert.equal(clockText(morning, "sv"), "06:05");
  assert.equal(clockText(evening), "18:05"); // no locale: 24 h
  assert.equal(clockText(evening, localeOf({ locale: { language: "sv", time_format: "language" } })), "18:05");
  assert.equal(clockText(evening, localeOf({ locale: { language: "en", time_format: "24" } })), "18:05");
  assert.match(clockText(evening, localeOf({ locale: { language: "en", time_format: "12" } })), /^6:05\sPM$/);
  assert.match(clockText(evening, localeOf({ locale: { language: "sv", time_format: "12" } })), /^6:05\s?em$/);
  assert.equal(toLoc("sv").lang, "sv");
});

test("Swedish durations, ages and countdowns", () => {
  assert.equal(formatAge(NOW - 5 * 60000, NOW, true, "sv"), "för 5 min sedan");
  assert.equal(formatAge(NOW + 1000, NOW, false, "sv"), "nyss");
  assert.equal(formatIn(NOW + 2 * H, NOW, true, "sv"), "om 2 h 00 min");
  assert.equal(formatIn(NOW + 2 * H, NOW, true, "en"), "in 2 h 00 min");
  assert.equal(countdown(iso(NOW - 1000), NOW, "sv").text, "utgången");
  assert.equal(countdown(iso(NOW + 90000), NOW, "sv").text, "1 min 30 s");
});

test("core helpers produce Swedish text", () => {
  const ids = { gateway_online: "g", lease_state_s1: "l", strategy_s1: "st", house_model_s1: "hm", rmu_feed_s1: "rf", rmu_lease_state_s1: "rl", away: "a", mode_s1: "m" };
  const hass = {
    states: {
      g: { state: "off" },
      l: { state: "error", attributes: { error: "write refused" } },
      st: { state: "optimiser" },
      hm: { state: "low" },
      rf: { state: "on" },
      rl: { state: "inactive" },
      a: { state: "on", attributes: {} },
      m: { state: "shadow" },
    },
  };
  assert.deepEqual(
    climateAlerts(hass, ids, "s1", "sv").map((a) => a.text),
    [
      "Gateway frånkopplad",
      "Fel i lease för kurvjustering: write refused",
      "Husmodellens säkerhet är låg men kostnadsoptimeraren är vald",
      "Borta är på men läge S1 är Skugga: inget skrivs till pumpen",
      "RMU40-matningen är på men dess lease är inaktiv",
    ],
  );
  // A state the cards do not know is shown as it is.
  hass.states.rl.state = "new_state";
  assert.match(climateAlerts(hass, ids, "s1", "sv").at(-1).text, /dess lease är new_state$/);

  const parts = contributions({ attributes: { source: "rules", components: { indoor: 1, price: 0, weather: 0, away: -3 } } }, null, "sv");
  assert.deepEqual(parts.map((c) => c.label), ["Inomhus", "Elpris", "Prognos", "Borta"]);
  const opt = contributions({ attributes: { source: "comfort_floor", rules_total: 1 } }, 2, "sv");
  assert.deepEqual(opt.map((c) => c.label), ["Komfortgolv", "Regler skulle ge"]);

  const rows = leaseRows({ states: { a: { state: "active", attributes: { expires: iso(NOW + 60000) } } } }, { hw_comfort_lease_state: "a" }, NOW, LEASES, "sv");
  assert.equal(rows[0].label, "Varmvattenläge");
  assert.equal(savingsModel(undefined, { state: "1", attributes: { hot_water: 0.5 } }, "sv").savingsParts[0].label, "Varmvatten");
  assert.equal(versionLine({ state: "2.0.0", attributes: { integration_version: "0.8.0" } }, null, "sv"), "Gateway v2.0.0 · Integration v0.8.0");

  const windows = hotwaterWindows({
    vvPlan: [{ start: iso(NOW + 2 * H), end: iso(NOW + 3 * H), mode: "luxury" }],
    blocks: [{ action: "charge", start_at: iso(NOW + 4 * H), end_at: iso(NOW + 5 * H), target: 50, reason: "cheap hour" }],
    legionella: { start: NOW + 6 * H, end: NOW + 7 * H, temperature: 60 },
    now: NOW,
    loc: "sv",
  });
  assert.match(windows[0].label, /^VV-AI lyx \d\d:\d\d–\d\d:\d\d$/);
  assert.match(windows[1].label, /^Laddning \d\d:\d\d–\d\d:\d\d till 50 °C: cheap hour$/);
  assert.match(windows[2].label, /^Legionella \d\d:\d\d–\d\d:\d\d till 60,0 °C$/);

  const chart = planChart({
    rows: [["", -1.5, 20.5, iso(NOW - 60000)]],
    levels: { [iso(NOW)]: "VERY_CHEAP" },
    comfort: { lower: 20, upper: 21.5 },
    now: NOW,
    loc: "sv",
  });
  assert.match(chart.segments[0].label, /: kurvjustering −2, inomhus 20,5 °C$/);
  assert.match(chart.priceBands[0].label, /väldigt billigt$/);
  assert.ok(chart.tempTicks.some((tick) => tick.label === "20,5"));
  assert.ok(chart.timeTicks.every((tick) => /^\d\d:\d\d$/.test(tick.label)));
});

test("the gateway's known lease errors are shown in Swedish", () => {
  const rmu = "RMU40 S1 is not connected: enable RMU40 for climate system 1 in the pump (menu 5.2) and keep system.rmu_emulation on";
  assert.equal(leaseErrorText("en", rmu), rmu);
  assert.equal(
    leaseErrorText("sv", rmu),
    "RMU40 S1 är inte ansluten: aktivera RMU40 för klimatsystem 1 i pumpen (meny 5.2) och låt system.rmu_emulation vara på",
  );
  assert.equal(leaseErrorText("sv", "Heatpump is not ready yet."), "Värmepumpen är inte redo än.");
  assert.equal(
    leaseErrorText("sv", "RMU40 S2 is read-only (another RMU40 answers on the bus or rmu_emulation=false)"),
    "RMU40 S2 är skrivskyddad (en annan RMU40 svarar på bussen eller rmu_emulation=false)",
  );
  assert.equal(leaseErrorText("sv", "Value 4.9 outside lease limits 5..35"), "Värdet 4.9 ligger utanför leasens gränser 5..35");
  assert.equal(leaseErrorText("sv", "Register dont exist"), "Register dont exist");
  assert.equal(leaseErrorText("sv", undefined), "");

  const hass = { states: { a: { state: "error", attributes: { error: "Heatpump is not ready yet." } } } };
  assert.equal(leaseRows(hass, { lease_state_s1: "a" }, NOW, LEASES, "sv")[0].error, "Värmepumpen är inte redo än.");
  assert.equal(leaseRows(hass, { lease_state_s1: "a" }, NOW)[0].error, "Heatpump is not ready yet.");
  assert.equal(
    climateAlerts(hass, { lease_state_s1: "a" }, "s1", "sv")[0].text,
    "Fel i lease för kurvjustering: Värmepumpen är inte redo än.",
  );
});
