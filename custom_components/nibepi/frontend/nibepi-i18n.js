// NibePi cards: translations and locale formatting. Pure, no DOM and no
// Home Assistant imports, so it runs under node:test (tests/js).
//
// The cards follow Home Assistant's language: Swedish for "sv*", English
// otherwise. A key missing in Swedish falls back to English, a key missing
// in both to the key itself. Strings take {placeholders}; they hold no
// HTML, so a card can escape them or put escaped values into them.
// The Swedish terms are the integration's (translations/sv.json), so the
// cards match the entity names and select options.

export const LANGS = ["en", "sv"];

const en = {
  // Problems shared by the cards
  "problem.no_entry": "No NibePi integration found. Is it set up, and is its Gateway entity enabled?",
  "problem.unknown_entry": "No NibePi entry with this entry_id.",
  "problem.several_entries": "Several NibePi entries: choose one with entry_id.",
  "problem.system_missing": "Climate system {system} is not configured in NibePi ({systems}).",
  "problem.card_error": "NibePi card error: {error}",

  // Time words (s, min, h and d are units in both languages)
  "time.ago": "{duration} ago",
  "time.in": "in {duration}",
  "time.just_now": "just now",
  "time.expired": "expired",
  "time.left": "{duration} left",

  // Away
  "away.away": "Away",
  "away.home": "Home",
  "away.until": "Away until {time}",
  "away.tap_end": "Away is on: tap to end it",
  "away.tap_start": "Tap to start Away (no end time)",
  "away.confirm_active": "Set {name} to active? NibePi will then write to the heat pump through leases.",

  // Alerts
  "alert.gateway_offline": "Gateway offline",
  "alert.lease_error": "Curve offset lease error",
  "alert.lease_error_detail": "Curve offset lease error: {error}",
  "alert.model_confidence": "House model confidence is {confidence} while the optimiser is selected",
  "alert.rmu_lease": "RMU40 feed is on but its lease is {state}",
  "alert.away_mode": "Away is on but {mode} is {state}: nothing is written to the pump",
  "alert.heating_mode": "heating mode {system}",
  "alert.hot_water_mode": "hot water mode",
  "state.missing": "missing",

  // Select and sensor states inside sentences
  "mode.off": "off",
  "mode.shadow": "shadow",
  "mode.active": "active",
  "model.prior": "prior",
  "model.low": "low",
  "model.medium": "medium",
  "model.high": "high",
  "lease.state.active": "active",
  "lease.state.grace": "grace",
  "lease.state.restoring": "restoring",
  "lease.state.inactive": "inactive",
  "lease.state.error": "error",
  // The gateway's lease refusals (its own English texts, see leaseErrorText)
  "lease.err.not_ready": "Heatpump is not ready yet.",
  "lease.err.rmu_not_connected": "RMU40 S{system} is not connected: enable RMU40 for climate system {system} in the pump (menu 5.2) and keep system.rmu_emulation on",
  "lease.err.rmu_read_only": "RMU40 S{system} is read-only (another RMU40 answers on the bus or rmu_emulation=false)",
  "lease.err.limits": "Value {value} outside lease limits {min}..{max}",
  "on": "on",
  "off": "off",

  // Price levels
  "price.very_cheap": "very cheap",
  "price.cheap": "cheap",
  "price.normal": "normal",
  "price.expensive": "expensive",
  "price.very_expensive": "very expensive",

  // Hot water modes (VV-AI windows)
  "hwmode.economy": "economy",
  "hwmode.normal": "normal",
  "hwmode.luxury": "luxury",

  // Leases
  "lease.lease_state_s1": "Curve offset S1",
  "lease.lease_state_s2": "Curve offset S2",
  "lease.rmu_lease_state_s1": "RMU40 S1",
  "lease.rmu_lease_state_s2": "RMU40 S2",
  "lease.hw_comfort_lease_state": "Hot water comfort",
  "lease.hw_period_lease_state": "Hot water period",
  "lease.hw_temp_lux_lease_state": "Temporary lux",
  "lease.hw_lux_stop_lease_state": "Luxury stop temperature",

  // Climate card
  "climate.title": "Climate {system}",
  "climate.indoor": "Indoor",
  "climate.target": "Target",
  "climate.outdoor": "Outdoor",
  "climate.away": "away",
  "climate.normal": "normal {value}",
  "climate.pump_offset": "Pump offset",
  "climate.planned": "Planned ({source})",
  "climate.optimiser_now": "Optimiser now",
  "climate.strategy": "Strategy",
  "climate.mode": "Mode",
  "source.rules": "rules",
  "source.optimiser": "optimiser",
  "source.comfort_floor": "comfort floor",
  "part.indoor": "Indoor",
  "part.price": "Price",
  "part.weather": "Forecast",
  "part.away": "Away",
  "part.optimiser": "Optimiser",
  "part.comfort_floor": "Comfort floor",
  "part.rules": "Rules would be",

  // Plan card
  "plan.title": "Optimiser plan {system}",
  "plan.empty": "No optimiser plan for the next {hours} h yet.",
  "plan.empty_why": "No optimiser plan for the next {hours} h yet: {problem}.",
  "plan.aria": "Optimiser plan",
  "plan.comfort_band_range": "Comfort band {lower}–{upper} °C",
  "plan.now": "now",
  "plan.legend_offset": "Planned offset (right)",
  "plan.legend_indoor": "Predicted indoor °C (left)",
  "plan.legend_band": "Comfort band",
  "plan.legend_cheap": "Cheap",
  "plan.legend_expensive": "Expensive",
  "plan.expected_savings": "Expected savings",
  "plan.cost_baseline": "Plan cost / baseline",
  "plan.planned": "Planned",
  "plan.not_applied": "(not applied)",
  "plan.why": "Why ({count})",
  "plan.segment": "{from}–{to}: offset {offset}, indoor {indoor} °C",

  // Hot water card
  "hw.title": "Hot water",
  "hw.top": "Top (BT7)",
  "hw.charging": "Charging (BT6)",
  "hw.planned_action": "Planned action",
  "hw.reasons": "Reasons ({count})",
  "hw.next_draw": "Next draw",
  "hw.next_charge": "Next charge",
  "hw.by_optimiser": "optimiser",
  "hw.by_vvai": "VV-AI",
  "hw.next_24h": "Next 24 h",
  "hw.now": "now",
  "hw.legend_vv": "VV-AI window",
  "hw.legend_charge": "Optimiser charge",
  "hw.legend_legionella": "Legionella",
  "hw.nothing_planned": "nothing planned",
  "hw.mode": "Mode",
  "hw.leases": "Leases",
  "hw.window_vv": "VV-AI {mode} {from}–{to}",
  "hw.window_charge": "Charge {from}–{to}",
  "hw.window_charge_to": "Charge {from}–{to} to {temperature} °C",
  "hw.window_legionella": "Legionella {from}–{to}",
  "hw.window_legionella_to": "Legionella {from}–{to} to {temperature} °C",
  "reason.away": "Away",
  "reason.price_comfort": "Price comfort",
  "reason.autoluxury": "Autoluxury",
  "reason.priority": "Priority",
  "reason.optimiser": "Optimiser",
  "reason.vv_control": "VV control",
  "reason.legionella": "Legionella",

  // Legionella
  "leg.label": "Legionella",
  "leg.label_temp": "Legionella {temperature} °C",
  "leg.next": "Next legionella",
  "leg.running": "running",
  "leg.to": "to {temperature} °C",
  "leg.planned": "planned",
  "leg.hot_water_off": "hot water off",
  "leg.guard_off": "guard off",
  "leg.if_pump_not": "if the pump has not",
  "leg.by.ha": "NibePi",
  "leg.by.pump": "pump",
  "leg.by.history": "history",
  "leg.result.done": "done",
  "leg.result.cancelled": "cancelled",
  "leg.result.aborted: timeout": "aborted: timeout",
  "leg.result.aborted: BT7 missing or stale": "aborted: BT7 missing or stale",

  // Gateway card
  "gw.title": "NibePi gateway",
  "gw.status_page": "Status page",
  "gw.gateway": "Gateway",
  "gw.online": "Online",
  "gw.offline": "Offline",
  "gw.unknown": "Unknown",
  "gw.core": "Core",
  "gw.last_data": "Last data",
  "gw.raw_writes": "Raw writes",
  "gw.bus_errors": "Bus errors",
  "gw.last_message": "Last message",
  "gw.leases": "Leases",
  "gw.value": "value {value}",
  "gw.rmu_feed": "RMU40 feed {system}",
  "gw.version_gateway": "Gateway v{version}",
  "gw.version_build": "Gateway v{version} (build {build})",
  "gw.version_integration": "Integration v{version}",

  // Savings card
  "sav.title": "Heat pump cost",
  "sav.no_sensors": "No cost or savings sensors found.",
  "sav.vs_plan": "({pct} % vs plan)",
  "sav.days": "{label}, {count} days",
  "sav.total": "total {value} {currency}",
  "sav.cost_today": "Cost today",
  "sav.savings_today": "Expected savings today",
  "sav.predicted": "Predicted so far",
  "sav.cost_yesterday": "Cost yesterday",
  "sav.saved_yesterday": "Saved yesterday",
  "sav.parts": "Savings today: {parts} {currency}",
  "sav.cost_per_day": "Cost per day",
  "sav.savings_per_day": "Savings per day",
  "sav.cost_note": "Cost needs a heat pump meter (meter scope: heat pump).",
  "sav.hot_water": "Hot water",

  // Editor
  "editor.system": "Climate system",
  "editor.entry_id": "NibePi entry",
  "editor.entry_id_help": "Only needed with several NibePi entries",
  "editor.title": "Title",
  "editor.title_help": "Replaces the card's default title",
  "editor.hours": "Hours ahead",
  "editor.status_url": "Gateway status page URL",
  "editor.status_url_help": "e.g. http://nibepi.local:8080",

  // Card picker
  "card.nibepi-climate-card.name": "NibePi climate",
  "card.nibepi-climate-card.description":
    "Indoor vs target, outdoor, the planned and pump curve offset with its parts, the Away toggle, strategy and mode, and alerts for one climate system.",
  "card.nibepi-plan-card.name": "NibePi optimiser plan",
  "card.nibepi-plan-card.description":
    "Chart of the optimiser's planned offset and predicted indoor temperature with the comfort band and price levels, plus the expected savings.",
  "card.nibepi-hotwater-card.name": "NibePi hot water",
  "card.nibepi-hotwater-card.description":
    "BT7/BT6, the planned action and its reasons, next draw and charge, the legionella guard, the next 24 h of planned charges, the Away toggle, hot water mode and leases.",
  "card.nibepi-gateway-card.name": "NibePi gateway",
  "card.nibepi-gateway-card.description":
    "Gateway status, last data, raw writes, bus errors, every lease with its countdown, and the RMU40 feeds.",
  "card.nibepi-savings-card.name": "NibePi cost and savings",
  "card.nibepi-savings-card.description":
    "Heat pump cost and expected savings today, against the plan and yesterday, with 7-day sparklines.",
};

