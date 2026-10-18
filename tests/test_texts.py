from __future__ import annotations

import copy
import json
import os
import pickle
import re
import unittest
from datetime import timedelta

import context  # noqa: F401
from context import UTC, utc
from logic.away import away_component
from logic.indoor import compute_indoor
from logic.inputs import ForecastPoint, Reading
from logic.optimizer.gate import gate
from logic.optimizer.house import HouseFit
from logic.optstrategy import choose_total
from logic.price import PriceLevel, compute_price
from logic.rmu import room_temperature
from logic.texts import (
    CATALOGUE,
    I18N,
    LANGS,
    Msg,
    attributes,
    code_of,
    codes,
    i18n,
    lang_of,
    localise,
    msg,
    placeholders,
    render,
    structured,
    term,
)
from logic.weather import WeatherSettings, compute_weather


class CatalogueTest(unittest.TestCase):
    def test_every_code_has_both_languages_with_the_same_placeholders(self) -> None:
        self.assertEqual(LANGS, ("en", "sv"))
        for code, texts in CATALOGUE.items():
            with self.subTest(code=code):
                self.assertEqual(len(texts), 2)
                en, sv = texts
                self.assertTrue(en.strip() and sv.strip())
                self.assertEqual(placeholders(en), placeholders(sv))

    def test_no_english_left_in_swedish(self) -> None:
        # Texts that are the same in both are names, units or codes.
        same = {code for code, (en, sv) in CATALOGUE.items() if en == sv}
        allowed = {
            "common.join", "price.level", "price.status_ok", "hw.comfort_mode",
            "hwmode.normal", "away_hw.minimum", "away_hw.normal",
        }
        self.assertEqual(same - allowed, set())


class RenderTest(unittest.TestCase):
    def test_english_is_the_str_value(self) -> None:
        text = msg("priority.over", bt7=50.5, start=46.0)
        self.assertIsInstance(text, str)
        self.assertEqual(text, "BT7 50.5 over the start temperature 46.0")
        self.assertEqual(text.code, "priority.over")
        self.assertEqual(text.render("en"), "BT7 50.5 over the start temperature 46.0")

    def test_swedish_numbers(self) -> None:
        self.assertEqual(msg("priority.over", bt7=50.5, start=46.0).render("sv"), "BT7 50,5 °C över starttemperaturen 46,0 °C")
        self.assertEqual(msg("away.fixed", offset=-3.0).render("sv"), "Borta: fast kurvjustering −3 (inomhusreglering inte aktiv)")
        self.assertEqual(msg("away.fixed", offset=2.0).render("sv"), "Borta: fast kurvjustering +2 (inomhusreglering inte aktiv)")
        self.assertEqual(msg("weather.raise_blocked", offset=1.25).render("sv"), "Höjning spärrad (+1,25)")
        self.assertEqual(msg("weather.lower_blocked", offset=-0.5).render("en"), "lowering blocked (-0.50)")
        self.assertEqual(msg("weather.lower_blocked", offset=-0.5).render("sv"), "Sänkning spärrad (−0,50)")
        # An integer spec takes a whole float.
        self.assertEqual(msg("opt.plan_old", minutes=90.0), "plan older than 90 min")

    def test_nested_texts_render_in_the_same_language_lower_case(self) -> None:
        reason = msg(
            "strategy.not_used",
            reason=msg("gate.low_confidence", level=term("model_level", "low"), confidence=0.52,
                       min_level=term("model_level", "high")),
        )
        self.assertEqual(reason, "optimiser not used: model confidence low (0.52) below high")
        self.assertEqual(reason.render("sv"), "Optimeraren används inte: modellens säkerhet är låg (0,52), lägre än hög")

    def test_a_leading_value_keeps_its_case(self) -> None:
        self.assertEqual(msg("meter.not_found", entity="sensor.meter").render("sv"), "sensor.meter hittades inte")
        level = msg("price.level", level=term("price_level", "CHEAP"), source=term("price_source", "optimiser"))
        self.assertEqual(level, "CHEAP (optimiser)")
        self.assertEqual(level.render("sv"), "Billigt (prisoptimering)")

    def test_fallbacks(self) -> None:
        self.assertEqual(render("no.such.code", {}, "sv"), "no.such.code")
        self.assertEqual(msg("opt.no_plan").render("fr"), "no plan")
        self.assertEqual(render("opt.no_plan", None, "de"), "no plan")
        # Missing parameters: the code rather than an exception.
        self.assertEqual(render("priority.over", {}, "sv"), "priority.over")
        self.assertEqual(term("price_level", "ODD"), "ODD")
        self.assertNotIsInstance(term("price_level", "ODD"), Msg)

    def test_lang_of(self) -> None:
        for tag, lang in (("sv", "sv"), ("sv-SE", "sv"), ("SV_fi", "sv"), ("en", "en"), ("en-GB", "en"),
                          ("de", "en"), ("svx", "en"), ("", "en"), (None, "en")):
            self.assertEqual(lang_of(tag), lang, tag)

    def test_localise_and_codes(self) -> None:
        reasons = {"indoor": msg("indoor.setpoint_missing"), "price": "plain", "n": 3}
        self.assertEqual(
            localise({"reasons": reasons, "list": [msg("opt.no_plan")], "t": (msg("common.off"),)}, "sv"),
            {"reasons": {"indoor": "Börvärde saknas", "price": "plain", "n": 3}, "list": ["Ingen plan"], "t": ("Av",)},
        )
        out = localise(reasons, "en")
        self.assertIs(type(out["indoor"]), str)
        self.assertEqual(codes(reasons), {"indoor": "indoor.setpoint_missing", "price": None, "n": None})
        self.assertIsNone(code_of("text"))

    def test_copy_and_pickle_keep_the_code(self) -> None:
        text = msg("gate.floor", floor=19.5)
        for clone in (copy.copy(text), copy.deepcopy(text), pickle.loads(pickle.dumps(text))):
            self.assertEqual(clone, text)
            self.assertEqual(clone.code, "gate.floor")
            self.assertEqual(clone.render("sv"), "Under komfortgolvet 19,5 °C: värmer")


