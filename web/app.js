/* GridCrate - artwork manager for non-Steam games */
"use strict";

const TOKEN = (window.__GRIDCRATE__ || {}).token || "";
const SLOTS = ["portrait", "wide", "hero", "logo", "icon"];
const SLOT_LABEL = {
  portrait: "Portrait", wide: "Wide", hero: "Hero", logo: "Logo", icon: "Icon",
};
const SLOT_HINT = {
  portrait: "600x900 - library card",
  wide: "920x430 - Big Picture & recent games",
  hero: "1920x620 - game page banner",
  logo: "transparent logo for the game page",
  icon: "square icon next to the name",
};
const STYLE_ORDER = ["official", "alternate", "white_logo", "black_logo", "no_logo", "material", "blur", "custom", "unknown"];

const state = {
  view: "library",
  games: [],
  status: null,
  orphans: null,
  filter: "all",
  query: "",
  sort: "name",
  config: {},
  sources: [],
  slotSource: {},
  slotQuery: {},
  detail: null,
  job: null,
  jobTimer: null,
  busy: false,
};

/* ---------------- plumbing ---------------- */

const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));

function el(tag, props = {}, ...children) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(props || {})) {
    if (value === null || value === undefined || value === false) continue;
    if (key === "class") node.className = value;
    else if (key === "html") node.innerHTML = value;
    else if (key === "text") node.textContent = value;
    else if (key === "style" && typeof value === "object") Object.assign(node.style, value);
    else if (key.startsWith("on")) node.addEventListener(key.slice(2), value);
    else if (key === "dataset") Object.assign(node.dataset, value);
    else node.setAttribute(key, value);
  }
  for (const child of children.flat()) {
    if (child === null || child === undefined || child === false) continue;
    node.append(child instanceof Node ? child : document.createTextNode(String(child)));
  }
  return node;
}

async function api(path, { method = "GET", body = null } = {}) {
  const options = { method, headers: { "X-GridCrate-Token": TOKEN } };
  if (body !== null) {
    options.headers["Content-Type"] = "application/json";
    options.body = JSON.stringify(body);
  }
  const response = await fetch(path, options);
  let payload = null;
  try { payload = await response.json(); } catch { payload = null; }
  if (!response.ok) throw new Error((payload && payload.error) || `HTTP ${response.status}`);
  return payload;
}

function toast(message, kind = "", timeout = 4200) {
  const node = el("div", { class: `toast ${kind}`, text: message });
  $("#toasts").append(node);
  setTimeout(() => node.remove(), timeout);
}

function thumbUrl(appid, slot, width = 320) {
  return `/api/thumb?appid=${appid}&slot=${slot}&w=${width}&token=${encodeURIComponent(TOKEN)}`;
}
function fullUrl(appid, slot) {
  return `/api/full?appid=${appid}&slot=${slot}&token=${encodeURIComponent(TOKEN)}`;
}

function bestArt(game, width = 320) {
  for (const slot of ["portrait", "wide", "hero"]) {
    if (game.slots && game.slots[slot]) return { url: thumbUrl(game.appid, slot, width), slot };
  }
  return null;
}

function fmtBytes(bytes) {
  if (!bytes) return "0 B";
  const units = ["B", "KB", "MB", "GB"];
  let value = bytes, index = 0;
  while (value >= 1024 && index < units.length - 1) { value /= 1024; index++; }
  return `${value.toFixed(value < 10 && index > 0 ? 1 : 0)} ${units[index]}`;
}

function fmtDate(seconds) {
  if (!seconds) return "never";
  return new Date(seconds * 1000).toLocaleDateString(undefined, { year: "numeric", month: "short", day: "numeric" });
}

/* ---------------- boot ---------------- */

async function boot() {
  try {
    await refresh({ keepView: true });
  } catch (error) {
    $("#view").replaceChildren(el("div", { class: "center", text: `Could not reach the backend: ${error.message}` }));
  }
  $("#nav").addEventListener("click", (event) => {
    const button = event.target.closest("button[data-view]");
    if (!button) return;
    switchView(button.dataset.view);
  });
  document.addEventListener("paste", (event) => {
    const drawer = document.querySelector(".drawer");
    if (!drawer || !state.detail) return;
    const item = Array.from(event.clipboardData?.items || []).find((entry) => entry.type.startsWith("image/"));
    if (!item) return;
    event.preventDefault();
    const game = state.detail.game;
    const target = SLOTS.find((slot) => !game.slots[slot]) || "portrait";
    uploadImage(game, target, item.getAsFile());
  });
  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape") closeModal();
    if (event.key === "/" && !["INPUT", "TEXTAREA"].includes(document.activeElement.tagName)) {
      event.preventDefault();
      const search = $("#library-search");
      if (search) search.focus();
    }
  });
}

async function refresh({ keepView = false } = {}) {
  const [status, games, sources] = await Promise.all([
    api("/api/state"), api("/api/games"), api("/api/sources"),
  ]);
  state.status = status;
  state.sources = sources.sources;
  state.config = status.config || {};
  state.games = games.games;
  applyAccent(status.theme && status.theme.accent);
  renderSidebar();
  if (!keepView || !state.view) state.view = "library";
  render();
}

function applyAccent(accent) {
  if (accent) document.documentElement.style.setProperty("--accent", accent);
}

function renderSidebar() {
  const stats = state.status.stats;
  $("#pill-library").textContent = `${stats.games}`;
  $("#pill-orphans").textContent = stats.orphans ? `${stats.orphans}` : "";
  $("#steam-status").innerHTML = "";
  const steam = state.status.steam;
  const faugus = state.status.faugus;
  $("#steam-status").append(
    el("div", {}, el("span", { class: `dot ${steam.running ? "on" : ""}` }), `Steam ${steam.running ? "running" : "closed"}`),
    el("div", { class: "dim", style: { marginTop: "4px" } }, `account ${steam.account_id || "-"}`),
    faugus.available ? el("div", { class: "dim", style: { marginTop: "4px" } }, `Faugus ${faugus.running ? "running" : "closed"} - ${faugus.count} games`) : null,
  );
}

function switchView(view) {
  state.view = view;
  $$("#nav button").forEach((button) => button.classList.toggle("active", button.dataset.view === view));
  render();
}

function render() {
  const view = $("#view");
  view.replaceChildren();
  if (state.view === "library") renderLibrary(view);
  else if (state.view === "orphans") renderOrphans(view);
  else if (state.view === "shortcuts") renderShortcuts(view);
  else renderSettings(view);
}

/* ---------------- library ---------------- */

