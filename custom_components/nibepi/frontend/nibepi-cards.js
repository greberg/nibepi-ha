// NibePi Lovelace cards: climate, plan, hot water, gateway and savings.
//
// Served by the integration (frontend_cards.py) and loaded on every
// dashboard; plain custom elements, no build step and no external imports.
// Entity resolution and data shaping are in nibepi-core.js (unit tested);
// it is imported with this module's own ?v= query so both update together.

const core = await import(new URL(`./nibepi-core.js${new URL(import.meta.url).search}`, import.meta.url).href);
const {
  HW_LEASES,
  LEASES,
  PRICE_COLORS,
  awayModel,
  climateAlerts,
  contributions,
  countdown,
  dailyValues,
  formatAge,
  formatClock,
  formatNumber,
  formatSigned,
  hotwaterAlerts,
  hotwaterWindows,
  isMissing,
  leaseColor,
  legionellaModel,
  leaseRows,
  num,
  parseTime,
  planChart,
  resolveEntities,
  savingsModel,
  sparkline,
  stateOf,
  sysKey,
  toNumber,
} = core;

const VERSION = new URL(import.meta.url).searchParams.get("v") || "dev";
const DOCS = "https://github.com/greberg/nibepi-ha#cards";

// --------------------------------------------------------------------------
// Helpers
// --------------------------------------------------------------------------

const esc = (value) =>
  String(value ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);

const SYSTEM_LABEL = (system) => String(system || "").toUpperCase();

const PROBLEMS = {
  no_entry: "No NibePi integration found. Is it set up, and is its Gateway entity enabled?",
  unknown_entry: "No NibePi entry with this entry_id.",
};

const STYLES = `
  :host { display: block; }
  ha-card { height: 100%; overflow: hidden; }
  .header { display: flex; align-items: center; justify-content: space-between; gap: 8px;
    padding: 12px 16px 0; }
  .title { font-size: 16px; font-weight: 500; color: var(--primary-text-color);
    display: flex; align-items: center; gap: 8px; min-width: 0; }
  .title ha-icon { color: var(--state-icon-color); --mdc-icon-size: 20px; }
  .sub { color: var(--secondary-text-color); font-size: 12px; }
  .content { padding: 8px 16px 16px; display: flex; flex-direction: column; gap: 12px; }
  .row { display: flex; flex-wrap: wrap; gap: 8px 16px; align-items: baseline; }
  .grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(96px, 1fr)); gap: 8px; }
  .stat { display: flex; flex-direction: column; min-width: 0; }
  .label { color: var(--secondary-text-color); font-size: 12px; line-height: 16px; }
  .value { font-size: 16px; color: var(--primary-text-color); overflow-wrap: anywhere; }
  .big { font-size: 34px; line-height: 40px; font-weight: 400; white-space: nowrap; }
  .unit { font-size: 0.5em; color: var(--secondary-text-color); margin-left: 2px; }
  .muted { color: var(--secondary-text-color); }
  .small { font-size: 12px; }
  button.link { all: unset; cursor: pointer; display: inline-flex; flex-direction: column;
    min-width: 0; border-radius: 6px; }
  button.link:focus-visible { outline: 2px solid var(--primary-color); outline-offset: 2px; }
  button.link:hover .value { text-decoration: underline dotted; }
  .chips { display: flex; flex-wrap: wrap; gap: 6px; align-items: center; }
  .chips .label { margin-right: 2px; }
  .chip { font: inherit; font-size: 13px; line-height: 18px; padding: 4px 10px; border-radius: 16px;
    border: 1px solid var(--divider-color); background: transparent; color: var(--primary-text-color);
    cursor: pointer; }
  .chip:hover { border-color: var(--primary-color); }
  .chip.on { background: var(--primary-color); border-color: var(--primary-color);
    color: var(--text-primary-color, #fff); cursor: default; }
  .chip:disabled { opacity: 0.5; cursor: default; }
  .chip.toggle.on { cursor: pointer; }
  .chip ha-icon { --mdc-icon-size: 16px; vertical-align: -3px; margin-right: 4px; }
  .alerts { display: flex; flex-direction: column; gap: 6px; }
  .alert { display: flex; gap: 8px; align-items: center; padding: 6px 10px; border-radius: 8px;
    font-size: 13px; cursor: pointer; color: var(--primary-text-color); }
  .alert ha-icon { --mdc-icon-size: 18px; flex: none; }
  .alert.error { background: color-mix(in srgb, var(--error-color, #db4437) 14%, transparent); }
  .alert.error ha-icon { color: var(--error-color, #db4437); }
  .alert.warning { background: color-mix(in srgb, var(--warning-color, #ffa600) 16%, transparent); }
  .alert.warning ha-icon { color: var(--warning-color, #ffa600); }
  .bars { display: flex; flex-direction: column; gap: 4px; }
  .bar { display: grid; grid-template-columns: 96px 1fr 44px; gap: 8px; align-items: center;
    font-size: 12px; color: var(--secondary-text-color); cursor: pointer; }
  .track { position: relative; height: 8px; border-radius: 4px; background: var(--divider-color); }
  .track .mid { position: absolute; left: 50%; top: -2px; bottom: -2px; width: 1px;
    background: var(--secondary-text-color); opacity: 0.5; }
  .fill { position: absolute; top: 0; bottom: 0; border-radius: 4px; }
  .fill.pos { background: var(--warning-color, #ffa600); }
  .fill.neg { background: var(--info-color, var(--primary-color)); }
  .num { text-align: right; color: var(--primary-text-color); font-variant-numeric: tabular-nums; }
  .reason { color: var(--secondary-text-color); font-size: 12px; }
  .dot { display: inline-block; width: 10px; height: 10px; border-radius: 50%; flex: none; }
  .leases { display: flex; flex-direction: column; gap: 4px; }
  .lease { display: grid; grid-template-columns: 10px 1fr auto; gap: 8px; align-items: center;
    font-size: 13px; cursor: pointer; }
  .lease .meta { color: var(--secondary-text-color); font-size: 12px; text-align: right;
    font-variant-numeric: tabular-nums; }
  .lease .err { grid-column: 2 / 4; color: var(--error-color); font-size: 12px; }
  .dots { display: flex; flex-wrap: wrap; gap: 4px 12px; font-size: 12px; color: var(--secondary-text-color); }
  .dots span { display: inline-flex; gap: 6px; align-items: center; cursor: pointer; }
  .strip { position: relative; height: 18px; border-radius: 4px; background: var(--divider-color);
    overflow: hidden; }
  .strip .seg { position: absolute; top: 0; bottom: 0; }
  .strip .seg.vv { background: var(--info-color, var(--primary-color)); opacity: 0.55; }
  .strip .seg.charge { background: var(--warning-color, #ffa600); }
  .strip .seg.legionella { background: var(--error-color, #db4437); }
  .ticks { position: relative; height: 14px; font-size: 10px; color: var(--secondary-text-color); }
  .ticks span { position: absolute; transform: translateX(-50%); }
  ul { margin: 0; padding-left: 18px; font-size: 13px; color: var(--primary-text-color); }
  li { margin: 2px 0; }
  details summary { cursor: pointer; font-size: 13px; color: var(--secondary-text-color); }
  .empty { color: var(--secondary-text-color); font-size: 14px; padding: 8px 0; }
  .legend { display: flex; flex-wrap: wrap; gap: 4px 12px; font-size: 11px; color: var(--secondary-text-color); }
  .legend i { display: inline-block; width: 12px; height: 3px; margin-right: 4px; vertical-align: middle; }
  svg { display: block; width: 100%; height: auto; overflow: visible; }
  svg text { font-size: 10px; fill: var(--secondary-text-color); font-family: inherit; }
  a { color: var(--primary-color); }
  .divider { height: 1px; background: var(--divider-color); }
`;

