"use strict";

let S = null; // last /api/status payload

// ---- helpers ---------------------------------------------------------------

function h(tag, attrs = {}, ...children) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (v === null || v === undefined || v === false) continue;
    if (k.startsWith("on")) el.addEventListener(k.slice(2), v);
    else if (k === "class") el.className = v;
    else if (v === true) el.setAttribute(k, "");
    else el.setAttribute(k, v);
  }
  for (const c of children.flat(Infinity)) {
    if (c === null || c === undefined || c === false) continue;
    el.append(c instanceof Node ? c : document.createTextNode(String(c)));
  }
  return el;
}

async function api(method, path, body) {
  const res = await fetch("api/" + path, {
    method,
    headers: body ? { "Content-Type": "application/json" } : {},
    body: body ? JSON.stringify(body) : undefined,
  });
  let data = {};
  try { data = await res.json(); } catch (_) { /* not JSON */ }
  if (!res.ok) {
    const err = new Error(data.error || `${res.status} ${res.statusText}`);
    err.details = data.details;
    throw err;
  }
  return data;
}

function fmtBytes(n) {
  if (!n) return "0 B";
  const u = ["B", "KB", "MB", "GB", "TB", "PB"];
  const i = Math.min(Math.floor(Math.log(n) / Math.log(1000)), u.length - 1);
  return (n / 1000 ** i).toFixed(i ? 1 : 0) + " " + u[i];
}

function errorText(err) {
  let msg = err.message;
  const procs = err.details && err.details.processes;
  if (procs && procs.length) {
    msg += "\n\nProcesses using the disk:\n" +
      procs.map(p => `  ${p.name} (pid ${p.pid}${p.container ? ", container " + p.container : ""})`).join("\n");
  }
  return msg;
}

let toastTimer;
function toast(msg, isError = false) {
  const t = document.getElementById("toast");
  t.textContent = msg;
  t.className = isError ? "error" : "";
  t.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { t.hidden = true; }, isError ? 10000 : 3500);
}

async function busy(button, fn) {
  button.disabled = true;
  button.classList.add("busy");
  try { await fn(); } catch (err) { toast(errorText(err), true); } finally {
    button.disabled = false;
    button.classList.remove("busy");
  }
}

function chip(text, kind = "") { return h("span", { class: "chip " + kind }, text); }

// ---- dialog ----------------------------------------------------------------

const dlg = document.getElementById("dialog");
let dlgSubmit = null;

function openDialog({ title, body, okText = "Save", danger = false, onOk }) {
  document.getElementById("dialog-title").textContent = title;
  const b = document.getElementById("dialog-body");
  b.replaceChildren(...[].concat(body));
  const ok = document.getElementById("dialog-ok");
  ok.textContent = okText;
  ok.className = danger ? "danger" : "primary";
  document.getElementById("dialog-error").hidden = true;
  dlgSubmit = onOk;
  dlg.showModal();
  const first = b.querySelector("input:not([type=checkbox]), select");
  if (first) first.focus();
}

document.getElementById("dialog-form").addEventListener("submit", async (ev) => {
  ev.preventDefault();
  const ok = document.getElementById("dialog-ok");
  const errEl = document.getElementById("dialog-error");
  ok.disabled = true;
  ok.classList.add("busy");
  errEl.hidden = true;
  try {
    await dlgSubmit();
    dlg.close();
    await refresh();
  } catch (err) {
    errEl.textContent = errorText(err);
    errEl.hidden = false;
  } finally {
    ok.disabled = false;
    ok.classList.remove("busy");
  }
});
document.getElementById("dialog-cancel").addEventListener("click", () => dlg.close());
dlg.addEventListener("close", () => {
  // Don't keep secrets in the DOM once the dialog is gone.
  document.getElementById("dialog-body").replaceChildren();
});

function field(label, input, hint) {
  return h("label", {}, label, input, hint ? h("small", {}, hint) : null);
}

