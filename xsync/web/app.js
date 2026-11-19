"use strict";

const $ = (selector, scope = document) => scope.querySelector(selector);
const incoming = new URLSearchParams(location.hash.slice(1));
const token = incoming.get("token") || (location.hash.includes("=") ? "" : location.hash.slice(1)) ||
  sessionStorage.getItem("xsync-token") || "";
if (token) sessionStorage.setItem("xsync-token", token);
if (location.hash) history.replaceState(null, "", location.pathname);

const LOCAL = "";
const UNSET = null;
const sides = ["left", "right"];
const workspaces = [];
let config = {hosts: [], home: ""};
let active;
let nextWorkspace = 1;
let jobs = [];
let noticeTimer;

function element(tag, className, content) {
  const item = document.createElement(tag);
  if (className) item.className = className;
  if (content !== undefined) item.textContent = content;
  return item;
}

function button(label, className, action, description) {
  const item = element("button", className, label);
  item.type = "button";
  if (description) {
    item.title = description;
    item.setAttribute("aria-label", description);
  }
  item.addEventListener("click", action);
  return item;
}

async function api(path, body = {}) {
  const response = await fetch("/api/" + path, {
    method: "POST",
    headers: {"Content-Type": "application/json", "Authorization": "Bearer " + token},
    body: JSON.stringify(body),
  });
  const result = await response.json();
  if (!response.ok) throw new Error(result.error || response.statusText);
  return result;
}

function notice(message) {
  const box = $("#notice");
  clearTimeout(noticeTimer);
  box.textContent = message;
  box.hidden = false;
  noticeTimer = setTimeout(() => { box.hidden = true; }, 4200);
}

function host(id) {
  return config.hosts.find(item => item.id === id);
}

function endpointName(id) {
  return id === LOCAL ? "This computer" : host(id)?.name ?? "Choose a connection";
}

function paneState(id, path = "") {
  return {id, path, listing: null, selected: null, error: "", request: 0, busy: false};
}

function newWorkspace(initial = {}) {
  if (workspaces.length === 16) {
    notice("Close a workspace before opening another.");
    return;
  }
  const workspace = {
    id: initial.id || crypto.randomUUID(),
    name: initial.name || "Workspace " + nextWorkspace++,
    left: paneState(initial.left?.id ?? LOCAL, initial.left?.path),
    right: paneState(initial.right?.id ?? UNSET, initial.right?.path),
  };
  workspaces.push(workspace);
  showWorkspace(workspace);
}

function showWorkspace(workspace) {
  active = workspace;
  renderWorkspace();
  for (const side of sides) {
    const pane = workspace[side];
    if (pane.id !== UNSET && !pane.listing && !pane.busy && !pane.error) {
      loadFolder(workspace, side, pane.path);
    }
  }
}

function closeWorkspace(workspace) {
  const index = workspaces.indexOf(workspace);
  workspaces.splice(index, 1);
  if (!workspaces.length) newWorkspace();
  else if (active === workspace) showWorkspace(workspaces[Math.max(0, index - 1)]);
  else renderTabs();
}

function renderTabs() {
  const tabs = $("#tabs");
  tabs.replaceChildren();
  for (const workspace of workspaces) {
    const tab = element("div", "tab" + (workspace === active ? " current" : ""));
    const open = button(workspace.name, "", () => showWorkspace(workspace));
    open.setAttribute("role", "tab");
    open.setAttribute("aria-selected", workspace === active);
    tab.append(open, button("×", "icon", () => closeWorkspace(workspace), "Close " + workspace.name));
    tabs.append(tab);
  }
}

function changeEndpoint(side, id) {
  active[side] = paneState(id);
  renderWorkspace();
  if (id !== UNSET) loadFolder(active, side);
}

function renderHosts() {
  const list = $("#hosts");
  list.replaceChildren();
  if (!config.hosts.length) {
    list.append(element("p", "host-empty", "No saved connections"));
    return;
  }
  for (const item of config.hosts) {
    const row = element("div", "host-row" + (sides.some(side => active[side].id === item.id) ? " active" : ""));
    const select = button("", "host-select", () => changeEndpoint("right", item.id));
    select.title = item.user + "@" + item.host + ":" + item.port;
    const dot = element("span", "host-dot");
    dot.setAttribute("aria-hidden", "true");
    select.append(dot, element("span", "", item.name));
    row.append(select, button("⋯", "icon", () => editHost(item), "Edit " + item.name));
    list.append(row);
  }
}

