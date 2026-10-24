import { decodeFragment } from "./codec.js";
import { validate } from "./validate.js";

// Built next to this page by scripts/build_site.py from schema/ and data/regions/.
const SCHEMA_URL = "config.schema.json";
const REGIONS_URL = "regions.json";

const WEEKDAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"];
const COMMANDS = ["metar", "taf", "brief", "winds", "pirep", "mountain", "alerts"];
const BRIEF_PARTS = {
  tafs: "TAFs",
  winds_aloft: "Winds aloft",
  airmets_sigmets: "AIRMETs & SIGMETs",
  pireps: "PIREPs",
  nws_alerts: "NWS alerts",
  afd_aviation: "Forecast discussion (aviation section)",
  mountain: "Mountain flying (also switch it on below)",
};
const NWS_EVENTS = [
  "Tornado Warning",
  "Severe Thunderstorm Warning",
  "Extreme Wind Warning",
  "High Wind Warning",
  "Blizzard Warning",
  "Dust Storm Warning",
  "Flash Flood Warning",
  "Winter Storm Warning",
  "Ice Storm Warning",
  "Dense Fog Advisory",
  "Wind Advisory",
  "Red Flag Warning",
];

let schema = null;
let regions = {};
let hints = null; // channels/roles from the bot, if the page was opened from its link
let config = null;

const $ = (sel) => document.querySelector(sel);
const form = $("#form");

// ------------------------------------------------------------- utilities

function resolve(node) {
  while (node && node.$ref) {
    const target = node.$ref.replace(/^#\//, "").split("/").reduce((a, k) => a[k], schema);
    const { $ref, ...rest } = node;
    node = { ...target, ...rest };
  }
  return node;
}

function defaultsFor(node) {
  node = resolve(node);
  if (node.default !== undefined) return structuredClone(node.default);
  if (node.type === "object" && node.properties) {
    const out = {};
    for (const [k, child] of Object.entries(node.properties)) {
      const v = defaultsFor(child);
      if (v !== undefined) out[k] = v;
    }
    // Optional blocks with no defaulted fields (quiet_hours) stay absent.
    if (!Object.keys(out).length && node.required) return undefined;
    return out;
  }
  return undefined;
}

function merge(base, over) {
  const out = structuredClone(base);
  for (const [k, v] of Object.entries(over || {})) {
    if (v && typeof v === "object" && !Array.isArray(v) && out[k] && typeof out[k] === "object") {
      out[k] = merge(out[k], v);
    } else {
      out[k] = structuredClone(v);
    }
  }
  return out;
}

function get(path) {
  return path.split(".").reduce((o, k) => (o == null ? undefined : o[k]), config);
}

function set(path, value) {
  const keys = path.split(".");
  let node = config;
  for (const k of keys.slice(0, -1)) {
    if (typeof node[k] !== "object" || node[k] === null) node[k] = {};
    node = node[k];
  }
  const last = keys[keys.length - 1];
  if (value === "" || value === null || value === undefined) delete node[last];
  else node[last] = value;
  refresh();
}

function el(tag, attrs = {}, ...children) {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (k === "class") node.className = v;
    else if (k.startsWith("on")) node.addEventListener(k.slice(2), v);
    else if (v === true) node.setAttribute(k, "");
    else if (v !== false && v !== undefined && v !== null) node.setAttribute(k, v);
  }
  for (const child of children.flat()) {
    if (child == null) continue;
    node.append(child instanceof Node ? child : document.createTextNode(child));
  }
  return node;
}

let fieldId = 0;
function field(label, control, hint) {
  const id = control.id || `f${++fieldId}`;
  control.id = id;
  return el("div", { class: "field" }, el("label", { for: id }, label), control, hint ? el("div", { class: "hint" }, hint) : null);
}

function groupField(label, control, hint) {
  return el("div", { class: "field", role: "group", "aria-label": label },
    el("span", { class: "label" }, label), control, hint ? el("div", { class: "hint" }, hint) : null);
}

function checkbox(path, label) {
  const input = el("input", { type: "checkbox", checked: !!get(path) });
  input.addEventListener("change", () => set(path, input.checked));
  return el("label", {}, input, label);
}

function toggle(path, label, onChange) {
  const input = el("input", { type: "checkbox", checked: !!get(path) });
  input.addEventListener("change", () => {
    set(path, input.checked);
    onChange?.();
  });
  return el("label", { class: "toggle" }, input, label);
}