function filteredGames() {
  const query = state.query.trim().toLowerCase();
  let games = state.games.filter((game) => {
    if (query && !game.name.toLowerCase().includes(query)) return false;
    const complete = game.missing.length === 0;
    if (state.filter === "missing") return game.slots === undefined || Object.keys(game.slots).length === 0;
    if (state.filter === "partial") return Object.keys(game.slots).length > 0 && !complete;
    if (state.filter === "complete") return complete;
    if (state.filter === "faugus") return game.source === "faugus";
    return true;
  });
  const byName = (a, b) => a.name.toLowerCase().localeCompare(b.name.toLowerCase());
  if (state.sort === "name") games.sort(byName);
  else if (state.sort === "played") games.sort((a, b) => (b.last_play || 0) - (a.last_play || 0));
  else if (state.sort === "missing") games.sort((a, b) => b.missing.length - a.missing.length || byName(a, b));
  return games;
}

function renderLibrary(root) {
  const stats = state.status.stats;
  root.append(
    el("h1", { text: "Library" }),
    el("div", { class: "sub", text: `${stats.games} non-Steam shortcuts in this account` }),
    el("div", { class: "toolbar" },
      el("input", {
        type: "search", id: "library-search", placeholder: "Search games   ( / )", value: state.query,
        oninput: (event) => { state.query = event.target.value; renderGrid(); },
      }),
      select({
        value: state.filter, options: [
          ["all", "All games"], ["missing", "No artwork"], ["partial", "Incomplete"], ["complete", "Complete"], ["faugus", "From Faugus"],
        ], onchange: (value) => { state.filter = value; renderGrid(); },
      }),
      select({
        value: state.sort, options: [["name", "Sort: name"], ["played", "Sort: last played"], ["missing", "Sort: missing art first"]],
        onchange: (value) => { state.sort = value; renderGrid(); },
      }),
      el("div", { class: "spacer" }),
      el("button", { class: "btn", onclick: () => refresh(), text: "Rescan" }),
      el("button", { class: "btn primary", onclick: () => openBatchDialog(), text: "Fetch missing artwork" }),
    ),
    el("div", { class: "stats", id: "library-stats" }),
    repairBanner(),
    el("div", { class: "grid", id: "library-grid" }),
  );
  renderGrid();
}

function repairBanner() {
  const stats = state.status.stats || {};
  const restored = state.status.repaired || [];
  const parts = [];
  if (restored.length) {
    parts.push(el("div", { class: "banner info" },
      el("div", { style: { flex: 1 } },
        el("b", { text: `Restored artwork for ${restored.map((item) => item.name).join(", ")}` }),
        el("div", { class: "dim", text: "Their appid changed when the shortcut was rewritten, so the artwork had been left behind under the old id." }),
      ),
      el("button", { class: "btn small", text: "View orphans", onclick: () => switchView("orphans") }),
    ));
  }
  if (stats.repairable) {
    parts.push(el("div", { class: "banner warn" },
      el("div", { style: { flex: 1 } },
        el("b", { text: `${stats.repairable} game(s) can get artwork back from orphaned files` }),
        el("div", { class: "dim", text: "Open the Orphans page to review and repair them." }),
      ),
      el("button", { class: "btn small", text: "Review", onclick: () => switchView("orphans") }),
    ));
  }
  return parts.length ? el("div", {}, parts) : el("div", {});
}

function renderGrid() {
  const grid = $("#library-grid");
  if (!grid) return;
  const games = filteredGames();
  const stats = state.status.stats;
  $("#library-stats").replaceChildren(
    el("span", {}, el("b", { text: `${stats.complete}` }), " complete"),
    el("span", {}, el("b", { text: `${stats.partial}` }), " incomplete"),
    el("span", {}, el("b", { text: `${stats.missing}` }), " without artwork"),
    el("span", {}, el("b", { text: `${stats.orphans}` }), " orphan groups"),
  );
  grid.replaceChildren(...games.map(tile));
  if (!games.length) grid.append(el("div", { class: "center", text: "No games match this filter." }));
}

function tile(game) {
  const art = bestArt(game, 320);
  const node = el("div", { class: "tile" },
    el("div", { class: "art", onclick: () => openDetail(game.appid) },
      art ? el("img", { src: art.url, loading: "lazy", alt: game.name })
          : el("div", { class: "placeholder", text: game.name.slice(0, 1).toUpperCase() }),
      el("div", { class: "overlay", text: game.name }),
      el("div", { class: "quick" },
        el("button", {
          class: "btn small primary", text: game.missing.length ? "Get art" : "Edit",
          onclick: (event) => { event.stopPropagation(); openDetail(game.appid); },
        }),
      ),
    ),
    el("div", { class: "meta" },
      el("div", { class: "slot-dots" }, SLOTS.map((slot) =>
        el("div", { class: `slot-dot ${game.slots[slot] ? "on" : "miss"}`, title: `${SLOT_LABEL[slot]}: ${game.slots[slot] ? "present" : "missing"}` }))),
      el("span", { class: "source", text: game.source }),
    ),
  );
  return node;
}

function select({ value, options, onchange, class: klass = "" }) {
  const node = el("select", { class: klass, onchange: (event) => onchange(event.target.value) });
  for (const [key, label] of options) {
    node.append(el("option", { value: key, selected: key === value, text: label }));
  }
  node.value = value;
  return node;
}

/* ---------------- game detail ---------------- */

async function openDetail(appid) {
  const drawer = el("div", { class: "drawer" });
  const back = el("div", { class: "drawer-back", onclick: closeDrawer });
  document.body.append(back, drawer);
  drawer.append(el("div", { class: "center" }, el("div", { class: "spin" }), "Loading…"));
  try {
    const data = await api(`/api/game?appid=${appid}`);
    drawer.replaceChildren(...detailContent(data));
    state.detail = data;
  } catch (error) {
    drawer.replaceChildren(el("div", { class: "center", text: error.message }));
  }
}

function closeDrawer() {
  $(".drawer-back")?.remove();
  $(".drawer")?.remove();
  state.detail = null;
}

