"""User-facing texts of the control logic, in English and Swedish.

The controllers explain themselves (a component's reason, the optimiser's
plan, the hot water reasons, the legionella guard). They return a ``Msg``:
a ``str`` whose value is the English text, so comparisons, logs and the
tests read as before, that also carries a stable ``code`` and the
``params`` to render it in another language. The entities render it with
Home Assistant's language (``localise``) and can expose the code.

- Templates take ``{name}`` or ``{name:spec}`` placeholders. A parameter
  that is itself a ``Msg`` is rendered in the same language.
- Numbers are formatted with the spec, then in Swedish with a decimal
  comma and the minus sign U+2212 (signs stay: "+2", "−1,5").
- Swedish texts start with a capital letter when they stand alone, and
  stay lower case inside another text.
- A code missing in a language falls back to English, an unknown code to
  the code itself. Log messages stay English (``str(msg)``).
- ``i18n`` gives the texts in an attribute as codes and parameters, so
  the cards can render them in the viewer's own language.

No Home Assistant imports; Python 3.9 compatible.
"""

from __future__ import annotations

import math
import re
import string
from typing import Any, Dict, Mapping, Optional, Tuple

LANGS = ("en", "sv")

# code -> (English, Swedish)
CATALOGUE: Dict[str, Tuple[str, str]] = {
    # -- shared --------------------------------------------------------------
    "common.off": ("off", "av"),
    "common.unknown": ("unknown", "okänd"),
    "common.error": ("error: {error}", "fel: {error}"),
    "common.join": ("{first}; {second}", "{first}; {second}"),
    "common.lux_running": ("temporary lux already running", "tillfällig lyx pågår redan"),
    "common.time_limit": ("time limit reached", "tidsgränsen nådd"),
    # Price levels (the English value is the level's code)
    "price_level.VERY_CHEAP": ("VERY_CHEAP", "väldigt billigt"),
    "price_level.CHEAP": ("CHEAP", "billigt"),
    "price_level.NORMAL": ("NORMAL", "normalt"),
    "price_level.EXPENSIVE": ("EXPENSIVE", "dyrt"),
    "price_level.VERY_EXPENSIVE": ("VERY_EXPENSIVE", "väldigt dyrt"),
    "price_source.optimiser": ("optimiser", "prisoptimering"),
    "price_source.own thresholds": ("own thresholds", "egna gränser"),
    # House model confidence
    "model_level.prior": ("prior", "startvärden"),
    "model_level.low": ("low", "låg"),
    "model_level.medium": ("medium", "medel"),
    "model_level.high": ("high", "hög"),
    # Hot water comfort modes and the Away hot water choice
    "hwmode.economy": ("economy", "ekonomi"),
    "hwmode.normal": ("normal", "normal"),
    "hwmode.luxury": ("luxury", "lyx"),
    "away_hw.economy": ("economy", "ekonomi"),
    "away_hw.minimum": ("minimum", "minimum"),
    "away_hw.normal": ("normal", "normal"),
    # Off / shadow / active
    "mode.off": ("off", "av"),
    "mode.shadow": ("shadow", "skugga"),
    "mode.active": ("active", "aktiv"),
    # -- indoor component (logic/indoor.py) ----------------------------------
    "indoor.sensor_missing": ("indoor sensor missing", "inomhustemperatur saknas"),
    "indoor.sensor_stale": ("indoor sensor stale", "inomhustemperaturen är inaktuell"),
    "indoor.implausible": ("indoor sensor implausible ({value:.1f})", "orimlig inomhustemperatur ({value:.1f} °C)"),
    "indoor.setpoint_missing": ("setpoint missing", "börvärde saknas"),
    "indoor.factor_missing": ("factor missing", "inomhusfaktor saknas"),
    "indoor.ok": (
        "setpoint {setpoint:.1f}, indoor {indoor:.1f}, factor {factor:g}",
        "börvärde {setpoint:.1f} °C, inomhus {indoor:.1f} °C, faktor {factor:g}",
    ),
    # -- price component (logic/price.py) ------------------------------------
    "price.no_price": ("no price for the current hour", "inget pris för den här timmen"),
    "price.level": ("{level} ({source})", "{level} ({source})"),
    "price.guard_needs": ("{level}, guard needs {why}", "{level}, spärren kan inte avgöra: {why}"),
    "price.guard_blocked": (
        "{level}, indoor {indoor:.1f} not above {limit:.1f}",
        "{level}, inomhus {indoor:.1f} °C är inte över {limit:.1f} °C",
    ),
    # Price status on the price level sensor (runtime.py)
    "price.status_ok": ("ok", "ok"),
    "price.no_source": ("no price source configured", "ingen priskälla vald"),
    "price.no_sensor": ("no price sensor configured", "ingen prissensor vald"),
    "price.no_prices_in": ("no prices in {entity}", "inga priser i {entity}"),
    "price.tibber_missing": (
        "{service} is not available; set up the Tibber integration or choose another price source",
        "{service} är inte tillgänglig; lägg till Tibber-integrationen eller välj en annan priskälla",
    ),
    "price.tibber_timeout": ("{service} timed out", "{service} svarade inte i tid"),
    "price.tibber_failed": ("{service} failed: {error}", "{service} misslyckades: {error}"),
    "price.tibber_no_prices": ("no prices for Tibber home {home}", "inga priser för Tibber-hemmet {home}"),
    # -- weather component (logic/weather.py) --------------------------------
    "weather.outdoor_missing": ("outdoor sensor missing", "utomhustemperatur saknas"),
    "weather.outdoor_stale": ("outdoor sensor stale", "utomhustemperaturen är inaktuell"),
    "weather.curve_unknown": ("heat curve unknown", "värmekurvan är okänd"),
    "weather.own_curve": ("own heat curve (0) is not supported", "egen värmekurva (0) stöds inte"),
    "weather.no_forecast": ("no forecast", "ingen prognos"),
    "weather.forecast_stale": ("forecast stale (fetched {fetched})", "prognosen är inaktuell (hämtad {fetched})"),
    "weather.not_covered": (
        "forecast does not cover now and {hours:g} h ahead",
        "prognosen täcker inte nu och {hours:g} h framåt",
    ),
    "weather.no_wind": ("wind control on but forecast lacks wind", "vindstyrning är på men prognosen saknar vind"),
    "weather.raise_blocked": ("raising blocked ({offset:+.2f})", "höjning spärrad ({offset:+.2f})"),
    "weather.lower_blocked": ("lowering blocked ({offset:+.2f})", "sänkning spärrad ({offset:+.2f})"),
    "weather.forecast": (
        "outdoor {outdoor:.1f}, forecast {basis:.1f} in {hours:g} h",
        "ute {outdoor:.1f} °C, prognos {basis:.1f} °C om {hours:g} h",
    ),
    "weather.feel": (
        "outdoor {outdoor:.1f}, feel {basis:.1f} in {hours:g} h",
        "ute {outdoor:.1f} °C, upplevt {basis:.1f} °C om {hours:g} h",
    ),
    # -- away component (logic/away.py) --------------------------------------
    "away.indoor": (
        "away: indoor control works on the lowered setpoint",
        "borta: inomhusregleringen styr mot det sänkta börvärdet",
    ),
    "away.fixed": (
        "away: fixed curve offset {offset:+g} (indoor control not active)",
        "borta: fast kurvjustering {offset:+g} (inomhusreglering inte aktiv)",
    ),
    "away.no_charges": ("away: no planned charges", "borta: inga planerade laddningar"),
    # -- RMU40 room temperature feed (logic/rmu.py) ---------------------------
    "rmu.room_missing": ("room temperature missing", "rumstemperatur saknas"),
    "rmu.room_stale": ("room temperature stale", "rumstemperaturen är inaktuell"),
    "rmu.implausible": ("room temperature implausible ({value:.1f})", "orimlig rumstemperatur ({value:.1f} °C)"),
    "rmu.room": ("room temperature {value:.1f}", "rumstemperatur {value:.1f} °C"),
    "rmu.feed_off": ("feed off", "matningen är av"),
    "rmu.no_entity": ("no room temperature entity configured", "ingen entitet för rumstemperatur vald"),
    # -- gateway (logic/gateway.py) ------------------------------------------
    "gateway.no_status": (
        "No message on {topic}: is the NibePi gateway running and connected to this "
        "MQTT broker with the same topic prefix?",
        "Ingen data på {topic}: körs NibePi-gatewayen och är den ansluten till samma "
        "MQTT-broker med samma prefix?",
    ),
    # -- strategy and optimiser gate (optstrategy.py, optimizer/gate.py) -------
    "strategy.rules": ("rules", "regler"),
    "strategy.not_ready": ("optimiser not ready: no plan yet", "optimeraren är inte redo: ingen plan än"),
    "strategy.not_used": ("optimiser not used: {reason}", "optimeraren används inte: {reason}"),
    "gate.optimiser": ("optimiser", "optimeraren styr"),
    "gate.floor": ("below comfort floor {floor:.1f} °C: heating", "under komfortgolvet {floor:.1f} °C: värmer"),
    "gate.not_fitted": ("house model not fitted ({why})", "husmodellen är inte inlärd ({why})"),
    "gate.no_data": ("no data", "ingen data"),
    "gate.low_confidence": (
        "model confidence {level} ({confidence:.2f}) below {min_level}",
        "modellens säkerhet är {level} ({confidence:.2f}), lägre än {min_level}",
    ),
    "gate.short_horizon": ("only {hours:.1f} h of prices", "bara {hours:.1f} h med priser"),
    # -- optimiser plans (optrun.py, opt_runtime.py) --------------------------
    "opt.no_plan": ("no plan", "ingen plan"),
    "opt.no_plan_why": ("no plan ({why})", "ingen plan ({why})"),
    "opt.plan_old": ("plan older than {minutes:d} min", "planen är äldre än {minutes:d} min"),
    "opt.not_planned": ("not planned yet", "inte planerad än"),
    "opt.no_indoor": ("indoor temperature missing", "inomhustemperatur saknas"),
    "opt.no_prices": ("no prices cover now", "inga priser för nu"),
    "opt.no_bt6": ("BT6 missing or stale", "BT6 saknas eller är inaktuell"),
    "opt.no_stops": ("stop temperatures unknown", "stopptemperaturerna är okända"),
    # Electricity meter (opt_runtime.py)
    "meter.not_found": ("{entity} not found", "{entity} hittades inte"),
    "meter.bad_unit": (
        "{entity}: unit {unit} is not a power or energy meter",
        "{entity}: enheten {unit} hör inte till en effekt- eller energimätare",
    ),
    # -- plan explanation (optimizer/explain.py) -------------------------------
    "explain.line": (
        "{span} offset {offset:+d}: {why}, indoor {t_end:.1f} °C at {at}",
        "{span} kurvjustering {offset:+d}: {why}, inomhus {t_end:.1f} °C kl. {at}",
    ),
    "explain.price": ("{price:.2f} {unit} vs {avg:.2f} average", "{price:.2f} {unit} mot snittet {avg:.2f}"),
    "explain.cheap": ("cheap ({price}), storing heat", "billigt ({price}), lagrar värme"),
    "explain.cheap_before": (
        "cheap ({price}), storing heat before the dearer {later}",
        "billigt ({price}), lagrar värme före det dyrare {later}",
    ),
    "explain.comfort_low": ("keeping indoor above {lower:.1f} °C", "håller inomhus över {lower:.1f} °C"),
    "explain.colder": ("colder outside ({t_out:.1f} °C) ahead", "kallare ute ({t_out:.1f} °C) framöver"),
    "explain.storing": ("storing heat ({price})", "lagrar värme ({price})"),
    "explain.expensive": ("expensive ({price}), coasting on stored heat", "dyrt ({price}), lever på lagrad värme"),
    "explain.comfort_high": ("indoor above {upper:.1f} °C", "inomhus över {upper:.1f} °C"),
    "explain.milder": ("milder or sunny ({t_out:.1f} °C outside)", "mildare eller sol ({t_out:.1f} °C ute)"),
    "explain.saving": ("saving ahead of later heating ({price})", "sparar inför senare uppvärmning ({price})"),
    "explain.neutral": ("normal operation", "normal drift"),
    "explain.summary": (
        "Planned {kwh:.1f} kWh for {cost:.2f} {unit}, base offset {base:+d} alone {base_kwh:.1f} kWh "
        "for {base_cost:.2f} {unit}: saves {savings:.2f} {unit} (heat left in the house counted). "
        "Indoor {t_min:.1f}-{t_max:.1f} °C, {changes:d} changes.",
        "Planerat {kwh:.1f} kWh för {cost:.2f} {unit}, med bara grundjusteringen {base:+d} {base_kwh:.1f} kWh "
        "för {base_cost:.2f} {unit}: sparar {savings:.2f} {unit} (värmen kvar i huset medräknad). "
        "Inomhus {t_min:.1f}–{t_max:.1f} °C, {changes:d} ändringar.",
    ),
    # -- hot water optimiser blocks (optimizer/tank.py) ------------------------
    "tank.legionella": ("legionella cycle (fixed)", "legionellacykel (fast)"),
    "tank.price": ("price {price:.2f} vs {avg:.2f} average", "pris {price:.2f} mot snittet {avg:.2f}"),
    "tank.price_before": (
        "price {price:.2f} vs {avg:.2f} average, before the draw expected at {at}",
        "pris {price:.2f} mot snittet {avg:.2f}, före tappningen som väntas kl. {at}",
    ),
    "tank.pump_forced": (
        "pump charges by itself below economy start",
        "pumpen laddar själv under starttemperaturen för ekonomi",
    ),
    "tank.waiting": ("waiting for cheaper slots", "väntar på billigare timmar"),
    # -- hot water: price comfort, automatic luxury, priority (hotwater.py) ---
    "hw.comfort_unchanged": ("{level}: unchanged", "{level}: oförändrat"),
    "hw.comfort_mode": ("{level}: {mode}", "{level}: {mode}"),
    "autolux.no_bt6": ("BT6 missing", "BT6 saknas"),
    "autolux.no_stop": ("stop temperature unknown", "stopptemperaturen är okänd"),
    "autolux.no_samples": ("no BT6 samples yet", "inga BT6-värden än"),
    "autolux.trigger": (
        "BT6 {bt6:.1f} fell to the trigger {trigger:.1f}: heat to {target:.1f}",
        "BT6 {bt6:.1f} °C föll till gränsen {trigger:.1f} °C: värmer till {target:.1f} °C",
    ),
    "autolux.waiting": ("BT6 {bt6:.1f}, trigger {trigger:.1f}", "BT6 {bt6:.1f} °C, gräns {trigger:.1f} °C"),
    "autolux.reached": ("BT6 {bt6:.1f} reached the target", "BT6 {bt6:.1f} °C har nått målet"),
    "autolux.heating": ("BT6 {bt6:.1f}, heat to {target:.1f}", "BT6 {bt6:.1f} °C, värmer till {target:.1f} °C"),
    "priority.no_bt7": ("BT7 missing", "BT7 saknas"),
    "priority.no_temps": ("start/stop temperature unknown", "start- eller stopptemperaturen är okänd"),
    "priority.start": (
        "BT7 {bt7:.1f} at the start temperature {start:.1f}",
        "BT7 {bt7:.1f} °C vid starttemperaturen {start:.1f} °C",
    ),
    "priority.over": (
        "BT7 {bt7:.1f} over the start temperature {start:.1f}",
        "BT7 {bt7:.1f} °C över starttemperaturen {start:.1f} °C",
    ),
    "priority.reached": ("BT7 {bt7:.1f} reached {limit:.1f}", "BT7 {bt7:.1f} °C har nått {limit:.1f} °C"),
    "priority.heating": ("BT7 {bt7:.1f}, heat to {limit:.1f}", "BT7 {bt7:.1f} °C, värmer till {limit:.1f} °C"),
    # -- VV-AI control (vvplan.py) ---------------------------------------------
    "vv.floor": (
        "BT7 {bt7:.1f} under the floor: heat to {target:.1f}",
        "BT7 {bt7:.1f} °C under lägsta temperatur: värmer till {target:.1f} °C",
    ),
    "vv.no_window": ("no heating window planned", "inget värmefönster planerat"),
    "vv.next_window": ("no window now, next {at}", "inget fönster nu, nästa {at}"),
    "vv.no_target": ("stop temperature for the window unknown", "fönstrets stopptemperatur är okänd"),
    "vv.span": (
        "window {start}-{end} for {peak} ({mode}, {target:.1f} °C)",
        "fönster {start}–{end} inför {peak} ({mode}, {target:.1f} °C)",
    ),
    "vv.waiting": ("{span}: waiting for the cheap hours", "{span}: väntar på de billiga timmarna"),
    "vv.waiting_cheapest": (
        "{span}: waiting for the cheap hours (cheapest {cheap_start}-{cheap_end})",
        "{span}: väntar på de billiga timmarna (billigast {cheap_start}–{cheap_end})",
    ),
    "vv.closed": ("{span}: done, closed until {until}", "{span}: klar, stängt till {until}"),
    "vv.heat": ("{span}: BT7 {bt7:.1f}, heat", "{span}: BT7 {bt7:.1f} °C, värmer"),
    "vv.reached": (
        "{span}: BT7 {bt7:.1f} reached, closed until {until}",
        "{span}: BT7 {bt7:.1f} °C nådd, stängt till {until}",
    ),
    # -- hot water control (hwcontrol.py) --------------------------------------
    "hw.mode_off": ("hot water mode off", "varmvattenläget är av"),
    "hw.away_reduced": (
        "away ({away}): economy mode, no planned charges or boosts",
        "borta ({away}): ekonomiläge, inga planerade laddningar eller lyft",
    ),
    "hw.away_unchanged": ("away ({away}): hot water unchanged", "borta ({away}): varmvattnet oförändrat"),
    "hw.away_economy": ("away: economy", "borta: ekonomi"),
    "hw.away_off": ("away: off", "borta: av"),
    "hw.opt_drives": ("drives the charges", "styr laddningarna"),
    "hw.opt_no_plan": ("no plan ({why})", "ingen plan ({why})"),
    "hw.opt_no_plan_vv": ("no plan ({why}): VV-AI plan used", "ingen plan ({why}): VV-AI-planen används"),
    "hw.away_economy_pump": (
        "away (economy): the pump heats hot water itself in economy mode",
        "borta (ekonomi): pumpen värmer varmvattnet själv i ekonomiläge",
    ),
    "hw.away_minimum_vv_off": (
        "away (minimum): VV-AI control is off, so the pump heats hot water itself in economy mode",
        "borta (minimum): VV-AI-styrningen är av, så pumpen värmer varmvattnet själv i ekonomiläge",
    ),
    "hw.no_opt_plan": (
        "no optimiser plan: hot water left to the pump",
        "ingen plan från optimeraren: pumpen sköter varmvattnet",
    ),
    "hw.precedence": (
        "automatic luxury or hot water priority is on and takes precedence",
        "automatisk lyx eller varmvattenprioritet är på och går före",
    ),
    "hw.no_bt7": (
        "BT7 missing or stale: hot water left to the pump",
        "BT7 saknas eller är inaktuell: pumpen sköter varmvattnet",
    ),
    "hw.no_stops": (
        "stop temperatures unknown: hot water left to the pump",
        "stopptemperaturerna är okända: pumpen sköter varmvattnet",
    ),
    "hw.no_period": (
        "hot water period (47134) unknown: hot water left to the pump",
        "varmvattenperioden (47134) är okänd: pumpen sköter varmvattnet",
    ),
    "hw.period_zero": (
        "the pump's hot water period (47134) is 0; the gateway would restore 0, so set it in the pump first",
        "pumpens varmvattenperiod (47134) är 0; gatewayen skulle återställa 0, så ställ in den i pumpen först",
    ),
    "hw.minimum_needs_floor": (
        "away (minimum) needs the min-temp floor: the pump heats hot water itself",
        "borta (minimum) kräver lägsta temperatur: pumpen värmer varmvattnet själv",
    ),
    "hw.nothing_learned": (
        "nothing learned yet and no min-temp floor: hot water left to the pump",
        "inget inlärt än och ingen lägsta temperatur: pumpen sköter varmvattnet",
    ),
    "hw.minimum_fallback": (
        "{reason} (away minimum falls back to economy)",
        "{reason} (borta minimum blir ekonomi)",
    ),
    "hw.floor_status": ("BT7 {bt7:.1f}, min-temp floor {floor:.1f}", "BT7 {bt7:.1f} °C, lägsta temperatur {floor:.1f} °C"),
    "hw.minimum_floor_only": (
        "away (minimum): only the min-temp floor; {reason}",
        "borta (minimum): bara lägsta temperatur; {reason}",
    ),
    # -- legionella guard (legionella.py) --------------------------------------
    "leg.result.done": ("done", "klar"),
    "leg.result.cancelled": ("cancelled", "avbruten"),
    "leg.result.timeout": ("aborted: timeout", "avbruten: tidsgräns"),
    "leg.result.bt7": ("aborted: BT7 missing or stale", "avbruten: BT7 saknas eller är inaktuell"),
    "leg.result.aborted": ("aborted", "avbruten"),
    "leg.how.price": ("price", "pris"),
    "leg.how.default hour": ("default hour", "standardtimme"),
    "leg.how.now": ("now", "nu"),
    "leg.how.deadline": ("deadline", "sista tidpunkt"),
    "leg.done": ("legionella cycle done: BT7 held {temp:.1f} °C", "legionellacykeln klar: BT7 höll {temp:.1f} °C"),
    "leg.aborted_bt7": (
        "BT7 missing or stale: legionella cycle aborted",
        "BT7 saknas eller är inaktuell: legionellacykeln avbruten",
    ),
    "leg.aborted_timeout": (
        "BT7 did not hold {temp:.1f} °C within {hours:g} h: legionella cycle aborted",
        "BT7 höll inte {temp:.1f} °C inom {hours:g} h: legionellacykeln avbruten",
    ),
    "leg.running": (
        "legionella cycle: heat to {temp:.1f} °C, BT7 {bt7:.1f}",
        "legionellacykel: värmer till {temp:.1f} °C, BT7 {bt7:.1f} °C",
    ),
    "leg.running_held": (
        "legionella cycle: heat to {temp:.1f} °C, BT7 {bt7:.1f} (held {minutes:d} min)",
        "legionellacykel: värmer till {temp:.1f} °C, BT7 {bt7:.1f} °C (hållit {minutes:d} min)",
    ),
    "leg.guard_off": ("legionella guard off", "legionellaskyddet är av"),
    "leg.who_pump": ("the pump's periodic increase", "pumpens periodiska höjning"),
    "leg.who_guard": ("the legionella guard", "legionellaskyddet"),
    "leg.next_due": ("next due {due} ({who})", "nästa gång {due} ({who})"),
    "leg.retry": ("{result}; retry from {at}", "{result}; nytt försök från {at}"),
    "leg.planned": ("due: planned {start}-{end} ({how})", "dags: planerad {start}–{end} ({how})"),
    "leg.would_run": ("due: would run now (hot water mode {mode})", "dags: skulle köra nu (varmvattenläge {mode})"),
    "leg.why_requested": ("run now (requested)", "kör nu (begärd)"),
    "leg.why_due": ("due ({how})", "dags ({how})"),
    "leg.started": (
        "legionella cycle started, {why}: heat to {temp:.1f} °C",
        "legionellacykel startad, {why}: värmer till {temp:.1f} °C",
    ),
}