def from_structured(value):
    """A Msg back from its structured form (what the cards render)."""
    if isinstance(value, dict) and "c" in value:
        return Msg(value["c"], {k: from_structured(v) for k, v in (value.get("p") or {}).items()})
    return value


class StructuredTest(unittest.TestCase):
    """The ``i18n`` attribute: the texts as codes and parameters."""

    def test_structured_text(self) -> None:
        reason = msg("strategy.not_used", reason=msg("gate.low_confidence", level=term("model_level", "low"),
                                                     confidence=0.52, min_level=term("model_level", "high")))
        self.assertEqual(structured(reason), {
            "c": "strategy.not_used",
            "p": {"reason": {"c": "gate.low_confidence", "p": {
                "level": {"c": "model_level.low"}, "confidence": 0.52, "min_level": {"c": "model_level.high"},
            }}},
        })
        self.assertEqual(structured(msg("opt.no_plan")), {"c": "opt.no_plan"})
        # JSON-safe parameters: plain str, no NaN, other objects as text.
        odd = structured(Msg("common.error", {"error": float("nan")}))
        self.assertEqual(odd, {"c": "common.error", "p": {"error": None}})
        self.assertIs(type(structured(msg("meter.not_found", entity=msg("common.off")))["p"]["entity"]), dict)
        self.assertEqual(structured(Msg("x", {"when": utc(2026, 1, 1)}))["p"]["when"], "2026-01-01 00:00:00+00:00")

    def test_i18n_keeps_only_the_texts(self) -> None:
        value = {"a": msg("opt.no_plan"), "b": "plain", "n": 1, "d": {"x": msg("common.off"), "y": 2},
                 "l": [msg("common.off"), "plain"], "e": {"z": 1}, "t": (1, 2)}
        self.assertEqual(i18n(value), {"a": {"c": "opt.no_plan"}, "d": {"x": {"c": "common.off"}},
                                       "l": [{"c": "common.off"}, None]})
        self.assertIsNone(i18n({"a": 1, "b": ["x"]}))
        self.assertIsNone(i18n("plain"))

    def assert_round_trip(self, attrs, lang="en") -> dict:
        out = attributes(attrs, lang)
        json.dumps(out)  # a state attribute must serialise
        texts = out[I18N]

        def check(orig, rendered, struct):
            if isinstance(orig, Msg):
                rebuilt = from_structured(struct)
                for lang_ in LANGS:
                    self.assertEqual(rebuilt.render(lang_), orig.render(lang_))
                self.assertEqual(rendered, orig.render(lang))
            elif isinstance(orig, dict):
                for key, item in orig.items():
                    if struct is not None and key in struct:
                        check(item, rendered[key], struct[key])
            elif isinstance(orig, (list, tuple)):
                for i, item in enumerate(orig):
                    check(item, rendered[i], struct[i] if struct else None)

        for key, value in attrs.items():
            if key in texts:
                check(value, out[key], texts[key])
            else:
                self.assertEqual(i18n(value), None, key)
        return out

    def test_optimiser_plan_sensor(self) -> None:
        fit = HouseFit(None, True, "ok", 400, 0.9, 0.02, 0.05, 0.05, 0.52, "low")
        decision = gate(fit, 2, horizon_h=24, t_in=21.0, floor=19.5, base_offset=0, max_up=3)
        attrs = {
            "strategy": "optimiser",
            "gate": {"use": decision.use_optimizer, "offset": decision.offset, "reason": decision.reason,
                     "reason_code": code_of(decision.reason)},
            "problem": msg("opt.no_plan_why", why=msg("opt.no_indoor")),
            "summary": msg("explain.summary", kwh=12.34, cost=5.6, unit="SEK", base=-1, base_kwh=13.0,
                           base_cost=6.1, savings=0.5, t_min=20.6, t_max=21.4, changes=3),
            "explanation": [msg("explain.line", span="06:00-08:00", offset=-2,
                                why=msg("explain.expensive", price=msg("explain.price", price=1.5, unit="SEK/kWh",
                                                                       avg=1.0)),
                                t_end=20.9, at="08:00")],
            "plan_24h": [["06:00", -2, 20.9]],
        }
        out = self.assert_round_trip(attrs, "sv")
        self.assertEqual(out["problem"], "Ingen plan (inomhustemperatur saknas)")
        self.assertEqual(set(out[I18N]), {"gate", "problem", "summary", "explanation"})
        self.assertEqual(out[I18N]["gate"], {"reason": structured(decision.reason)})
        self.assertNotIn("plan_24h", out[I18N])

    def test_planned_offset_and_component_sensors(self) -> None:
        now = utc(2026, 1, 10, 12, 0)
        indoor = compute_indoor(Reading(21.5, now), 21.0, 2.0, now)
        level = PriceLevel("CHEAP", 50.0, now, {}, "optimiser")
        price = compute_price(level, {"CHEAP": 2}, now)
        choice = choose_total("rules", 1.0, None)
        reasons = {"indoor": indoor.reason, "price": price.reason, "weather": "disabled"}
        out = self.assert_round_trip({"strategy_reason": choice.reason, "reasons": reasons,
                                      "reason_codes": codes(reasons), "components": {"indoor": -1.0}})
        self.assertEqual(out[I18N]["reasons"], {"indoor": structured(indoor.reason),
                                                "price": structured(price.reason)})
        self.assertEqual(out[I18N]["reasons"]["price"]["p"]["level"], {"c": "price_level.CHEAP"})
        component = self.assert_round_trip({"reason": indoor.reason, "inputs_ok": True, "indoor": 21.5})
        self.assertEqual(set(component[I18N]), {"reason"})

    def test_hot_water_and_gateway_sensors(self) -> None:
        from logic.gateway import status_hint

        blocks = [{"start": "06:00", "action": "charge", "reason": msg("tank.price", price=1.0, avg=1.5)},
                  {"start": "08:00", "action": "idle", "reason": ""}]
        out = self.assert_round_trip({"problem": None, "blocks": blocks}, "sv")
        self.assertEqual(out[I18N], {"blocks": [{"reason": structured(blocks[0]["reason"])}, None]})
        out = self.assert_round_trip({"hint": status_hint("nibe/status"), "entities": {"a": "b"}})
        self.assertEqual(out[I18N]["hint"], {"c": "gateway.no_status", "p": {"topic": "nibe/status"}})

    def test_no_texts_no_attribute(self) -> None:
        self.assertEqual(attributes({"a": 1}, "sv"), {"a": 1})

    def test_platforms_leave_the_i18n_attribute_unrecorded(self) -> None:
        root = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "custom_components", "nibepi")
        found = 0
        for name in sorted(os.listdir(root)):
            if not name.endswith(".py"):
                continue
            with open(os.path.join(root, name), encoding="utf-8") as handle:
                source = handle.read()
            for match in re.finditer(r"_unrecorded_attributes = frozenset\((.*?)\)\n", source, re.S):
                found += 1
                self.assertIn("I18N", match.group(1), name)
        self.assertGreater(found, 5)