function textInput(path, { placeholder = "", transform } = {}) {
  const input = el("input", { type: "text", value: get(path) ?? "", placeholder });
  input.addEventListener("input", () => set(path, transform ? transform(input.value) : input.value.trim()));
  return input;
}

function stationsInput(path, placeholder = "KBJC KAPA KFNL") {
  const input = el("input", {
    type: "text",
    value: (get(path) || []).join(" "),
    placeholder,
    autocapitalize: "characters",
    spellcheck: "false",
  });
  input.addEventListener("input", () => {
    const list = input.value.toUpperCase().split(/[\s,;]+/).filter(Boolean)
      .map((s) => (s.length === 3 && /^[A-Z]+$/.test(s) ? `K${s}` : s));
    set(path, [...new Set(list)]);
  });
  return input;
}

function numberInput(path, { min, max }) {
  const input = el("input", { type: "number", value: get(path) ?? "", min, max });
  input.addEventListener("input", () => set(path, input.value === "" ? null : Number(input.value)));
  return input;
}

function timeInput(path) {
  const input = el("input", { type: "time", value: get(path) ?? "" });
  input.addEventListener("input", () => set(path, input.value));
  return input;
}

function selectInput(path, options, { blank, onChange } = {}) {
  const select = el("select");
  if (blank !== undefined) select.append(el("option", { value: "" }, blank));
  for (const opt of options) {
    if (opt.group) {
      const og = el("optgroup", { label: opt.group });
      opt.options.forEach((o) => og.append(el("option", { value: o.value, disabled: o.disabled }, o.label)));
      select.append(og);
    } else {
      select.append(el("option", { value: opt.value, disabled: opt.disabled }, opt.label));
    }
  }
  select.value = get(path) ?? "";
  select.addEventListener("change", () => {
    set(path, select.value);
    onChange?.();
  });
  return select;
}

function multiCheck(path, items) {
  const chosen = new Set(get(path) || []);
  const wrap = el("div", { class: "checks" });
  for (const { value, label } of items) {
    const input = el("input", { type: "checkbox", checked: chosen.has(value) });
    input.addEventListener("change", () => {
      input.checked ? chosen.add(value) : chosen.delete(value);
      set(path, items.map((i) => i.value).filter((v) => chosen.has(v)));
    });
    wrap.append(el("label", {}, input, label));
  }
  return wrap;
}

// ----------------------------------------------------- channels & roles

function channelOptions() {
  const channels = hints?.channels || [];
  const label = (c) => `#${c.name}${c.category ? ` (${c.category})` : ""}${c.ok ? "" : " — bot can't post here"}`;
  const groups = [];
  const text = channels.filter((c) => c.type === "text");
  const forums = channels.filter((c) => c.type === "forum");
  if (text.length) groups.push({ group: "Text channels", options: text.map((c) => ({ value: c.id, label: label(c), disabled: !c.ok })) });
  if (forums.length) groups.push({ group: "Forums (one post per brief/alert)", options: forums.map((c) => ({ value: c.id, label: label(c), disabled: !c.ok })) });
  return groups;
}

function channelPicker(section, rerender) {
  const path = `${section}.channel_id`;
  if (!hints) {
    return field("Channel ID", textInput(path, { placeholder: "e.g. 123456789012345678" }),
      "Right-click the channel in Discord → Copy Channel ID (needs Developer Mode). Open this page from /avwx config edit to pick from a list instead.");
  }
  return field("Channel", selectInput(path, channelOptions(), { blank: "— choose —", onChange: rerender }),
    "A forum gets a new post for each one, and people can run commands inside it to dig deeper.");
}

function forumTagPicker(section) {
  const channel = (hints?.channels || []).find((c) => c.id === get(`${section}.channel_id`));
  if (!channel || channel.type !== "forum") {
    if (get(`${section}.forum_tag_id`)) delete config[section].forum_tag_id;
    return null;
  }
  if (!channel.tags?.length) return null;
  return field("Forum tag", selectInput(`${section}.forum_tag_id`,
    channel.tags.map((t) => ({ value: t.id, label: t.name })), { blank: "— none —" }));
}

function roleOptions() {
  return (hints?.roles || []).map((r) => ({ value: r.id, label: r.name }));
}

// ------------------------------------------------------------- sections

// replaceChildren would print a skipped (null) field as the text "null".
function fill(node, ...children) {
  node.replaceChildren(...children.filter((c) => c != null));
}

function card(title, ...body) {
  return el("section", { class: "card" }, el("h2", {}, title), ...body);
}