function detailContent(data) {
  const game = data.game;
  const hero = game.slots.hero ? fullUrl(game.appid, "hero") : null;
  const cover = game.slots.portrait ? thumbUrl(game.appid, "portrait", 200) : null;
  const head = el("div", { class: "drawer-head" },
    hero ? el("div", { class: "bg", style: { backgroundImage: `url("${hero}")` } }) : null,
    el("div", { class: "inner" },
      cover ? el("img", { class: "cover", src: cover }) : el("div", { class: "cover" }),
      el("div", { class: "titles" },
        el("h2", { text: game.name }),
        el("div", { class: "path", text: `${game.exe} ${game.launch_options || ""}`.trim() }),
        el("div", { class: "badges" },
          el("span", { class: "badge", text: `appid ${game.appid}` }),
          el("span", { class: "badge", text: game.source }),
          data.sgdb_id ? el("span", { class: "badge accent", text: `SteamGridDB: ${data.sgdb_name || data.sgdb_id}` }) : el("span", { class: "badge warn", text: "no SteamGridDB match" }),
          data.faugus && data.faugus.gameid ? el("span", { class: "badge", text: `Faugus: ${data.faugus.gameid}` }) : null,
          el("span", { class: "badge", text: `played ${fmtDate(game.last_play)}` }),
        ),
      ),
      el("button", { class: "btn ghost", onclick: closeDrawer, text: "✕" }),
    ),
  );

  const body = el("div", { class: "drawer-body" });
  body.append(matchRow(data));
  for (const slot of SLOTS) body.append(slotSection(data, slot));

  const foot = el("div", { class: "drawer-foot" },
    el("button", {
      class: "btn primary",
      text: game.missing.length ? `Fetch ${game.missing.length} missing` : "Refetch all slots",
      onclick: () => openBatchDialog({ appids: [game.appid], slots: game.missing.length ? game.missing : SLOTS, onlyMissing: game.missing.length > 0 }),
    }),
    el("button", { class: "btn", text: "Open grid folder", onclick: () => api("/api/open", { method: "POST", body: { target: "grid" } }) }),
    el("div", { class: "spacer" }),
    el("button", { class: "btn ghost", text: "Close", onclick: closeDrawer }),
  );
  return [head, body, foot];
}

function matchRow(data) {
  const game = data.game;
  return el("div", { class: "card", style: { display: "flex", alignItems: "center", gap: "12px", marginBottom: "16px" } },
    el("div", { style: { flex: 1 } },
      el("h3", { text: "SteamGridDB match" }),
      el("div", { class: "hint", text: data.sgdb_id ? `Matched to “${data.sgdb_name || data.sgdb_id}” - all artwork below comes from this entry.` : "No entry matched yet - search to pick the right game." }),
    ),
    el("button", { class: "btn", text: data.sgdb_id ? "Change match" : "Search", onclick: () => openMatchDialog(game, data) }),
  );
}

function slotSection(data, slot) {
  const game = data.game;
  const info = game.slots[slot];
  const section = el("div", { class: "slot", dataset: { slot } });
  const head = el("div", { class: "slot-head" },
    info ? el("img", { class: `slot-thumb ${slot}`, src: thumbUrl(game.appid, slot, 200), title: info.file })
         : el("div", { class: `slot-thumb ${slot}` }),
    el("div", { style: { flex: 1 } },
      el("div", { class: "name", text: SLOT_LABEL[slot] }),
      el("div", { class: "hint", text: info ? `${info.width}×${info.height} · ${fmtBytes(info.size)} · ${info.file.split("/").pop()}` : SLOT_HINT[slot] }),
    ),
    el("button", { class: "btn small", text: "Browse", onclick: () => loadCandidates(section, game, slot, true) }),
    info ? el("button", { class: "btn small danger", text: "Remove", onclick: () => removeSlot(game.appid, slot) }) : null,
  );
  const body = el("div", { class: "slot-body" });
  body.append(sourceRow(section, game, slot));
  body.append(el("div", { class: "strip" },
    el("div", { class: "empty-slot", text: "Pick a source to see artwork, drop an image here, or press Ctrl+V." })));
  section.append(head, body);
  attachDropTarget(section, game, slot);
  return section;
}

function sourceRow(section, game, slot) {
  const current = state.slotSource[slot] || "steamgriddb";
  const row = el("div", { class: "filters source-row" });
  for (const source of state.sources) {
    const supported = !source.slots || source.slots.includes(slot);
    row.append(el("button", {
      class: `chip ${current === source.id ? "active" : ""} ${supported ? "" : "disabled"}`,
      title: supported ? source.note : `${source.label} has no ${SLOT_LABEL[slot].toLowerCase()} artwork`,
      text: source.label + (source.available ? "" : " ⚠"),
      onclick: () => {
        if (!supported) return;
        if (state.slotSource[slot] !== source.id) delete state.slotQuery[slot];
        state.slotSource[slot] = source.id;
        renderSlotRow(section, game, slot);
        loadCandidates(section, game, slot, true);
      },
    }));
  }
  row.append(el("button", {
    class: "chip", text: "From file…",
    onclick: () => pickFile(game, slot),
  }));
  const source = state.sources.find((item) => item.id === current);
  if (source && !source.available && source.reason) {
    row.append(el("span", { class: "dim", text: source.reason }));
  }
  if (current === "web") {
    const input = el("input", {
      type: "search", class: "query", placeholder: "search terms…",
      value: state.slotQuery[slot] || defaultQuery(game, slot),
      onkeydown: (event) => {
        if (event.key === "Enter") {
          state.slotQuery[slot] = event.target.value;
          loadCandidates(section, game, slot, true);
        }
      },
    });
    row.append(input, el("button", {
      class: "btn small", text: "Search",
      onclick: () => {
        state.slotQuery[slot] = input.value;
        loadCandidates(section, game, slot, true);
      },
    }), el("button", {
      class: "btn small ghost", text: "Open Google Images",
      title: "Open the same search in your browser (then drag the image back here)",
      onclick: () => api("/api/browse", { method: "POST", body: { query: input.value, engine: "google" } }),
    }));
  }
  if (current === "steamgriddb" || current === "igdb") {
    const input = el("input", {
      type: "search", class: "query", placeholder: `search ${source ? source.label : ""}…`,
      value: state.slotQuery[slot] || "",
      onkeydown: (event) => {
        if (event.key === "Enter") {
          state.slotQuery[slot] = event.target.value;
          loadCandidates(section, game, slot, true);
        }
      },
    });
    row.append(input, el("button", {
      class: "btn small", text: "Search",
      onclick: () => {
        state.slotQuery[slot] = input.value;
        loadCandidates(section, game, slot, true);
      },
    }));
  }
  return row;
}

function renderSlotRow(section, game, slot) {
  const body = section.querySelector(".slot-body");
  body.replaceChildren(sourceRow(section, game, slot));
  body.append(el("div", { class: "strip" }, el("div", { class: "empty-slot", text: "Loading…" })));
}

function defaultQuery(game, slot) {
  const hint = { portrait: "cover art", wide: "key art wallpaper", hero: "key art wallpaper",
                 logo: "logo png", icon: "icon png" }[slot] || "artwork";
  return `${game.name} ${hint}`;
}

function pickFile(game, slot) {
  const input = el("input", { type: "file", accept: "image/*", style: { display: "none" } });
  input.addEventListener("change", async () => {
    const file = input.files && input.files[0];
    if (file) await uploadImage(game, slot, file);
    input.remove();
  });
  document.body.append(input);
  input.click();
}

