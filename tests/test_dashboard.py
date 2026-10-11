from __future__ import annotations

import os
import re
import sys
import unittest

import context  # noqa: F401
from logic import dashboard as d


def full_ids(systems=("s1",), gateway=True, bus=True):
    ids = {key: "%s.nibe_%s" % (domain, key) for key, (domain, _) in d.integration_unique_ids("E", systems).items()}
    if gateway:
        ids.update({key: "sensor.gw_%s" % key for key in d.gateway_registers(systems)})
        for system in systems:
            ids["indoor_" + system] = ids["bt50_" + system]
    if bus:
        ids["bus_health"] = "sensor.nibe_bus_health"
    return ids


def cards_of(dashboard):
    return list(d.iter_cards(dashboard))


def view(dashboard, path):
    return next(v for v in dashboard["views"] if v["path"] == path)


class KeysTest(unittest.TestCase):
    def test_unique_ids(self) -> None:
        found = d.integration_unique_ids("abc", ["s1", "s2"])
        self.assertEqual(found["gateway_online"], ("binary_sensor", "abc_gateway_online"))
        self.assertEqual(found["planned_offset_s2"], ("sensor", "abc_s2_planned_offset"))
        self.assertEqual(found["strategy_s1"], ("select", "abc_s1_strategy"))
        self.assertEqual(found["hw_optimiser"], ("switch", "abc_hw_optimiser"))
        self.assertNotIn("planned_offset_s2", d.integration_unique_ids("abc", ["s1"]))

    def test_gateway_registers(self) -> None:
        regs = d.gateway_registers(["s1", "s2"])
        self.assertEqual(regs["outdoor"], 40004)
        self.assertEqual(regs["supply_s1"], 40008)
        self.assertEqual(regs["supply_s2"], 40007)
        self.assertEqual(regs["bt50_s2"], 40032)
        self.assertEqual(regs["bt7_hw_top"], 40013)
        self.assertNotIn("bt50_s2", d.gateway_registers(["s1"]))

    def test_systems_in(self) -> None:
        self.assertEqual(d.systems_in(full_ids(("s1", "s2"))), ["s1", "s2"])
        self.assertEqual(d.systems_in(full_ids(("s1",))), ["s1"])
        # A gateway register key alone does not make a system.
        self.assertEqual(d.systems_in({"supply_s2": "sensor.x"}), [])


