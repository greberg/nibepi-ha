from __future__ import annotations

import json
import unittest
from datetime import datetime, timezone

import context  # noqa: F401
from context import utc
from logic.opthistory import (
    BinAccumulator,
    SampleLog,
    bin_meter_energy,
    discovery_device_id,
    gateway_ids,
    house_samples,
    hourly,
    import_series,
    indicator,
    meter_kind,
    meter_readings,
    parse_states,
    register_unique_id,
    resample_states,
)
from logic.optimizer.meter import MeterConfig

T0 = int(utc(2026, 1, 10).timestamp())  # a bin and hour boundary


class _State:
    def __init__(self, state, when):
        self.state = state
        self.last_updated = when
        self.last_changed = when


class MeterKindTest(unittest.TestCase):
    def test_power_units(self) -> None:
        self.assertEqual(meter_kind("W", "power", "measurement"), ("power", "W"))
        self.assertEqual(meter_kind("kW"), ("power", "kW"))
        self.assertEqual(meter_kind(" kw ", None, None), ("power", "kW"))

    def test_energy_units(self) -> None:
        self.assertEqual(meter_kind("kWh", "energy", "total_increasing"), ("energy", "kWh"))
        self.assertEqual(meter_kind("Wh", "energy", "total"), ("energy", "Wh"))
        self.assertEqual(meter_kind("MWh"), ("energy", "MWh"))

    def test_refused(self) -> None:
        self.assertIsNone(meter_kind("kWh", "energy", "measurement"))  # per-period value
        self.assertIsNone(meter_kind("kWh", "power"))  # class contradicts the unit
        self.assertIsNone(meter_kind("W", "energy"))
        self.assertIsNone(meter_kind("A", "current"))
        self.assertIsNone(meter_kind(None, "power"))
        self.assertIsNone(meter_kind(""))


class ParseStatesTest(unittest.TestCase):
    def test_formats(self) -> None:
        when = utc(2026, 1, 10, 0, 5)
        rows = [
            {"s": "21.5", "lu": T0 + 600.0},  # compressed
            _State("21.0", when),  # State object
            {"state": "unavailable", "last_changed": "2026-01-10T00:20:00+00:00"},  # minimal
            {"state": "22", "last_changed": "2026-01-10T00:30:00Z"},
            {"s": "x"},  # no time: skipped
        ]
        points = parse_states(rows)
        self.assertEqual(
            points,
            [(T0 + 300.0, 21.0), (T0 + 600.0, 21.5), (T0 + 1200.0, None), (T0 + 1800.0, 22.0)],
        )

    def test_convert(self) -> None:
        points = parse_states([{"s": "212", "lu": T0}], convert=lambda f: (f - 32) / 1.8)
        self.assertAlmostEqual(points[0][1], 100.0)


class ResampleTest(unittest.TestCase):
    def test_time_weighted_mean_with_hold(self) -> None:
        points = [(T0, 20.0), (T0 + 300, 22.0)]
        out = resample_states(points, T0, T0 + 1800)
        self.assertAlmostEqual(out[T0], 21.0)
        # 22.0 holds through the next bins.
        self.assertAlmostEqual(out[T0 + 600], 22.0)
        self.assertAlmostEqual(out[T0 + 1200], 22.0)

    def test_unavailable_is_a_gap(self) -> None:
        points = [(T0, 20.0), (T0 + 120, None), (T0 + 900, 21.0)]
        out = resample_states(points, T0, T0 + 1800)
        self.assertNotIn(T0, out)  # 2 of 10 minutes covered
        self.assertAlmostEqual(out[T0 + 600], 21.0)  # 5 of 10 minutes: enough
        self.assertAlmostEqual(out[T0 + 1200], 21.0)

    def test_max_hold(self) -> None:
        out = resample_states([(T0, 20.0)], T0, T0 + 7200, max_hold=1200)
        self.assertEqual(sorted(out), [T0, T0 + 600])

    def test_last_for_counters(self) -> None:
        points = [(T0 + 60, 100.0), (T0 + 500, 101.0), (T0 + 1300, 103.0)]
        out = resample_states(points, T0, T0 + 1800, mode="last")
        self.assertEqual(out, {T0: 101.0, T0 + 600: 101.0, T0 + 1200: 103.0})

    def test_indicator(self) -> None:
        points = indicator([(T0, 20.0), (T0 + 300, 30.0), (T0 + 600, None)], 20)
        out = resample_states(points, T0, T0 + 600)
        self.assertAlmostEqual(out[T0], 0.5)