async function uploadImage(game, slot, file) {
  if (!file.type.startsWith("image/")) return toast("That is not an image file", "err");
  const reader = new FileReader();
  reader.onload = async () => {
    try {
      await api("/api/apply_upload", {
        method: "POST",
        body: { appid: game.appid, slot, data: reader.result, name: file.name, mime: file.type },
      });
      toast(`${SLOT_LABEL[slot]} applied from ${file.name}`, "ok");
      closeDrawer();
      await refresh({ keepView: true });
      openDetail(game.appid);
    } catch (error) {
      toast(error.message, "err");
    }
  };
  reader.readAsDataURL(file);
}

function attachDropTarget(section, game, slot) {
  section.addEventListener("dragover", (event) => {
    event.preventDefault();
    section.classList.add("drop-target");
  });
  section.addEventListener("dragleave", () => section.classList.remove("drop-target"));
  section.addEventListener("drop", async (event) => {
    event.preventDefault();
    section.classList.remove("drop-target");
    const file = event.dataTransfer.files && event.dataTransfer.files[0];
    if (file) {
      await uploadImage(game, slot, file);
      return;
    }
    const url = event.dataTransfer.getData("text/uri-list") || event.dataTransfer.getData("text/plain");
    if (url && /^https?:\/\//.test(url.trim())) {
      try {
        await api("/api/apply", {
          method: "POST",
          body: { appid: game.appid, slot, image: { source: "file", url: url.trim(), thumb: url.trim(), id: url.trim() } },
        });
        toast(`${SLOT_LABEL[slot]} applied from URL`, "ok");
        closeDrawer();
        await refresh({ keepView: true });
        openDetail(game.appid);
      } catch (error) {
        toast(error.message, "err");
      }
    }
  });
}

async function loadCandidates(section, game, slot, force = false) {
  const body = section.querySelector(".slot-body");
  if (body.dataset.loaded === "1" && !force) return;
  body.dataset.loaded = "1";
  const source = state.slotSource[slot] || "steamgriddb";
  const strip = el("div", { class: "strip" },
    el("div", { class: "filters" }, el("div", { class: "spin" }),
      el("span", { class: "dim", text: `searching ${sourceLabel(source)}…` })));
  body.replaceChildren(sourceRow(section, game, slot), strip);
  const filters = state.candidateFilters || { style: "all", nsfw: false, humor: false };
  const params = new URLSearchParams({ appid: game.appid, slot, source });
  if (state.slotQuery[slot]) params.set("q", state.slotQuery[slot]);
  if (filters.style && filters.style !== "all") params.set("styles", filters.style);
  if (filters.nsfw) params.set("nsfw", "1");
  if (filters.humor) params.set("humor", "1");
  try {
    const data = await api(`/api/candidates?${params}`);
    const notes = data.note ? el("div", { class: "dim", style: { padding: "2px 0 6px" }, text: data.note }) : null;
    if (data.needs_review && data.needs_review.length) {
      body.replaceChildren(sourceRow(section, game, slot), notes || el("div", {}),
        el("div", { class: "banner warn", style: { margin: "0 0 10px" } },
          el("div", { text: `No confident match for “${game.name}”. Pick the right entry:` })),
        el("div", { class: "filters" }, data.needs_review.map((item) =>
          el("button", { class: "chip", text: item.name, onclick: () => setMatch(game.appid, item, section, slot) }))));
      return;
    }
    if (!data.images.length) {
      body.replaceChildren(sourceRow(section, game, slot), notes || el("div", {}),
        el("div", { class: "strip" }, el("div", { class: "empty-slot", text: "No images from this source." })));
      return;
    }
    body.replaceChildren(sourceRow(section, game, slot), notes || el("div", {}),
      candidateStrip(data.images, game, slot, filters, section));
  } catch (error) {
    body.replaceChildren(sourceRow(section, game, slot), el("div", { class: "banner err", text: error.message }));
  }
}

function sourceLabel(id) {
  const source = state.sources.find((item) => item.id === id);
  return source ? source.label : id;
}

function candidateStrip(images, game, slot, filters, section) {
  const strip = el("div", { class: "strip" });
  const styles = Array.from(new Set(images.map((image) => image.style))).sort(
    (a, b) => STYLE_ORDER.indexOf(a) - STYLE_ORDER.indexOf(b));
  const holder = section || strip.closest(".slot");
  const hasStyles = styles.filter((style) => style && style !== "unknown" && style !== "web").length > 1;
  const filterRow = el("div", { class: "filters" },
    el("span", { class: "dim", text: `${images.length} images` }),
    hasStyles ? el("button", { class: `chip ${filters.style === "all" ? "active" : ""}`, text: "all styles", onclick: () => { filters.style = "all"; state.candidateFilters = filters; loadCandidates(holder, game, slot, true); } }) : null,
    hasStyles ? styles.filter((style) => style && style !== "unknown" && style !== "web").map((style) => el("button", {
      class: `chip ${filters.style === style ? "active" : ""}`, text: style.replace(/_/g, " "),
      onclick: () => { filters.style = style; state.candidateFilters = filters; loadCandidates(holder, game, slot, true); },
    })) : null,
    el("label", { class: "check", style: { marginLeft: "6px" } },
      el("input", { type: "checkbox", checked: filters.nsfw, onchange: (event) => { filters.nsfw = event.target.checked; state.candidateFilters = filters; loadCandidates(holder, game, slot, true); } }),
      "NSFW"),
  );
  if (!images.length) {
    strip.append(el("div", { class: "empty-slot", text: "No images for this slot." }));
  }
  for (const image of images) {
    const dims = image.width && image.height ? `${image.width}×${image.height} · ` : "";
    const meta = `${dims}${image.label || image.style || ""}${image.author ? " · " + image.author : ""}`;
    strip.append(el("div", {
      class: `cand ${slot}`, title: meta,
      onclick: () => openImagePreview(game, slot, image, section),
    },
      el("img", { src: image.thumb, loading: "lazy", referrerpolicy: "no-referrer" }),
      el("div", { class: "tag", text: `${sourceLabel(image.source)} · ${image.label || image.style || ""}` }),
    ));
  }
  return el("div", {}, filterRow, strip);
}

