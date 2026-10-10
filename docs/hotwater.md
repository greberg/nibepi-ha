# Hot water and VV learning (Phase G5)

Phase G5 moves NibePi's hot water control from Node-RED (`config_node.js`
at the `nodered-final` tag) into this integration. G5 is a port: same
behaviour, same defaults, except for the differences listed below. The cost
optimiser comes later, in G6.

## What Node-RED did

Three independent features wrote the same pump registers:

| Feature | Node-RED | Registers |
|---|---|---|
| VV-AI learning | `vvAiTick` draw detection, `registerVvDraw`, `vvAiApplyDailyDecayIfNeeded` | none (profile file) |
| VV-AI control | `vvAiBuildPlan`, `vvAiPlanHeatAllowed`, control part of `vvAiTick` | 47134 hot water period, 48132 temporary lux |
| Automatic luxury, hot water priority | `hotwaterPlugin` | 48132 |
| Price comfort mode | hot water part of `priceAdjustCurve`, `priceSetHwMode`, `priceReleaseHwMode` | 47041 comfort mode |

**Learning model.** A weekly profile of 168 hour slots in local wall-clock
time (Monday 00:00 = 0). Each value is an exponential moving average of the
hot water drawn in that hour, measured as the fall of BT6 (40014, hot water
load):

- *Draw detection.* An event opens when BT6 falls 1.0 °C below its running
  peak. It closes when BT6 recovers 0.5 °C, makes no new low for 15 min,
  runs 6 h, or has fallen slower than 0.9 °C/h over the last hour. A slow
  event (under 0.9 °C/h for 30 min) is re-based instead of closed, so
  standby loss is not credited to a later draw. The drop (peak − trough)
  is learned when it is at least `min_drop` (3 °C) and fell at 1.2 °C/h or
  faster (standby and circulation loss is slower). It is booked on the hour
  in which most degrees were drawn.
- *Charging lockout.* While the pump charges hot water (43086 prio = 20)
  and for 10 min after, BT6 measures the charging circuit, not the tank, so
  detection restarts. An open event is evaluated, not dropped, when
  charging starts.
- *Update.* `profile[slot] = old · (1 − α) + drop · α`, α = learning rate / 100
  (default 10 → 0.10).
- *Decay.* At each local day change, the 24 slots of the day that ended are
  multiplied by (1 − α) unless a draw was learned in them that day; days the
  system did not run are decayed as wholly unseen (at most 32).

**Plan.** Days run 02:00 to 02:00 (a draw just after midnight belongs to the
evening before). A day's sum of profile values picks its mode: below
`day_small` (1.0) no heating, below `day_medium` (3.0) economy, below
`day_large` (6.0) normal, else luxury. The day's largest slot is its peak;
a second slot of at least `peak_threshold` (3.0) and 6 h or more away is a
second peak (and caps the day at normal). Before each peak a heating window
of `preheat` hours (2) ends at the peak; with price control it is moved to
the cheapest such window within `price_window` hours (7) before the peak.
Inside the window, the two cheapest consecutive hours are where price
control allows heating, or the last two hours before the peak once those
have passed.