class ImportSeriesTest(unittest.TestCase):
    def test_kinds(self) -> None:
        rows = [{"s": "20", "lu": T0}, {"s": "30", "lu": T0 + 300.0}, {"s": "30", "lu": T0 + 600.0}]
        self.assertEqual(import_series(rows, "mean", T0, T0 + 600), {T0: 25.0})
        self.assertEqual(import_series(rows, "last", T0, T0 + 1200), {T0: 30.0, T0 + 600: 30.0})
        self.assertEqual(import_series(rows, "indicator", T0, T0 + 600, wanted=20), {T0: 0.5})
        pump = [{"s": "0", "lu": T0}, {"s": "45", "lu": T0 + 150.0}, {"s": "60", "lu": T0 + 300.0}]
        self.assertEqual(import_series(pump, "running", T0, T0 + 600), {T0: 0.75})
        meter = [{"s": "1000", "lu": T0}, {"s": "1000", "lu": T0 + 600.0}]
        out = import_series(meter, "meter", T0, T0 + 600, meter=MeterConfig("power", "W"))
        self.assertAlmostEqual(out[T0], 1000 * 600 / 3600 / 1000)
        with self.assertRaises(ValueError):
            import_series(meter, "meter", T0, T0 + 600)


class MeterBinsTest(unittest.TestCase):
    def test_power_meter(self) -> None:
        readings = meter_readings([(T0, 1200.0), (T0 + 600, 600.0), (T0 + 1200, 0.0)])
        out = bin_meter_energy(readings, [T0, T0 + 600], MeterConfig("power", "W"))
        self.assertAlmostEqual(out[T0], 0.2)
        self.assertAlmostEqual(out[T0 + 600], 0.1)

    def test_energy_meter(self) -> None:
        readings = meter_readings([(T0, 10.0), (T0 + 600, 10.3), (T0 + 1200, None), (T0 + 1500, 10.5)])
        out = bin_meter_energy(readings, [T0, T0 + 600], MeterConfig("energy", "kWh"))
        self.assertAlmostEqual(out[T0], 0.3)
        self.assertNotIn(T0 + 600, out)  # not bridged across unavailable


class DiscoveryTest(unittest.TestCase):
    def test_device_id_from_config(self) -> None:
        payload = json.dumps(
            {"dev": {"ids": ["nibepi_ab12cd34ef"]}, "avty_t": "nibe/modbus/status", "cmps": {}}
        )
        self.assertEqual(discovery_device_id(payload, "nibe/modbus/"), "nibepi_ab12cd34ef")
        self.assertIsNone(discovery_device_id(payload, "other/"))
        self.assertIsNone(discovery_device_id("not json", "nibe/modbus/"))
        self.assertIsNone(discovery_device_id("", "nibe/modbus/"))

    def test_gateway_ids(self) -> None:
        ids = gateway_ids(["nibepi_ab12_40004", "nibepi_ab12_40008", "other_1", None, "nibepi_ff_1"])
        self.assertEqual(ids, {"nibepi_ab12", "nibepi_ff"})
        self.assertEqual(register_unique_id("nibepi_ab12", 40004), "nibepi_ab12_40004")


class SampleLogTest(unittest.TestCase):
    def test_put_fill_trim_roundtrip(self) -> None:
        log = SampleLog()
        log.put(T0 + 30, {"t_out": -2.0, "s1_t_in": 21.0, "bad": None})
        self.assertEqual(log.rows, {T0: {"t_out": -2.0, "s1_t_in": 21.0}})
        added = log.fill("t_out", {T0: 5.0, T0 + 600: -1.0})
        self.assertEqual(added, 1)
        self.assertEqual(log.rows[T0]["t_out"], -2.0)  # live value kept
        log.put(T0 + 1800, {"heat_kwh": 1234.5678})
        data = json.loads(json.dumps(log.to_dict()))
        self.assertEqual(data["columns"]["t_out"], [-2.0, -1.0, None, None])
        again = SampleLog.from_dict(data)
        self.assertEqual(again.rows[T0 + 1800], {"heat_kwh": 1234.568})
        self.assertEqual(again.count("t_out"), 2)
        again.trim(T0 + 600)
        self.assertEqual(sorted(again.rows), [T0 + 600, T0 + 1800])

    def test_from_dict_tolerant(self) -> None:
        self.assertEqual(len(SampleLog.from_dict(None)), 0)
        self.assertEqual(len(SampleLog.from_dict({"step": 300, "start": 0, "columns": {}})), 0)
        self.assertEqual(len(SampleLog.from_dict({"step": 600, "start": "x", "columns": {}})), 0)
        log = SampleLog.from_dict({"step": 600, "start": T0, "columns": {"a": [1, "bad", None], 3: [1]}})
        self.assertEqual(log.rows, {T0: {"a": 1.0}})