function confirmDialog(title, text, okText, fn) {
  openDialog({ title, body: h("p", {}, text), okText, danger: true, onOk: fn });
}

// ---- tabs ------------------------------------------------------------------

function showTab(name) {
  for (const btn of document.querySelectorAll("#tabs button")) {
    btn.classList.toggle("active", btn.dataset.tab === name);
  }
  for (const sec of document.querySelectorAll("main > section")) {
    sec.hidden = sec.id !== "tab-" + name;
  }
  try { localStorage.setItem("tab", name); } catch (_) { /* storage unavailable */ }
}
document.getElementById("tabs").addEventListener("click", (ev) => {
  if (ev.target.dataset.tab) showTab(ev.target.dataset.tab);
});

// ---- disks -----------------------------------------------------------------

function renderVolumes() {
  const box = document.getElementById("volumes");
  if (!S.volumes.length) {
    box.replaceChildren(h("div", { class: "empty" },
      "No disks configured yet. Pick one from the list below."));
    return;
  }
  box.replaceChildren(...S.volumes.map((v) => {
    const chips = [];
    if (!v.present) chips.push(chip("Not connected", "danger"));
    else if (v.mounted) chips.push(chip("Mounted", "ok"));
    else if (v.unlocked && v.luks) chips.push(chip("Unlocked, not mounted", "warn"));
    else chips.push(chip(v.luks ? "Locked" : "Not mounted"));
    if (v.luks) chips.push(chip("LUKS"));
    if (v.fstype && v.fstype !== "crypto_LUKS") chips.push(chip(v.fstype));

    let usage = null;
    if (v.usage && v.usage.total) {
      const used = v.usage.total - v.usage.free;
      usage = h("div", {},
        h("div", { class: "bar" }, h("div", { style: `width:${(used / v.usage.total * 100).toFixed(1)}%` })),
        h("small", {}, `${fmtBytes(used)} used of ${fmtBytes(v.usage.total)} · ${fmtBytes(v.usage.free)} free`));
    }

    const actions = [];
    if (v.mounted) {
      actions.push(h("button", { onclick: (e) => busy(e.target, async () => {
        await api("POST", `volumes/${v.id}/lock`);
        toast(`${v.name} ${v.luks ? "locked" : "unmounted"} - safe to unplug`);
        await refresh();
      }) }, v.luks ? "Lock" : "Unmount"));
    } else {
      if (v.present) {
        actions.push(h("button", { class: "primary", onclick: (e) => unlockVolume(v, e.target) },
          v.luks && !v.unlocked ? "Unlock" : "Mount"));
      }
      if (v.luks && v.unlocked) {
        actions.push(h("button", { onclick: (e) => busy(e.target, async () => {
          await api("POST", `volumes/${v.id}/lock`);
          await refresh();
        }) }, "Lock"));
      }
      actions.push(h("button", { onclick: () => volumeDialog(v) }, "Edit"));
      actions.push(h("button", { class: "danger", onclick: () => confirmDialog(
        "Remove disk", `Remove "${v.name}" from the add-on? The disk itself is not changed.`, "Remove",
        () => api("DELETE", `volumes/${v.id}`)) }, "Remove"));
    }

    return h("div", { class: "card" },
      h("div", { class: "title" }, h("strong", {}, v.name), h("div", {}, chips)),
      h("div", { class: "meta" },
        `${v.path}` + (v.device ? ` · /dev/${v.device}` : "") + (v.model ? ` · ${v.model}` : "") +
        (v.size ? ` · ${fmtBytes(v.size)}` : "")),
      usage,
      v.shares.length ? h("div", { class: "meta" }, "Shares: " + v.shares.join(", ")) : null,
      h("div", { class: "actions" }, actions));
  }));
}