**Control.** VV-AI control holds 47134 at 0, so the pump does not heat hot
water on its own, and starts every charge itself with 48132 = 4 ("one time
increase"), stopping it (48132 = 0) when BT7 (40013, hot water top) reaches
the target. Priority: the min-temp floor (BT7 below `min_temp` → heat to
`min_temp + 4`, at most the luxury stop temperature), then the plan (in a
window, target = the stop temperature of the day's mode; once BT7 reaches it,
the rest of the window is closed). Automatic luxury (BT6 fell `diff` °C
within `time` minutes → 48132 = 4 until BT6 is 5 °C below where it started,
or 1 °C under the stop temperature) and hot water priority (BT7 at the start
temperature → 48132 = 4 until 1 °C under the stop temperature) switched
VV-AI control off. Price comfort mode set 47041 to the mode configured for
the current price level.

## What is ported

Everything above, with the same constants and defaults, as pure modules:

- `logic/vvlearn.py`: profile, decay, draw detection.
- `logic/vvplan.py`: day modes, peaks, heating windows, price placement,
  heat-allowed rule, min-temp floor and window closing.
- `logic/hotwater.py`: automatic luxury, hot water priority, comfort mode by
  price level, and **one controller** that combines them into a single
  decision per tick with a reason per feature; plus the lease decisions for
  47041, 47134 and 48132.
- `logic/vvimport.py`: conversion of the Node-RED profile file.

One controller owns the three registers, so two features never write the
same register: 48132 is 4 when any feature wants a charge, 47134 is 0 only
while VV-AI control is in charge, 47041 comes from the price level only.
As in Node-RED, automatic luxury or hot water priority on means VV-AI
control stands aside.

Mode select **Hot water mode** `off` / `shadow` (default) / `active`, separate
from the curve-offset modes. Learning runs in `shadow` and `active` (it only
observes); `off` stops everything.

## Deliberate differences

| Node-RED | Here | Why |
|---|---|---|
| Direct writes; ownership flags (`vv_hw_period_owned`, `vvStartHWLast`, `priceOwned.hwMode`) to undo them | Leases with owner `ha`; releasing restores 47134 and 47041 to the gateway's captured baseline and 48132 to 0 | The gateway is the safety boundary; a dead HA restores the pump within 15 min |
| BT6/BT7 unreadable 5 ticks → hand back | BT6/BT7 missing or older than the sensor timeout, or a failed tick → release the leases at once | Same rule as the other inputs; a frozen value must not steer the pump |
| Learning alone held 47134 at 0 (fixed on 2026-10-10) | 47134 is leased only while VV-AI control is effective; otherwise it is released | That bug left the pump without hot water |
| Refuses nothing | VV-AI control does not take 47134 while the pump reads 0 there (the baseline would be 0), nor without stop temperatures, nor with an empty plan and no min-temp floor | Never leave the pump with hot water blocked |
| Min-temp floor off by default, no default temperature | On by default, 35 °C | With 47134 held at 0, the floor is the only guarantee of hot water |
| Learning off by default | On by default | It only observes; the profile is then ready when control goes active |
| Hourly plan by hour-of-week index over a 48 h price list; a 15-minute price list overwrote each hour with its last quarter | Plan in absolute time, windows placed in 15-minute steps on the real slot prices (time-weighted) | Prices are 15-minute since 2025-10-01; the index ring also broke across DST |
| Window arithmetic on slot indexes | Peaks at local wall-clock hours, windows as real durations | DST-safe; same result on all other days |
| Settings as switch + slider pairs (`*_enable`) | One option each, default = the value used when the switch was off | Fewer settings, same behaviour |
| Price comfort mode: no price for 30 min → back to `hotwater_normal` | No price for the current hour → release 47041 at once | As the curve offset's price component; the baseline is the pump's own setting |
| Automatic luxury followed the pump's 48132 reading as "running" | Own state, plus a 3 h limit | The lease value is not read back; the limit ends a charge whose target is never reached (e.g. in shadow mode) |
| A draw booked before midnight but closed after it could decay a day twice | Decay is driven by the tick's date only | Bug fix |
| Time schedule (`vv_manual_schedule_*`) | Not ported | Use an HA schedule/automation on the mode select; it also fought the leases (it released 47134 5 s after each start) |
| Learned profile in `/etc/nibepi/vv_ai_profile.json` | HA storage (`.storage/nibepi_vv_profile_<entry>`), versioned, debounced saves | Survives restarts; import with `nibepi.import_vv_profile` |
| Chart (`hotwaterAiBuildGraph`) | Sensor attributes (plan for 24 h, profile) | Use any HA chart card |