class AccumulatorTest(unittest.TestCase):
    def test_bins(self) -> None:
        acc = BinAccumulator()
        self.assertIsNone(acc.add(T0 + 0, {"t_out": 1.0, "heat_kwh": 10.0}))
        self.assertIsNone(acc.add(T0 + 60, {"t_out": 3.0, "heat_kwh": 10.5, "x": None}))
        done = acc.add(T0 + 600, {"t_out": 9.0})
        self.assertEqual(done, (T0, {"t_out": 2.0, "heat_kwh": 10.5}))
        self.assertIsNone(acc.add(T0 + 660, {}))


class ModelInputsTest(unittest.TestCase):
    def _rows(self):
        rows = {}
        for i in range(-1, 12):
            rows[T0 + i * 600] = {
                "t_out": -5.0,
                "s1_t_in": 21.0 + 0.01 * i,
                "s1_supply": 35.0,
                "heat_kwh": 100.0 + 0.2 * (i + 1),
                "meter_kwh": 0.25,
            }
        return rows

    def test_house_samples(self) -> None:
        rows = self._rows()
        rows[T0 + 600].pop("s1_supply")
        samples = house_samples(rows, "s1", 59.3, 18.0)
        self.assertEqual(len(samples), 12)
        self.assertEqual(samples[0].ts, datetime.fromtimestamp(T0 - 600, timezone.utc))
        self.assertEqual(samples[0].solar, 0.0)  # January midnight
        # History from before 0.2.2 has no BT3 or pump: unknown, not zero.
        self.assertEqual((samples[0].t_return, samples[0].pump, samples[0].hw), (None, None, 0.0))

    def test_house_samples_with_return(self) -> None:
        rows = self._rows()
        rows[T0].update({"bt3": 31.5, "pump_on": 1.0, "prio_hw": 0.5, "freq_hz": 42.0})
        sample = [s for s in house_samples(rows, "s1") if s.ts.timestamp() == T0][0]
        self.assertEqual((sample.t_return, sample.pump, sample.hw, sample.compressor), (31.5, 1.0, 0.5, 42.0))
        # BT3 and GP1 are the heat pump's: a second (shunted) system does not get them.
        rows[T0]["s2_t_in"], rows[T0]["s2_supply"] = 21.0, 30.0
        self.assertIsNone(house_samples(rows, "s2")[0].t_return)

    def test_old_store_without_return_loads(self) -> None:
        # A 0.2.1 store: same format, no bt3/pump_on columns; a new bin adds them.
        old = {"version": 1, "step": 600, "start": T0,
               "columns": {"t_out": [-5.0, -5.0], "s1_t_in": [21.0, 21.1], "s1_supply": [35.0, 34.0]}}
        log = SampleLog.from_dict(json.loads(json.dumps(old)))
        self.assertEqual(len(house_samples(log.rows, "s1")), 2)
        log.put(T0 + 1200, {"t_out": -5.0, "s1_t_in": 21.1, "s1_supply": 34.0, "bt3": 30.0, "pump_on": 1.0})
        again = SampleLog.from_dict(json.loads(json.dumps(log.to_dict())))
        self.assertEqual(again.to_dict()["columns"]["bt3"], [None, None, 30.0])
        self.assertEqual([s.t_return for s in house_samples(again.rows, "s1")], [None, None, 30.0])

    def test_hourly(self) -> None:
        rows = self._rows()
        out = hourly(rows, ["t_out", "heat_kwh", "meter_kwh", "s1_t_in"])
        self.assertEqual([h for h, _ in out], [T0, T0 + 3600])
        first = out[0][1]
        self.assertAlmostEqual(first["heat_kwh"], 1.2)
        self.assertAlmostEqual(first["meter_kwh"], 1.5)
        self.assertAlmostEqual(first["t_out"], -5.0)
        rows[T0 + 4200].pop("meter_kwh")
        self.assertEqual([h for h, _ in hourly(rows, ["meter_kwh"])], [T0])


if __name__ == "__main__":
    unittest.main()