async function loadFolder(workspace, side, path = "") {
  const pane = workspace[side];
  if (pane.id === UNSET) return;
  const request = ++pane.request;
  pane.path = path;
  pane.busy = true;
  pane.error = "";
  pane.listing = null;
  pane.selected = null;
  if (workspace === active) renderPane(side);
  try {
    const result = await api("list", {host: pane.id, path});
    if (pane.request !== request || workspace[side] !== pane) return;
    pane.path = result.path;
    pane.listing = result;
  } catch (error) {
    if (pane.request !== request || workspace[side] !== pane) return;
    pane.error = error.message;
  } finally {
    if (pane.request === request && workspace[side] === pane) {
      pane.busy = false;
      if (workspace === active) renderPane(side);
    }
  }
}

function bytes(value) {
  if (value < 1024) return value + " B";
  const power = Math.min(4, Math.floor(Math.log(value) / Math.log(1024)));
  return (value / 1024 ** power).toFixed(1) + " " + ["B", "KiB", "MiB", "GiB", "TiB"][power];
}

function emptyState(title, detail) {
  const block = element("div", "empty");
  block.append(element("strong", "", title));
  if (detail) block.append(element("span", "", detail));
  return block;
}

function renderPane(side) {
  const workspace = active;
  const pane = workspace[side];
  const target = $("#" + side);
  const item = host(pane.id);
  target.classList.toggle("remote", !!item);
  target.replaceChildren($("#pane-template").content.cloneNode(true));
  $(".pane-identity strong", target).textContent = endpointName(pane.id);
  $(".pane-identity small", target).textContent = item ? item.user + "@" + item.host + ":" + item.port :
    (pane.id === LOCAL ? "Local filesystem" : "Remote endpoint");
  const picker = $(".endpoint", target);
  picker.setAttribute("aria-label", side + " endpoint");
  picker.append(new Option("Choose endpoint", "unset"), new Option("This computer", LOCAL));
  for (const item of config.hosts) picker.append(new Option(item.name, item.id));
  picker.value = pane.id ?? "unset";
  picker.addEventListener("change", () => changeEndpoint(side, picker.value === "unset" ? UNSET : picker.value));
  const path = $(".path", target);
  path.setAttribute("aria-label", side + " folder path");
  path.value = pane.path;
  path.disabled = pane.id === UNSET;
  const submit = () => loadFolder(workspace, side, path.value);
  $(".navigation", target).addEventListener("submit", event => {
    event.preventDefault();
    submit();
  });
  const up = $(".up", target);
  up.disabled = !pane.listing || pane.listing.parent === pane.path;
  up.addEventListener("click", () => loadFolder(workspace, side, pane.listing.parent));
  const refresh = $(".refresh", target);
  refresh.disabled = path.disabled || pane.busy;
  refresh.addEventListener("click", submit);
  const listing = $(".file-list", target);
  if (pane.id === UNSET) {
    const block = emptyState("Choose an endpoint", "Select a saved connection or open a local folder.");
    if (!config.hosts.length) block.append(button("Add connection", "button", () => editHost()));
    listing.append(block);
  } else if (pane.busy) {
    listing.append(emptyState("Opening folder…"));
  } else if (pane.error) {
    listing.append(emptyState("Folder unavailable", pane.error));
  } else if (pane.listing) {
    if (!pane.listing.entries.length) listing.append(emptyState("This folder is empty"));
    const fragment = document.createDocumentFragment();
    for (const entry of pane.listing.entries) {
      const row = button("", "file-row" + (pane.selected?.path === entry.path ? " selected" : ""), () => {
        $(".file-row.selected", listing)?.setAttribute("aria-pressed", "false");
        $(".file-row.selected", listing)?.classList.remove("selected");
        pane.selected = entry;
        row.classList.add("selected");
        row.setAttribute("aria-pressed", "true");
      });
      row.title = entry.name;
      row.setAttribute("aria-pressed", pane.selected?.path === entry.path);
      const name = element("span", "file-name");
      name.append(element("span", "entry-icon " + (entry.kind === "directory" ? "folder" : entry.kind)),
        element("span", "", entry.name));
      row.append(name, element("span", "file-size", entry.kind === "file" ? bytes(entry.size) : "—"));
      if (entry.kind === "directory") {
        row.addEventListener("dblclick", () => loadFolder(workspace, side, entry.path));
        row.addEventListener("keydown", event => {
          if (event.key === "Enter") {
            event.preventDefault();
            loadFolder(workspace, side, entry.path);
          }
        });
      }
      fragment.append(row);
    }
    listing.append(fragment);
  }
  if (pane.listing) {
    $(".pane-footer", target).textContent = pane.listing.entries.length + " items" +
      (pane.listing.truncated ? " shown · enter a path to open other items" : "");
  }
}