const sv = {
  "problem.no_entry": "Ingen NibePi-integration hittades. Är den uppsatt och är dess Gateway-entitet aktiverad?",
  "problem.unknown_entry": "Ingen NibePi-post med detta entry_id.",
  "problem.several_entries": "Flera NibePi-poster: välj en med entry_id.",
  "problem.system_missing": "Klimatsystem {system} är inte konfigurerat i NibePi ({systems}).",
  "problem.card_error": "Fel i NibePi-kortet: {error}",

  "time.ago": "för {duration} sedan",
  "time.in": "om {duration}",
  "time.just_now": "nyss",
  "time.expired": "utgången",
  "time.left": "{duration} kvar",

  "away.away": "Borta",
  "away.home": "Hemma",
  "away.until": "Borta till {time}",
  "away.tap_end": "Borta är på: tryck för att avsluta",
  "away.tap_start": "Tryck för att starta Borta (utan sluttid)",
  "away.confirm_active": "Sätta {name} till Aktiv? NibePi skriver då till värmepumpen via lease.",

  "alert.gateway_offline": "Gateway frånkopplad",
  "alert.lease_error": "Fel i lease för kurvjustering",
  "alert.lease_error_detail": "Fel i lease för kurvjustering: {error}",
  "alert.model_confidence": "Husmodellens säkerhet är {confidence} men kostnadsoptimeraren är vald",
  "alert.rmu_lease": "RMU40-matningen är på men dess lease är {state}",
  "alert.away_mode": "Borta är på men {mode} är {state}: inget skrivs till pumpen",
  "alert.heating_mode": "läge {system}",
  "alert.hot_water_mode": "varmvattenläget",
  "state.missing": "saknas",

  "mode.off": "Av",
  "mode.shadow": "Skugga",
  "mode.active": "Aktiv",
  "model.prior": "startvärden (ej inlärd)",
  "model.low": "låg",
  "model.medium": "medel",
  "model.high": "hög",
  "lease.state.active": "aktiv",
  "lease.state.grace": "respit",
  "lease.state.restoring": "återställer",
  "lease.state.inactive": "inaktiv",
  "lease.state.error": "fel",
  "lease.err.not_ready": "Värmepumpen är inte redo än.",
  "lease.err.rmu_not_connected": "RMU40 S{system} är inte ansluten: aktivera RMU40 för klimatsystem {system} i pumpen (meny 5.2) och låt system.rmu_emulation vara på",
  "lease.err.rmu_read_only": "RMU40 S{system} är skrivskyddad (en annan RMU40 svarar på bussen eller rmu_emulation=false)",
  "lease.err.limits": "Värdet {value} ligger utanför leasens gränser {min}..{max}",
  "on": "på",
  "off": "av",

  "price.very_cheap": "väldigt billigt",
  "price.cheap": "billigt",
  "price.normal": "normalt",
  "price.expensive": "dyrt",
  "price.very_expensive": "väldigt dyrt",

  "hwmode.economy": "ekonomi",
  "hwmode.normal": "normal",
  "hwmode.luxury": "lyx",

  "lease.lease_state_s1": "Kurvjustering S1",
  "lease.lease_state_s2": "Kurvjustering S2",
  "lease.rmu_lease_state_s1": "RMU40 S1",
  "lease.rmu_lease_state_s2": "RMU40 S2",
  "lease.hw_comfort_lease_state": "Varmvattenläge",
  "lease.hw_period_lease_state": "Varmvattenperiod",
  "lease.hw_temp_lux_lease_state": "Tillfällig lyx",
  "lease.hw_lux_stop_lease_state": "Stopptemperatur lyx",

  "climate.title": "Klimat {system}",
  "climate.indoor": "Inomhus",
  "climate.target": "Börvärde",
  "climate.outdoor": "Utomhus",
  "climate.away": "borta",
  "climate.normal": "normalt {value}",
  "climate.pump_offset": "Pumpens kurvjustering",
  "climate.planned": "Planerad ({source})",
  "climate.optimiser_now": "Optimerare nu",
  "climate.strategy": "Strategi",
  "climate.mode": "Läge",
  "source.rules": "regler",
  "source.optimiser": "optimerare",
  "source.comfort_floor": "komfortgolv",
  "part.indoor": "Inomhus",
  "part.price": "Elpris",
  "part.weather": "Prognos",
  "part.away": "Borta",
  "part.optimiser": "Optimerare",
  "part.comfort_floor": "Komfortgolv",
  "part.rules": "Regler skulle ge",

  "plan.title": "Optimerarens plan {system}",
  "plan.empty": "Ingen plan från optimeraren för de närmaste {hours} h än.",
  "plan.empty_why": "Ingen plan från optimeraren för de närmaste {hours} h än: {problem}.",
  "plan.aria": "Optimerarens plan",
  "plan.comfort_band_range": "Komfortintervall {lower}–{upper} °C",
  "plan.now": "nu",
  "plan.legend_offset": "Planerad kurvjustering (höger)",
  "plan.legend_indoor": "Beräknad inomhus °C (vänster)",
  "plan.legend_band": "Komfortintervall",
  "plan.legend_cheap": "Billigt",
  "plan.legend_expensive": "Dyrt",
  "plan.expected_savings": "Förväntad besparing",
  "plan.cost_baseline": "Kostnad plan / referens",
  "plan.planned": "Planerad",
  "plan.not_applied": "(ej tillämpad)",
  "plan.why": "Varför ({count})",
  "plan.segment": "{from}–{to}: kurvjustering {offset}, inomhus {indoor} °C",

  "hw.title": "Varmvatten",
  "hw.top": "Topp (BT7)",
  "hw.charging": "Laddning (BT6)",
  "hw.planned_action": "Planerad åtgärd",
  "hw.reasons": "Orsaker ({count})",
  "hw.next_draw": "Nästa tappning",
  "hw.next_charge": "Nästa laddning",
  "hw.by_optimiser": "optimerare",
  "hw.by_vvai": "VV-AI",
  "hw.next_24h": "Närmaste 24 h",
  "hw.now": "nu",
  "hw.legend_vv": "VV-AI-fönster",
  "hw.legend_charge": "Optimerad laddning",
  "hw.legend_legionella": "Legionella",
  "hw.nothing_planned": "inget planerat",
  "hw.mode": "Läge",
  "hw.leases": "Leases",
  "hw.window_vv": "VV-AI {mode} {from}–{to}",
  "hw.window_charge": "Laddning {from}–{to}",
  "hw.window_charge_to": "Laddning {from}–{to} till {temperature} °C",
  "hw.window_legionella": "Legionella {from}–{to}",
  "hw.window_legionella_to": "Legionella {from}–{to} till {temperature} °C",
  "reason.away": "Borta",
  "reason.price_comfort": "Prisstyrd komfort",
  "reason.autoluxury": "Automatisk lyx",
  "reason.priority": "Prioritet",
  "reason.optimiser": "Optimering",
  "reason.vv_control": "VV-styrning",
  "reason.legionella": "Legionella",

  "leg.label": "Legionella",
  "leg.label_temp": "Legionella {temperature} °C",
  "leg.next": "Nästa legionella",
  "leg.running": "pågår",
  "leg.to": "till {temperature} °C",
  "leg.planned": "planerad",
  "leg.hot_water_off": "varmvatten av",
  "leg.guard_off": "skyddet av",
  "leg.if_pump_not": "om pumpen inte gjort det",
  "leg.by.ha": "NibePi",
  "leg.by.pump": "pumpen",
  "leg.by.history": "historik",
  "leg.result.done": "klar",
  "leg.result.cancelled": "avbruten",
  "leg.result.aborted: timeout": "avbruten: tidsgräns",
  "leg.result.aborted: BT7 missing or stale": "avbruten: BT7 saknas eller är inaktuell",

  "gw.title": "NibePi-gateway",
  "gw.status_page": "Statussida",
  "gw.gateway": "Gateway",
  "gw.online": "Ansluten",
  "gw.offline": "Frånkopplad",
  "gw.unknown": "Okänd",
  "gw.core": "Kärna",
  "gw.last_data": "Senaste data",
  "gw.raw_writes": "Råskrivningar",
  "gw.bus_errors": "Bussfel",
  "gw.last_message": "Senaste meddelande",
  "gw.leases": "Leases",
  "gw.value": "värde {value}",
  "gw.rmu_feed": "RMU40-matning {system}",
  "gw.version_gateway": "Gateway v{version}",
  "gw.version_build": "Gateway v{version} (build {build})",
  "gw.version_integration": "Integration v{version}",

  "sav.title": "Värmepumpens kostnad",
  "sav.no_sensors": "Inga sensorer för kostnad eller besparing hittades.",
  "sav.vs_plan": "({pct} % mot planen)",
  "sav.days": "{label}, {count} dagar",
  "sav.total": "totalt {value} {currency}",
  "sav.cost_today": "Kostnad idag",
  "sav.savings_today": "Förväntad besparing idag",
  "sav.predicted": "Beräknat hittills",
  "sav.cost_yesterday": "Kostnad igår",
  "sav.saved_yesterday": "Besparing igår",
  "sav.parts": "Besparing idag: {parts} {currency}",
  "sav.cost_per_day": "Kostnad per dag",
  "sav.savings_per_day": "Besparing per dag",
  "sav.cost_note": "Kostnaden kräver en elmätare för bara värmepumpen (Mätaren mäter: värmepumpen).",
  "sav.hot_water": "Varmvatten",

  "editor.system": "Klimatsystem",
  "editor.entry_id": "NibePi-post",
  "editor.entry_id_help": "Behövs bara med flera NibePi-poster",
  "editor.title": "Rubrik",
  "editor.title_help": "Ersätter kortets standardrubrik",
  "editor.hours": "Timmar framåt",
  "editor.status_url": "Adress till gatewayns statussida",
  "editor.status_url_help": "t.ex. http://nibepi.local:8080",

  "card.nibepi-climate-card.name": "NibePi klimat",
  "card.nibepi-climate-card.description":
    "Inomhus mot börvärde, utomhus, planerad och pumpens kurvjustering med dess delar, Borta-knappen, strategi och läge samt varningar för ett klimatsystem.",
  "card.nibepi-plan-card.name": "NibePi optimerarens plan",
  "card.nibepi-plan-card.description":
    "Diagram över optimerarens planerade kurvjustering och beräknade inomhustemperatur med komfortintervall och prisnivåer, samt förväntad besparing.",
  "card.nibepi-hotwater-card.name": "NibePi varmvatten",
  "card.nibepi-hotwater-card.description":
    "BT7/BT6, planerad åtgärd och dess orsaker, nästa tappning och laddning, legionellaskyddet, planerade laddningar de närmaste 24 h, Borta-knappen, varmvattenläge och leases.",
  "card.nibepi-gateway-card.name": "NibePi-gateway",
  "card.nibepi-gateway-card.description":
    "Gatewayns status, senaste data, råskrivningar, bussfel, varje lease med nedräkning och RMU40-matningarna.",
  "card.nibepi-savings-card.name": "NibePi kostnad och besparing",
  "card.nibepi-savings-card.description":
    "Värmepumpens kostnad och förväntade besparing idag, mot planen och igår, med kurvor för 7 dagar.",
};