// --------------------------------------------------------------------------
// Base card
// --------------------------------------------------------------------------

class NibePiCard extends HTMLElement {
  static cardType = "";
  static defaultIcon = "mdi:heat-pump";
  // ms between re-renders for clocks and countdowns (0: only on changes)
  tickMs = 0;

  constructor() {
    super();
    this.attachShadow({ mode: "open" });
    this._seen = new Map();
    this.shadowRoot.addEventListener("click", (ev) => this._onClick(ev));
    this.shadowRoot.addEventListener("keydown", (ev) => {
      if ((ev.key === "Enter" || ev.key === " ") && ev.target?.dataset?.more && ev.target.tagName !== "BUTTON") {
        ev.preventDefault();
        this._onClick(ev);
      }
    });
  }

  static getConfigElement() {
    return createEditor(this);
  }

  static getStubConfig(hass) {
    const found = hass ? resolveEntities(hass, {}) : null;
    return { system: found?.systems?.[0] || "s1" };
  }

  // Extra ha-form schema items for this card.
  static extraSchema() {
    return [];
  }

  static perSystem = true;

  setConfig(config) {
    if (!config || typeof config !== "object") throw new Error("Invalid configuration");
    if (config.system !== undefined && !["s1", "s2"].includes(config.system)) {
      throw new Error("system must be s1 or s2");
    }
    this._config = { ...config };
    this._seen.clear();
    this._render();
  }

  get system() {
    return this._config?.system || "s1";
  }

  set hass(hass) {
    const first = !this._hass;
    this._hass = hass;
    if (first || hass.entities !== this._entitiesRef || this._changed()) {
      this._entitiesRef = hass.entities;
      this._render();
    }
  }

  get hass() {
    return this._hass;
  }

  connectedCallback() {
    if (this.tickMs && !this._timer) this._timer = setInterval(() => this._render(), this.tickMs);
  }

  disconnectedCallback() {
    clearInterval(this._timer);
    this._timer = undefined;
  }

  getCardSize() {
    return 4;
  }

  getGridOptions() {
    return { columns: 12, min_columns: 6 };
  }

  // The entity ids whose state changes re-render the card.
  watched() {
    return Object.values(this._ids || {});
  }

  _changed() {
    let changed = false;
    for (const id of this.watched()) {
      const s = this._hass.states[id];
      if (this._seen.get(id) !== s) {
        this._seen.set(id, s);
        changed = true;
      }
    }
    return changed;
  }

  st(key) {
    return stateOf(this._hass, this._ids, key);
  }

  sk(key) {
    return sysKey(key, this.system);
  }

  fmt(stateObj, digits) {
    if (isMissing(stateObj)) return "–";
    if (digits === undefined && this._hass.formatEntityState) return this._hass.formatEntityState(stateObj);
    const unit = stateObj.attributes?.unit_of_measurement;
    const n = toNumber(stateObj.state);
    const text = n !== null && digits !== undefined ? n.toFixed(digits) : stateObj.state;
    return unit ? `${text} ${unit}` : text;
  }

  fmtOption(stateObj, option) {
    try {
      if (stateObj && this._hass.formatEntityState) return this._hass.formatEntityState(stateObj, option);
    } catch (_err) {
      // older frontends: fall through
    }
    return String(option).replace(/_/g, " ");
  }

  fmtTime(value) {
    const t = parseTime(value);
    if (t === null) return "–";
    const d = new Date(t);
    const sameDay = new Date().toDateString() === d.toDateString();
    const time = formatClock(t);
    if (sameDay) return time;
    const lang = this._hass?.locale?.language || this._hass?.language || undefined;
    return `${d.toLocaleDateString(lang, { weekday: "short" })} ${time}`;
  }

  // A tappable value that opens the more-info dialog.
  stat(key, label, text, extraClass = "") {
    const id = this._ids?.[key];
    const inner = `<span class="label">${esc(label)}</span><span class="value ${extraClass}">${text}</span>`;
    if (!id) return `<div class="stat">${inner}</div>`;
    return `<button class="link stat" data-more="${esc(key)}" title="${esc(id)}">${inner}</button>`;
  }

