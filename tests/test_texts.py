from __future__ import annotations

import copy
import pickle
import unittest

import context  # noqa: F401
from logic.texts import CATALOGUE, LANGS, Msg, code_of, codes, lang_of, localise, msg, placeholders, render, term


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


if __name__ == "__main__":
    unittest.main()
