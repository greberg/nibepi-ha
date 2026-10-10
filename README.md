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
Separately, it can feed a room temperature to the pump's virtual RMU40
room unit (see [Room temperature to the pump](#room-temperature-to-the-pump-rmu40)).

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

Setup asks, in this order:

1. A name, the gateway's topic prefix (`config.mqtt.topic`, default
   `nibe/modbus/`) and whether to control climate system 2.
2. The price source: one entry per loaded Tibber integration
   (`Tibber: <name>`), *Price sensor entity (attributes)*, or *None*.
   Then, for Tibber, the home (a list fetched from Tibber, skipped when
   the account has one home; a text field if the list cannot be fetched),
   or, for a price sensor, the sensor.
3. Per climate system: the indoor temperature sensor for the indoor
   component, the room temperature entity for the RMU40 feed (empty =
   the indoor sensor) and the indoor setpoint.

Everything, including the above, can be changed later in the
integration's **Configure** dialog. Entries set up with an older version
keep working: settings they lack take the defaults below.

Shared:

| Option | Default | Notes |
|---|---|---|
| Sensor timeout | 60 min | 0 disables the check (as `config.home.sensor_timeout`) |
| Price source | Tibber | a Tibber integration, a price sensor (attributes) or none, see below; none makes the price component contribute 0 |
| Tibber home | - | nickname of the home in the Tibber app; only asked with several homes |
| Price sensor | - | only for the price sensor source |
| Price unit factor | 100 | to öre: 100 for SEK/kWh (Tibber), 1 for öre/kWh |
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
| Indoor sensor | pump's BT50 | a room temperature sensor such as Netatmo (recommended), see below |
| RMU40 room temperature | the indoor sensor | the entity fed to the pump's RMU40, see below |
| Setpoint | 20.0 °C | or an `input_number`/`number` entity |
| Indoor factor | 2.0 | Nibe's default for menu 1.9.4 |
| Offsets per price level | 0 | `heat_very_cheap_sN` ... `heat_very_expensive_sN` |
| Price guard, margin | off, 0 | `enable_temp_sN`, `temp_low_sN` |
| Forecast hours ahead | 6 | `hours_sN` |

The defaults are Node-RED's. When migrating, copy your values from the
gateway's `config.json` (`price`, `weather`, `indoor`, `home` sections).

### Prices from Tibber (default)

The official Tibber integration has no price-list attributes; prices are
fetched with its `tibber.get_prices` action.

1. Settings -> Devices & services -> *Add integration* -> **Tibber**, and
   sign in. Do this before adding NibePi, so setup can offer it.
2. In NibePi's setup (or its **Configure** dialog), choose
   `Tibber: <name>`. With several homes on the Tibber account, choose the home
   from the list (with an empty home the first one is used and a warning
   is logged).
3. Keep the price unit factor at 100: Tibber prices are the total
   (energy + tax) in currency/kWh, e.g. SEK/kWh.

`tibber.get_prices` has no option to choose a Tibber account: Home
Assistant's Tibber integration always uses its **first** config entry
(`async_entries("tibber")[0]` in its `services.py`). With more than one
Tibber integration, choosing another one only records your choice; the
flow warns about this, and prices still come from the first. Remove the
other Tibber integration, or add the one you want first.

The action is called for today 00:00 through tomorrow 24:00 local time:
on start, at every new hour, every 15 minutes while no price covers the
current time, and every 15 minutes from 13:00 until tomorrow's prices are
published. Calls run in the background; the tick uses the last good list
for as long as it covers the current hour. Problems (action missing,
timeout, unknown home) show in the *Price level* sensor's `price_status`
attribute and the log, and a missing Tibber integration raises a repair
issue. Tibber returns 15-minute prices; they are averaged into hours as
below.

### Prices from a price sensor

With the source *Price sensor attributes*, the price sensor's attributes
are read every minute. Supported:

- **Nordpool (HACS)**: `raw_today` / `raw_tomorrow`, lists of
  `{start, end, value}`.
- Lists of numbers in `today` / `tomorrow`, spread evenly over the local
  day: 24 hourly, 96 quarter-hour, and 23/25 or 92/100 values on DST days.
- **Tibber style**: any attribute holding a list of `{startsAt, total}`.

Entries configured with a price sensor before the price source option
existed keep using it.

Slots are never assumed to be hourly (Nordic day-ahead prices have been
15-minute since 2025-10-01). As in NibePi, they are averaged into hours
before the level is computed.

### Indoor temperature from Netatmo

A room sensor gives the indoor controller a better value than the pump's
BT50 (or replaces a missing one). With the Netatmo integration set up,
pick the indoor module's temperature sensor (e.g.
`sensor.<room>_temperature`) as *Indoor temperature sensor* for each
climate system. Left empty, the pump's BT50 register is used instead.

Netatmo polls the cloud about every 10 minutes and often reports an
unchanged value, which does not move the entity's `last_updated`. The
sensor timeout is therefore checked against `last_reported` (Home
Assistant 2024.3+), so a steady temperature is not taken for a stale one.
`unavailable` and `unknown` count as missing: the indoor part, and the
price guard that needs it, contribute 0 until the sensor is back.

## Room temperature to the pump (RMU40)