  chips(key, label) {
    const s = this.st(key);
    if (!s) return "";
    const options = s.attributes?.options || [];
    const disabled = isMissing(s) ? "disabled" : "";
    return `<div class="chips"><span class="label">${esc(label)}</span>${options
      .map(
        (o) =>
          `<button class="chip ${o === s.state ? "on" : ""}" ${disabled} data-select="${esc(key)}" data-option="${esc(o)}" title="${esc(
            this.fmtOption(s, o),
          )}">${esc(this.fmtOption(s, o))}</button>`,
      )
      .join("")}</div>`;
  }

  // The Away toggle chip with its return time; "" without the switch.
  awayChip(now) {
    const away = awayModel(this._hass, this._ids, now);
    if (!away) return "";
    const until = away.on && away.until !== null ? ` until ${esc(this.fmtTime(away.until))}` : "";
    const title = away.on ? "Away is on: tap to end it" : "Tap to start Away (no end time)";
    return `<div class="chips"><button class="chip toggle ${away.on ? "on" : ""}" ${away.available ? "" : "disabled"} data-toggle="away" title="${esc(
      title,
    )}"><ha-icon icon="${away.on ? "mdi:home-export-outline" : "mdi:home"}"></ha-icon>${away.on ? `Away${until}` : "Home"}</button></div>`;
  }

  alertList(alerts) {
    return alerts
      .map(
        (a) =>
          `<div class="alert ${a.level}" data-more="${esc(a.key)}" role="button" tabindex="0"><ha-icon icon="${
            a.level === "error" ? "mdi:alert-circle" : "mdi:alert"
          }"></ha-icon><span>${esc(a.text)}</span></div>`,
      )
      .join("");
  }

  _onClick(ev) {
    const target = ev.composedPath().find((el) => el.dataset && (el.dataset.more || el.dataset.select || el.dataset.toggle));
    if (!target) return;
    if (target.dataset.toggle) {
      this._toggle(target.dataset.toggle);
    } else if (target.dataset.select) {
      this._select(target.dataset.select, target.dataset.option);
    } else {
      const entityId = this._ids?.[target.dataset.more];
      if (entityId) {
        this.dispatchEvent(new CustomEvent("hass-more-info", { detail: { entityId }, bubbles: true, composed: true }));
      }
    }
  }

  // A switch toggle (reversible, so no confirmation).
  _toggle(key) {
    const s = this.st(key);
    if (!s || isMissing(s)) return;
    this._hass.callService("switch", s.state === "on" ? "turn_off" : "turn_on", { entity_id: s.entity_id || this._ids[key] });
  }

  _select(key, option) {
    const s = this.st(key);
    if (!s || s.state === option) return;
    if (option === "active") {
      const name = s.attributes?.friendly_name || s.entity_id;
      // eslint-disable-next-line no-alert
      if (!window.confirm(`Set ${name} to active? NibePi will then write to the heat pump through leases.`)) return;
    }
    this._hass.callService("select", "select_option", { entity_id: s.entity_id, option });
  }

  _render() {
    if (!this._config || !this._hass) return;
    const found = resolveEntities(this._hass, this._config);
    this._ids = found.ids;
    this._systems = found.systems;
    for (const id of this.watched()) this._seen.set(id, this._hass.states[id]);
    let body;
    if (found.problem && PROBLEMS[found.problem]) {
      body = `<div class="content"><div class="empty">${esc(PROBLEMS[found.problem])}</div></div>`;
    } else if (this.constructor.perSystem && found.systems.length && !found.systems.includes(this.system)) {
      body = `<div class="content"><div class="empty">Climate system ${esc(SYSTEM_LABEL(this.system))} is not configured in NibePi (${esc(
        found.systems.map(SYSTEM_LABEL).join(", "),
      )}).</div></div>`;
    } else {
      try {
        body = this.renderBody(Date.now());
      } catch (err) {
        body = `<div class="content"><div class="empty">NibePi card error: ${esc(err?.message || err)}</div></div>`;
        console.error("NibePi card", err);
      }
    }
    const note =
      found.problem === "several_entries"
        ? `<div class="content sub">Several NibePi entries: choose one with entry_id.</div>`
        : "";
    this.shadowRoot.innerHTML = `<style>${STYLES}</style><ha-card>${this.renderHeader()}${body}${note}</ha-card>`;
  }

  renderHeader(extra = "") {
    const title = this._config.title ?? this.defaultTitle();
    if (!title && !extra) return "";
    return `<div class="header"><div class="title"><ha-icon icon="${esc(this._config.icon || this.constructor.defaultIcon)}"></ha-icon><span>${esc(
      title,
    )}</span></div>${extra}</div>`;
  }

  defaultTitle() {
    return "NibePi";
  }

  renderBody() {
    return "";
  }
}

// --------------------------------------------------------------------------
// Climate
// --------------------------------------------------------------------------

class NibePiClimateCard extends NibePiCard {
  static cardType = "nibepi-climate-card";
  static defaultIcon = "mdi:home-thermometer";

  defaultTitle() {
    return `Climate ${SYSTEM_LABEL(this.system)}`;
  }

  watched() {
    const ids = this._ids || {};
    const s = this.system;
    return [
      `indoor_${s}`,
      "outdoor",
      "gateway_online",
      "away",
      "away_until",
      ...["planned_offset", "pump_offset", "optimiser_plan", "mode", "strategy", "lease_state", "house_model", "rmu_feed", "rmu_lease_state", "indoor_offset"].map(
        (k) => sysKey(k, s),
      ),
    ]
      .map((k) => ids[k])
      .filter(Boolean);
  }

  getCardSize() {
    return 6;
  }

