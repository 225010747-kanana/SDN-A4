"use strict";

const $ = id => document.getElementById(id);
const say = (id, text) => { $(id).textContent = text; };

const store = {
  get(key, fallback) { try { const v = localStorage.getItem(key); return v ? JSON.parse(v) : fallback; } catch (e) { return fallback; } },
  set(key, value) { try { localStorage.setItem(key, JSON.stringify(value)); return true; } catch (e) { return false; } },
  del(key) { try { localStorage.removeItem(key); } catch (e) {  } }
};

function h(tag, attrs, ...kids) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (k === "class") el.className = v;
    else if (k.startsWith("on")) el.addEventListener(k.slice(2), v);
    else if (v !== false && v !== undefined) el.setAttribute(k, v === true ? "" : v);
  }
  kids.flat().forEach(kid => el.append(kid));
  return el;
}

let token = sessionStorage.getItem("stm_token");
const isOnline = () => !$("sim").checked && navigator.onLine !== false;

async function api(path, method = "GET", body) {
  if (!isOnline()) throw new Error("offline");
  const headers = {"Content-Type": "application/json"};
  if (token) headers.Authorization = "Bearer " + token;
  const response = await fetch(path, {method, headers, body: body ? JSON.stringify(body) : undefined});
  const isJson = (response.headers.get("Content-Type") || "").includes("json");
  return {status: response.status, data: isJson ? await response.json() : await response.text()};
}

const LABELS = ["L01", "L02", "L03", "L04", "L05", "L06", "L07", "L08"];
const pad2 = n => String(n).padStart(2, "0");

function parseSlot(text) {
  const m = /^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2})$/.exec(text || "");
  if (!m) return null;
  const d = new Date(+m[1], m[2] - 1, +m[3], +m[4], +m[5]);
  return d.getDate() === +m[3] && d.getMonth() === +m[2] - 1 ? d : null;
}
const formatSlot = d => `${d.getFullYear()}-${pad2(d.getMonth() + 1)}-${pad2(d.getDate())}T${pad2(d.getHours())}:${pad2(d.getMinutes())}`;

function validatePlan(p) {
  const e = {};
  const title = (p.title || "").trim();
  if (!title) e.title = "Enter a plan title, for example 'Bridge day'.";
  else if (title.length > 60) e.title = "Shorten the title to 60 characters or fewer.";
  if (!p.activity_id) e.activity_id = "Choose an activity from the list.";
  if (!parseSlot(p.slot_start)) e.slot_start = "Enter the start as date and time, for example 2026-11-10T09:00.";
  if (!Number.isInteger(p.duration_min) || p.duration_min < 10 || p.duration_min > 180) e.duration_min = "Enter a duration between 10 and 180 minutes.";
  if (!p.learners.length) e.learners = "Add at least one learner label, L01 to L08.";
  return e;
}

function findClash(plans, plan) {
  const start = parseSlot(plan.slot_start), end = new Date(start.getTime() + plan.duration_min * 60000);
  for (const other of plans) {
    if (other.id === plan.id) continue;
    const s = parseSlot(other.slot_start), e = new Date(s.getTime() + other.duration_min * 60000);
    if (start < e && s < end) return {title: other.title, freeFrom: formatSlot(e)};
  }
  return null;
}

function filterSaved(list, f) {
  return list.filter(a => (!f.level || a.level === f.level) && (!f.topic || a.topic === f.topic) &&
    (!f.materials || a.materials === f.materials) && (!f.max_minutes || a.minutes <= Number(f.max_minutes)) && (!f.offline_only || a.offline));
}

let syncing = false;
const firstError = data => Object.values((data && data.errors) || {})[0] || (data && data.error) || "Rejected by server.";

function refreshStatus() {
  const plans = store.get("plans", []);
  const waiting = plans.filter(p => p.sync === "pending").length, bad = plans.filter(p => p.sync === "fix").length;
  say("conn", (isOnline() ? "Status: online." : "Status: offline. You can keep working. Changes stay on this device and are sent when you are back online.") +
    ` ${waiting} waiting to sync${bad ? `, ${bad} need attention` : ""}.`);
}

async function syncAll() {
  if (syncing) return;
  if (!token || !isOnline()) { refreshStatus(); return; }
  syncing = true;
  const plans = store.get("plans", []);
  for (const p of plans) {
    if (p.sync !== "pending") continue;
    const payload = {...p}; delete payload.sync; delete payload.msg;
    try {
      const r = await api("/api/plans", "POST", payload);
      if (r.status === 200) { p.sync = "synced"; p.msg = ""; }
      else if ([400, 409, 422].includes(r.status)) { p.sync = "fix"; p.msg = firstError(r.data); }
      else if (r.status === 401) { say("result", "Your session ended. Log in again to send waiting plans."); break; }
    } catch (e) { break; }
  }
  store.set("plans", plans);
  try {
    const r = await api("/api/plans");
    if (r.status === 200) {
      const local = store.get("plans", []);
      r.data.forEach(sp => {
        const i = local.findIndex(p => p.id === sp.id);
        if (i < 0) local.push({...sp, sync: "synced"});
        else if (local[i].sync === "synced") local[i] = {...sp, sync: "synced"};
      });
      store.set("plans", local);
    }
  } catch (e) {  }
  syncing = false;
  refreshStatus();
  if (typeof renderSchedule === "function") renderSchedule();
}