function unlockVolume(v, button) {
  if (!v.luks || v.unlocked) {
    return busy(button, async () => {
      await api("POST", `volumes/${v.id}/unlock`, {});
      toast(`${v.name} mounted at ${v.path}`);
      await refresh();
    });
  }
  const pass = h("input", { type: "password", autocomplete: "off", required: true });
  openDialog({
    title: `Unlock ${v.name}`,
    body: [
      field("Passphrase", pass, "Used once to unlock the disk. It is never stored."),
      h("small", {}, "Unlocking can take a few seconds."),
    ],
    okText: "Unlock",
    onOk: async () => {
      await api("POST", `volumes/${v.id}/unlock`, { passphrase: pass.value });
      pass.value = "";
      toast(`${v.name} unlocked and mounted at ${v.path}`);
    },
  });
}

function volumeDialog(v, disk) {
  const isNew = !v;
  v = v || {
    name: disk.label || disk.name, uuid: disk.uuid, luks: disk.fstype === "crypto_LUKS",
    mount_name: (disk.label || "Disk").replace(/[^A-Za-z0-9._-]/g, "_"), mount_options: "",
  };
  const name = h("input", { value: v.name, maxlength: 64 });
  const mount = h("input", { value: v.mount_name, required: true, pattern: "[A-Za-z0-9][A-Za-z0-9._\\-]*" });
  const uuid = h("input", { value: v.uuid, required: true });
  const luks = h("input", { type: "checkbox", checked: v.luks });
  const opts = h("input", { value: v.mount_options, placeholder: "e.g. noatime" });
  openDialog({
    title: isNew ? "Add disk" : `Edit ${v.name}`,
    body: [
      field("Name", name),
      field("Mount folder", mount, "The disk appears as /media/<folder> in Home Assistant."),
      field("Partition UUID", uuid),
      h("label", { class: "inline" }, luks, "LUKS encrypted"),
      field("Mount options", opts, "Optional, comma-separated."),
    ],
    onOk: async () => {
      const body = { name: name.value, mount_name: mount.value, uuid: uuid.value,
                     luks: luks.checked, mount_options: opts.value };
      if (isNew) await api("POST", "volumes", body);
      else await api("PUT", `volumes/${v.id}`, body);
    },
  });
}

function renderDisks() {
  const showSystem = document.getElementById("show-system").checked;
  const rows = S.disks.filter(d => showSystem || !d.system);
  const table = document.getElementById("disk-table");
  table.replaceChildren(
    h("tr", {}, ["Device", "Type", "Label", "Size", "Model", "UUID", ""].map(t => h("th", {}, t))),
    ...(rows.length ? rows.map(d => h("tr", {},
      h("td", {}, "/dev/" + d.name),
      h("td", {}, d.fstype === "crypto_LUKS" ? chip("LUKS", "ok") : d.fstype),
      h("td", {}, d.label),
      h("td", {}, fmtBytes(d.size)),
      h("td", {}, d.model + (d.system ? " (system)" : "")),
      h("td", {}, h("small", {}, d.uuid)),
      h("td", { class: "actions-cell" }, d.configured ? chip("Added") :
        h("button", { onclick: () => volumeDialog(null, d) }, "Add")),
    )) : [h("tr", {}, h("td", { colspan: 7, class: "empty" }, "No disks with a filesystem found."))]),
  );
}
document.getElementById("show-system").addEventListener("change", renderDisks);

// ---- shares ----------------------------------------------------------------