function openImagePreview(game, slot, image, section) {
  const sizeLabel = el("span", { class: "dim", text: `${sourceLabel(image.source)} · ${image.label || image.style || ""}` });
  const preview = el("img", {
    class: "preview", src: image.url, referrerpolicy: "no-referrer",
    onload: (event) => {
      const img = event.target;
      sizeLabel.textContent = `${sourceLabel(image.source)} · ${img.naturalWidth}×${img.naturalHeight} · ${image.label || image.style || ""}`;
    },
    onerror: () => { sizeLabel.textContent = `${sourceLabel(image.source)} · could not load the full image (try another result)`; },
  });
  openModal(
    el("div", { class: "modal-head" },
      el("strong", { text: `${SLOT_LABEL[slot]} for ${game.name}` }),
      sizeLabel,
      el("div", { class: "spacer" }),
      el("button", { class: "btn ghost", text: "✕", onclick: closeModal }),
    ),
    el("div", { class: "modal-body" }, preview),
    el("div", { class: "modal-foot" },
      el("div", { class: "spacer" }),
      el("button", { class: "btn", text: "Cancel", onclick: closeModal }),
      el("button", {
        class: "btn primary", text: "Apply to Steam",
        onclick: async (event) => {
          event.target.disabled = true;
          try {
            await api("/api/apply", { method: "POST", body: { appid: game.appid, slot, image } });
            toast(`${SLOT_LABEL[slot]} applied to ${game.name}`, "ok");
            closeModal();
            closeDrawer();
            await refresh({ keepView: true });
            openDetail(game.appid);
          } catch (error) {
            toast(error.message, "err");
            event.target.disabled = false;
          }
        },
      }),
    ),
  );
}

async function removeSlot(appid, slot) {
  if (!confirm(`Remove the ${SLOT_LABEL[slot].toLowerCase()} artwork from Steam? (a copy goes to GridCrate's trash)`)) return;
  try {
    await api("/api/remove", { method: "POST", body: { appid, slot } });
    toast(`${SLOT_LABEL[slot]} removed`, "ok");
    closeDrawer();
    await refresh({ keepView: true });
    openDetail(appid);
  } catch (error) {
    toast(error.message, "err");
  }
}

function openMatchDialog(game, data) {
  const results = el("div", { style: { marginTop: "14px", display: "flex", flexDirection: "column", gap: "6px" } });
  const input = el("input", { type: "search", style: { width: "100%" }, placeholder: `Search SteamGridDB…`, value: game.name });
  let timer = null;
  const run = async () => {
    const term = input.value.trim();
    if (!term) return;
    results.replaceChildren(el("div", { class: "center" }, el("div", { class: "spin" }), "Searching…"));
    try {
      const payload = await api(`/api/search?q=${encodeURIComponent(term)}`);
      results.replaceChildren(...payload.results.map((item) =>
        el("button", {
          class: "btn", style: { justifyContent: "flex-start" },
          onclick: async () => { await setMatch(game.appid, item); closeModal(); },
        },
          el("span", { text: item.name }),
          el("span", { class: "dim", text: `${Math.round(item.score * 100)}%${item.verified ? " · verified" : ""}` }),
        )));
      if (!payload.results.length) results.replaceChildren(el("div", { class: "dim", text: "No results." }));
    } catch (error) {
      results.replaceChildren(el("div", { class: "banner err", text: error.message }));
    }
  };
  input.addEventListener("input", () => { clearTimeout(timer); timer = setTimeout(run, 350); });
  openModal(
    el("div", { class: "modal-head" },
      el("strong", { text: `SteamGridDB entry for ${game.name}` }),
      el("div", { class: "spacer" }),
      el("button", { class: "btn ghost", text: "✕", onclick: closeModal }),
    ),
    el("div", { class: "modal-body", style: { width: "560px" } }, input, results),
  );
  run();
}

async function setMatch(appid, item, section, slot) {
  try {
    await api("/api/match", {
      method: "POST",
      body: { appid, source: item.source || "steamgriddb", match_id: item.id, name: item.name },
    });
    toast(`Matched to ${item.name}`, "ok");
    if (section && slot) {
      state.slotQuery[slot] = "";
      loadCandidates(section, game0(appid), slot, true);
    } else {
      closeDrawer();
      await refresh({ keepView: true });
      openDetail(appid);
    }
  } catch (error) {
    toast(error.message, "err");
  }
}

function game0(appid) {
  return state.games.find((game) => game.appid === appid) || { appid, name: "" };
}

/* ---------------- batch ---------------- */

function openBatchDialog(preset = {}) {
  const appids = preset.appids || null;
  const chosenSlots = new Set(preset.slots || SLOTS);
  const options = { onlyMissing: preset.onlyMissing !== undefined ? preset.onlyMissing : true,
                    preferOfficial: !!state.config.prefer_official };
  const slotBoxes = SLOTS.map((slot) => el("label", { class: "check" },
    el("input", { type: "checkbox", checked: chosenSlots.has(slot), onchange: (event) => { event.target.checked ? chosenSlots.add(slot) : chosenSlots.delete(slot); } }),
    SLOT_LABEL[slot]));
  const scope = appids ? `${appids.length} selected game` : `${state.games.length} games`;
  const jobBox = el("div", { id: "batch-job" });
  const startButton = el("button", {
    class: "btn primary", text: "Start",
    onclick: async () => {
      startButton.disabled = true;
      try {
        await api("/api/batch/fetch", {
          method: "POST",
          body: {
            appids: appids || [], slots: Array.from(chosenSlots),
            only_missing: options.onlyMissing, threshold: 0.72,
            prefer_official: options.preferOfficial,
            faugus: state.config.faugus_integration,
          },
        });
        pollJob(jobBox, () => { refresh({ keepView: true }); });
      } catch (error) {
        toast(error.message, "err");
        startButton.disabled = false;
      }
    },
  });
  openModal(
    el("div", { class: "modal-head" },
      el("strong", { text: "Fetch missing artwork" }),
      el("div", { class: "spacer" }),
      el("button", { class: "btn ghost", text: "✕", onclick: closeModal }),
    ),
    el("div", { class: "modal-body", style: { width: "620px" } },
      el("div", { class: "hint", text: `GridCrate searches SteamGridDB for each game (${scope}), matches it by name and applies the best image per slot. Games it cannot match confidently are left for you to review.` }),
      el("div", { class: "filters" }, slotBoxes),
      el("label", { class: "check", style: { marginTop: "10px" } },
        el("input", { type: "checkbox", checked: options.onlyMissing, onchange: (event) => { options.onlyMissing = event.target.checked; } }),
        "Only fill empty slots (never overwrite existing art)"),
      el("label", { class: "check", style: { marginTop: "8px" } },
        el("input", { type: "checkbox", checked: options.preferOfficial, onchange: (event) => { options.preferOfficial = event.target.checked; } }),
        "Prefer official Steam art when the game is on Steam (icons still come from SteamGridDB)"),
      jobBox,
    ),
    el("div", { class: "modal-foot" },
      el("div", { class: "spacer" }),
      el("button", { class: "btn", text: "Cancel", onclick: () => { api("/api/batch/cancel", { method: "POST", body: {} }); closeModal(); } }),
      startButton,
    ),
  );
}