  renderBody(now) {
    const s = this.system;
    const planned = this.st(this.sk("planned_offset"));
    const plan = this.st(this.sk("optimiser_plan"));
    const pump = this.st(this.sk("pump_offset"));
    const indoor = this.st(`indoor_${s}`);
    const outdoor = this.st("outdoor");
    const pa = planned?.attributes || {};
    const away = awayModel(this._hass, this._ids, now);
    const comfort = plan?.attributes?.comfort || {};
    const awayTarget = away?.on ? toNumber(away.targets?.[s]) : null;
    // While away: the lowered target (the optimiser's band, else the switch's).
    const target = away?.on
      ? (comfort.away ? toNumber(comfort.target) : null) ?? awayTarget ?? toNumber(this.st(this.sk("indoor_offset"))?.attributes?.setpoint)
      : toNumber(comfort.target) ?? toNumber(this.st(this.sk("indoor_offset"))?.attributes?.setpoint);
    const normalTarget = toNumber(comfort.normal_target);
    const targetText =
      target === null
        ? "–"
        : `${formatNumber(target)} °C${
            away?.on ? ` <span class="muted small">away${normalTarget !== null && normalTarget !== target ? ` · normal ${formatNumber(normalTarget)}` : ""}</span>` : ""
          }`;
    const source = pa.source || "rules";
    const sourceText = { rules: "rules", optimiser: "optimiser", comfort_floor: "comfort floor" }[source] || source;

    const temps = `<div class="row">
      ${this.stat(`indoor_${s}`, "Indoor", indoor ? `${formatNumber(num(indoor))}<span class="unit">°C</span>` : "–", "big")}
      <div class="grid" style="flex:1; min-width: 180px">
        ${
          this._ids?.[this.sk("optimiser_plan")]
            ? this.stat(this.sk("optimiser_plan"), "Target", targetText)
            : `<div class="stat"><span class="label">Target</span><span class="value">${targetText}</span></div>`
        }
        ${this.stat("outdoor", "Outdoor", outdoor ? `${formatNumber(num(outdoor))} °C` : "–")}
      </div>
    </div>`;

    const offsets = `<div class="grid">
      ${this.stat(this.sk("pump_offset"), "Pump offset", formatSigned(num(pump)))}
      ${this.stat(this.sk("planned_offset"), `Planned (${esc(sourceText)})`, formatSigned(num(planned)))}
      ${
        plan && !isMissing(plan)
          ? this.stat(this.sk("optimiser_plan"), "Optimiser now", formatSigned(num(plan)))
          : ""
      }
    </div>`;
    const reason = pa.strategy_reason ? `<div class="reason">${esc(pa.strategy_reason)}</div>` : "";

    const bars = contributions(planned, num(plan))
      .map((c) => {
        const width = Math.abs(c.frac) * 50;
        const left = c.frac < 0 ? 50 - width : 50;
        const key = ["indoor", "price", "weather"].includes(c.key)
          ? this.sk(`${c.key}_offset`)
          : c.key === "away" && this._ids?.away
            ? "away"
            : this.sk("planned_offset");
        return `<div class="bar" data-more="${esc(key)}" title="${esc(c.reason || c.label)}" role="button" tabindex="0">
          <span>${esc(c.label)}</span>
          <span class="track"><span class="mid"></span><span class="fill ${c.frac < 0 ? "neg" : "pos"}" style="left:${left}%;width:${width}%"></span></span>
          <span class="num">${formatSigned(c.value, 1)}</span></div>`;
      })
      .join("");

    const alerts = this.alertList(climateAlerts(this._hass, this._ids, s));

    return `<div class="content">
      ${alerts ? `<div class="alerts">${alerts}</div>` : ""}
      ${temps}
      <div class="divider"></div>
      ${offsets}${reason}
      ${planned ? `<div class="bars">${bars}</div>` : ""}
      ${this.awayChip(now)}
      ${this.chips(this.sk("strategy"), "Strategy")}
      ${this.chips(this.sk("mode"), "Mode")}
    </div>`;
  }
}

// --------------------------------------------------------------------------
// Optimiser plan chart
// --------------------------------------------------------------------------

class NibePiPlanCard extends NibePiCard {
  static cardType = "nibepi-plan-card";
  static defaultIcon = "mdi:chart-timeline-variant-shimmer";
  tickMs = 60000;

  static extraSchema() {
    return [{ name: "hours", selector: { number: { min: 6, max: 48, step: 1, mode: "slider" } } }];
  }

  defaultTitle() {
    return `Optimiser plan ${SYSTEM_LABEL(this.system)}`;
  }

  watched() {
    const ids = this._ids || {};
    return [ids[this.sk("optimiser_plan")], ids.price_level, ids.away].filter(Boolean);
  }

  connectedCallback() {
    super.connectedCallback();
    if (!this._resize && "ResizeObserver" in window) {
      this._resize = new ResizeObserver((entries) => {
        const w = Math.round(entries[0]?.contentRect?.width || 0);
        if (w && Math.abs(w - (this._width || 0)) > 8) {
          this._width = w;
          this._render();
        }
      });
      this._resize.observe(this);
    }
  }

  disconnectedCallback() {
    super.disconnectedCallback();
    this._resize?.disconnect();
    this._resize = undefined;
  }

  getCardSize() {
    return 7;
  }

