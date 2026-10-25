// eufy Clean (Local) dashboard card: the live map with tappable rooms, the
// robot's controls, this clean's settings and its consumables, in one card.
//
//   type: custom:eufy-clean-card
//   entity: vacuum.l60
//
// The card finds the robot's other entities (map, battery, passes, ...) through
// the entity registry, by device and translation key, so the vacuum is all it
// needs. The map is the image entity, loaded as ordinary pictures, which works
// in every browser (Safari can't show a camera stream through Home Assistant's
// service worker).

const DOMAIN = "eufy_clean_local";
const VERSION = "0.4.1";

const ACTIVE = new Set(["cleaning", "returning"]);

const esc = (text) =>
  String(text ?? "").replace(/[&<>"']/g, (ch) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[ch]);

const STYLE = `
  :host { display: block; }
  ha-card { overflow: hidden; }
  .head { display: flex; align-items: center; gap: 12px; padding: 12px 16px 8px; cursor: pointer; }
  .head .text { flex: 1; min-width: 0; }
  .name { font-size: 18px; font-weight: 500; color: var(--primary-text-color); }
  .status { font-size: 13px; color: var(--secondary-text-color); white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
  .battery { display: flex; align-items: center; gap: 4px; font-size: 14px; color: var(--secondary-text-color); }
  .banner { margin: 0 16px 8px; padding: 8px 12px; border-radius: 8px; display: flex; gap: 8px; align-items: center;
            font-size: 13px; background: rgba(var(--rgb-error-color, 219, 68, 55), 0.12); color: var(--error-color, #db4437); }
  .banner.warning { background: rgba(var(--rgb-warning-color, 255, 166, 0), 0.15); color: var(--warning-color, #ffa600); }
  .tabs { display: flex; gap: 4px; margin: 0 16px 8px; overflow-x: auto; }
  .tab { border: none; background: none; color: var(--secondary-text-color); font: inherit; font-size: 13px;
         padding: 6px 12px; border-radius: 16px; cursor: pointer; white-space: nowrap; }
  .tab.on { background: rgba(var(--rgb-primary-color, 3, 169, 244), 0.15); color: var(--primary-color); }
  .tab .here { display: inline-block; width: 6px; height: 6px; border-radius: 50%; margin-left: 6px;
               background: var(--primary-color); vertical-align: middle; }
  .note { font-size: 12px; color: var(--secondary-text-color); margin-bottom: 6px; }
  .map { position: relative; margin: 0 16px; border-radius: 12px; overflow: hidden;
         background: var(--secondary-background-color); min-height: 80px; }
  .map img { display: block; width: 100%; height: auto; }
  .map svg { position: absolute; inset: 0; width: 100%; height: 100%; }
  .map .empty { padding: 32px 16px; text-align: center; font-size: 13px; color: var(--secondary-text-color); }
  polygon.room { fill: transparent; stroke: none; cursor: pointer; pointer-events: all; }
  polygon.room:hover { fill: rgba(var(--rgb-primary-color, 3, 169, 244), 0.12); }
  polygon.room.sel { fill: rgba(var(--rgb-primary-color, 3, 169, 244), 0.32); stroke: var(--primary-color);
                     stroke-width: 2; vector-effect: non-scaling-stroke; }
  .section { padding: 10px 16px 0; }
  .label { font-size: 12px; color: var(--secondary-text-color); margin-bottom: 6px; }
  .chips { display: flex; flex-wrap: wrap; gap: 6px; }
  .chip { border: 1px solid var(--divider-color); background: none; color: var(--primary-text-color);
          border-radius: 16px; padding: 5px 12px; font: inherit; font-size: 13px; cursor: pointer; }
  .chip:hover { background: rgba(var(--rgb-primary-text-color, 0, 0, 0), 0.05); }
  .chip.on { background: rgba(var(--rgb-primary-color, 3, 169, 244), 0.15); border-color: var(--primary-color);
             color: var(--primary-color); }
  .chip[disabled] { opacity: 0.5; cursor: default; }
  .seg { display: inline-flex; border: 1px solid var(--divider-color); border-radius: 16px; overflow: hidden; }
  .seg button { border: none; background: none; color: var(--primary-text-color); font: inherit; font-size: 13px;
                padding: 5px 12px; cursor: pointer; }
  .seg button + button { border-left: 1px solid var(--divider-color); }
  .seg button.on { background: rgba(var(--rgb-primary-color, 3, 169, 244), 0.15); color: var(--primary-color); }
  .row { display: flex; flex-wrap: wrap; gap: 12px 16px; align-items: flex-end; }
  .controls { display: flex; gap: 8px; padding: 12px 16px 0; }
  .btn { flex: 1; display: flex; align-items: center; justify-content: center; gap: 6px; min-height: 40px;
         border: 1px solid var(--divider-color); border-radius: 10px; background: none; color: var(--primary-text-color);
         font: inherit; font-size: 14px; cursor: pointer; }
  .btn:hover { background: rgba(var(--rgb-primary-text-color, 0, 0, 0), 0.05); }
  .btn.primary { background: var(--primary-color); border-color: var(--primary-color); color: var(--text-primary-color, #fff); }
  .btn.primary:hover { filter: brightness(1.05); }
  .wear { display: grid; grid-template-columns: repeat(auto-fit, minmax(120px, 1fr)); gap: 10px 16px; }
  .wear .item { cursor: pointer; }
  .wear .top { display: flex; justify-content: space-between; font-size: 12px; color: var(--secondary-text-color); }
  .bar { height: 6px; border-radius: 3px; background: var(--divider-color); margin-top: 4px; overflow: hidden; }
  .bar > div { height: 100%; border-radius: 3px; background: var(--primary-color); }
  .bar > div.low { background: var(--warning-color, #ffa600); }
  .bar > div.out { background: var(--error-color, #db4437); }
  .foot { padding: 12px 16px 14px; font-size: 12px; color: var(--secondary-text-color); display: flex; gap: 16px; flex-wrap: wrap; }
  .error { color: var(--error-color, #db4437); }
  ha-icon { --mdc-icon-size: 20px; }
`;

class EufyCleanCard extends HTMLElement {
  static getStubConfig(hass) {
    const vac = Object.values(hass?.entities || {}).find(
      (e) => e.platform === DOMAIN && e.entity_id.startsWith("vacuum.")
    );
    return { entity: vac ? vac.entity_id : "vacuum.l60" };
  }

  constructor() {
    super();
    this._selected = new Set();
    this._floor = null; // the floor tab shown (map id); null follows the robot
    this._signature = "";
    this._imageUrl = "";
    this._message = "";
  }

  setConfig(config) {
    if (!config?.entity?.startsWith("vacuum.")) {
      throw new Error("Set entity to an eufy Clean (Local) vacuum, for example vacuum.l60");
    }
    this._config = config;
    this._signature = "";
    if (this._hass) this._update();
  }

  set hass(hass) {
    this._hass = hass;
    this._update();
  }

  getCardSize() {
    return 10;
  }

  getGridOptions() {
    return { columns: 12, min_columns: 6, rows: "auto" };
  }

  // ------------------------------------------------------------- entities

  _entities() {
    const reg = this._hass.entities || {};
    if (reg === this._reg && this._ids) return this._ids;
    this._reg = reg;
    const device = reg[this._config.entity]?.device_id;
    const ids = {};
    this._floorImages = [];
    for (const e of Object.values(reg)) {
      if (device && e.device_id === device && e.platform === DOMAIN && e.translation_key) {
        if (e.translation_key === "floor_map") this._floorImages.push(e.entity_id);
        else ids[`${e.entity_id.split(".")[0]}.${e.translation_key}`] = e.entity_id;
      }
    }
    this._ids = ids;
    return ids;
  }

  // Floors (multi-floor maps): the robot's floor is shown live from the map image; the
  // others from their own pictures, with no room tapping (the robot can't clean them).
  _floors() {
    return this._state("image.map")?.attributes.floors || [];
  }

  _view() {
    const main = this._state("image.map");
    const floors = this._floors();
    const active = floors.find((f) => f.active);
    if (this._floor === null || floors.length < 2 || (active && active.id === this._floor)) {
      return { image: main, live: true, floor: active };
    }
    const image = (this._floorImages || [])
      .map((id) => this._hass.states[id])
      .find((s) => s && s.attributes.map_id === this._floor);
    return image ? { image, live: false, floor: floors.find((f) => f.id === this._floor), active }
      : { image: main, live: true, floor: active };
  }

  _state(key) {
    const id = key.includes(".") && this._entities()[key];
    return id ? this._hass.states[id] : undefined;
  }

  _format(stateObj) {
    if (!stateObj) return "";
    try {
      return this._hass.formatEntityState(stateObj);
    } catch (_err) {
      const unit = stateObj.attributes.unit_of_measurement;
      return unit ? `${stateObj.state} ${unit}` : stateObj.state;
    }
  }

  // ------------------------------------------------------------- render

  _update() {
    if (!this._config || !this._hass) return;
    if (!this.shadowRoot) {
      this.attachShadow({ mode: "open" });
      this.shadowRoot.innerHTML = `<style>${STYLE}</style><ha-card>
        <div id="top"></div>
        <div class="map" id="map"><img alt="" /><svg viewBox="0 0 1000 1000" preserveAspectRatio="none"></svg></div>
        <div id="body"></div></ha-card>`;
      this.shadowRoot.addEventListener("click", (ev) => this._click(ev));
      this._img = this.shadowRoot.querySelector("#map img");
      this._svg = this.shadowRoot.querySelector("#map svg");
    }
    const keys = [this._config.entity, ...Object.values(this._entities()), ...(this._floorImages || [])];
    const signature =
      keys.map((id) => {
        const s = this._hass.states[id];
        return s ? `${id}=${s.state}@${s.last_updated}` : id;
      }).join("|") + `|${[...this._selected].join(",")}|${this._floor}|${this._message}`;
    if (signature === this._signature) return;
    this._signature = signature;
    this._render();
  }

  _render() {
    const vac = this._hass.states[this._config.entity];
    const top = this.shadowRoot.getElementById("top");
    const body = this.shadowRoot.getElementById("body");
    if (!vac) {
      top.innerHTML = `<div class="head"><div class="text"><div class="name">${esc(this._config.entity)}</div>
        <div class="status">Not found</div></div></div>`;
      body.innerHTML = "";
      this.shadowRoot.getElementById("map").style.display = "none";
      return;
    }
    top.innerHTML = this._renderHead(vac) + this._renderBanner() + this._renderTabs();
    this._renderMap();
    body.innerHTML = [
      this._renderRooms(),
      this._renderControls(vac),
      this._renderSettings(vac),
      this._renderWear(),
      this._renderFoot(vac),
    ].join("");
  }

  _renderHead(vac) {
    const battery = this._state("sensor.battery");
    const level = Number(battery?.state);
    const charging = vac.state === "docked" && /charg/i.test(vac.attributes.status || "");
    const step = Number.isFinite(level) ? Math.max(0, Math.min(100, Math.round(level / 10) * 10)) : null;
    const icon = step === null ? "mdi:battery-unknown"
      : charging ? `mdi:battery-charging-${Math.max(step, 10)}`
      : step === 100 ? "mdi:battery" : step === 0 ? "mdi:battery-outline" : `mdi:battery-${step}`;
    const name = this._config.title || vac.attributes.friendly_name || this._config.entity;
    let status = vac.attributes.status || this._format(vac);
    if (this._floors().length > 1 && vac.attributes.floor) status += ` · ${vac.attributes.floor}`;
    return `<div class="head" data-action="more-info" data-entity="${esc(this._config.entity)}">
      <div class="text"><div class="name">${esc(name)}</div><div class="status">${esc(status)}</div></div>
      ${Number.isFinite(level) ? `<div class="battery"><ha-icon icon="${icon}"></ha-icon>${Math.round(level)}%</div>` : ""}
    </div>`;
  }

  _renderBanner() {
    const problem = this._state("binary_sensor.problem");
    if (!problem || problem.state !== "on") return "";
    const { error, warning } = problem.attributes;
    const hasError = error && error !== "No error";
    const text = hasError ? error : warning;
    if (!text || text === "No warning") return "";
    return `<div class="banner ${hasError ? "" : "warning"}"><ha-icon icon="mdi:alert-circle-outline"></ha-icon>${esc(text)}</div>`;
  }

  _renderTabs() {
    const floors = this._floors();
    if (floors.length < 2) return "";
    const shown = this._view().floor;
    return `<div class="tabs">${floors
      .map((f) => `<button class="tab ${shown && shown.id === f.id ? "on" : ""}" data-action="floor" data-floor="${f.id}"
        title="${f.active ? "The robot is on this floor" : ""}">${esc(f.name)}${f.active ? '<span class="here"></span>' : ""}</button>`)
      .join("")}</div>`;
  }

  _renderMap() {
    const map = this.shadowRoot.getElementById("map");
    const view = this._view();
    const image = view.image;
    if (!image) {
      map.style.display = "none";
      return;
    }
    map.style.display = "";
    const token = image.attributes.access_token;
    if (image.state === "unavailable" || !token) {
      if (!map.querySelector(".empty")) {
        map.insertAdjacentHTML("afterbegin", `<div class="empty">The map appears after the robot's first clean.</div>`);
      }
      return;
    }
    map.querySelector(".empty")?.remove();
    const url = `/api/image_proxy/${image.entity_id}?token=${token}&state=${encodeURIComponent(image.state)}`;
    if (url !== this._imageUrl) {
      this._imageUrl = url;
      // Load first, then swap, so the map never blinks between frames.
      const next = new Image();
      next.onload = () => {
        if (this._imageUrl === url) this._img.src = url;
      };
      next.src = url;
    }
    const shapes = view.live ? image.attributes.room_shapes || [] : [];
    this._svg.innerHTML = shapes
      .map((room) => {
        const points = room.points.map(([x, y]) => `${Math.round(x * 1000)},${Math.round(y * 1000)}`).join(" ");
        const on = this._selected.has(room.id) ? " sel" : "";
        return `<polygon class="room${on}" data-action="room" data-room="${room.id}" points="${points}"><title>${esc(room.name)}</title></polygon>`;
      })
      .join("");
  }

  _rooms() {
    const shapes = this._view().image?.attributes.room_shapes;
    if (shapes?.length) return shapes.map((r) => ({ id: r.id, name: r.name }));
    const vac = this._hass.states[this._config.entity];
    return (vac?.attributes.rooms || []).map((r) => ({ id: r.id, name: r.name }));
  }

  _renderRooms() {
    const rooms = this._rooms();
    if (!rooms.length) return "";
    const view = this._view();
    if (!view.live) {
      const where = view.active ? view.active.name : "another floor";
      const chips = rooms.map((r) => `<button class="chip" disabled>${esc(r.name)}</button>`).join("");
      return `<div class="section"><div class="note">The robot is on ${esc(where)}. Rooms on ${esc(view.floor?.name || "this floor")}
        can be cleaned once it's there.</div><div class="chips">${chips}</div></div>`;
    }
    const chips = rooms
      .map((r) => `<button class="chip ${this._selected.has(r.id) ? "on" : ""}" data-action="room" data-room="${r.id}">${esc(r.name)}</button>`)
      .join("");
    const hint = this._selected.size ? "Rooms to clean" : "Tap rooms on the map or here to clean just those";
    return `<div class="section"><div class="label">${hint}</div><div class="chips">${chips}</div></div>`;
  }

  _renderControls(vac) {
    const state = vac.state;
    const buttons = [];
    if (this._selected.size) {
      const n = this._selected.size;
      buttons.push(`<button class="btn primary" data-action="clean-rooms"><ha-icon icon="mdi:play"></ha-icon>Clean ${n} room${n > 1 ? "s" : ""}</button>`);
      buttons.push(`<button class="btn" data-action="clear"><ha-icon icon="mdi:close"></ha-icon>Clear</button>`);
    } else if (ACTIVE.has(state)) {
      buttons.push(`<button class="btn" data-action="pause"><ha-icon icon="mdi:pause"></ha-icon>Pause</button>`);
      buttons.push(`<button class="btn" data-action="stop"><ha-icon icon="mdi:stop"></ha-icon>Stop</button>`);
      if (state !== "returning") {
        buttons.push(`<button class="btn" data-action="dock"><ha-icon icon="mdi:home-import-outline"></ha-icon>Dock</button>`);
      }
    } else if (state === "paused") {
      buttons.push(`<button class="btn primary" data-action="start"><ha-icon icon="mdi:play"></ha-icon>Resume</button>`);
      buttons.push(`<button class="btn" data-action="stop"><ha-icon icon="mdi:stop"></ha-icon>Stop</button>`);
      buttons.push(`<button class="btn" data-action="dock"><ha-icon icon="mdi:home-import-outline"></ha-icon>Dock</button>`);
    } else {
      buttons.push(`<button class="btn primary" data-action="start"><ha-icon icon="mdi:play"></ha-icon>Clean everywhere</button>`);
      if (state !== "docked") {
        buttons.push(`<button class="btn" data-action="dock"><ha-icon icon="mdi:home-import-outline"></ha-icon>Dock</button>`);
      }
      buttons.push(`<button class="btn" data-action="locate" title="Locate"><ha-icon icon="mdi:map-marker-question-outline"></ha-icon>Find</button>`);
    }
    return `<div class="controls">${buttons.join("")}</div>`;
  }

  _renderSettings(vac) {
    const parts = [];
    const speeds = vac.attributes.fan_speed_list || [];
    if (speeds.length) {
      const current = vac.attributes.fan_speed;
      parts.push(`<div><div class="label">Suction</div><div class="seg">${speeds
        .map((s) => `<button class="${s === current ? "on" : ""}" data-action="fan" data-value="${esc(s)}">${esc(s)}</button>`)
        .join("")}</div></div>`);
    }
    const passes = this._state("select.clean_times");
    if (passes && passes.attributes.options) {
      parts.push(`<div><div class="label">Passes</div><div class="seg">${passes.attributes.options
        .map((o) => `<button class="${o === passes.state ? "on" : ""}" data-action="option" data-entity="${passes.entity_id}" data-value="${esc(o)}">${esc(o)}</button>`)
        .join("")}</div></div>`);
    }
    const chips = [];
    const boost = this._state("switch.boost_iq");
    if (boost) {
      chips.push(`<button class="chip ${boost.state === "on" ? "on" : ""}" data-action="toggle" data-entity="${boost.entity_id}">BoostIQ</button>`);
    }
    const empty = this._state("button.empty_dust");
    if (empty) {
      const off = empty.state === "unavailable" ? "disabled" : "";
      chips.push(`<button class="chip" ${off} data-action="press" data-entity="${empty.entity_id}" title="${off ? "Only while docked" : ""}">Empty dust</button>`);
    }
    if (chips.length) parts.push(`<div class="chips">${chips.join("")}</div>`);
    return parts.length ? `<div class="section"><div class="row">${parts.join("")}</div></div>` : "";
  }

  _renderWear() {
    const items = [
      ["sensor.side_brush_left", "Side brush"],
      ["sensor.rolling_brush_left", "Rolling brush"],
      ["sensor.filter_mesh_left", "Filter"],
      ["sensor.dustbag_left", "Dust bag"],
    ]
      .map(([key, name]) => [this._state(key), name])
      .filter(([s]) => s && Number.isFinite(Number(s.state)))
      .map(([s, name]) => {
        const pct = Math.max(0, Math.min(100, Math.round(Number(s.state))));
        const cls = pct <= 10 ? "out" : pct <= 20 ? "low" : "";
        return `<div class="item" data-action="more-info" data-entity="${s.entity_id}">
          <div class="top"><span>${esc(name)}</span><span>${pct}%</span></div>
          <div class="bar"><div class="${cls}" style="width:${pct}%"></div></div></div>`;
      });
    return items.length ? `<div class="section"><div class="wear">${items.join("")}</div></div>` : "";
  }

  _renderFoot(vac) {
    const area = this._state("sensor.clean_area");
    const minutes = this._state("sensor.clean_minutes");
    const bits = [];
    const which = ACTIVE.has(vac.state) || vac.state === "paused" ? "This clean" : "Last clean";
    if (minutes && minutes.state !== "unknown" && minutes.state !== "unavailable") bits.push(`${which} ${esc(this._format(minutes))}`);
    if (area && area.state !== "unknown" && area.state !== "unavailable") bits.push(esc(this._format(area)));
    if (this._message) bits.push(`<span class="error">${esc(this._message)}</span>`);
    return `<div class="foot">${bits.join("<span>·</span>")}</div>`;
  }

  // ------------------------------------------------------------- actions

  _click(ev) {
    const el = ev.composedPath().find((n) => n instanceof Element && n.dataset && n.dataset.action);
    if (!el) return;
    const { action, entity, value, room } = el.dataset;
    const vacuum = { entity_id: this._config.entity };
    switch (action) {
      case "more-info":
        this.dispatchEvent(new CustomEvent("hass-more-info", { detail: { entityId: entity }, bubbles: true, composed: true }));
        return;
      case "floor":
        this._floor = Number(el.dataset.floor);
        this._selected.clear();
        this._signature = "";
        this._update();
        return;
      case "room": {
        if (!this._view().live) return;
        const id = Number(room);
        if (this._selected.has(id)) this._selected.delete(id);
        else this._selected.add(id);
        this._signature = "";
        this._update();
        return;
      }
      case "clear":
        this._selected.clear();
        this._signature = "";
        this._update();
        return;
      case "clean-rooms": {
        const passes = Number(this._state("select.clean_times")?.state) || 1;
        const rooms = [...this._selected];
        this._selected.clear();
        this._call("vacuum", "send_command", { command: "room_clean", params: { rooms, clean_times: passes } }, vacuum);
        return;
      }
      case "start":
      case "pause":
      case "stop":
      case "locate":
        this._call("vacuum", action, {}, vacuum);
        return;
      case "dock":
        this._call("vacuum", "return_to_base", {}, vacuum);
        return;
      case "fan":
        this._call("vacuum", "set_fan_speed", { fan_speed: value }, vacuum);
        return;
      case "option":
        this._call("select", "select_option", { option: value }, { entity_id: entity });
        return;
      case "toggle":
        this._call("switch", "toggle", {}, { entity_id: entity });
        return;
      case "press":
        this._call("button", "press", {}, { entity_id: entity });
        return;
    }
  }

  async _call(domain, service, data, target) {
    this._message = "";
    this._signature = "";
    this._update();
    try {
      await this._hass.callService(domain, service, data, target);
    } catch (err) {
      this._message = err?.message || String(err);
      this._signature = "";
      this._update();
      setTimeout(() => {
        this._message = "";
        this._signature = "";
        this._update();
      }, 8000);
    }
  }
}

const register = () => {
  // window.customElements, read now: Home Assistant replaces it when its app starts.
  if (window.customElements.get("eufy-clean-card")) return;
  window.customElements.define("eufy-clean-card", EufyCleanCard);
  window.customCards = window.customCards || [];
  window.customCards.push({
    type: "eufy-clean-card",
    name: "eufy Clean",
    description: "Live map with tappable rooms, controls, settings and consumables for eufy Clean (Local) robots.",
    documentationURL: "https://github.com/kkrankall/eufy-clean-local",
  });
  console.info(`%c eufy-clean-card ${VERSION} `, "background:#005d8e;color:#fff;border-radius:3px");
};

// Home Assistant loads this file from its start page, which can run it before the
// app has swapped in its scoped custom element registry. A card defined in the
// browser's own registry before that swap is invisible to dashboards ("Custom
// element doesn't exist"). The app defines <home-assistant> right after the swap,
// so wait for that (10 s at most, for pages without the app).
const whenAppStarted = (done, waited = 0) => {
  if (window.customElements.get("home-assistant") || waited >= 10000) done();
  else setTimeout(() => whenAppStarted(done, waited + 50), 50);
};
whenAppStarted(register);