export const STRINGS = { en, sv };

/** "sv" for any Swedish language tag, else "en". */
export function normLang(code) {
  return /^sv(?:$|[-_])/i.test(String(code || "")) ? "sv" : "en";
}

/** The cards' language from Home Assistant: its profile language. */
export function langOf(hass) {
  return normLang(hass?.locale?.language || hass?.language);
}

/** The language for the card picker, before any hass object exists. */
export function registrationLang() {
  return normLang(globalThis.document?.documentElement?.lang || globalThis.navigator?.language);
}

export function hasKey(key) {
  return Object.prototype.hasOwnProperty.call(en, key);
}

/** The text of ``key`` in ``lang`` with {placeholders} from ``vars``;
 * English when the language lacks it, the key when English does too. */
export function t(lang, key, vars) {
  const dict = STRINGS[normLang(lang)];
  let text = dict[key] ?? en[key] ?? key;
  if (vars) {
    text = text.replace(/\{(\w+)\}/g, (whole, name) => (vars[name] === undefined || vars[name] === null ? whole : String(vars[name])));
  }
  return text;
}

// The gateway's lease errors are English; the ones the cards know are
// shown in the language (with their values), any other one as it is.
const LEASE_ERRORS = [
  [/^Heatpump is not ready yet\.?$/, "lease.err.not_ready", () => ({})],
  [/^RMU40 S(\d+) is not connected\b/, "lease.err.rmu_not_connected", (m) => ({ system: m[1] })],
  [/^RMU40 S(\d+) is read-only\b/, "lease.err.rmu_read_only", (m) => ({ system: m[1] })],
  [/^Value (\S+) outside lease limits (\S+?)\.\.(\S+)$/, "lease.err.limits", (m) => ({ value: m[1], min: m[2], max: m[3] })],
];