The pump can take the room temperature from an RMU40 room unit. The
gateway emulates one per climate system, and this integration can feed it
any Home Assistant temperature sensor (e.g. Netatmo). The pump then uses
it like its own room sensor: for the display and, if enabled in the pump,
for its room sensor correction of the heating curve.

Turn on the switch **Send room temperature to pump (RMU40)** for the
system (off by default, restored on restart). The temperature comes from
the system's *RMU40 room temperature* entity, or from its *Indoor
temperature sensor* when that is empty. It is independent of the
curve-offset mode: shadow and off do not stop it.

How it is sent: a lease on register 10060 (S1) / 10160 (S2),
`<prefix>lease/10060/set` with `{"value": 21.4, "ttl": 900, "owner": "ha"}`
(°C, one decimal, limits 5..35 °C).

- A change of 0.1 °C or more is sent at once (the gateway writes at most
  once a minute); otherwise the lease is renewed every 5 minutes.
- When the switch is turned off, or the temperature is `unavailable`,
  `unknown`, outside -30..50 °C or older than the sensor timeout (checked
  against `last_reported`, as for the indoor component), the lease is
  released at once. Removing or reloading the integration releases it
  too.
- On release or expiry (Home Assistant gone for 15 minutes) the gateway
  writes the system's room setpoint as the room temperature, so the pump's
  room correction becomes zero instead of acting on an old value.
- Rejections show in the *RMU40 room temperature lease* sensor's `error`
  attribute and the log, e.g. "RMU40 S1 is not connected: enable RMU40
  for climate system 1 in the pump (menu 5.2) and keep
  system.rmu_emulation on". The request is retried after 5 minutes.

In the pump:

1. Menu 5.2 (system settings): enable RMU40 for the climate system.
2. The gateway's `system.rmu_emulation` must stay on.
3. To let the room temperature steer the heating, enable *Use room sensor
   S1* in menu 1.9.4 (register 47394) and set its factor (register 47402);
   S2 likewise.

**Do not correct twice.** The pump's room sensor correction (menu 1.9.4)
and this integration's indoor component both move the heating curve from
the same room temperature. With both on, the correction is applied twice
and the heating overshoots. Use one of them:

- recommended: RMU40 feed on, the pump's room sensor factor set, and the
  **Indoor control** switch off; or
- the indoor component on (curve offset via the lease), and *Use room
  sensor* off in the pump. The RMU40 feed can then stay on for the display
  only.

## Entities

Per system (`S1`, `S2`):

- `select` Mode
- `switch` Indoor control, Price control, Forecast control
- `switch` Send room temperature to pump (RMU40), off by default
- `sensor` Planned curve offset (attributes: total, components, reasons,
  inputs_ok, base_adjust, last_tick, healthy, last_action)
- `sensor` Indoor offset, Price offset, Forecast offset (attributes: reason
  and inputs)
- `sensor` Pump curve offset (47011/47010 as last read)
- `sensor` Lease (active / grace / restoring / inactive / error; attributes:
  expires, owner, written, baseline, error, last_state_received). It shows
  `inactive` with `last_state_received: null` until the gateway has
  published a state for the register, which happens only after the first
  lease was taken (never in shadow mode).
- `sensor` RMU40 room temperature lease (same states and attributes, plus
  feed_enabled, source_entity, room_temperature, reason, last_action)

Shared: `sensor` Price level (attributes: price_ore, hour_start, levels,
price_source, price_status, and for Tibber tibber_home and
prices_fetched), `binary_sensor` Gateway (online/offline, see below).

The Gateway sensor is on when `<prefix>status` says `online` or a live
`gateway/state` arrived in the last 3 minutes, and off when the status
says `offline`. When nothing has arrived 120 s after setup it turns off
and a warning is logged once. Its attributes `status_topic`,
`last_status`, `last_message_at` and, while no status was received,
`hint` help find out why: usually the gateway is not running, or is
connected to another broker or with another topic prefix.

## Going live

The NibePi gateway (`ghcr.io/greberg/nibepi-gateway`, installed on the Pi
with `pi/install.sh` from
[nibepi-docker](https://github.com/greberg/nibepi-docker)) replaces the old
Node-RED NibePi. Until this integration runs in `active` mode, the pump runs
on its own heat curve with no price, indoor or weather adjustment.

Leases move registers, not controllers: 47011 is the sum of indoor, price
and weather, so all three are controlled together.

1. Install the gateway on the Pi, pointing it at the same MQTT broker as
   Home Assistant.
2. Install this integration in `shadow` mode. Enter your settings (price
   level offsets, setpoint, weather); the old values are still in the
   Pi's `config.json` under `price`, `indoor` and `weather` for reference.
3. Watch *Planned curve offset* and its parts for a few days.
4. Switch the system to `active`, and check that the lease sensor shows
   an active lease.
5. Optionally set the gateway's `NIBEPI_RAW_SET=none`, so only leases can
   write to the pump.

Later phases move hot water and a cost optimiser into this integration.
The Node-RED version of NibePi is kept under the `nodered-final` tag of
the NibePi repositories.

## Development

The control logic in `custom_components/nibepi/logic/` is plain Python with
no Home Assistant imports; each module's docstring says which Node-RED
function it ports and where it deliberately differs.

```sh
python3 -m unittest discover -s tests
```

## License

MIT, see [LICENSE](LICENSE).