function renderWorkspace() {
  renderTabs();
  renderHosts();
  for (const side of sides) renderPane(side);
  const unset = sides.some(side => active[side].id === UNSET);
  $("#send-left").disabled = unset;
  $("#send-right").disabled = unset;
  renderJobs();
}

const hostDialog = $("#host-dialog");
const hostForm = $("#host-form");
const hostFields = hostForm.elements;
let editing;
let scanned;
let scanSequence = 0;

function setScan(key) {
  scanned = key;
  $("#key-result").hidden = !key;
  hostFields.trusted.checked = false;
  $("#fingerprint").textContent = key?.fingerprint ?? "";
}

function editHost(item) {
  editing = item;
  scanSequence++;
  hostForm.reset();
  $("#scan-key").disabled = false;
  $(".form-error", hostForm).textContent = "";
  for (const field of ["name", "host", "port", "user", "identity"]) {
    hostFields.namedItem(field).value = item?.[field] ?? (field === "port" ? 22 : "");
  }
  $("#host-title").textContent = item ? "Edit connection" : "Add connection";
  $("#remove-host").hidden = !item;
  setScan(item?.hostKey);
  hostFields.trusted.checked = !!item;
  hostDialog.showModal();
}

for (const field of ["host", "port"]) {
  hostFields[field].addEventListener("input", () => {
    scanSequence++;
    $("#scan-key").disabled = false;
    setScan(null);
  });
}

$("#scan-key").addEventListener("click", async event => {
  if (!hostFields.host.reportValidity() || !hostFields.port.reportValidity()) return;
  const button = event.currentTarget;
  const request = ++scanSequence;
  button.disabled = true;
  const errorBox = $(".form-error", hostForm);
  errorBox.textContent = "";
  setScan(null);
  try {
    const key = await api("scan", {host: hostFields.host.value, port: hostFields.port.value});
    if (request === scanSequence) setScan(key);
  } catch (error) {
    if (request === scanSequence) errorBox.textContent = error.message;
  } finally {
    if (request === scanSequence) button.disabled = false;
  }
});

async function refreshConfig() {
  const updated = await api("config");
  if (JSON.stringify(updated) === JSON.stringify(config)) return;
  config = updated;
  for (const workspace of workspaces) {
    for (const side of sides) {
      const id = workspace[side].id;
      if (id && !host(id)) workspace[side] = paneState(UNSET);
    }
  }
  if (active) renderWorkspace();
}

async function formAction(form, action) {
  const submit = $('button[type="submit"]', form);
  const errorBox = $(".form-error", form);
  errorBox.textContent = "";
  submit.disabled = true;
  try {
    await action();
  } catch (error) {
    errorBox.textContent = error.message;
  } finally {
    submit.disabled = false;
  }
}

hostForm.addEventListener("submit", event => {
  event.preventDefault();
  formAction(hostForm, async () => {
    if (!scanned || !hostFields.trusted.checked) throw new Error("Check and verify the server’s host key.");
    const values = Object.fromEntries(new FormData(hostForm));
    const saved = await api("hosts/save", {...values, id: editing?.id, hostKey: scanned});
    await refreshConfig();
    hostDialog.close();
    if (!editing) changeEndpoint("right", saved.id);
    else {
      for (const side of sides) {
        if (active[side].id === saved.id) loadFolder(active, side, active[side].path);
      }
    }
  });
});

$("#remove-host").addEventListener("click", async event => {
  if (!confirm("Remove the connection “" + editing.name + "”?")) return;
  const remove = event.currentTarget;
  remove.disabled = true;
  try {
    await api("hosts/remove", {id: editing.id});
    await refreshConfig();
    hostDialog.close();
  } catch (error) {
    $(".form-error", hostForm).textContent = error.message;
  } finally {
    remove.disabled = false;
  }
});

const transferDialog = $("#transfer-dialog");
const transferForm = $("#transfer-form");
let transferDetails;

function showDirectOptions() {
  const fields = transferForm.elements;
  const eligible = transferDetails.source && transferDetails.destination &&
    ["auto", "rsync"].includes(fields.protocol.value);
  $("#direct-option").hidden = !eligible;
  $("#direct-key").hidden = !eligible || !fields.direct.checked;
}