function currentFilters() {
  return {level: $("f-level").value, topic: $("f-topic").value, materials: $("f-mats").value,
          max_minutes: $("f-dur").value, offline_only: $("f-off").checked ? "1" : ""};
}

async function loadCatalogue() {
  try { const r = await api("/api/activities"); if (r.status === 200) store.set("catalogue", r.data); } catch (e) {  }
  try { const r = await api("/api/saved"); if (r.status === 200) store.set("saved_activities", r.data); } catch (e) {  }
}

async function drawFind() {
  const filters = currentFilters(), saved = store.get("saved_activities", []);
  let list = null, note = "";
  if (token && isOnline()) {
    const query = Object.entries(filters).filter(([, v]) => v).map(([k, v]) => `${k}=${encodeURIComponent(v)}`).join("&");
    try { const r = await api("/api/activities?" + query); if (r.status === 200) list = r.data; } catch (e) {  }
  }
  if (!list) { list = filterSaved(saved, filters); note = " You are offline, so only saved activities are shown."; }
  const savedIds = saved.map(a => a.id);
  $("results").replaceChildren(h("p", {class: "hint"}, `${list.length} result${list.length === 1 ? "" : "s"}.${note}`),
    ...list.map(a => activityCard(a, savedIds.includes(a.id))));
  $("saved").replaceChildren(...(saved.length
    ? saved.map(a => h("p", {}, `${a.title} (${a.minutes} min) - opens without internet`))
    : [h("p", {class: "hint"}, "Nothing saved yet.")]));
}

function activityCard(a, isSaved) {
  const note = h("span", {role: "status"}, isSaved ? "Saved for offline use." : "");
  const button = h("button", {type: "button", class: "alt", disabled: isSaved, onclick: async () => {
    if (!isOnline()) { note.textContent = "You need internet to save an activity."; return; }
    note.textContent = "Downloading..."; button.disabled = true;
    try {
      const r = await api("/api/saved", "POST", {activity_id: a.id});
      if (r.status === 200) { store.set("saved_activities", [...store.get("saved_activities", []).filter(x => x.id !== a.id), a]); drawFind(); }
      else { note.textContent = r.data.error || "Could not save."; button.disabled = false; }
    } catch (e) { note.textContent = "Could not reach the server. Try again."; button.disabled = false; }
  }}, isSaved ? "Saved" : "Save for offline");
  return h("div", {class: "card"}, h("h3", {}, a.title),
    h("span", {class: "tag"}, a.level), h("span", {class: "tag"}, `${a.minutes} min`), h("span", {class: "tag"}, a.topic),
    h("span", {class: "tag"}, a.materials), h("span", {class: "tag"}, a.offline ? "Works offline" : "Needs internet"),
    h("details", {}, h("summary", {}, "Details"), h("p", {}, a.steps)), button, note);
}

function initFind() {
  ["f-level", "f-topic", "f-mats", "f-dur", "f-off"].forEach(id => $(id).addEventListener("change", drawFind));
}

const FIELDS = {title: ["title", "e-title"], activity_id: ["activity", "e-activity"], slot_start: ["slot", "e-slot"],
                duration_min: ["dur", "e-dur"], learners: ["lr-L01", "e-learners"]};
let draftTimer = null;
const newId = () => "p" + Date.now().toString(36) + Math.random().toString(36).slice(2, 6);

function readForm() {
  const minutes = parseInt($("dur").value, 10);
  return {title: $("title").value, activity_id: $("activity").value, slot_start: $("slot").value,
          duration_min: Number.isNaN(minutes) ? null : minutes, learners: LABELS.filter(l => $("lr-" + l).checked)};
}
function fillForm(p) {
  $("title").value = p.title || ""; $("activity").value = p.activity_id || ""; $("slot").value = p.slot_start || "";
  $("dur").value = p.duration_min ?? ""; LABELS.forEach(l => { $("lr-" + l).checked = (p.learners || []).includes(l); });
}