function renderShares() {
  const box = document.getElementById("shares");
  if (!S.shares.length) {
    box.replaceChildren(h("div", { class: "empty" }, "No shares yet."));
    return;
  }
  box.replaceChildren(...S.shares.map((s) => {
    const access = Object.entries(s.access);
    const guest = s.guest || "none";
    const chips = [];
    if (!access.length && guest === "none") chips.push(chip("No users - disabled", "warn"));
    else if (!s.online) chips.push(chip("Disk locked", "warn"));
    else chips.push(chip("Active", "ok"));
    if (!s.browseable) chips.push(chip("Hidden"));
    return h("div", { class: "card" },
      h("div", { class: "title" }, h("strong", {}, s.name), h("div", {}, chips)),
      h("div", { class: "meta" }, s.path + (s.comment ? ` · ${s.comment}` : "")),
      h("div", {},
        guest !== "none" ? chip(`Guest: ${guest === "rw" ? "read/write" : "read only"}`, "warn") : null,
        access.map(([u, a]) => chip(`${u}: ${a === "rw" ? "read/write" : "read only"}`))),
      h("div", { class: "actions" },
        h("button", { onclick: () => shareDialog(s) }, "Edit"),
        h("button", { class: "danger", onclick: () => confirmDialog(
          "Remove share", `Stop sharing "${s.name}"? Files are not deleted.`, "Remove",
          () => api("DELETE", `shares/${s.id}`)) }, "Remove")));
  }));
}

function folderBrowser(input) {
  const list = h("div", { class: "browser", hidden: true });
  async function load(path) {
    try {
      const r = await api("GET", "browse?path=" + encodeURIComponent(path));
      const items = [];
      if (r.parent !== null) {
        items.push(h("div", { onclick: () => load(r.parent) }, "⬑ ..", ));
      }
      for (const d of r.dirs) {
        items.push(h("div", { onclick: () => { input.value = d; load(d); } }, "📁 " + d));
      }
      if (!items.length) items.push(h("div", {}, "(no sub-folders)"));
      list.replaceChildren(...items);
      list.hidden = false;
    } catch (err) { toast(err.message, true); }
  }
  const btn = h("button", { type: "button", onclick: () => load(input.value || "") }, "Browse");
  return { row: h("div", { class: "row" }, input, btn), list };
}

function shareDialog(s) {
  const isNew = !s;
  s = s || { name: "", path: "", comment: "", browseable: true, access: {}, guest: "none" };
  const name = h("input", { value: s.name, required: true, maxlength: 80 });
  const path = h("input", { value: s.path, required: true, placeholder: "/media/Storage" });
  const browser = folderBrowser(path);
  const comment = h("input", { value: s.comment, maxlength: 128 });
  const browseable = h("input", { type: "checkbox", checked: s.browseable });
  const selects = {};
  const grid = h("div", { class: "access-grid" }, S.users.length ? S.users.map((u) => {
    selects[u.name] = h("select", {},
      ["none", "ro", "rw"].map(a => h("option", { value: a, selected: (s.access[u.name] || "none") === a },
        { none: "No access", ro: "Read only", rw: "Read / write" }[a])));
    return [h("span", {}, u.name), selects[u.name]];
  }) : h("small", {}, "Create users on the Users tab first."));
  const hints = S.volumes.map(v => v.path);
  const guest = h("select", {},
    ["none", "ro", "rw"].map(a => h("option", { value: a, selected: (s.guest || "none") === a },
      { none: "No guest access", ro: "Read only", rw: "Read / write" }[a])));

  openDialog({
    title: isNew ? "Add share" : `Edit ${s.name}`,
    body: [
      field("Share name", name, "Clients connect to \\\\<HA-IP>\\<name>"),
      h("label", {}, "Folder", browser.row,
        hints.length ? h("small", {}, "Disks: " + hints.join(", ")) : null),
      browser.list,
      field("Description", comment),
      h("label", { class: "inline" }, browseable, "Visible when browsing the network"),
      h("div", {}, h("strong", {}, "User access")), grid,
      field("Guest access (no password)", guest,
        "Anyone on your network can connect without logging in. Windows 10/11 block guest " +
        "logins by default; this is meant for TVs and media players."),
    ],
    onOk: async () => {
      const access = {};
      for (const [u, sel] of Object.entries(selects)) if (sel.value !== "none") access[u] = sel.value;
      const body = { name: name.value, path: path.value, comment: comment.value,
                     browseable: browseable.checked, access, guest: guest.value };
      if (isNew) await api("POST", "shares", body);
      else await api("PUT", `shares/${s.id}`, body);
    },
  });
}
document.getElementById("add-share").addEventListener("click", () => shareDialog(null));