/** A lease ``error`` from the gateway in ``lang``: English unchanged. */
export function leaseErrorText(lang, error) {
  const text = String(error ?? "");
  if (normLang(lang) === "en") return text;
  for (const [pattern, key, vars] of LEASE_ERRORS) {
    const m = pattern.exec(text.trim());
    if (m) return t(lang, key, vars(m));
  }
  return text;
}

// --------------------------------------------------------------------------
// Locale formatting
// --------------------------------------------------------------------------

// Home Assistant's number_format setting -> an Intl locale for numbers.
const NUMBER_LOCALES = { comma_decimal: "en-US", decimal_comma: "de", space_comma: "fr" };

/** The locale context of the cards from hass: {lang, locale, numberLocale,
 * hour12, grouping}. ``hour12`` is undefined when the language decides. */
export function localeOf(hass) {
  const l = hass?.locale || {};
  const lang = langOf(hass);
  const locale = l.language || hass?.language || lang;
  let hour12;
  if (l.time_format === "12") hour12 = true;
  else if (l.time_format === "24") hour12 = false;
  else if (l.time_format === "system") hour12 = systemHour12();
  return {
    lang,
    locale,
    numberLocale: NUMBER_LOCALES[l.number_format] || (l.number_format === "system" ? undefined : locale),
    grouping: l.number_format !== "none",
    hour12,
  };
}