function showErrors(errors) {
  Object.values(FIELDS).forEach(([input, msg]) => { $(msg).hidden = true; $(msg).textContent = ""; $(input).removeAttribute("aria-invalid"); });
  const keys = Object.keys(errors).filter(k => FIELDS[k]), summary = $("err-summary");
  summary.replaceChildren();
  summary.hidden = !keys.length;
  if (!keys.length) return;
  summary.append(h("p", {}, `${keys.length} problem${keys.length > 1 ? "s" : ""} to fix before saving:`),
    h("ul", {}, keys.map(k => {
      const [input, msg] = FIELDS[k];
      $(msg).textContent = errors[k]; $(msg).hidden = false; $(input).setAttribute("aria-invalid", "true");
      return h("li", {}, h("a", {href: "#" + input, onclick: ev => { ev.preventDefault(); $(input).focus(); }}, errors[k]));
    })));
  summary.focus();
}

function fillActivityOptions() {
  const select = $("activity"), keep = select.value;
  select.replaceChildren(h("option", {value: ""}, "Choose an activity"),
    ...store.get("catalogue", []).map(a => h("option", {value: a.id}, `${a.title} (${a.minutes} min)`)));
  select.value = keep;
}

function restoreDraft() {
  const draft = store.get("draft", null);
  if (draft) { fillForm(draft); say("draft-note", "Draft restored from this device. Check it and save when ready."); }
}

function scheduleDraft() {
  clearTimeout(draftTimer);
  draftTimer = setTimeout(() => {
    if (!$("lr-L01")) return;
    const old = store.get("draft", {});
    const ok = store.set("draft", {...readForm(), id: old.id || newId(), attendance: old.attendance || {}});
    say("draft-note", ok ? "Draft saved on this device." : "Warning: this device is full or blocks storage, so the draft could not be saved. Save the plan now.");
  }, 400);
}

async function onSavePlan(event) {
  event.preventDefault();
  const form = readForm(), errors = validatePlan(form);
  showErrors(errors);
  if (Object.keys(errors).length) return;
  const draft = store.get("draft", {}), plans = store.get("plans", []);
  const plan = {...form, title: form.title.trim(), id: draft.id || newId(), attendance: draft.attendance || {},
                saved_at: new Date().toISOString(), sync: "pending"};
  const clash = findClash(plans, plan);
  if (clash) { showErrors({slot_start: `This time clashes with '${clash.title}'. Choose a start at or after ${clash.freeFrom}.`}); return; }
  if (!store.set("plans", [...plans.filter(p => p.id !== plan.id), plan])) { say("result", "Could not save on this device (storage full or blocked)."); return; }
  store.del("draft"); fillForm({}); say("draft-note", ""); $("title").focus();
  say("result", isOnline() ? "Plan saved. Sending..." : "Plan saved on this device. It will be sent when you are back online.");
  await syncAll();
  const after = store.get("plans", []).find(p => p.id === plan.id);
  if (after && after.sync === "fix") say("result", "The server did not accept this plan. See My schedule.");
  else if (after && after.sync === "synced") say("result", "Plan saved and synced.");
}

function initPlan() {
  $("learners").replaceChildren(...LABELS.map(l => h("label", {}, h("input", {type: "checkbox", id: "lr-" + l, value: l}), l)));
  $("plan-form").addEventListener("input", scheduleDraft);
  $("plan-form").addEventListener("submit", onSavePlan);
  $("discard").addEventListener("click", () => { store.del("draft"); fillForm({}); showErrors({}); say("draft-note", "Draft discarded."); $("title").focus(); });
}

let attendanceFor = null;

function statusText(p) {
  if (p.sync === "fix") return "Needs attention: " + p.msg;
  return p.sync === "pending" ? "Pending sync (saved on this device)" : "Synced";
}

function renderSchedule() {
  const plans = store.get("plans", []).sort((a, b) => a.slot_start.localeCompare(b.slot_start));
  const body = document.querySelector("#plans tbody");
  body.replaceChildren(...plans.map(p => {
    const present = Object.values(p.attendance || {}).filter(a => a.present).length;
    const actions = [h("button", {type: "button", class: "alt", onclick: () => openAttendance(p.id)}, "Attendance")];
    if (p.sync === "fix") actions.push(h("button", {type: "button", class: "alt", onclick: () => fixAndResend(p.id)}, "Fix and resend"));
    return h("tr", {}, ...[p.title, p.slot_start.replace("T", " "), String(p.duration_min), p.learners.join(" "),
      `${present} of ${p.learners.length} present`, statusText(p)].map(t => h("td", {}, t)), h("td", {}, actions));
  }));
  const waiting = plans.filter(p => p.sync === "pending").length;
  say("pending", plans.length ? `${waiting} plan${waiting === 1 ? "" : "s"} waiting to sync.` : "No plans yet. Use Plan a session.");
  $("retry").hidden = !waiting;
}

function fixAndResend(id) {
  const plans = store.get("plans", []), plan = plans.find(p => p.id === id);
  store.set("draft", plan); store.set("plans", plans.filter(p => p.id !== id));
  go("plan");
}