  renderBody(now) {
    const plan = this.st(this.sk("optimiser_plan"));
    const a = plan?.attributes || {};
    const hours = toNumber(this._config.hours) || 24;
    const width = Math.max(280, Math.min(1200, (this._width || this.clientWidth || 600) - 32));
    const height = width < 420 ? 200 : 220;
    const chart = planChart({
      rows: a.plan_24h,
      levels: this.st("price_level")?.attributes?.levels,
      comfort: a.comfort,
      now,
      hours,
      width,
      height,
    });
    if (!plan || chart.empty) {
      const why = a.problem ? `: ${esc(a.problem)}` : "";
      return `<div class="content"><div class="empty">No optimiser plan for the next ${hours} h yet${why}.</div></div>`;
    }
    const p = chart.plot;
    const svg = `<svg viewBox="0 0 ${chart.width} ${chart.height}" role="img" aria-label="Optimiser plan">
      ${chart.priceBands
        .map((b) => {
          const c = PRICE_COLORS[b.level] || PRICE_COLORS.normal;
          return `<rect x="${b.x}" y="${p.y0}" width="${b.w}" height="${p.y1 - p.y0}" fill="${c.color}" fill-opacity="${c.opacity}"><title>${esc(
            b.label,
          )}</title></rect>`;
        })
        .join("")}
      ${chart.bands
        .map(
          (b) =>
            `<rect x="${b.x}" y="${b.y}" width="${b.w}" height="${b.h}" fill="var(--primary-color)" fill-opacity="0.10" stroke="var(--primary-color)" stroke-opacity="0.35" stroke-dasharray="3 3"><title>Comfort band ${formatNumber(
              b.lower,
            )}–${formatNumber(b.upper)} °C</title></rect>`,
        )
        .join("")}
      <line x1="${p.x0}" x2="${p.x1}" y1="${chart.zeroY}" y2="${chart.zeroY}" stroke="var(--divider-color)" />
      ${chart.tempTicks.map((t) => `<text x="${p.x0 - 4}" y="${t.y + 3}" text-anchor="end">${esc(t.label)}</text>`).join("")}
      ${chart.offsetTicks.map((t) => `<text x="${p.x1 + 4}" y="${t.y + 3}">${esc(t.label)}</text>`).join("")}
      ${chart.timeTicks
        .map(
          (t) =>
            `<line x1="${t.x}" x2="${t.x}" y1="${p.y0}" y2="${p.y1}" stroke="var(--divider-color)" stroke-opacity="0.6" /><text x="${t.x}" y="${p.y1 + 14}" text-anchor="middle">${esc(
              t.label,
            )}</text>`,
        )
        .join("")}
      <path d="${chart.offsetPath}" fill="none" stroke="var(--warning-color, #ffa600)" stroke-width="2" />
      ${chart.indoorPath ? `<path d="${chart.indoorPath}" fill="none" stroke="var(--primary-color)" stroke-width="2" stroke-linejoin="round" />` : ""}
      <line x1="${chart.nowX}" x2="${chart.nowX}" y1="${p.y0 - 4}" y2="${p.y1}" stroke="var(--primary-text-color)" stroke-dasharray="2 3" />
      <text x="${chart.nowX + 3}" y="${p.y0 + 2}">now</text>
      ${chart.segments
        .map((s) => `<rect x="${s.x}" y="${p.y0}" width="${s.w}" height="${p.y1 - p.y0}" fill="transparent"><title>${esc(s.label)}</title></rect>`)
        .join("")}
    </svg>`;
    const legend = `<div class="legend">
      <span><i style="background: var(--warning-color, #ffa600)"></i>Planned offset (right)</span>
      <span><i style="background: var(--primary-color)"></i>Predicted indoor °C (left)</span>
      ${chart.band ? `<span><i style="background: var(--primary-color); opacity: .3; height: 8px"></i>Comfort band</span>` : ""}
      ${chart.priceBands.length ? `<span><i style="background: var(--success-color, #43a047); opacity:.5; height: 8px"></i>Cheap</span><span><i style="background: var(--error-color, #db4437); opacity:.5; height: 8px"></i>Expensive</span>` : ""}
    </div>`;
    const currency = a.currency || "";
    const savings =
      toNumber(a.savings) !== null
        ? `${this.stat(this.sk("optimiser_plan"), "Expected savings", `${formatNumber(a.savings, 2)} ${esc(currency)}${
            toNumber(a.savings_pct) !== null ? ` <span class="muted small">(${formatNumber(a.savings_pct)} %)</span>` : ""
          }`)}`
        : "";
    const cost =
      toNumber(a.expected_cost) !== null
        ? `<div class="stat"><span class="label">Plan cost / baseline</span><span class="value">${formatNumber(a.expected_cost, 2)} / ${formatNumber(
            a.baseline_cost,
            2,
          )} ${esc(currency)}</span></div>`
        : "";
    const status = `<div class="stat"><span class="label">Planned</span><span class="value">${esc(
      a.planned_at ? formatAge(a.planned_at, now, true) : "–",
    )}${a.applied ? "" : ' <span class="muted small">(not applied)</span>'}</span></div>`;
    const explanation = Array.isArray(a.explanation) && a.explanation.length
      ? `<details><summary>Why (${a.explanation.length})</summary><ul>${a.explanation.map((l) => `<li>${esc(l)}</li>`).join("")}</ul></details>`
      : "";
    return `<div class="content">
      ${svg}${legend}
      ${a.summary ? `<div class="small">${esc(a.summary)}</div>` : ""}
      <div class="grid">${savings}${cost}${status}</div>
      ${explanation}
    </div>`;
  }
}

// --------------------------------------------------------------------------
// Hot water
// --------------------------------------------------------------------------

class NibePiHotWaterCard extends NibePiCard {
  static cardType = "nibepi-hotwater-card";
  static defaultIcon = "mdi:water-boiler";
  static perSystem = false;
  tickMs = 60000;

  defaultTitle() {
    return "Hot water";
  }

  watched() {
    const ids = this._ids || {};
    return ["bt7_hw_top", "bt6_hw_load", "hw_mode", "hw_planned_action", "hw_draw_profile", "hw_optimiser_plan", "hw_legionella_next_due", "hw_legionella_last_done", "away", "away_until", ...HW_LEASES.map((l) => l.key)]
      .map((k) => ids[k])
      .filter(Boolean);
  }

  getCardSize() {
    return 6;
  }