function generalSection() {
  let zones = [];
  try { zones = Intl.supportedValuesOf("timeZone"); } catch { zones = [config.timezone]; }
  if (!zones.includes(config.timezone)) zones.unshift(config.timezone);
  return card("General",
    field("Time zone", selectInput("timezone", zones.map((z) => ({ value: z, label: z }))),
      "Brief times and quiet hours use this."),
    field("Home airports", stationsInput("home_airports"),
      "Used when someone runs /metar or /taf without naming an airport, and as the fallback for the brief and alerts."),
    groupField("Units", el("div", { class: "row" },
      selectInput("units.altimeter", [{ value: "inHg", label: "Altimeter: inHg" }, { value: "hPa", label: "Altimeter: hPa" }]),
      selectInput("units.temperature", [{ value: "C", label: "Temperature: °C" }, { value: "F", label: "Temperature: °F" }]))),
    hints
      ? groupField("Admin roles", roleOptions().length ? multiCheck("admin_role_ids", roleOptions()) : el("p", { class: "muted" }, "No roles."),
        "Besides anyone with Manage Server, these roles can change the bot's settings.")
      : null,
    groupField("Commands available", commandChecks()),
  );
}

function commandChecks() {
  const wrap = el("div", { class: "checks" });
  for (const name of COMMANDS) wrap.append(checkbox(`commands.${name}`, `/${name}`));
  return wrap;
}

function briefSection() {
  const body = el("div", { class: "section-body" });
  const draw = () => {
    fill(body,
      channelPicker("brief", draw),
      forumTagPicker("brief"),
      field("Time", timeInput("brief.time"), `Local time, ${config.timezone}.`),
      groupField("Days", multiCheck("brief.days", WEEKDAYS.map((d) => ({ value: d, label: d[0].toUpperCase() + d.slice(1) })))),
      field("Airports", stationsInput("brief.airports"), "Leave empty to use the home airports. Up to 12."),
      field("Winds-aloft site", textInput("brief.winds_aloft_station", { placeholder: "e.g. DEN", transform: (v) => v.trim().toUpperCase() }),
        "Three-letter forecast site. Leave empty for the one nearest the first airport."),
      groupField("Include", (() => {
        const wrap = el("div", { class: "checks" });
        for (const [k, label] of Object.entries(BRIEF_PARTS)) wrap.append(checkbox(`brief.include.${k}`, label));
        return wrap;
      })()),
    );
    body.setAttribute("aria-disabled", String(!get("brief.enabled")));
  };
  draw();
  return card("Morning brief", toggle("brief.enabled", "Post a brief every morning", draw), body);
}

function alertsSection() {
  const body = el("div", { class: "section-body" });
  const draw = () => {
    const quiet = !!config.alerts?.quiet_hours;
    const quietToggle = el("input", { type: "checkbox", checked: quiet });
    quietToggle.addEventListener("change", () => {
      if (quietToggle.checked) config.alerts.quiet_hours = { start: "22:00", end: "06:00" };
      else delete config.alerts.quiet_hours;
      refresh();
      draw();
    });
    const events = [...new Set([...NWS_EVENTS, ...(get("alerts.triggers.nws_events") || [])])];
    fill(body,
      channelPicker("alerts", draw),
      forumTagPicker("alerts"),
      hints
        ? field("Ping a role", selectInput("alerts.mention_role_id", roleOptions(), { blank: "— no ping —" }))
        : field("Role ID to ping", textInput("alerts.mention_role_id", { placeholder: "optional" })),
      field("Area airports", stationsInput("alerts.area.airports"), "Leave empty to use the home airports."),
      field("Radius (nm)", numberInput("alerts.area.radius_nm", { min: 5, max: 150 }), "How close a hazard has to be to count."),
      groupField("NWS warnings", multiCheck("alerts.triggers.nws_events", events.map((e) => ({ value: e, label: e }))),
        "Only warnings marked Immediate are posted."),
      groupField("Aviation hazards", el("div", { class: "checks" },
        checkbox("alerts.triggers.convective_sigmet", "Convective SIGMETs"),
        checkbox("alerts.triggers.cwa", "Center Weather Advisories"),
        checkbox("alerts.triggers.metar_thunderstorm", "Thunderstorm in a METAR"),
        checkbox("alerts.triggers.metar_funnel", "Funnel cloud in a METAR"))),
      field("Gust alert (kt)", numberInput("alerts.triggers.metar_gust_kt", { min: 0, max: 100 }), "Post when a METAR reports gusts this strong. 0 turns it off."),
      field("Cooldown (minutes)", numberInput("alerts.cooldown_min", { min: 0, max: 240 }), "Minimum gap between new posts of the same kind."),
      el("div", { class: "field" }, el("label", { class: "toggle" }, quietToggle, "Quiet hours (post, but don't ping)")),
      quiet ? el("div", { class: "row" }, timeInput("alerts.quiet_hours.start"), "to", timeInput("alerts.quiet_hours.end")) : null,
    );
    body.setAttribute("aria-disabled", String(!get("alerts.enabled")));
  };
  draw();
  return card("Severe-weather alerts", toggle("alerts.enabled", "Post when severe weather is imminent", draw), body);
}