// ---- users -----------------------------------------------------------------

function passwordFields() {
  const p1 = h("input", { type: "password", autocomplete: "new-password", required: true, minlength: 4 });
  const p2 = h("input", { type: "password", autocomplete: "new-password", required: true });
  return {
    nodes: [field("Password", p1), field("Repeat password", p2)],
    value() {
      if (p1.value !== p2.value) throw new Error("Passwords do not match");
      return p1.value;
    },
  };
}

function renderUsers() {
  const table = document.getElementById("user-table");
  table.replaceChildren(
    h("tr", {}, ["User", "Shares", ""].map(t => h("th", {}, t))),
    ...(S.users.length ? S.users.map(u => h("tr", {},
      h("td", {}, u.name),
      h("td", {}, u.shares.join(", ") || h("small", {}, "none")),
      h("td", { class: "actions-cell" },
        h("button", { onclick: () => {
          const pw = passwordFields();
          openDialog({ title: `Password for ${u.name}`, body: pw.nodes,
            onOk: () => api("PUT", `users/${encodeURIComponent(u.name)}`, { password: pw.value() }) });
        } }, "Password"),
        h("button", { class: "danger", onclick: () => confirmDialog(
          "Delete user", `Delete ${u.name}? They lose access to all shares.`, "Delete",
          () => api("DELETE", `users/${encodeURIComponent(u.name)}`)) }, "Delete")),
    )) : [h("tr", {}, h("td", { colspan: 3, class: "empty" }, "No users yet."))]),
  );
}

document.getElementById("add-user").addEventListener("click", () => {
  const name = h("input", { required: true, maxlength: 32, autocomplete: "off", pattern: "[a-zA-Z_][a-zA-Z0-9_\\-]*" });
  const pw = passwordFields();
  openDialog({
    title: "Add user",
    body: [field("Username", name, "Lowercase letters, digits, '_' and '-'."), ...pw.nodes],
    onOk: () => api("POST", "users", { name: name.value, password: pw.value() }),
  });
});

// ---- settings --------------------------------------------------------------

const settingsForm = document.getElementById("settings-form");
let settingsDirty = false;
settingsForm.addEventListener("input", () => { settingsDirty = true; });

function renderSettings() {
  if (settingsDirty) return;
  for (const [k, v] of Object.entries(S.settings)) {
    const el = settingsForm.elements[k];
    if (!el) continue;
    if (el.type === "checkbox") el.checked = v; else el.value = v;
  }
}

settingsForm.addEventListener("submit", (ev) => {
  ev.preventDefault();
  const f = settingsForm.elements;
  busy(ev.submitter, async () => {
    await api("PUT", "settings", {
      workgroup: f.workgroup.value, server_string: f.server_string.value,
      hosts_allow: f.hosts_allow.value, min_protocol: f.min_protocol.value,
      macos: f.macos.checked, veto_junk: f.veto_junk.checked,
    });
    settingsDirty = false;
    toast("Settings saved - Samba reloaded");
    await refresh();
  });
});

// ---- main loop -------------------------------------------------------------

async function refresh() {
  try {
    S = await api("GET", "status");
  } catch (err) {
    toast("Cannot load status: " + err.message, true);
    return;
  }
  const banner = document.getElementById("banner");
  banner.hidden = S.host_access.ok;
  banner.textContent = S.host_access.ok ? "" :
    "No access to the host (" + S.host_access.reason + "). Turn off Protection mode " +
    "in the add-on's Info tab and restart it - unlocking and mounting disks will not work until then.";
  renderVolumes();
  renderDisks();
  renderShares();
  renderUsers();
  renderSettings();
}

try { showTab(localStorage.getItem("tab") || "disks"); } catch (_) { showTab("disks"); }
refresh();
setInterval(() => { if (!dlg.open && !document.hidden) refresh(); }, 10000);