  renderBody(now) {
    const action = this.st("hw_planned_action");
    const aa = action?.attributes || {};
    const draw = this.st("hw_draw_profile");
    const opt = this.st("hw_optimiser_plan");
    const oa = opt?.attributes || {};
    const bt7 = this.st("bt7_hw_top");
    const bt6 = this.st("bt6_hw_load");

    const temps = `<div class="row">
      ${this.stat("bt7_hw_top", "Top (BT7)", bt7 ? `${formatNumber(num(bt7))}<span class="unit">°C</span>` : "–", "big")}
      <div class="grid" style="flex:1; min-width: 180px">
        ${this.stat("bt6_hw_load", "Charging (BT6)", bt6 ? `${formatNumber(num(bt6))} °C` : "–")}
        ${this.stat("hw_planned_action", "Planned action", esc(this.fmt(action)))}
      </div>
    </div>`;

    const reasons = aa.reasons && typeof aa.reasons === "object" ? Object.entries(aa.reasons).filter(([, v]) => v) : [];
    const reasonList = reasons.length
      ? `<details><summary>Reasons (${reasons.length})</summary><ul>${reasons
          .map(([k, v]) => `<li><span class="muted">${esc(k.replace(/_/g, " "))}:</span> ${esc(v)}</li>`)
          .join("")}</ul></details>`
      : "";

    const confidence = toNumber(draw?.attributes?.confidence);
    const drawText = isMissing(draw)
      ? "–"
      : `${esc(this.fmtTime(draw.state))}${confidence !== null ? ` <span class="muted small">(${Math.round(confidence * 100)} %)</span>` : ""}`;
    const vvNext = aa.next_heat_start ? this.fmtTime(aa.next_heat_start) : null;
    const optNext = !isMissing(opt) ? this.fmtTime(opt.state) : null;
    const usingOpt = oa.in_use || (oa.enabled && optNext);
    const nextCharge = usingOpt && optNext ? `${esc(optNext)} <span class="muted small">optimiser</span>` : vvNext ? `${esc(vvNext)} <span class="muted small">VV-AI</span>` : "–";

    const leg = legionellaModel(this.st("hw_legionella_next_due"), this.st("hw_legionella_last_done"));
    const windows = hotwaterWindows({ vvPlan: aa.plan_24h, blocks: oa.blocks, legionella: leg?.window, now, hours: 24 });
    const ticks = [];
    const hour = new Date(now);
    hour.setMinutes(0, 0, 0);
    for (let t = hour.getTime() + 3600000; t < now + 24 * 3600000 - 1800000; t += 3600000) {
      if (new Date(t).getHours() % 6 === 0) ticks.push(`<span style="left:${(((t - now) / (24 * 3600000)) * 100).toFixed(2)}%">${esc(formatClock(t))}</span>`);
    }
    const strip = `<div>
      <div class="label">Next 24 h</div>
      <div class="strip">${windows
        .map(
          (w) =>
            `<div class="seg ${w.kind}" style="left:${(w.from * 100).toFixed(2)}%;width:${((w.to - w.from) * 100).toFixed(2)}%" title="${esc(w.label)}"></div>`,
        )
        .join("")}</div>
      <div class="ticks"><span style="left:0; transform:none">now</span>${ticks.join("")}</div>
      <div class="legend"><span><i style="background: var(--info-color, var(--primary-color)); opacity:.55; height: 8px"></i>VV-AI window</span><span><i style="background: var(--warning-color, #ffa600); height: 8px"></i>Optimiser charge</span>${
        windows.some((w) => w.kind === "legionella") ? '<span><i style="background: var(--error-color, #db4437); height: 8px"></i>Legionella</span>' : ""
      }${
        windows.length ? "" : '<span>nothing planned</span>'
      }</div>
    </div>`;

    const leases = HW_LEASES.map((l) => {
      const s = this.st(l.key);
      if (!s) return "";
      return `<span data-more="${esc(l.key)}" title="${esc(`${l.label} (${l.register}): ${s.state}`)}" role="button" tabindex="0"><i class="dot" style="background:${leaseColor(
        s.state,
      )}"></i>${esc(l.register)}</span>`;
    }).join("");

    let legionella = "";
    if (leg) {
      const last = leg.lastDone !== null ? `${esc(formatAge(leg.lastDone, now, true))}${leg.lastBy ? ` <span class="muted small">${esc(leg.lastBy === "ha" ? "NibePi" : leg.lastBy)}</span>` : ""}` : "–";
      let next;
      if (leg.running) next = `<b>running</b>${leg.temperature !== null ? ` <span class="muted small">to ${esc(formatNumber(leg.temperature))} °C</span>` : ""}`;
      else if (leg.state === "planned" && leg.window) next = `${esc(this.fmtTime(leg.window.start))}–${esc(formatClock(leg.window.end))} <span class="muted small">planned</span>`;
      else if (leg.state === "off" || leg.state === "disabled") next = `<span class="muted">${esc(leg.state === "off" ? "hot water off" : "guard off")}</span>`;
      else if (leg.due !== null) next = `${esc(this.fmtTime(leg.due))}${leg.pumpPeriodic ? ' <span class="muted small">if the pump has not</span>' : ""}`;
      else next = "–";
      if (leg.state === "aborted" && leg.lastResult) next += ` <span class="muted small">${esc(leg.lastResult)}</span>`;
      legionella = `<div class="grid">
        ${this.stat("hw_legionella_last_done", `Legionella${leg.temperature !== null ? ` ${formatNumber(leg.temperature)} °C` : ""}`, last)}
        ${this.stat("hw_legionella_next_due", "Next legionella", next)}
      </div>`;
    }

    const alerts = this.alertList(hotwaterAlerts(this._hass, this._ids));
    const awayNote = aa.reasons?.away ? `<div class="reason">${esc(aa.reasons.away)}</div>` : "";

    return `<div class="content">
      ${alerts ? `<div class="alerts">${alerts}</div>` : ""}
      ${temps}
      ${awayNote}
      ${reasonList}
      <div class="grid">
        ${this.stat("hw_draw_profile", "Next draw", drawText)}
        ${this.stat(usingOpt ? "hw_optimiser_plan" : "hw_planned_action", "Next charge", nextCharge)}
      </div>
      ${legionella}
      ${strip}
      ${this.awayChip(now)}
      ${this.chips("hw_mode", "Mode")}
      ${leases ? `<div class="dots"><span class="label" style="cursor:default">Leases</span>${leases}</div>` : ""}
    </div>`;
  }
}

// --------------------------------------------------------------------------
// Gateway
// --------------------------------------------------------------------------

class NibePiGatewayCard extends NibePiCard {
  static cardType = "nibepi-gateway-card";
  static defaultIcon = "mdi:router-wireless";
  static perSystem = false;
  tickMs = 1000;

  static extraSchema() {
    return [{ name: "status_url", selector: { text: { type: "url" } } }];
  }

  static getStubConfig() {
    return {};
  }

  defaultTitle() {
    return "NibePi gateway";
  }

  getCardSize() {
    return 7;
  }

  renderHeader() {
    const url = this._config.status_url;
    const link = url && /^https?:\/\//i.test(url) ? `<a href="${esc(url)}" target="_blank" rel="noopener noreferrer" class="small">Status page</a>` : "";
    return super.renderHeader(link);
  }