_LANG_INDEX = {"en": 0, "sv": 1}
_MINUS = "−"


def lang_of(language: Optional[str]) -> str:
    """"sv" for any Swedish language tag (sv, sv-SE, sv_FI), else "en"."""
    return "sv" if re.match(r"^sv(?:$|[-_])", str(language or ""), re.IGNORECASE) else "en"


def _template(code: str, lang: str) -> Optional[str]:
    entry = CATALOGUE.get(code)
    if entry is None:
        return None
    text = entry[_LANG_INDEX.get(lang, 0)]
    return text or entry[0]


class _Formatter(string.Formatter):
    def __init__(self, lang: str) -> None:
        super().__init__()
        self.lang = lang

    def format_field(self, value: Any, format_spec: str) -> str:
        if isinstance(value, Msg):
            return value.render(self.lang, nested=True)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            if format_spec.endswith("d") and isinstance(value, float):
                value = int(value)
            text = format(value, format_spec)
            if self.lang == "sv":
                text = text.replace(".", ",").replace("-", _MINUS)
            return text
        return format(value, format_spec)


def _capitalise(text: str) -> str:
    return text[:1].upper() + text[1:]


def render(code: str, params: Optional[Mapping[str, Any]] = None, lang: str = "en", nested: bool = False) -> str:
    """The text of ``code`` in ``lang`` ("en"/"sv", other values: English).

    Falls back to the English template when the language's does not
    format, and to the code itself when the code is unknown."""
    lang = lang if lang in _LANG_INDEX else "en"
    params = dict(params or {})
    for attempt in ((lang,) if lang == "en" else (lang, "en")):
        template = _template(code, attempt)
        if template is None:
            return code
        try:
            text = _Formatter(attempt).vformat(template, (), params)
        except (KeyError, IndexError, ValueError, TypeError):
            continue
        if attempt == "sv" and not nested and _starts_with_words(template, params):
            text = _capitalise(text)
        return text
    return code