class DashboardTest(unittest.TestCase):
    def test_views(self) -> None:
        dash = d.build_dashboard(full_ids(), {"title": "Pump"})
        self.assertEqual(dash["title"], "Pump")
        self.assertEqual([v["path"] for v in dash["views"]], ["overview", "heating", "hot-water", "gateway"])
        for v in dash["views"]:
            self.assertEqual(v["type"], "sections")
            for section in v["sections"]:
                self.assertEqual(section["type"], "grid")
                self.assertTrue(any(card["type"] != "heading" for card in section["cards"]))
        self.assertEqual(view(dash, "overview")["badges"][0]["entity"], "binary_sensor.nibe_gateway_online")

    def test_card_types_known(self) -> None:
        for systems in (("s1",), ("s1", "s2")):
            for card in cards_of(d.build_dashboard(full_ids(systems))):
                self.assertIn(card["type"], d.CARD_TYPES)
                if card["type"] == "conditional":
                    self.assertTrue(card["conditions"])
                    for cond in card["conditions"]:
                        self.assertEqual(cond["condition"], "state")
                        self.assertTrue(("state" in cond) != ("state_not" in cond))

    def test_resolves_keys(self) -> None:
        ids = full_ids()
        ids["planned_offset_s1"] = "sensor.my_own_name"
        refs = d.referenced_entities(d.build_dashboard(ids))
        self.assertIn("sensor.my_own_name", refs)
        self.assertNotIn("sensor.nibe_planned_offset_s1", refs)

    def test_references_only_given_entities(self) -> None:
        for systems in (("s1",), ("s1", "s2")):
            ids = full_ids(systems)
            refs = d.referenced_entities(d.build_dashboard(ids))
            self.assertTrue(refs)
            self.assertLessEqual(set(refs), set(ids.values()))

    def test_templates_reference_existing_entities(self) -> None:
        ids = full_ids(("s1", "s2"))
        dash = d.build_dashboard(ids)
        templates = [c["content"] for c in cards_of(dash) if c["type"] == "markdown" and "{%" in c["content"]]
        self.assertGreaterEqual(len(templates), 6)
        for text in templates:
            found = d.ENTITY_ID.findall(text)
            self.assertTrue(found, text[:80])
            self.assertLessEqual(set(found), set(ids.values()))
            self.assertNotIn("<<", text)

    def test_templates_balanced(self) -> None:
        for card in cards_of(d.build_dashboard(full_ids(("s1", "s2")))):
            text = card.get("content", "")
            opens = len(re.findall(r"{%-?\s*(?:if|for)\b", text))
            closes = len(re.findall(r"{%-?\s*end(?:if|for)\b", text))
            self.assertEqual(opens, closes, text[:80])
            self.assertEqual(text.count("{{"), text.count("}}"))
            self.assertEqual(text.count("{%"), text.count("%}"))

    def test_s2_present_and_absent(self) -> None:
        one = d.referenced_entities(d.build_dashboard(full_ids(("s1",))))
        two = d.referenced_entities(d.build_dashboard(full_ids(("s1", "s2"))))
        self.assertFalse([e for e in one if e.endswith("_s2")])
        for key in ("planned_offset_s2", "strategy_s2", "optimiser_plan_s2", "house_model_s2", "rmu_feed_s2", "supply_s2"):
            self.assertIn(full_ids(("s1", "s2"))[key], two)
        headings = [c["heading"] for c in cards_of(d.build_dashboard(full_ids(("s1", "s2")))) if c["type"] == "heading"]
        self.assertIn("Control S2", headings)
        self.assertIn("House model S2", headings)

    def test_drops_cards_for_missing_entities(self) -> None:
        ids = full_ids(gateway=False, bus=False)
        dash = d.build_dashboard(ids)
        refs = d.referenced_entities(dash)
        self.assertLessEqual(set(refs), set(ids.values()))
        types = [c["type"] for c in cards_of(dash)]
        self.assertNotIn("gauge", types)  # compressor frequency
        headings = [c["heading"] for c in cards_of(dash) if c["type"] == "heading"]
        self.assertNotIn("RS485 bus", headings)
        # Indoor, outdoor and supply are gateway sensors; the offsets are not.
        hist = [c for c in cards_of(dash) if c["type"] == "history-graph"]
        self.assertEqual([c["title"] for c in hist if c["title"] == "Indoor and outdoor"], [])
        self.assertIn("Curve offset", [c["title"] for c in hist])

    def test_drops_rows_and_empty_sections(self) -> None:
        ids = {"gateway_online": "binary_sensor.gw"}
        dash = d.build_dashboard(ids)
        self.assertEqual([v["path"] for v in dash["views"]], ["overview", "gateway"])
        self.assertEqual(set(d.referenced_entities(dash)), {"binary_sensor.gw"})
        self.assertEqual(d.build_dashboard({})["views"], [])
        # None and empty values count as missing.
        self.assertEqual(d.build_dashboard({"gateway_online": None, "price_level": ""})["views"], [])

    def test_alerts(self) -> None:
        dash = d.build_dashboard(full_ids())
        alerts = [c for c in cards_of(view(dash, "overview")) if c["type"] == "conditional"]
        by_entity = {c["conditions"][0]["entity"]: c for c in alerts}
        self.assertEqual(by_entity["binary_sensor.nibe_gateway_online"]["conditions"][0]["state"], "off")
        self.assertEqual(by_entity["sensor.nibe_lease_state_s1"]["conditions"][0]["state"], "error")
        model = by_entity["sensor.nibe_house_model_s1"]["conditions"]
        self.assertEqual(model[0]["state"], ["prior", "low"])
        self.assertEqual(model[1], {"condition": "state", "entity": "select.nibe_strategy_s1", "state": "optimiser"})
        rmu = by_entity["switch.nibe_rmu_feed_s1"]["conditions"]
        self.assertEqual(rmu[1]["state_not"], ["active", "grace"])
        for card in alerts:
            self.assertIn("<ha-alert", card["card"]["content"])

    def test_statistics_and_selects(self) -> None:
        dash = d.build_dashboard(full_ids())
        stats = [c for c in cards_of(dash) if c["type"] == "statistics-graph"]
        self.assertEqual(stats[0]["entities"], ["sensor.nibe_heat_pump_cost_today", "sensor.nibe_optimiser_savings_today"])
        self.assertEqual(stats[0]["stat_types"], ["change"])
        selects = [c for c in cards_of(dash) if c["type"] == "tile" and c["entity"].startswith("select.")]
        self.assertTrue(selects)
        for card in selects:
            self.assertEqual(card["features"], [{"type": "select-options"}])

    def test_lease_table(self) -> None:
        dash = d.build_dashboard(full_ids(("s1", "s2")))
        table = next(c["content"] for c in cards_of(view(dash, "gateway")) if c["type"] == "markdown")
        self.assertIn("('Curve offset S2', 'sensor.nibe_lease_state_s2')", table)
        self.assertIn("('Temporary lux', 'sensor.nibe_hw_temp_lux_lease_state')", table)