function openAttendance(id) {
  attendanceFor = id;
  const plan = store.get("plans", []).find(p => p.id === id);
  $("att-h").textContent = `Attendance: ${plan.title}`;
  $("att-rows").replaceChildren(...plan.learners.map(l => {
    const a = (plan.attendance || {})[l] || {present: false, level: "Medium"};
    return h("div", {class: "row"}, h("strong", {}, l),
      h("label", {}, h("input", {type: "checkbox", id: "pr-" + l, checked: a.present}), " Present"),
      h("label", {for: "pa-" + l}, "Participation"),
      h("select", {id: "pa-" + l}, ["Low", "Medium", "High"].map(v => h("option", {value: v, selected: v === a.level}, v))));
  }));
  $("att-panel").hidden = false; $("att-h").focus();
}

function saveAttendance() {
  const plans = store.get("plans", []), plan = plans.find(p => p.id === attendanceFor);
  plan.attendance = {};
  plan.learners.forEach(l => { plan.attendance[l] = {present: $("pr-" + l).checked, level: $("pa-" + l).value}; });
  plan.sync = "pending"; plan.saved_at = new Date().toISOString();
  store.set("plans", plans);
  $("att-panel").hidden = true; attendanceFor = null;
  renderSchedule(); syncAll();
}

async function exportCsv() {
  try {
    const r = await api("/api/export.csv");
    if (r.status !== 200) throw new Error();
    const link = h("a", {href: URL.createObjectURL(new Blob([r.data], {type: "text/csv"})), download: "stemmate-plans.csv"});
    link.click(); say("export-note", "Export started.");
  } catch (e) { say("export-note", "Export needs a connection. Try again when you are online."); }
}

function initSchedule() {
  $("att-save").addEventListener("click", saveAttendance);
  $("att-cancel").addEventListener("click", () => { $("att-panel").hidden = true; attendanceFor = null; });
  $("retry").addEventListener("click", syncAll);
  $("export").addEventListener("click", exportCsv);
}

const VIEWS = {login: "login-view", find: "find-view", plan: "plan-view", sched: "sched-view"};
let currentView = "login";

function go(name) {
  currentView = name;
  Object.entries(VIEWS).forEach(([key, id]) => { $(id).hidden = key !== name; });
  $("nav").hidden = name === "login";
  document.querySelectorAll("[data-go]").forEach(b => b.setAttribute("aria-current", b.dataset.go === name ? "page" : "false"));
  if (name === "find") drawFind();
  if (name === "plan") { fillActivityOptions(); restoreDraft(); }
  if (name === "sched") renderSchedule();
  const heading = $(VIEWS[name]).querySelector("h2");
  heading.tabIndex = -1; heading.focus();
}

async function onLogin() {
  const err = $("login-err"); err.hidden = true;
  if (!isOnline()) { err.textContent = "You are offline. Logging in needs a connection the first time."; err.hidden = false; return; }
  try {
    const r = await api("/api/login", "POST", {facilitator_id: $("fid").value, password: $("pw").value});
    if (r.status !== 200) { err.textContent = r.data.error; err.hidden = false; $("fid").focus(); return; }
    token = r.data.token; sessionStorage.setItem("stm_token", token); $("pw").value = "";
    await loadCatalogue(); go("find"); syncAll();
  } catch (e) { err.textContent = "Could not reach the server. Check your connection and try again."; err.hidden = false; }
}

async function onLogout() {
  const waiting = store.get("plans", []).filter(p => p.sync !== "synced").length;
  if (waiting && !confirm(`${waiting} plan(s) have not synced and will be deleted from this device if you log out. Log out anyway?`)) return;
  try { await api("/api/logout", "POST", {}); } catch (e) {  }
  ["draft", "plans", "saved_activities", "catalogue"].forEach(k => store.del(k));
  sessionStorage.removeItem("stm_token"); location.reload();
}

function onConnectionChange() { refreshStatus(); syncAll(); if (currentView === "find") drawFind(); }

initFind(); initPlan(); initSchedule();
document.querySelectorAll("[data-go]").forEach(b => b.addEventListener("click", () => go(b.dataset.go)));
$("login-btn").addEventListener("click", onLogin);
$("pw").addEventListener("keydown", e => { if (e.key === "Enter") onLogin(); });
$("logout").addEventListener("click", onLogout);
$("sim").addEventListener("change", onConnectionChange);
addEventListener("online", onConnectionChange);
addEventListener("offline", onConnectionChange);
if ("serviceWorker" in navigator) navigator.serviceWorker.register("/sw.js").catch(() => {  });
if (token) { loadCatalogue().then(() => { go("find"); syncAll(); }); } else { go("login"); refreshStatus(); }