def both(text):
    """(English, Swedish) of a logic text."""
    return text.render("en"), text.render("sv")


class ClimateTextsTest(unittest.TestCase):
    def setUp(self) -> None:
        self.now = utc(2026, 1, 10, 12, 0)

    def test_indoor(self) -> None:
        d = compute_indoor(Reading(21.5, self.now), 21.0, 2.0, self.now)
        self.assertEqual(both(d.reason), (
            "setpoint 21.0, indoor 21.5, factor 2",
            "Börvärde 21,0 °C, inomhus 21,5 °C, faktor 2",
        ))
        self.assertEqual(d.reason.code, "indoor.ok")
        d = compute_indoor(None, 21.0, 2.0, self.now)
        self.assertEqual(both(d.reason), ("indoor sensor missing", "Inomhustemperatur saknas"))
        old = Reading(21.0, self.now - timedelta(hours=2))
        self.assertEqual(compute_indoor(old, 21.0, 2.0, self.now).reason.render("sv"), "Inomhustemperaturen är inaktuell")

    def test_price(self) -> None:
        level = PriceLevel("CHEAP", 50.0, self.now, {}, "optimiser")
        d = compute_price(level, {"CHEAP": 2}, self.now)
        self.assertEqual(both(d.reason), ("CHEAP (optimiser)", "Billigt (prisoptimering)"))
        self.assertEqual(both(compute_price(None, {}, self.now).reason),
                         ("no price for the current hour", "Inget pris för den här timmen"))
        level = PriceLevel("VERY_EXPENSIVE", 300.0, self.now, {}, "own thresholds")
        d = compute_price(level, {"VERY_EXPENSIVE": -2}, self.now, True, 0.5, Reading(20.2, self.now), 20.0)
        self.assertEqual(both(d.reason), (
            "VERY_EXPENSIVE, indoor 20.2 not above 20.5",
            "Väldigt dyrt, inomhus 20,2 °C är inte över 20,5 °C",
        ))
        d = compute_price(level, {"VERY_EXPENSIVE": -2}, self.now, True, 0.5, None, 20.0)
        self.assertEqual(d.reason.render("sv"), "Väldigt dyrt, spärren kan inte avgöra: inomhustemperatur saknas")

    def test_weather(self) -> None:
        forecast = [ForecastPoint(self.now + timedelta(hours=h), temperature=-5.0 - h) for h in range(8)]
        d = compute_weather(Reading(-5.0, self.now), 9.0, forecast, self.now, self.now, WeatherSettings(hours=6))
        self.assertEqual(both(d.reason), (
            "outdoor -5.0, forecast -11.0 in 6 h",
            "Ute \u22125,0 °C, prognos \u221211,0 °C om 6 h",
        ))
        d = compute_weather(Reading(-5.0, self.now), 9.0, None, None, self.now)
        self.assertEqual(both(d.reason), ("no forecast", "Ingen prognos"))

    def test_away(self) -> None:
        self.assertEqual(both(away_component(False, -3).reason), (
            "away: fixed curve offset -3 (indoor control not active)",
            "Borta: fast kurvjustering \u22123 (inomhusreglering inte aktiv)",
        ))

    def test_gate_and_strategy(self) -> None:
        fit = HouseFit(None, True, "ok", 400, 0.9, 0.02, 0.05, 0.05, 0.52, "low")
        args = dict(horizon_h=24, t_in=21.0, floor=19.5, base_offset=0, max_up=3)
        decision = gate(fit, 2, **args)
        choice = choose_total("optimiser", 1.0, decision)
        self.assertEqual(choice.reason.code, "strategy.not_used")
        self.assertEqual(both(choice.reason), (
            "optimiser not used: model confidence low (0.52) below high",
            "Optimeraren används inte: modellens säkerhet är låg (0,52), lägre än hög",
        ))
        cold = gate(fit, 2, **dict(args, t_in=19.0))
        self.assertEqual(both(choose_total("optimiser", 1.0, cold).reason), (
            "below comfort floor 19.5 °C: heating", "Under komfortgolvet 19,5 °C: värmer",
        ))
        self.assertEqual(choose_total("optimiser", 1.0, cold).source, "comfort_floor")
        ok = gate(HouseFit(None, True, "ok", 400, 0.9, 0.02, 0.05, 0.05, 0.9, "high"), 2, **args)
        self.assertEqual(choose_total("optimiser", 1.0, ok).source, "optimiser")
        self.assertEqual(both(choose_total("optimiser", 1.0, ok).reason), ("optimiser", "Optimeraren styr"))
        self.assertEqual(both(choose_total("rules", 1.0, None).reason), ("rules", "Regler"))
        missing = gate(fit, 2, **dict(args, t_in=None))
        self.assertEqual(missing.reason.render("sv"), "Inomhustemperatur saknas")

    def test_rmu(self) -> None:
        self.assertEqual(both(room_temperature(Reading(21.44, self.now), self.now).reason),
                         ("room temperature 21.4", "Rumstemperatur 21,4 °C"))
        self.assertEqual(both(room_temperature(None, self.now).reason), ("room temperature missing", "Rumstemperatur saknas"))