class ApexTest(unittest.TestCase):
    def test_chart(self) -> None:
        dash = d.build_apexcharts(full_ids(("s1", "s2")))
        cards = [c for c in cards_of(dash) if c["type"] == "custom:apexcharts-card"]
        self.assertEqual(len(cards), 2)
        series = cards[0]["series"]
        self.assertEqual([s["entity"] for s in series], ["sensor.nibe_optimiser_plan_s1"] * 2 + ["sensor.nibe_price_level"])
        self.assertIn("r[1]]", series[0]["data_generator"])
        self.assertIn("r[2]]", series[1]["data_generator"])
        self.assertIsNone(d.build_apexcharts({"price_level": "sensor.p"}))

    def test_without_price(self) -> None:
        ids = full_ids()
        del ids["price_level"]
        card = next(c for c in cards_of(d.build_apexcharts(ids)) if c["type"].startswith("custom:"))
        self.assertEqual(len(card["series"]), 2)
        self.assertEqual([y["id"] for y in card["yaxis"]], ["offset", "temp"])


class YamlTest(unittest.TestCase):
    def test_scalars(self) -> None:
        self.assertEqual(d.dump_yaml({"a": "off", "b": "on", "c": "Yes", "d": "null"}), 'a: "off"\nb: "on"\nc: "Yes"\nd: "null"\n')
        self.assertEqual(d.dump_yaml({"a": "mdi:x", "b": "24h", "c": "x: y", "d": "a #b"}), 'a: "mdi:x"\nb: "24h"\nc: "x: y"\nd: "a #b"\n')
        self.assertEqual(d.dump_yaml({"a": 1, "b": -1.5, "c": True, "d": None, "e": "°C"}), 'a: 1\nb: -1.5\nc: true\nd: null\ne: "°C"\n')
        self.assertEqual(d.dump_yaml({"e": "sensor.x_1", "name": "Top (BT7)"}), "e: sensor.x_1\nname: Top (BT7)\n")
        self.assertEqual(d.dump_yaml({"q": 'say "hi"'}), 'q: "say \\"hi\\""\n')

    def test_nesting(self) -> None:
        data = {"views": [{"a": 1, "cards": [{"type": "x", "l": ["p", "q"]}], "e": {}}, "s"], "m": {"k": [1]}}
        self.assertEqual(
            d.dump_yaml(data),
            "views:\n"
            "  - a: 1\n"
            "    cards:\n"
            "      - type: x\n"
            "        l:\n"
            "          - p\n"
            "          - q\n"
            "    e: {}\n"
            "  - s\n"
            "m:\n"
            "  k:\n"
            "    - 1\n",
        )

    def test_literal_blocks(self) -> None:
        self.assertEqual(d.dump_yaml({"c": "a\n\n  b"}), "c: |-\n  a\n\n    b\n")
        self.assertEqual(d.dump_yaml([{"c": "a\nb\n"}]), "- c: |\n    a\n    b\n")
        # Trailing spaces or a leading space would change in a block: quoted.
        self.assertEqual(d.dump_yaml({"c": "a \nb"}), 'c: "a \\nb"\n')
        self.assertEqual(d.dump_yaml({"c": " a\nb"}), 'c: " a\\nb"\n')

    def test_header(self) -> None:
        self.assertEqual(d.dump_yaml({"a": 1}, "one\n\ntwo"), "# one\n#\n# two\na: 1\n")

    def test_round_trip_with_pyyaml(self) -> None:
        try:
            import yaml  # type: ignore
        except ImportError:
            self.skipTest("PyYAML not installed")
        for dash in (d.build_dashboard(full_ids(("s1", "s2"))), d.build_apexcharts(full_ids())):
            self.assertEqual(yaml.safe_load(d.dump_yaml(dash)), dash)


ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
sys.path.insert(0, os.path.join(ROOT, "tools"))
import gen_dashboard  # noqa: E402


class GeneratedFilesTest(unittest.TestCase):
    def test_committed_files_are_current(self) -> None:
        for path, text in gen_dashboard.render().items():
            with open(os.path.join(ROOT, path), encoding="utf-8") as handle:
                self.assertEqual(handle.read(), text, "%s is stale: run tools/gen_dashboard.py" % path)

    def test_default_entity_ids(self) -> None:
        ids = gen_dashboard.default_entity_ids()
        self.assertEqual(ids["planned_offset_s1"], "sensor.nibe_f1255_planned_curve_offset_s1")
        self.assertEqual(ids["gateway_online"], "binary_sensor.nibe_f1255_gateway")
        self.assertEqual(ids["rmu_feed_s1"], "switch.nibe_f1255_send_room_temperature_to_pump_rmu40_s1")
        self.assertEqual(ids["outdoor"], "sensor.nibe_f1255_bt1_outdoor_temperature")
        self.assertEqual(ids["indoor_s1"], ids["bt50_s1"])
        self.assertEqual(len(set(ids.values())), len(ids) - 1)  # indoor_s1 is BT50

    def test_static_dashboard_is_complete(self) -> None:
        ids = gen_dashboard.default_entity_ids()
        refs = set(d.referenced_entities(d.build_dashboard(ids)))
        unused = set(ids.values()) - refs
        self.assertEqual(unused, set())


if __name__ == "__main__":
    unittest.main()