def _starts_with_words(template: str, params: Mapping[str, Any]) -> bool:
    """False when the text starts with a value such as an entity id or a
    time, which keeps its own case."""
    for literal, name, _spec, _conv in string.Formatter().parse(template):
        if literal:
            return True
        if name is not None:
            return isinstance(params.get(name), Msg)
    return True


class Msg(str):
    """A text: the English rendering as the ``str`` value, plus the
    ``code`` and ``params`` to render it in another language."""

    code: str
    params: Dict[str, Any]

    def __new__(cls, code: str, params: Optional[Mapping[str, Any]] = None) -> "Msg":
        params = dict(params or {})
        obj = super().__new__(cls, render(code, params, "en"))
        obj.code = code
        obj.params = params
        return obj

    def __reduce__(self):  # copy and pickle keep the code
        return (Msg, (self.code, self.params))

    def render(self, lang: str, nested: bool = False) -> str:
        return render(self.code, self.params, lang, nested)


def msg(code: str, **params: Any) -> Msg:
    return Msg(code, params)


def term(group: str, value: Any) -> Any:
    """``value`` of an enumeration (a price level, a mode) as a Msg when
    the catalogue has ``group.value``; otherwise the value unchanged."""
    code = "%s.%s" % (group, value)
    return Msg(code) if code in CATALOGUE else value