  renderBody(now) {
    const gw = this.st("gateway_online");
    const ga = gw?.attributes || {};
    const online = gw?.state === "on" ? "Online" : gw?.state === "off" ? "Offline" : "Unknown";
    const color = gw?.state === "on" ? "var(--success-color, #43a047)" : gw?.state === "off" ? "var(--error-color, #db4437)" : "var(--disabled-color, #bdbdbd)";
    const bus = this.st("bus_health");
    const status = `<div class="grid">
      ${this.stat("gateway_online", "Gateway", `<i class="dot" style="background:${color}"></i> ${online}${ga.version ? ` <span class="muted small">${esc(ga.version)}</span>` : ""}`)}
      <div class="stat"><span class="label">Core</span><span class="value">${esc(ga.core ?? "–")}</span></div>
      <div class="stat"><span class="label">Last data</span><span class="value">${esc(formatAge(ga.last_data, now) || "–")}</span></div>
      <div class="stat"><span class="label">Raw writes</span><span class="value">${esc(Array.isArray(ga.raw_set) ? ga.raw_set.join(", ") : ga.raw_set ?? "–")}</span></div>
      ${bus ? this.stat("bus_health", "Bus errors", esc(this.fmt(bus))) : ""}
      <div class="stat"><span class="label">Last message</span><span class="value">${esc(formatAge(ga.last_message_at, now) || "–")}</span></div>
    </div>`;
    const hint = ga.hint ? `<div class="alert warning" data-more="gateway_online"><ha-icon icon="mdi:alert"></ha-icon><span>${esc(ga.hint)}</span></div>` : "";

    const rows = leaseRows(this._hass, this._ids, now, LEASES.filter((l) => !/_s[12]$/.test(l.key) || this._systems.some((s) => l.key.endsWith(s))));
    const leases = rows.length
      ? `<div class="leases"><div class="label">Leases</div>${rows
          .map(
            (r) => `<div class="lease" data-more="${esc(r.key)}" role="button" tabindex="0" title="${esc(`${r.label} (${r.register})`)}">
              <i class="dot" style="background:${leaseColor(r.state)}"></i>
              <span>${esc(r.label)} <span class="muted small">${esc(r.register)}</span></span>
              <span class="meta">${esc(this.fmt(this.st(r.key)))}${r.value !== null && r.state !== "inactive" ? ` · value ${esc(r.value)}` : ""}${
                r.countdown ? ` · ${r.countdown.expired ? "expired" : `${esc(r.countdown.text)} left`}` : ""
              }</span>
              ${r.error ? `<span class="err">${esc(r.error)}</span>` : ""}
            </div>`,
          )
          .join("")}</div>`
      : "";

    const rmu = this._systems
      .map((s) => {
        const feed = this.st(sysKey("rmu_feed", s));
        if (!feed) return "";
        const lease = this.st(sysKey("rmu_lease_state", s));
        const room = toNumber(lease?.attributes?.room_temperature);
        const text = feed.state === "on" ? `on${room !== null ? ` · ${formatNumber(room)} °C` : ""}` : "off";
        return this.stat(sysKey("rmu_feed", s), `RMU40 feed ${SYSTEM_LABEL(s)}`, esc(text));
      })
      .join("");

    return `<div class="content">
      ${hint}${status}
      ${leases ? `<div class="divider"></div>${leases}` : ""}
      ${rmu ? `<div class="grid">${rmu}</div>` : ""}
    </div>`;
  }
}

// --------------------------------------------------------------------------
// Savings
// --------------------------------------------------------------------------

const STATS_REFRESH_MS = 30 * 60000;

class NibePiSavingsCard extends NibePiCard {
  static cardType = "nibepi-savings-card";
  static defaultIcon = "mdi:piggy-bank-outline";
  static perSystem = false;

  static getStubConfig() {
    return {};
  }

  defaultTitle() {
    return "Heat pump cost";
  }

  watched() {
    const ids = this._ids || {};
    return [ids.heat_pump_cost_today, ids.optimiser_savings_today].filter(Boolean);
  }

  getCardSize() {
    return 4;
  }

  _maybeFetchStats(now) {
    const ids = [this._ids?.optimiser_savings_today, this._ids?.heat_pump_cost_today].filter(Boolean);
    if (!ids.length || !this._hass?.callWS) return;
    const key = ids.join(",");
    if (this._statsKey === key && now - (this._statsAt || 0) < STATS_REFRESH_MS) return;
    this._statsKey = key;
    this._statsAt = now;
    const start = new Date(now);
    start.setHours(0, 0, 0, 0);
    start.setDate(start.getDate() - 7);
    this._hass
      .callWS({
        type: "recorder/statistics_during_period",
        start_time: start.toISOString(),
        statistic_ids: ids,
        period: "day",
        types: ["change"],
      })
      .then((result) => {
        this._stats = result;
        this._render();
      })
      .catch(() => {
        // No long-term statistics (recorder off, too new): no sparkline.
        this._stats = null;
      });
  }

  _history(key, now) {
    const id = this._ids?.[key];
    if (!id || !this._stats) return [];
    const today = new Date(now);
    today.setHours(0, 0, 0, 0);
    return dailyValues(this._stats, id).filter((v) => v.start < today.getTime()).slice(-7);
  }