function mountainSection() {
  const body = el("div", { class: "section-body" });
  const draw = () => {
    const regionList = Object.values(regions);
    const region = regions[get("mountain.region")];
    fill(body,
      regionList.length
        ? field("Region", selectInput("mountain.region", regionList.map((r) => ({ value: r.id, label: r.name })), { onChange: () => { set("mountain.passes", []); draw(); } }))
        : field("Region", textInput("mountain.region")),
      region?.passes?.length
        ? groupField("Passes", multiCheck("mountain.passes", region.passes.map((p) => ({ value: p.id, label: p.name }))), "None ticked means all of them.")
        : null,
    );
    body.setAttribute("aria-disabled", String(!get("mountain.enabled")));
  };
  draw();
  return card("Mountain flying", toggle("mountain.enabled", "Show mountain-flying information (/mountain, and in the brief if ticked above)", draw), body);
}

// ------------------------------------------------------------- output

function exportDoc() {
  const { _guild, ...doc } = config;
  return doc;
}

function refresh() {
  const doc = exportDoc();
  const errors = validate(schema, doc);
  if (doc.brief?.enabled && !doc.brief.channel_id) errors.push("Morning brief: choose a channel.");
  if (doc.alerts?.enabled && !doc.alerts.channel_id) errors.push("Alerts: choose a channel.");
  const list = $("#errors");
  list.replaceChildren(...errors.map((e) => el("li", {}, e)));
  $("#json").textContent = JSON.stringify(doc, null, 2);
  $("#download").disabled = errors.length > 0;
}

function download() {
  const blob = new Blob([JSON.stringify(exportDoc(), null, 2) + "\n"], { type: "application/json" });
  const a = el("a", { href: URL.createObjectURL(blob), download: "config.json" });
  document.body.append(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(a.href), 1000);
}

async function copy() {
  try {
    await navigator.clipboard.writeText(JSON.stringify(exportDoc(), null, 2));
    $("#copy-status").textContent = "Copied.";
  } catch {
    $("#copy-status").textContent = "Couldn't copy; use Show JSON instead.";
  }
}

// ------------------------------------------------------------- loading

function start(doc) {
  hints = doc._guild || null;
  config = merge(defaultsFor(schema), doc);
  config.schema_version = 1;
  $("#server-line").textContent = hints ? `Editing settings for ${hints.name}` : "Editing a config without a server attached — channels are entered by ID.";
  form.replaceChildren(generalSection(), briefSection(), alertsSection(), mountainSection());
  form.hidden = false;
  $("#output").hidden = false;
  $("#intro").querySelector("ol").hidden = !!hints;
  refresh();
}

function showError(message) {
  const p = $("#load-error");
  p.textContent = message;
  p.hidden = false;
}

async function boot() {
  [schema, regions] = await Promise.all([
    fetch(SCHEMA_URL).then((r) => r.json()),
    fetch(REGIONS_URL).then((r) => (r.ok ? r.json() : {})).catch(() => ({})),
  ]);

  $("#start-blank").addEventListener("click", () => start({ schema_version: 1 }));
  $("#open-file").addEventListener("change", async (e) => {
    const file = e.target.files[0];
    if (!file) return;
    try {
      start(JSON.parse(await file.text()));
    } catch {
      showError("That file isn't a valid config.json.");
    }
  });
  $("#download").addEventListener("click", download);
  $("#copy").addEventListener("click", copy);
  form.addEventListener("submit", (e) => e.preventDefault());

  const match = location.hash.match(/^#c=([A-Za-z0-9_-]+)$/);
  if (match) {
    try {
      start(await decodeFragment(match[1]));
    } catch {
      showError("This link looks damaged. Run /avwx config edit again for a fresh one.");
    }
  }
}

boot().catch((err) => showError(`The page failed to load: ${err.message}`));