function pollJob(container, onDone) {
  clearInterval(state.jobTimer);
  state.jobTimer = setInterval(async () => {
    let payload = null;
    try { payload = await api("/api/job"); } catch { return; }
    const job = payload.job;
    if (!job) return;
    container.replaceChildren(
      el("div", { class: "row", style: { margin: "16px 0 8px" } },
        el("strong", { text: job.state === "running" ? "Working…" : `Finished (${job.state})` }),
        el("span", { class: "dim", text: `${job.done}/${job.total}` }),
      ),
      el("div", { class: "progress" }, el("div", { style: { width: `${job.total ? (job.done / job.total) * 100 : 0}%` } })),
      el("div", { style: { marginTop: "10px", maxHeight: "240px", overflowY: "auto" } },
        job.items.slice().reverse().map((item) =>
          el("div", { class: "job-item" },
            el("span", { class: `status-dot status-${item.status}` }),
            el("span", { style: { flex: 1, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }, text: item.name }),
            el("span", { class: "dim", text: item.status === "ok" ? (item.slots || []).join(", ") : (item.detail || item.status) }),
          )),
      ),
    );
    if (job.state !== "running") {
      clearInterval(state.jobTimer);
      state.jobTimer = null;
      if (onDone) onDone();
      const needsReview = job.items.filter((item) => item.status === "needs_review").length;
      if (needsReview) toast(`${needsReview} game(s) need a manual match - open a game and use “Change match”.`, "", 7000);
    }
  }, 600);
}

/* ---------------- orphans ---------------- */

async function renderOrphans(root) {
  root.append(el("h1", { text: "Orphaned artwork" }), el("div", { class: "sub", text: "Files left behind when the shortcut appid changed" }));
  const loading = el("div", { class: "center" }, el("div", { class: "spin" }), "Fingerprinting artwork…");
  root.append(loading);
  try {
    const data = await api("/api/orphans");
    state.orphans = data;
    loading.remove();
    const repairs = data.repair || [];
    if (repairs.length) {
      root.append(el("div", { class: "banner info" },
        el("div", { style: { flex: 1 } },
          el("b", { text: `${repairs.length} game(s) can get their artwork back` }),
          el("div", { class: "dim", text: repairs.map((item) => `${item.name} (${item.slots.length} slots)`).join(", ") }),
        ),
        el("button", {
          class: "btn primary", text: "Repair now",
          onclick: async (event) => {
            event.target.disabled = true;
            try {
              const result = await api("/api/orphans/repair", { method: "POST", body: {} });
              toast(`${result.actions.length} game(s) restored`, "ok");
              await refresh({ keepView: true });
              render();
            } catch (error) { toast(error.message, "err"); }
          },
        }),
      ));
    }
    if (!data.orphans.length) {
      root.append(el("div", { class: "center", text: "No orphaned artwork. Everything on disk belongs to a live shortcut." }));
      return;
    }
    const list = el("div");
    for (const orphan of data.orphans) list.append(orphanRow(orphan));
    root.append(el("h2", { text: `${data.orphans.length} groups` }), list);
  } catch (error) {
    loading.replaceChildren(el("div", { class: "banner err", text: error.message }));
  }
}

function orphanRow(orphan) {
  const sample = orphan.sample
    ? `/api/full?appid=${orphan.appid}&slot=${orphan.slots.portrait ? "portrait" : (orphan.slots.wide ? "wide" : "hero")}&token=${encodeURIComponent(TOKEN)}`
    : null;
  const games = state.games.slice().sort((a, b) => a.name.toLowerCase().localeCompare(b.name.toLowerCase()));
  const picker = el("select", {},
    el("option", { value: "", text: "Assign to game…" }),
    games.map((game) => el("option", { value: game.appid, text: `${game.name}${game.missing.length ? ` (${game.missing.length} missing)` : ""}` })),
  );
  picker.addEventListener("change", async () => {
    if (!picker.value) return;
    try {
      await api("/api/orphans/relink", { method: "POST", body: { from_appid: orphan.appid, to_appid: picker.value, slots: null, move: false } });
      toast("Artwork re-linked", "ok");
      await refresh({ keepView: true });
      render();
    } catch (error) { toast(error.message, "err"); }
  });
  const badges = [];
  if (orphan.guess) {
    badges.push(el("span", { class: "badge accent", text: orphan.guess_source === "boilr" ? `BoilR cache: ${orphan.guess}` : `guess: ${orphan.guess}` }));
  }
  if (orphan.duplicate_of) badges.push(el("span", { class: "badge", text: `duplicate of ${orphan.duplicate_of}` }));
  if (orphan.art_match && !orphan.guess_source) {
    badges.push(el("span", { class: "badge", text: `resembles ${orphan.art_match.name} (${orphan.art_match.slot})` }));
  }
  return el("div", { class: "orphan" },
    sample ? el("img", { src: sample, loading: "lazy" }) : el("img", {}),
    el("div", { class: "info" },
      el("div", { class: "title", text: orphan.guess || `appid ${orphan.appid}` }),
      el("div", { class: "sub2", text: `${orphan.files.length} files · ${fmtBytes(orphan.size)} · appid ${orphan.appid} · ${Object.keys(orphan.slots).length} slots` }),
      badges.length ? el("div", { class: "badges", style: { marginTop: "6px" } }, badges) : null,
    ),
    picker,
    el("button", {
      class: "btn small danger", text: "Delete",
      onclick: async () => {
        if (!confirm(`Move ${orphan.files.length} orphaned file(s) to GridCrate's trash?`)) return;
        try {
          await api("/api/orphans/delete", { method: "POST", body: { appids: [orphan.appid] } });
          toast("Moved to trash", "ok");
          await refresh({ keepView: true });
          render();
        } catch (error) { toast(error.message, "err"); }
      },
    }),
  );
}

/* ---------------- shortcuts ---------------- */