function systemHour12() {
  try {
    const cycle = new Intl.DateTimeFormat(undefined, { hour: "numeric" }).resolvedOptions().hourCycle;
    return cycle === "h11" || cycle === "h12";
  } catch (_err) {
    return undefined;
  }
}

/** A locale context from a context, a language code or nothing (English,
 * 24 h, no grouping: what the helpers produce without one). */
export function toLoc(loc) {
  if (loc && typeof loc === "object") return loc;
  const lang = normLang(loc);
  return { lang, locale: lang, numberLocale: lang, grouping: Boolean(loc), hour12: loc ? undefined : false };
}

const cache = new Map();

function cached(kind, locale, options, make) {
  const key = `${kind}|${locale}|${JSON.stringify(options)}`;
  let f = cache.get(key);
  if (!f) {
    try {
      f = make(locale, options);
    } catch (_err) {
      f = make("en", options); // an unknown locale tag
    }
    cache.set(key, f);
  }
  return f;
}

/** ``n`` (a finite number) with ``digits`` decimals in the locale. */
export function numberText(n, digits, loc) {
  const c = toLoc(loc);
  const options = { minimumFractionDigits: digits, maximumFractionDigits: digits, useGrouping: c.grouping !== false };
  return cached("n", c.numberLocale, options, (l, o) => new Intl.NumberFormat(l, o)).format(n);
}