  renderBody(now) {
    this._maybeFetchStats(now);
    const cost = this.st("heat_pump_cost_today");
    const savings = this.st("optimiser_savings_today");
    if (!cost && !savings) {
      return `<div class="content"><div class="empty">No cost or savings sensors found.</div></div>`;
    }
    const m = savingsModel(cost, savings);
    const cur = esc(m.currency);
    const money = (v) => (v === null ? "–" : `${formatNumber(v, 2)} ${cur}`);
    const bigMoney = (v) => (v === null ? "–" : `${formatNumber(v, 2)}<span class="unit">${cur}</span>`);
    const gap = (pct) => (pct === null ? "" : ` <span class="muted small">(${formatSigned(pct, 1)} % vs plan)</span>`);

    const costHist = this._history("heat_pump_cost_today", now);
    const saveHist = this._history("optimiser_savings_today", now);
    const spark = (hist, label, stroke) => {
      const d = sparkline(hist.map((h) => h.value), 120, 32);
      if (!d) return "";
      const total = hist.reduce((acc, h) => acc + h.value, 0);
      return `<div class="stat"><span class="label">${esc(label)}, ${hist.length} days</span>
        <svg viewBox="0 0 120 32" style="max-width:160px" preserveAspectRatio="none"><path d="${d}" fill="none" stroke="${stroke}" stroke-width="1.5" vector-effect="non-scaling-stroke"><title>${esc(
          hist.map((h) => `${new Date(h.start).toLocaleDateString()}: ${formatNumber(h.value, 2)}`).join("\n"),
        )}</title></path></svg>
        <span class="muted small">total ${formatNumber(total, 2)} ${cur}</span></div>`;
    };
    const savedYesterday = saveHist.length ? saveHist[saveHist.length - 1] : null;
    const yesterdayDate = new Date(now);
    yesterdayDate.setHours(0, 0, 0, 0);
    yesterdayDate.setDate(yesterdayDate.getDate() - 1);
    const isYesterday = savedYesterday && savedYesterday.start === yesterdayDate.getTime();

    const today = `<div class="grid">
      ${cost ? this.stat("heat_pump_cost_today", "Cost today", bigMoney(m.costToday), "big") : ""}
      ${savings ? this.stat("optimiser_savings_today", "Expected savings today", bigMoney(m.savingsToday), "big") : ""}
    </div>`;
    const detail = `<div class="grid">
      ${cost ? `<div class="stat"><span class="label">Predicted so far</span><span class="value">${money(m.predictedCost)}${gap(m.gapPct)}</span></div>` : ""}
      ${
        m.yesterday
          ? `<div class="stat"><span class="label">Cost yesterday</span><span class="value">${money(m.yesterday.cost)}${gap(m.yesterday.gapPct)}</span></div>`
          : ""
      }
      ${isYesterday ? `<div class="stat"><span class="label">Saved yesterday</span><span class="value">${money(savedYesterday.value)}</span></div>` : ""}
    </div>`;
    const parts = m.savingsParts.length
      ? `<div class="sub">Savings today: ${m.savingsParts.map((p) => `${esc(p.label)} ${formatNumber(p.value, 2)}`).join(" · ")} ${cur}</div>`
      : "";
    const sparks = `${spark(costHist, "Cost per day", "var(--warning-color, #ffa600)")}${spark(saveHist, "Savings per day", "var(--success-color, #43a047)")}`;
    const costNote = cost && m.costToday === null ? `<div class="sub">Cost needs a heat pump meter (meter scope: heat pump).</div>` : "";
    return `<div class="content">${today}${detail}${parts}${sparks ? `<div class="grid">${sparks}</div>` : ""}${costNote}</div>`;
  }
}

// --------------------------------------------------------------------------
// Editor (ha-form)
// --------------------------------------------------------------------------

const LABELS = {
  system: "Climate system",
  entry_id: "NibePi entry (only with several)",
  title: "Title",
  hours: "Hours ahead",
  status_url: "Gateway status page URL (e.g. http://nibepi.local:8080)",
};

async function ensureHaForm() {
  if (customElements.get("ha-form")) return;
  // ha-form is lazy-loaded by the frontend; creating a built-in card's
  // editor loads it.
  try {
    const helpers = await window.loadCardHelpers?.();
    const card = await helpers?.createCardElement({ type: "entities", entities: [] });
    await card?.constructor?.getConfigElement?.();
  } catch (_err) {
    // the editor falls back to YAML when ha-form never appears
  }
}

async function createEditor(cardClass) {
  await ensureHaForm();
  const editor = document.createElement("nibepi-card-editor");
  const schema = [];
  if (cardClass.perSystem) {
    schema.push({
      name: "system",
      selector: {
        select: {
          mode: "dropdown",
          options: [
            { value: "s1", label: "S1" },
            { value: "s2", label: "S2" },
          ],
        },
      },
    });
  }
  schema.push({ name: "entry_id", selector: { config_entry: { integration: "nibepi" } } });
  schema.push({ name: "title", selector: { text: {} } });
  schema.push(...cardClass.extraSchema());
  editor.schema = schema;
  return editor;
}

class NibePiCardEditor extends HTMLElement {
  setConfig(config) {
    this._config = { ...config };
    this._update();
  }

  set hass(hass) {
    this._hass = hass;
    this._update();
  }

  _update() {
    if (!this._hass || !this._config) return;
    if (!this._form) {
      this._form = document.createElement("ha-form");
      this._form.computeLabel = (s) => LABELS[s.name] || s.name;
      this._form.addEventListener("value-changed", (ev) => {
        ev.stopPropagation();
        const config = { ...ev.detail.value };
        for (const [k, v] of Object.entries(config)) if (v === "" || v === undefined || v === null) delete config[k];
        this._config = config;
        this.dispatchEvent(new CustomEvent("config-changed", { detail: { config }, bubbles: true, composed: true }));
      });
      this.appendChild(this._form);
    }
    this._form.hass = this._hass;
    this._form.schema = this.schema || [];
    this._form.data = this._config;
  }
}

// --------------------------------------------------------------------------
// Registration
// --------------------------------------------------------------------------

const CARDS = [
  [NibePiClimateCard, "NibePi climate", "Indoor vs target, outdoor, the planned and pump curve offset with its parts, the Away toggle, strategy and mode, and alerts for one climate system."],
  [NibePiPlanCard, "NibePi optimiser plan", "Chart of the optimiser's planned offset and predicted indoor temperature with the comfort band and price levels, plus the expected savings."],
  [NibePiHotWaterCard, "NibePi hot water", "BT7/BT6, the planned action and its reasons, next draw and charge, the legionella guard, the next 24 h of planned charges, the Away toggle, hot water mode and leases."],
  [NibePiGatewayCard, "NibePi gateway", "Gateway status, last data, raw writes, bus errors, every lease with its countdown, and the RMU40 feeds."],
  [NibePiSavingsCard, "NibePi cost and savings", "Heat pump cost and expected savings today, against the plan and yesterday, with 7-day sparklines."],
];

if (!customElements.get("nibepi-card-editor")) customElements.define("nibepi-card-editor", NibePiCardEditor);
window.customCards = window.customCards || [];
for (const [cls, name, description] of CARDS) {
  if (!customElements.get(cls.cardType)) customElements.define(cls.cardType, cls);
  if (!window.customCards.some((c) => c.type === cls.cardType)) {
    window.customCards.push({ type: cls.cardType, name, description, preview: true, documentationURL: DOCS });
  }
}

console.info(`%c NibePi cards %c ${VERSION} `, "background:#0b6b3a;color:#fff;border-radius:3px 0 0 3px", "background:#555;color:#fff;border-radius:0 3px 3px 0");