def code_of(value: Any) -> Optional[str]:
    return value.code if isinstance(value, Msg) else None


def codes(values: Mapping[str, Any]) -> Dict[str, Optional[str]]:
    """{key: code} of a mapping of texts (None for a text without one)."""
    return {key: code_of(value) for key, value in values.items()}


def localise(value: Any, lang: str) -> Any:
    """``value`` with every Msg in it (also inside dicts, lists and tuples)
    rendered in ``lang`` as a plain str; anything else unchanged."""
    if isinstance(value, Msg):
        return value.render(lang)
    if isinstance(value, dict):
        return {key: localise(item, lang) for key, item in value.items()}
    if isinstance(value, list):
        return [localise(item, lang) for item in value]
    if isinstance(value, tuple):
        return tuple(localise(item, lang) for item in value)
    return value


def _param(value: Any) -> Any:
    if isinstance(value, Msg):
        return structured(value)
    if value is None or isinstance(value, (bool, int)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    return str(value)


def structured(value: Msg) -> Dict[str, Any]:
    """A text as the cards render it: ``{"c": code, "p": params}``.

    Parameters that are texts are structured the same way; ``p`` is left
    out when there are none. The cards render it in the viewer's language
    (frontend/nibepi-texts.js is generated from the catalogue)."""
    out: Dict[str, Any] = {"c": value.code}
    if value.params:
        out["p"] = {key: _param(item) for key, item in value.params.items()}
    return out


def i18n(value: Any) -> Any:
    """The texts inside ``value`` in their structured form, None when it
    holds none: a Msg as ``structured``; of a dict the keys that hold
    texts; of a list or tuple every item (None for one without texts)."""
    if isinstance(value, Msg):
        return structured(value)
    if isinstance(value, dict):
        out = {}
        for key, item in value.items():
            found = i18n(item)
            if found is not None:
                out[key] = found
        return out or None
    if isinstance(value, (list, tuple)):
        items = [i18n(item) for item in value]
        return items if any(item is not None for item in items) else None
    return None


# The attribute with the texts as codes and parameters (``attributes``).
I18N = "i18n"


def attributes(attrs: Mapping[str, Any], lang: str) -> Dict[str, Any]:
    """Entity attributes with their texts rendered in ``lang`` and, under
    ``i18n``, as codes and parameters (``{"problem": {"c": ..., "p": ...}}``),
    for the cards to render in the viewer's language."""
    out = localise(dict(attrs), lang)
    texts = i18n(dict(attrs))
    if texts:
        out[I18N] = texts
    return out


def placeholders(template: str) -> set:
    """The placeholder names of a template (for the catalogue tests)."""
    return {name for _text, name, _spec, _conv in string.Formatter().parse(template) if name is not None}