function clockOptions(c) {
  const options = { hour: "2-digit", minute: "2-digit" };
  // hourCycle, not hour12: hour12: false gives "24:05" in some engines.
  if (c.hour12 === true) options.hourCycle = "h12";
  else if (c.hour12 === false) options.hourCycle = "h23";
  return options;
}

// "06:05" in 24 h, but "6:05 AM" rather than "06:05 AM" in 12 h.
function clockFormat(locale, options) {
  const f = new Intl.DateTimeFormat(locale, options);
  return f.resolvedOptions().hour12 ? new Intl.DateTimeFormat(locale, { ...options, hour: "numeric" }) : f;
}

/** The time of day of epoch ms ``t``: "14:05", or "2:05 PM" in 12 h. */
export function clockText(t, loc) {
  const c = toLoc(loc);
  return cached("c", c.locale, clockOptions(c), clockFormat).format(new Date(t));
}

/** The short weekday of epoch ms ``t``: "Mon", "mån". */
export function weekdayText(t, loc) {
  const c = toLoc(loc);
  return cached("w", c.locale, { weekday: "short" }, (l, o) => new Intl.DateTimeFormat(l, o)).format(new Date(t));
}

/** The date of epoch ms ``t``: "10/12/2026", "2026-10-12". */
export function dateText(t, loc) {
  const c = toLoc(loc);
  return cached("d", c.locale, {}, (l, o) => new Intl.DateTimeFormat(l, o)).format(new Date(t));
}