class OptimiserTextsTest(unittest.TestCase):
    def test_plan_explanation_and_summary(self) -> None:
        from datetime import date
        from zoneinfo import ZoneInfo

        from logic.optimizer.explain import explain_plan, summary
        from logic.optimizer.horizon import build_horizon
        from logic.optimizer.planner import plan_heating
        from test_opt_planner import COMFORT, CURVE, ENERGY, HOUSE, base_settings, slots_from_numbers

        tz = ZoneInfo("Europe/Stockholm")
        values = [3.0 if i % 24 < 6 else 0.5 for i in range(96)]
        prices = slots_from_numbers(values, date(2026, 1, 10), tz)
        settings = base_settings()
        plan = plan_heating(build_horizon(prices, prices[0].start, -2.0), 21.0, HOUSE, CURVE, ENERGY, COMFORT,
                            settings, settings.base_offset)
        blocks = explain_plan(plan, COMFORT, tz)
        line = next(b.text for b in blocks if b.reason == "expensive")
        self.assertEqual(line.code, "explain.line")
        en, sv = both(line)
        self.assertRegex(en, r"^\d\d:\d\d-\d\d:\d\d offset -\d: .*, indoor \d+\.\d °C at \d\d:\d\d$")
        self.assertRegex(sv, r"^\d\d:\d\d-\d\d:\d\d kurvjustering \u2212\d: .*, inomhus \d+,\d °C kl\. \d\d:\d\d$")
        self.assertIn("mot snittet", sv)
        en, sv = both(summary(plan, "SEK"))
        self.assertTrue(en.startswith("Planned ") and en.endswith(" changes."))
        self.assertTrue(sv.startswith("Planerat ") and sv.endswith(" ändringar."))

    def test_problems(self) -> None:
        from logic.optrun import TankInputs, plan_tank

        now = utc(2026, 1, 10, 12, 0)
        tank = plan_tank(TankInputs([], now, UTC, None, 50.0, 45.0, 50.0, 60.0, 45.0, [0.0] * 168))
        self.assertEqual(both(tank.problem), ("BT6 missing or stale", "BT6 saknas eller är inaktuell"))
        self.assertEqual(both(msg("opt.no_plan_why", why=msg("opt.no_prices"))),
                         ("no plan (no prices cover now)", "Ingen plan (inga priser för nu)"))

    def test_hot_water_blocks(self) -> None:
        self.assertEqual(both(msg("tank.price_before", price=1.234, avg=1.5, at="06:00")), (
            "price 1.23 vs 1.50 average, before the draw expected at 06:00",
            "Pris 1,23 mot snittet 1,50, före tappningen som väntas kl. 06:00",
        ))
        self.assertEqual(both(msg("tank.waiting")), ("waiting for cheaper slots", "Väntar på billigare timmar"))