function renderShortcuts(root) {
  const steam = state.status.steam;
  root.append(el("h1", { text: "Steam shortcuts" }), el("div", { class: "sub", text: `shortcuts.vdf · ${state.games.length} entries` }));
  if (steam.running) {
    root.append(el("div", { class: "banner warn" },
      el("div", { style: { flex: 1 } },
        el("b", { text: "Steam is running" }),
        el("div", { class: "dim", text: "Steam rewrites shortcuts.vdf from memory when it quits, so changes made now would be lost. Close Steam to edit shortcuts." }),
      ),
      el("button", {
        class: "btn", text: "Close Steam",
        onclick: async () => {
          if (!confirm("Close Steam now? GridCrate will ask it to shut down and wait for it to exit.")) return;
          const result = await api("/api/steam/shutdown", { method: "POST", body: {} });
          toast(result.output, "ok");
          await refresh({ keepView: true });
          render();
        },
      }),
    ));
  }
  root.append(el("div", { class: "toolbar" },
    el("button", { class: "btn primary", text: "Add game", disabled: steam.running, onclick: () => openAddDialog() }),
    el("div", { class: "spacer" }),
    el("button", { class: "btn", text: "Re-scan", onclick: async () => { await api("/api/rescan", { method: "POST", body: {} }); await refresh({ keepView: true }); render(); } }),
  ));
  const table = el("table", {},
    el("thead", {}, el("tr", {},
      el("th", { text: "Name" }), el("th", { text: "appid" }), el("th", { text: "Executable" }),
      el("th", { text: "Launch options" }), el("th", { text: "Played" }), el("th", { text: "" }))),
    el("tbody", {}, state.games.map((game) => el("tr", {},
      el("td", {}, el("div", { style: { fontWeight: 600 }, text: game.name }), el("div", { class: "dim", style: { fontSize: "11px" }, text: game.hidden ? "hidden" : "" })),
      el("td", { class: "mono", text: game.appid }),
      el("td", { class: "mono", text: game.exe }),
      el("td", { class: "mono", text: game.launch_options || "-" }),
      el("td", { class: "dim", text: fmtDate(game.last_play) }),
      el("td", { class: "actions" },
        el("button", { class: "btn small", text: "Artwork", onclick: () => openDetail(game.appid) }),
        el("button", { class: "btn small", text: "Edit", disabled: steam.running, onclick: () => openEditDialog(game) }),
        el("button", {
          class: "btn small danger", text: "Delete", disabled: steam.running,
          onclick: async () => {
            if (!confirm(`Remove the shortcut “${game.name}” from Steam? The artwork stays on disk.`)) return;
            try {
              await api("/api/shortcuts/remove", { method: "POST", body: { index: game.index } });
              toast("Shortcut removed", "ok");
              await refresh({ keepView: true });
              render();
            } catch (error) { toast(error.message, "err"); }
          },
        }),
      ),
    ))),
  );
  root.append(table);
}

function openAddDialog() {
  const name = el("input", { type: "text", placeholder: "Game name" });
  const exe = el("input", { type: "text", placeholder: "/path/to/executable or %command%" });
  const options = el("input", { type: "text", placeholder: "launch options (optional)" });
  const startDir = el("input", { type: "text", placeholder: "start directory (optional)" });
  const faugusSelect = el("select", {});
  faugusSelect.append(el("option", { value: "", text: "choose a Faugus game…" }));
  api("/api/faugus/games").then((payload) => {
    for (const game of payload.games) faugusSelect.append(el("option", { value: game.gameid, text: game.title || game.gameid }));
  }).catch(() => {});
  openModal(
    el("div", { class: "modal-head" }, el("strong", { text: "Add a non-Steam shortcut" }), el("div", { class: "spacer" }), el("button", { class: "btn ghost", text: "✕", onclick: closeModal })),
    el("div", { class: "modal-body", style: { width: "560px" } },
      el("div", { class: "card" },
        el("h3", { text: "From Faugus Launcher" }),
        el("div", { class: "hint", text: "Creates exactly the shortcut Faugus would: same executable, same launch options, same icon - so the appid and artwork line up." }),
        faugusSelect,
        el("button", {
          class: "btn primary", style: { marginTop: "12px" }, text: "Add from Faugus",
          onclick: async () => {
            if (!faugusSelect.value) return toast("Pick a Faugus game first", "err");
            try {
              const result = await api("/api/shortcuts/import_faugus", { method: "POST", body: { gameid: faugusSelect.value } });
              toast(`Added ${result.name}`, "ok");
              closeModal();
              await refresh({ keepView: true });
              render();
            } catch (error) { toast(error.message, "err"); }
          },
        }),
      ),
      el("div", { class: "card" },
        el("h3", { text: "Manual" }),
        el("div", { class: "hint", text: "Any executable - a native game, a script, an emulator." }),
        el("div", { style: { display: "grid", gap: "10px" } },
          el("label", { class: "field" }, "Name", name),
          el("label", { class: "field" }, "Executable", exe),
          el("label", { class: "field" }, "Launch options", options),
          el("label", { class: "field" }, "Start directory", startDir),
        ),
        el("button", {
          class: "btn", style: { marginTop: "12px" }, text: "Add shortcut",
          onclick: async () => {
            if (!name.value.trim() || !exe.value.trim()) return toast("Name and executable are required", "err");
            try {
              await api("/api/shortcuts/add", { method: "POST", body: { name: name.value, exe: exe.value, launch_options: options.value, start_dir: startDir.value } });
              toast("Shortcut added", "ok");
              closeModal();
              await refresh({ keepView: true });
              render();
            } catch (error) { toast(error.message, "err"); }
          },
        }),
      ),
    ),
  );
}

function openEditDialog(game) {
  const name = el("input", { type: "text", value: game.name });
  const exe = el("input", { type: "text", value: game.exe });
  const options = el("input", { type: "text", value: game.launch_options || "" });
  const startDir = el("input", { type: "text", value: game.start_dir || "" });
  const hidden = el("input", { type: "checkbox", checked: game.hidden });
  openModal(
    el("div", { class: "modal-head" }, el("strong", { text: `Edit ${game.name}` }), el("div", { class: "spacer" }), el("button", { class: "btn ghost", text: "✕", onclick: closeModal })),
    el("div", { class: "modal-body", style: { width: "560px" } },
      el("div", { style: { display: "grid", gap: "10px" } },
        el("label", { class: "field" }, "Name", name),
        el("label", { class: "field" }, "Executable", exe),
        el("label", { class: "field" }, "Launch options", options),
        el("label", { class: "field" }, "Start directory", startDir),
        el("label", { class: "check" }, hidden, "Hidden in the library"),
      ),
      el("div", { class: "hint", style: { marginTop: "12px" }, text: "Changing the name or executable changes the appid - GridCrate will move the artwork along automatically." }),
    ),
    el("div", { class: "modal-foot" },
      el("div", { class: "spacer" }),
      el("button", { class: "btn", text: "Cancel", onclick: closeModal }),
      el("button", {
        class: "btn primary", text: "Save",
        onclick: async () => {
          try {
            await api("/api/shortcuts/update", {
              method: "POST",
              body: { index: game.index, AppName: name.value, Exe: exe.value, LaunchOptions: options.value, StartDir: startDir.value, hidden: hidden.checked },
            });
            toast("Shortcut updated", "ok");
            closeModal();
            await refresh({ keepView: true });
            render();
          } catch (error) { toast(error.message, "err"); }
        },
      }),
    ),
  );
}

/* ---------------- settings ---------------- */

