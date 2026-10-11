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

Default mode **shadow**. Separately, it can feed a room temperature to
the pump's virtual RMU40 room unit (see [Room temperature to the
pump](#room-temperature-to-the-pump-rmu40)), control hot water with a
learned draw profile (see [Hot water](#hot-water-and-vv-learning)), and
plan heating and hot water against the electricity price with a learned
model of the house (see [Cost optimiser](#cost-optimiser)).

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
4. The house, for the cost optimiser: heated floor area, floors, building
   era, construction, heat emitters, how many indoor sensors are
   averaged, an optional electricity meter and what it measures, and the
   model confidence the optimiser needs before it may steer (see [House
   profile](#house-profile)).

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
| House: floor area, floors, era, construction, emitters, sensors | 150 m², 1.5, unknown, unknown, floor heating, 1 | see [House profile](#house-profile); entries from 0.2.0 keep their emitter choice |
| Electricity meter | - | optional power or energy sensor; calibration and the heat pump's cost, never steering |
| The meter measures | the heat pump only | or the whole house |
| Minimum model confidence to steer | high | low / medium / high |
| Optimiser hot water floor (hot water step) | 45 °C | BT6, for the optimiser's tank plan |

Per climate system:

| Option | Default | Notes |
|---|---|---|
| Indoor sensor | pump's BT50 | a room temperature sensor such as Netatmo (recommended), see below |
| RMU40 room temperature | the indoor sensor | the entity fed to the pump's RMU40, see below |
| Setpoint | 20.0 °C | or an `input_number`/`number` entity; also the optimiser's target |
| Indoor factor | 2.0 | Nibe's default for menu 1.9.4 |
| Offsets per price level | 0 | `heat_very_cheap_sN` ... `heat_very_expensive_sN` |
| Price guard, margin | off, 0 | `enable_temp_sN`, `temp_low_sN` |
| Forecast hours ahead | 6 | `hours_sN` |
| Comfort band below / above the setpoint | 0.5 / 1.0 °C | cost optimiser |
| Hard floor below the setpoint | 1.5 °C | cost optimiser: below it, heat whatever the price |

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

## Hot water and VV learning

Ported from NibePi's Node-RED VV-AI and hot water nodes; what it does, and
where it deliberately differs, is in [docs/hotwater.md](docs/hotwater.md).
It has its own **Hot water mode** select (`off` / `shadow` (default) /
`active`), separate from the curve-offset modes, and a switch per feature:

| Switch | Default | What it does | Registers |
|---|---|---|---|
| Hot water learning | on | Learns a weekly draw profile from BT6 (in shadow and active) | none |
| Hot water VV-AI control | off | Holds the hot water period at 0 and starts charges itself before the learned draws, to the stop temperature of the day's mode; a min-temp floor on BT7 | 47134, 48132 |
| Hot water automatic luxury | off | A one-time increase when BT6 falls fast | 48132 |
| Hot water priority | off | A one-time increase when BT7 reaches the start temperature | 48132 |
| Hot water comfort mode by price | off | Comfort mode (economy/normal/luxury) per price level | 47041 |

Automatic luxury or priority on makes VV-AI control stand aside, as in
Node-RED. One controller owns the three registers and writes them through
leases (`ttl` 900 s, renewed every 5 minutes):

- 48132 temporary lux is leased at 4 only while a charge is wanted, and
  released (the gateway writes 0) when it ends.
- 47134 hot water period is leased at 0 only while VV-AI control is in
  charge; otherwise it is released and the gateway restores the value it
  read before (the baseline). VV-AI control will not start while the
  pump's period reads 0 (the gateway would capture 0 and restore it), nor
  without the stop temperatures, nor with nothing learned and the floor
  off.
- 47041 comfort mode is released (baseline) when the price for the
  current hour is missing or the level's setting is *Unchanged*.
- A stale or missing BT6/BT7 (sensor timeout), a failed tick, mode
  `shadow`/`off`, or unloading the integration releases the leases at
  once. If Home Assistant goes away, they expire after 15 minutes.

Registers read (and added to the gateway's polling): BT7 40013, BT6
40014, prio 43086, 47041, 47134, 48132, and the start/stop temperatures
47043-47045 / 47047-47049.

The learned profile is stored in `.storage/nibepi_vv_profile_<entry id>`
(debounced saves, survives restarts, removed with the integration). Its
168 hours are local wall-clock time (DST-safe). Plans use the configured
price source in its own slot length (15-minute or hourly).

Options (**Configure** -> last step, *Hot water*), with Node-RED's
defaults: learning rate 10, smallest draw 3 °C, day sums for economy /
normal / luxury 1 / 3 / 6 °C, second peak threshold 3 °C, heating window
2 h, heat in the cheapest hours off, price search window 7 h, min-temp
floor on at 35 °C (Node-RED: off), automatic luxury 20 °C within 10
minutes, comfort mode per price level very cheap luxury, cheap normal,
the rest economy.

### Bringing over the Node-RED profile

Node-RED's VV-AI kept what it learned in `/etc/nibepi/vv_ai_profile.json`
inside its container, i.e. in the folder the Pi mounted there (often
`~/nibepi/etc/nibepi/` or `/opt/nibepi/etc/nibepi/`; old bare installs:
`~/.node-red/node_modules/node-red-contrib-nibepi/vv_ai_profile.json`).
Removing the container kept that folder.

1. On the Pi, find the file:

   ```sh
   docker inspect -f '{{range .Mounts}}{{if eq .Destination "/etc/nibepi"}}{{.Source}}{{end}}{{end}}' nibepi  # if the container still exists
   ls -l ~/nibepi/etc/nibepi/vv_ai_profile.json
   ```

2. Copy it into Home Assistant's configuration directory, e.g. to
   `/config/nibepi/vv_ai_profile.json` (Samba share, or from the Pi with
   `scp ~/nibepi/etc/nibepi/vv_ai_profile.json root@<ha>:/config/nibepi/`
   through the *Terminal & SSH* add-on).
3. Developer tools -> Actions -> **NibePi: Import Node-RED hot water
   profile** (`nibepi.import_vv_profile`) with `path:
   nibepi/vv_ai_profile.json`. The response lists the learned hours; the
   *Hot water next expected draw* sensor shows them in `top_slots`.

Only paths inside the configuration directory are accepted. The import
replaces the current profile; days since Node-RED stopped are not decayed.

## Cost optimiser

The cost optimiser plans the curve offset for every price slot ahead (up
to 36 h), and optionally the hot water charges, so that the heat pump
runs when electricity is cheap without the house getting cold. The design
and its evaluation are in the NibePi workspace's `docs/optimizer.md`; the
pure Python core is in `custom_components/nibepi/logic/optimizer/`.

### House profile

The optimiser starts from a small physical model of the house: how fast it
loses heat (W/K), how much heat it stores (kWh/K), how much heat the
emitters give per degree of supply temperature, and gains from people and
sun. The *House* step of the setup and options flow gives the starting
values ("priors"); the optimiser learns the real ones from the history.

| Field | Default | Used for |
|---|---|---|
| Heated floor area | 150 m² | heat loss, stored heat, gains |
| Floors (1, 1.5, 2, 3 or more) | 1.5 | heat loss per m²; one sensor on one floor of a taller house gets a safety margin (0.1-0.3 °C) |
| Built (before 1960 ... 2011 or later, unknown) | unknown | heat loss per m²; unknown gives a wider range |
| Construction (light, medium, heavy, unknown) | unknown | stored heat |
| Heat emitters (radiators, floor heating, both) | floor heating | how slowly the emitters react: the slab's lag (see below) |
| Indoor sensors averaged | 1 | two or more halve the margin |
| Electricity meter | none | calibrates the pump's efficiency; see below |
| The meter measures (the heat pump only, the whole house) | the heat pump only | see below |
| Minimum model confidence to steer | high | the gate, see below |

The location (sun position, design outdoor temperature) and the time zone
are Home Assistant's own (Settings -> System -> General); the step shows
them. The target indoor temperature is the system's **setpoint**, the
same one the indoor component uses, so the rules and the optimiser can't
disagree about it. Around it, per system, the **comfort band** (default
0.5 °C below to 1.0 °C above: no comfort penalty inside it) and the **hard
floor** (default 1.5 °C below the setpoint).

**Floor heating.** The water heats a concrete slab, and the slab heats
the room: an offset change shows up indoors one to six hours later, and
the slab keeps heating after the offset drops. The model therefore has a
second state for floor heating (and both), the slab temperature, which
follows the supply with a lag (prior 3 h for floor heating, 1.5 h for
both, 0.3 h for radiators; the fit learns it) and coasts down slowly when
the compressor idles. The planner then buys heat hours before an
expensive period instead of just before it, and counts what is stored in
the slab.

The return temperature **BT3** (40012) makes the slab visible: while the
floor water circulates, the water comes back from the long floor loops
just above the slab's temperature (slab = BT3 − κ × (BT2 − BT3), κ
learned, typically 0.1-0.4). The fit uses it to learn the slab's lag and
how slowly it coasts (otherwise fixed at 3 × the lag) directly, and the
planner starts from the slab as BT3 shows it. BT3 is only used while the
heating medium pump (GP1, 43437) runs and no hot water is being charged
(BT2/BT3 then show the tank circuit); without pump data (older history)
only while the compressor runs. Without enough BT3 (48 h) the slab is
filtered from BT2 as before.

The meter may be a power sensor (W, kW) or an energy meter counting up
(Wh, kWh, MWh); the kind is taken from its unit and device class. **The
meter measures** says what it covers:

- *The heat pump only* (default): its readings are the heat pump's
  electricity. The compressor's efficiency is calibrated directly against
  them, and the *Heat pump cost today* sensor shows what the heat pump
  really cost against what the plans predicted.
- *The whole house*: the heat pump's share is estimated by regressing the
  meter on the pump's own power estimate, which needs weeks of data; it
  is only used as a check.

Either way the meter is never used to steer.

### History

Every minute the integration reads the indoor sensor, BT1, BT2 (40008 /
40007), the curve offset, brine in (BT10 40015), BT6, the priority
(43086), compressor power (43141) and frequency (43136), the electric
addition (43084), the heat meters (44300 heating, 44298 hot water), the
return BT3 (40012), whether the heating medium pump GP1 runs (43437 > 0)
and the meter, and keeps 10-minute means for 35 days in
`.storage/nibepi_optimizer_<entry id>` (saved hourly and at shutdown).
These registers are added to the gateway's polling.

At the first start (and when the indoor sensor or the meter is changed)
it imports the last 28 days from Home Assistant's **recorder**, so the
model can be fitted at once. The gateway's register sensors are found
through its MQTT discovery: the gateway publishes
`homeassistant/device/<id>/config` with `<prefix>status` as availability,
and each register sensor has the unique id `<id>_<register>`. Without
discovery (`config.mqtt.discovery` off on the gateway), or with the
registers not recorded, the import of those columns is skipped (logged
once, listed in the diagnostics) and the history builds up live.

### Fitting

Ten minutes after start and every night at 03:15 the models are fitted on
the last 28 days, in the background: the house model (heat loss, emitter
gain, solar and internal gains and, with floor heating, the slab's lag,
chosen from 0.25-10 h by how well each predicts the indoor temperature;
with BT3 also the slab's coasting time, fitted against BT3, and of the
BT3 and the BT2-only fit the one that predicts the indoor temperature
better is kept),
the heat curve (bias, slope, offset gain from BT1, offset and BT2), the
emitter conductance from the heat meter (one climate system only; the
meter is shared), the floor loop's water flow from the heat meter and
BT2 − BT3 (a check of the slab's coasting time), the compressor's efficiency (Carnot fraction, from the
heat meter and the heat pump meter, or the compressor power with a
whole-house meter) and, with a whole-house meter, the heat pump's share
of it. A fit that is
implausible is rejected and the priors are used. The *House model* sensor
shows the result; its state is the confidence (`prior`, `low`, `medium`,
`high`), from the amount of data, how well the model predicts the indoor
temperature 24 h ahead and how certain the emitter gain is.

The confidence only becomes high with offset movement in the history:
the supply follows the outdoor temperature through the curve, so without
the offset changing, heat loss and emitter gain can't be told apart. The
price and indoor components, and the optimiser in shadow mode, provide
that movement.

### How it decides

Every 15 minutes, and on new prices or a mode or strategy change, it
plans in the background:

- **Horizon**: the known prices from the configured price source (15
  minutes or hourly, up to 36 h; averaged to 30 minutes with floor
  heating), the outdoor forecast from the weather entity (shifted to match
  BT1 now), and the sun.
- **Heating**: for each slot the curve offset, within ±3 of the offset
  that would hold the setpoint at the horizon's mean outdoor temperature
  (the *base offset*), that minimises electricity cost (price ×
  heat / COP) plus a comfort penalty for leaving the band, and a small
  cost for each change. It stores heat in cheap slots and coasts through
  expensive ones as far as the band allows, and counts the heat left in
  the house at the end. With floor heating it also tracks the slab
  temperature (estimated from the last day's BT2 and indoor history,
  pulled towards BT3 when the fit uses it),
  changes the offset at most once an hour, at a higher cost per change,
  and counts the heat left in the slab. Each block of the plan is explained in a
  sentence, e.g. *"16:00-21:00 offset -3: expensive (2.30 SEK/kWh vs 1.58
  average), coasting on stored heat, indoor 20.6 °C at 21:00"*.
- **Savings** are the plan's cost against holding the base offset on the
  same model, heat left at the end included.
- **Hot water**: charges are planned against a tank model and the draws
  the hot water learning expects, keeping BT6 above the optimiser's hot
  water floor (default 45 °C).

### Heating strategy, the gate and shadow comparison

Each system has a **Heating strategy** select:

| Strategy | Curve offset |
|---|---|
| `rules` (default) | the sum of the indoor, price and forecast components, as before |
| `optimiser` | the optimiser's planned offset for the current slot, **instead of** the sum, while the gate passes; otherwise the sum |

The planned offset replaces the whole sum, base adjustment included: the
plan already takes the price, the forecast and the indoor temperature
into account. The gate passes only when the house model is fitted with
at least the configured confidence (default high), the prices cover at
least 6 h, the indoor sensor is fresh and the plan is less than 40 min
old. Whenever the indoor temperature is below the hard floor, the gate
heats at the base offset + 3 whatever the price or the confidence. When
the gate refuses, the rules are used and the *Planned curve offset*
sensor's `strategy_reason` says why.

The **Mode** select still decides whether anything is written: optimiser
in `shadow` mode only computes and shows, and the lease, its TTL and the
gateway's fall-back work as before.

The plan is computed whatever the strategy, so it can be compared with
the rules in shadow mode: *Optimiser plan* (the planned offset now) next
to *Planned curve offset* (what would be written) and the price/indoor
offsets, *Expected savings today*, and in the *Optimiser plan*
attributes the predicted indoor temperature for the next 24 h, to set
against the measured one. With a heat pump meter, *Heat pump cost today*
sets the measured cost against what the plans predicted for the same
hours; in shadow mode the pump follows the rules, so the gap includes the
difference between the strategies, in active mode it is the model's
error.

### When to switch to the optimiser

1. Leave the strategy at `rules` and the mode as it is for at least two
   weeks, with the price component on, so the history has offset
   movement.
2. Wait for *House model* to show `high`, with `sim_rmse` (the model's
   24 h indoor prediction error) around 0.25 °C or less.
3. For a few days, compare *Optimiser plan*'s `plan_24h` predicted indoor
   temperature with the real one; expect them within about 0.3 °C, and
   *Expected savings today* above 0 on most days.
4. Set the strategy to `optimiser` (with the mode `active`). Watch the
   indoor temperature for a week; if it often goes under the band, widen
   the band or raise the setpoint, or go back to `rules`. With a heat
   pump meter, *Heat pump cost today*'s `cost_vs_predicted_pct` now
   measures the energy model: a day-after-day gap of more than about
   15 % means the efficiency or the emitter fit is off.

### Hot water charges

The switch **Optimise hot water charges** (off by default) lets the
optimiser's charge blocks replace the VV-AI plan's heating windows in the
hot water controller, which stays the one owner of 47134 and 48132: the
hot water period is held at 0, a block starts a charge with temporary lux
and ends when BT7 reaches its target (the normal or luxury stop
temperature), and the BT7 min-temp floor still applies. Automatic luxury
or hot water priority on still make it stand aside. Without a current
plan (BT6 or the stop temperatures missing, no prices) the VV-AI plan is
used when *VV-AI control* is on; otherwise hot water is left to the pump.
As before, nothing is written unless the **Hot water mode** is `active`.

### Optimiser entities

Per system:

- `select` Heating strategy (`rules` / `optimiser`)
- `sensor` Optimiser plan: the planned offset now; attributes `plan_24h`
  (`[time, offset, predicted indoor]` at each change and every hour),
  `explanation`, `summary`, `expected_cost`, `baseline_cost`, `savings`,
  `savings_pct`, `gate` (use, offset, reason), `applied`, `strategy`,
  `model_confidence`, `base_offset`, `planned_at`, `problem`,
  `slab_temp` (floor heating: the slab temperature the plan starts from,
  comparable with BT3), `slab_state` (the planner's slab state, which
  settles at the supply) and `slab_source` (`bt3`, `bt2`, or `settled`
  without history)
- `sensor` House model (`prior` / `low` / `medium` / `high`): `a`, `b`,
  `c`, `d`, `time_constant_h`, `emitter`, `emitter_lag_h`,
  `slab_coast_h` (`slab_coast_fitted`: from BT3, else 3 × the lag),
  `slab_coupling_per_h` (indoor °C/h per K of slab over room),
  `bt3_used`, `return_kappa` (κ), `slab_temperature` and `slab_source`
  (the slab now), `slab_fit_source`, `bt3_fit` and `loop_fit` (the BT3
  slab fit and the loop flow, unrecorded), `slab_capacity_kwh_per_k`,
  `planner` (`lagged` / `one_node`), `heat_loss_w_per_k`,
  `capacity_kwh_per_k`,
  `emitter_kw_per_k`, `carnot_eta`, `confidence`, `sim_rmse`,
  `rel_se_b`, `data_hours`, `last_fit`, the fits of the emitter,
  efficiency and meter (`meter_share`: the regressed share, or with a heat
  pump meter its ratio to the pump's own power estimate), and the
  history's size

Shared:

- `sensor` Expected savings today (the price currency): what the plans
  expect to save today against the base offset, heating per system and
  hot water as attributes. It is an expectation from the model, made in
  shadow mode too, not a measurement.
- `sensor` Heat pump cost today (the price currency), with a heat pump
  meter only: the meter's kWh priced per slot; attributes `kwh`,
  `predicted_cost`, `predicted_kwh`, `predicted_heating_cost`,
  `predicted_hot_water_cost`, `baseline_heating_cost`,
  `cost_vs_predicted_pct`, `hours_measured`, `unpriced_kwh` and
  `yesterday`.

  Both are daily totals (state class `total`, reset at local midnight),
  so the long-term statistics give the savings and the cost per day.
- `sensor` Hot water optimiser plan: when the next planned charge starts;
  attributes `blocks` (next 24 h), `next_target`, `savings`, `min_temp`,
  `enabled`, `in_use`, `problem`
- `switch` Optimise hot water charges

Long attributes (plans, explanations, fits) are not recorded in the
history database.

## Diagnostics

Settings -> Devices & services -> NibePi -> menu -> **Download
diagnostics** gives a JSON file with the options (Tibber home and location
redacted), the register readings, each system's components, rules sum,
strategy choice, gate and leases, the hot water decision and leases, and
the optimiser's profile, fits, models, plans, savings and history
coverage, including what the recorder import found. Attach it when
reporting a problem.

## Entities

Per system (`S1`, `S2`):

- `select` Mode
- `select` Heating strategy (see [Cost optimiser](#cost-optimiser))
- `switch` Indoor control, Price control, Forecast control
- `switch` Send room temperature to pump (RMU40), off by default
- `sensor` Planned curve offset (attributes: strategy, source, strategy_reason,
  rules_total, total, components, reasons, inputs_ok, base_adjust,
  last_tick, healthy, last_action)
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
- `sensor` Optimiser plan, House model (see [Optimiser
  entities](#optimiser-entities))

Shared: `sensor` Price level (attributes: price_ore, hour_start, levels,
price_source, price_status, and for Tibber tibber_home and
prices_fetched), `binary_sensor` Gateway (online/offline, see below),
`sensor` Expected savings today, Heat pump cost today and Hot water optimiser plan (see
[Optimiser entities](#optimiser-entities)).

Hot water:

- `select` Hot water mode
- `switch` Hot water learning, VV-AI control, automatic luxury, priority,
  comfort mode by price, optimise hot water charges
- `sensor` Hot water planned action (off / fault / idle / heat_now /
  block / economy / normal / luxury; attributes: reasons per feature,
  targets per register, next_heat_start, closed_until, plan_24h, inputs,
  last_actions)
- `sensor` Hot water next expected draw (timestamp; attributes:
  confidence (the hour's learned value relative to the largest), slot,
  expected_drop, learned_draws, last_learned, last_event, day_sums,
  top_slots, imported_from, profile)
- `sensor` Hot water comfort mode lease (47041), Hot water period lease
  (47134), Temporary lux lease (48132), as the lease sensors above

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

Hot water starts the same way: `shadow`, watch *Hot water planned action*
for a few days, then `active`.
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