class HotWaterTextsTest(unittest.TestCase):
    def test_controller_reasons(self) -> None:
        from dataclasses import replace

        from logic.hwcontrol import MODE_ACTIVE
        from test_hwcontrol import Rig, local

        rig = Rig(local(2026, 10, 13, 12))
        rig.values[47050] = 1.0
        rig.controller.legionella.state.last_done = rig.now - timedelta(days=1)
        rig.price_level = "CHEAP"
        decision = rig.decide(priority=True, price_comfort=True)
        reasons = localise(decision.reasons, "sv")
        self.assertEqual(reasons["priority"], "BT7 45,0 °C vid starttemperaturen 45,0 °C")
        self.assertEqual(reasons["price_comfort"], "Billigt: normal")
        self.assertEqual(reasons["autoluxury"], "Av")
        self.assertEqual(reasons["vv_control"], "Av")
        self.assertTrue(reasons["legionella"].startswith("Nästa gång "))
        self.assertEqual(codes(decision.reasons)["priority"], "priority.start")
        self.assertEqual(decision.reasons["priority"], "BT7 45.0 at the start temperature 45.0")

        inputs = replace(rig.inputs(), away="economy")
        decision = rig.controller.decide(MODE_ACTIVE, {"vv_control": True, "optimiser": True}, inputs, rig.settings)
        self.assertEqual(both(decision.reasons["away"]), (
            "away (economy): economy mode, no planned charges or boosts",
            "Borta (ekonomi): ekonomiläge, inga planerade laddningar eller lyft",
        ))
        self.assertEqual(both(decision.reasons["optimiser"]),
                         ("away: no planned charges", "Borta: inga planerade laddningar"))
        self.assertTrue(decision.reasons["vv_control"].render("sv").startswith(
            "Borta (ekonomi): pumpen värmer varmvattnet själv i ekonomiläge"))

    def test_legionella(self) -> None:
        from logic.legionella import RESULT_TIMEOUT, result_text
        from test_legionella import Rig

        rig = Rig(utc(2026, 10, 14, 10))
        rig.guard.state.last_done = rig.now - timedelta(days=15)
        status = rig.tick(mode="shadow")
        self.assertEqual(both(status.reason), (
            "due: would run now (hot water mode shadow)", "Dags: skulle köra nu (varmvattenläge skugga)",
        ))
        status = rig.tick()
        self.assertEqual(both(status.reason), (
            "legionella cycle started, due (deadline): heat to 60.0 °C",
            "Legionellacykel startad, dags (sista tidpunkt): värmer till 60,0 °C",
        ))
        status = rig.tick(minutes=4 * 60)
        self.assertEqual(both(status.reason), (
            "BT7 did not hold 60.0 °C within 4 h: legionella cycle aborted",
            "BT7 höll inte 60,0 °C inom 4 h: legionellacykeln avbruten",
        ))
        status = rig.tick(minutes=60)
        self.assertEqual(status.reason.render("sv"), "Avbruten: tidsgräns; nytt försök från 2026-10-15 04:00")
        self.assertEqual(both(result_text(RESULT_TIMEOUT)), ("aborted: timeout", "Avbruten: tidsgräns"))
        self.assertEqual(result_text("something else"), "something else")
        self.assertIsNone(result_text(None))


class GatewayTextsTest(unittest.TestCase):
    def test_hint(self) -> None:
        from logic.gateway import status_hint

        self.assertEqual(both(status_hint("nibe/modbus/status")), (
            "No message on nibe/modbus/status: is the NibePi gateway running and connected to this MQTT broker "
            "with the same topic prefix?",
            "Ingen data på nibe/modbus/status: körs NibePi-gatewayen och är den ansluten till samma MQTT-broker "
            "med samma prefix?",
        ))

    def test_meter_and_price_status(self) -> None:
        self.assertEqual(both(msg("meter.bad_unit", entity="sensor.m", unit=repr("°C"))), (
            "sensor.m: unit '°C' is not a power or energy meter",
            "sensor.m: enheten '°C' hör inte till en effekt- eller energimätare",
        ))
        self.assertEqual(both(msg("price.no_prices_in", entity="sensor.nordpool")),
                         ("no prices in sensor.nordpool", "Inga priser i sensor.nordpool"))


if __name__ == "__main__":
    unittest.main()