function renderSettings(root) {
  const config = state.config;
  const steam = state.status.steam;
  const apiKey = el("input", { type: "password", value: config.api_key || "", placeholder: "SteamGridDB API key", style: { width: "340px" } });
  const accountSelect = select({
    value: steam.account_id || "", options: steam.accounts.map((account) => [account.account_id, `${account.persona} (${account.account_id}) · ${account.shortcut_count} shortcuts`]),
    onchange: async (value) => { await api("/api/config", { method: "POST", body: { account_id: value } }); await refresh({ keepView: true }); render(); },
  });
  const toggle = (key, label, hint) => el("label", { class: "check", style: { marginBottom: "10px", alignItems: "flex-start" } },
    el("input", {
      type: "checkbox", checked: !!config[key], style: { marginTop: "2px" },
      onchange: async (event) => {
        config[key] = event.target.checked;
        await api("/api/config", { method: "POST", body: { [key]: event.target.checked } });
        toast("Saved", "ok", 1500);
      },
    }),
    el("span", {}, el("b", { text: label }), hint ? el("div", { class: "dim", text: hint }) : null),
  );
  root.append(
    el("h1", { text: "Settings" }),
    el("div", { class: "sub", text: "Everything is stored locally in ~/.config/gridcrate" }),
    el("div", { class: "card" },
      el("h3", { text: "SteamGridDB" }),
      el("div", { class: "hint", text: "GridCrate reads the key BoilR/Faugus already use. Get one at steamgriddb.com/profile/preferences/api" }),
      el("div", { class: "row" }, apiKey,
        el("button", {
          class: "btn", text: "Test",
          onclick: async (event) => {
            event.target.disabled = true;
            try {
              const result = await api("/api/test-key", { method: "POST", body: { api_key: apiKey.value } });
              toast(result.ok ? "API key works" : `Key rejected: ${result.message}`, result.ok ? "ok" : "err");
            } catch (error) { toast(error.message, "err"); }
            event.target.disabled = false;
          },
        }),
        el("button", {
          class: "btn primary", text: "Save key",
          onclick: async () => {
            await api("/api/config", { method: "POST", body: { api_key: apiKey.value } });
            toast("API key saved", "ok");
            await refresh({ keepView: true });
          },
        }),
      ),
    ),
    el("div", { class: "card" },
      el("h3", { text: "IGDB (optional)" }),
      el("div", { class: "hint", text: "Extra source for covers, artworks and screenshots. Create a free app at dev.twitch.tv/console/apps and paste the client id and secret." }),
      el("div", { class: "row" },
        el("input", { type: "text", placeholder: "Twitch client id", id: "igdb-id", value: config.igdb_client_id || "", style: { width: "280px" } }),
        el("input", { type: "password", placeholder: "client secret", id: "igdb-secret", value: config.igdb_client_secret || "", style: { width: "220px" } }),
        el("button", {
          class: "btn primary", text: "Save",
          onclick: async () => {
            await api("/api/config", { method: "POST", body: {
              igdb_client_id: $("#igdb-id").value.trim(),
              igdb_client_secret: $("#igdb-secret").value.trim(),
            } });
            toast("IGDB credentials saved", "ok");
            await refresh({ keepView: true });
          },
        }),
      ),
    ),
    el("div", { class: "card" },
      el("h3", { text: "Sources" }),
      el("div", { class: "hint", text: "Available artwork sources. Web search uses Bing (safe search on) because Google blocks apps from reading its results; the “Open Google Images” button opens the search in your browser and you can drag the image back into the app." }),
      el("div", { class: "filters" },
        state.sources.map((source) => el("span", {
          class: `chip ${source.available ? "active" : ""}`,
          title: source.note,
          text: `${source.label}${source.available ? "" : " — " + source.reason}`,
        })),
      ),
    ),
    el("div", { class: "card" },
      el("h3", { text: "Steam account" }),
      el("div", { class: "hint", text: "Artwork is written to the selected account's userdata folder." }),
      el("div", { class: "row" }, accountSelect),
      el("div", { class: "row", style: { marginTop: "12px" } },
        el("button", { class: "btn", text: "Open grid folder", onclick: () => api("/api/open", { method: "POST", body: { target: "grid" } }) }),
        el("button", { class: "btn", text: "Open trash folder", onclick: () => api("/api/open", { method: "POST", body: { target: "trash" } }) }),
        el("button", { class: "btn", text: "Open Faugus covers", onclick: () => api("/api/open", { method: "POST", body: { target: "covers" } }) }),
      ),
    ),
    el("div", { class: "card" },
      el("h3", { text: "Behaviour" }),
      toggle("auto_repair", "Restore artwork automatically on launch", "When a shortcut's appid changes, the old files are left behind - GridCrate detects this (via its appid history and BoilR's cache) and copies the artwork to the new appid."),
      toggle("faugus_integration", "Mirror artwork into Faugus", "Writes cover, banner and icon into Faugus Launcher's own folders (460x690 / 1920x620 / 256px), matching what its own picker does."),
      toggle("trash_after_relink", "Move orphan files after re-linking", "Otherwise the originals are kept, so a wrong guess costs nothing."),
      toggle("include_nsfw", "Allow NSFW results in bulk fetch", "Manual browsing always has its own toggle."),
      toggle("include_humor", "Allow humorous results in bulk fetch"),
    ),
    el("div", { class: "card" },
      el("h3", { text: "Steam" }),
      el("div", { class: "hint", text: `Currently ${steam.running ? "running" : "closed"}. Artwork appears immediately; if the library shows a stale image, restart Steam. Shortcut edits require Steam to be closed.` }),
      el("div", { class: "row" },
        el("button", { class: "btn", text: "Close Steam", onclick: async () => { const r = await api("/api/steam/shutdown", { method: "POST", body: {} }); toast(r.output, "ok"); await refresh({ keepView: true }); render(); } }),
        el("button", { class: "btn", text: "Restart Steam", onclick: async () => { if (!confirm("Restart Steam now?")) return; await api("/api/steam/restart", { method: "POST", body: {} }); toast("Steam restarting", "ok"); } }),
      ),
    ),
    el("div", { class: "card" },
      el("h3", { text: "Backups" }),
      el("div", { class: "hint", text: "shortcuts.vdf is copied before every change (last 10 kept)." }),
      el("div", { id: "backups", class: "dim", text: "loading…" }),
    ),
  );
  api("/api/backups").then((payload) => {
    const box = $("#backups");
    if (!box) return;
    box.replaceChildren(...(payload.backups.length
      ? payload.backups.map((backup) => el("div", { class: "job-item" },
          el("span", { style: { flex: 1 }, text: backup.name }),
          el("span", { class: "dim", text: `${fmtBytes(backup.size)} · ${new Date(backup.mtime * 1000).toLocaleString()}` })))
      : [el("div", { text: "No backups yet." })]));
  }).catch(() => {});
}

/* ---------------- modal ---------------- */

function openModal(...children) {
  const back = el("div", { class: "modal-back", onclick: (event) => { if (event.target === back) closeModal(); } },
    el("div", { class: "modal" }, ...children));
  $("#modal-root").replaceChildren(back);
}
function closeModal() { $("#modal-root").replaceChildren(); }

boot();
