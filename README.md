# NibePi for Home Assistant

A Home Assistant custom integration that controls a Nibe heat pump (built
and tested against an F1255) through a [NibePi](https://github.com/greberg/nibepi-backend)
gateway over MQTT.

The gateway runs on a Raspberry Pi with an RS485 HAT and owns the bus. This
integration runs in Home Assistant, usually on a different machine. It
computes the heating curve offset (register 47011, and 47010 for climate
system 2) from three parts, ported from NibePi's Node-RED controllers:

| Part | What it does | Ported from (`config_node.js`) |
|---|---|---|
| Indoor | Room temperature P-controller: `(setpoint - indoor) * factor` | `runIndoor` |
| Price | Price level (cheap ... very expensive) to offset, with an optional indoor guard | `runPrice`, `priceAdjustCurve`, `optimizeElectricityTrading`, `priceLevelsFromTrades` |
| Weather | Raises or lowers the curve ahead of a forecast temperature change, with optional wind and sun factors | `runWeather`, `checkWind` |

The parts are clamped to ±10 each, summed with the gateway's base
adjustment (`config.home.adjust_sN`), clamped to ±10 and rounded. A new
value is only chosen when the total moves more than 0.75 away from the
current one (`curveAdjust`).

Phase 1 of the migration: curve offset only, default mode **shadow**.

## Safety model

- **The gateway is the safety boundary.** Home Assistant never writes the
  register directly. In `active` mode it holds it through a *lease*:
  `<prefix>lease/47011/set` with `{"value": -2, "ttl": 900, "owner": "ha"}`.
- The lease is renewed every 5 minutes. If Home Assistant, the broker or the
  network goes away, renewals stop and the gateway restores the register by
  itself when the 15 minute TTL runs out.
- If an input is missing or stale (sensor older than the timeout, no price
  for the current hour, forecast older than 3 hours), that part contributes
  0 and says why. If the tick itself fails, nothing is sent, so the lease
  runs out and the gateway falls back.
- For the first 150 s after start no lease is sent: register values over
  MQTT are not retained, so the first totals would be built from zeros.
- Switching a system to `shadow` or `off`, or removing/reloading the
  integration, releases the lease: the gateway restores the register at
  once. Stopping Home Assistant does **not** release it; a restart within
  15 minutes picks the lease up again.
- After a gateway restart, an active lease becomes a grace lease (owner
  `grace`); the integration takes it over in `active` mode and releases it
  otherwise.
- Rejected lease requests (`error` in `lease/<reg>/state`) are shown on
  the lease sensor; "Heatpump is not ready yet." is retried after a minute,
  other rejections after 5 minutes.

## Modes

Each climate system has a **Mode** select, restored across restarts:

| Mode | Computes | Writes |
|---|---|---|
| `off` | no | no (releases a held lease) |
| `shadow` (default) | yes, shows the planned offset and reasons | never |
| `active` | yes | yes, through the lease |

The **Indoor/Price/Forecast control** switches turn single parts off
(they then contribute 0).

## Installation (HACS)

1. HACS -> Integrations -> menu -> *Custom repositories*, add
   `https://github.com/greberg/nibepi-ha` as an *Integration*.
2. Install **NibePi** and restart Home Assistant.
3. Make sure the MQTT integration is connected to the same broker as the
   gateway.
4. Settings -> Devices & services -> *Add integration* -> **NibePi**.

Manual installation: copy `custom_components/nibepi` into your
configuration's `custom_components` directory.

## Configuration

Setup asks for a name, the gateway's topic prefix (`config.mqtt.topic`,
default `nibe/modbus/`) and whether to control climate system 2.

Everything else is in the integration's **Configure** dialog.

Shared:

| Option | Default | Notes |
|---|---|---|
| Sensor timeout | 60 min | 0 disables the check (as `config.home.sensor_timeout`) |
| Price sensor | - | see below |
| Price unit factor | 100 | to öre: 100 for SEK/kWh, 1 for öre/kWh |
| Price analysis window | 12 h | `config.price.time` |
| Minimum spread | 20 öre | `config.price.min_spread` |
| Own thresholds | - | both set: replaces the optimiser's level (`verycheap` / `veryexpensive`) |
| Weather entity | - | hourly forecast via `weather.get_forecasts`, refreshed every 30 min |
| Forecast adjust, allow raising/lowering | off, on, on | `forecast_adjust`, `enable_up`, `enable_down` |
| Wind control and factors N/S/W/E | off, 0 | `wind_enable`, `wind_factor_*` |
| Sun factor and values | off, 0 | `sun_enable`, `clear`, `half_clear` |

Per climate system:

| Option | Default | Notes |
|---|---|---|
| Indoor sensor | pump's BT50 | any HA temperature sensor; staleness from its last report |
| Setpoint | 20.0 °C | or an `input_number`/`number` entity |
| Indoor factor | 2.0 | Nibe's default for menu 1.9.4 |
| Offsets per price level | 0 | `heat_very_cheap_sN` ... `heat_very_expensive_sN` |
| Price guard, margin | off, 0 | `enable_temp_sN`, `temp_low_sN` |
| Forecast hours ahead | 6 | `hours_sN` |

The defaults are Node-RED's. When migrating, copy your values from the
gateway's `config.json` (`price`, `weather`, `indoor`, `home` sections).

### Price sensors

The price sensor's attributes are read every minute. Supported:

- **Nordpool (HACS)**: `raw_today` / `raw_tomorrow`, lists of
  `{start, end, value}`.
- Lists of numbers in `today` / `tomorrow`, spread evenly over the local
  day: 24 hourly, 96 quarter-hour, and 23/25 or 92/100 values on DST days.
- **Tibber style**: any attribute holding a list of `{startsAt, total}`.

Slots are never assumed to be hourly (Nordic day-ahead prices have been
15-minute since 2025-10-01). As in NibePi, they are averaged into hours
before the level is computed.

## Entities

Per system (`S1`, `S2`):

- `select` Mode
- `switch` Indoor control, Price control, Forecast control
- `sensor` Planned curve offset (attributes: total, components, reasons,
  inputs_ok, base_adjust, last_tick, healthy, last_action)
- `sensor` Indoor offset, Price offset, Forecast offset (attributes: reason
  and inputs)
- `sensor` Pump curve offset (47011/47010 as last read)
- `sensor` Lease (active / grace / restoring / inactive / error; attributes:
  expires, owner, written, baseline, error)

Shared: `sensor` Price level, `binary_sensor` Gateway (online/offline
from `<prefix>status`, with `gateway/state` as attributes).

## Migrating from Node-RED

Leases move registers, not controllers: 47011 is the sum of indoor, price
and weather, so all three move together.

1. **Phase 1 (now)**: update the gateway to a version with leases. Install
   this integration in `shadow` mode next to the Node-RED controllers.
   Compare *Planned curve offset* and its parts with what Node-RED writes.
2. **Phase 2**: switch the system to `active`. The lease blocks other
   writers to 47011 (including Node-RED), but turn off Node-RED's indoor,
   price and weather control anyway to avoid error noise. Then set the
   gateway's `raw_set` to `"none"`.
3. Later phases: hot water and the cost optimiser move to Home Assistant,
   and Node-RED is retired.

## Development

The control logic in `custom_components/nibepi/logic/` is plain Python with
no Home Assistant imports; each module's docstring says which Node-RED
function it ports and where it deliberately differs.

```sh
python3 -m unittest discover -s tests
```

## License

MIT, see [LICENSE](LICENSE).
