from __future__ import annotations

import copy
import pickle
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
from logic.texts import CATALOGUE, LANGS, Msg, code_of, codes, lang_of, localise, msg, placeholders, render, term
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


if __name__ == "__main__":
    unittest.main()