function showTransfer(from, to) {
  const source = active[from];
  const dest = active[to];
  transferDetails = {source: source.id, destination: dest.id, workspace: active.id};
  transferForm.reset();
  $(".form-error", transferForm).textContent = "";
  transferForm.elements.path.value = source.selected?.path ?? "";
  transferForm.elements.directory.value = dest.path || (dest.id === LOCAL ? config.home : "");
  $("#source-label").textContent = "Source path · " + endpointName(source.id);
  $("#destination-label").textContent = "Destination folder · " + endpointName(dest.id);
  showDirectOptions();
  transferDialog.showModal();
}

transferForm.elements.protocol.addEventListener("change", showDirectOptions);
transferForm.elements.direct.addEventListener("change", showDirectOptions);
transferForm.addEventListener("submit", event => {
  event.preventDefault();
  formAction(transferForm, async () => {
    const values = Object.fromEntries(new FormData(transferForm));
    const job = await api("jobs/start", {...transferDetails, ...values,
      direct: values.direct === "on", overwrite: values.overwrite === "on"});
    jobs = [job, ...jobs.filter(item => item.id !== job.id)];
    renderJobs();
    transferDialog.close();
  });
});

function renderJobs() {
  const queue = $("#jobs");
  const scoped = $("#workspace-only").checked ? jobs.filter(item => item.workspace === active?.id) : jobs;
  $("#queue-count").textContent = scoped.length;
  queue.replaceChildren();
  if (!scoped.length) {
    queue.append(element("p", "queue-empty", "No transfers yet"));
    return;
  }
  for (const job of scoped) {
    const row = element("div", "job");
    const description = element("div");
    description.append(element("strong", "job-name", job.file),
      element("div", "job-detail", job.source + " → " + job.destination + (job.protocol ? " · " + job.protocol : "")));
    if (job.error) description.append(element("div", "job-detail", job.error));
    const status = element("div", "job-state " + job.state, job.phase);
    if (job.bytes) status.append(element("div", "job-detail", bytes(job.bytes)));
    row.append(description, status);
    if (job.state === "queued" || job.state === "running") row.append(button("×", "icon", async () => {
      try {
        await api("jobs/cancel", {id: job.id});
      } catch (error) {
        notice(error.message);
      }
    }, "Cancel " + job.file));
    queue.append(row);
  }
}

async function poll() {
  try {
    const current = await api("jobs");
    const previous = new Map(jobs.map(job => [job.id, job.state]));
    const completed = current.some(job => job.workspace === active?.id && job.state === "done" &&
      ["queued", "running"].includes(previous.get(job.id)));
    if (JSON.stringify(current) !== JSON.stringify(jobs)) {
      jobs = current;
      renderJobs();
    }
    if (completed) {
      for (const side of sides) {
        const pane = active[side];
        const target = $("#" + side);
        if (pane.listing && document.activeElement !== $(".path", target)) loadFolder(active, side, pane.path);
      }
    }
  } catch {
    // A local app restart invalidates existing browser sessions.
  } finally {
    setTimeout(poll, document.hidden ? 4000 : 1200);
  }
}

$("#add-tab").addEventListener("click", () => newWorkspace());
$("#add-host").addEventListener("click", () => editHost());
$("#send-right").addEventListener("click", () => showTransfer("left", "right"));
$("#send-left").addEventListener("click", () => showTransfer("right", "left"));
$("#workspace-only").addEventListener("change", renderJobs);
$("#new-window").addEventListener("click", () => {
  const snapshot = {id: active.id, name: active.name};
  for (const side of sides) snapshot[side] = {id: active[side].id, path: active[side].path};
  const params = new URLSearchParams({token, workspace: JSON.stringify(snapshot)});
  window.open("/#" + params, "_blank", "popup,noopener,width=1280,height=850");
});
for (const dialog of [hostDialog, transferDialog]) {
  $(".close", dialog).addEventListener("click", () => dialog.close());
}
window.addEventListener("focus", () => refreshConfig().catch(() => {}));

(async () => {
  try {
    await refreshConfig();
    const initial = JSON.parse(incoming.get("workspace") || "{}");
    for (const side of sides) {
      if (initial[side]?.id && !host(initial[side].id)) initial[side].id = UNSET;
    }
    newWorkspace(initial);
    poll();
  } catch (error) {
    notice(error.message);
    newWorkspace();
  }
})();
