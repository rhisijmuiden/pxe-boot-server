/* PXE Boot Server front-end. Plain JavaScript, no build step. */
(function () {
  "use strict";
  const P = window.PXE || {};
  const L = P.i18n || {};
  const t = (k, vars) => {
    let s = L[k] || k;
    if (vars) Object.keys(vars).forEach((v) => { s = s.replace("{" + v + "}", vars[v]); });
    return s;
  };
  const $ = (sel, root) => (root || document).querySelector(sel);
  const $$ = (sel, root) => Array.from((root || document).querySelectorAll(sel));
  const esc = (s) => String(s == null ? "" : s).replace(/[&<>"']/g, (c) => ({"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"}[c]));
  const icon = (id, size) => `<svg width="${size || 18}" height="${size || 18}"><use href="#i-${id}"/></svg>`;

  function fmtBytes(n) {
    n = Number(n || 0);
    const u = ["B", "KB", "MB", "GB", "TB"];
    let i = 0;
    while (n >= 1000 && i < u.length - 1) { n /= 1000; i++; }
    return (i < 2 ? n.toFixed(0) : n.toFixed(1)) + " " + u[i];
  }
  const fmtMbit = (bps) => (bps / 1e6).toFixed(bps >= 1e8 ? 0 : 1);
  const fmtClock = (ts) => { const d = new Date(ts * 1000); return d.toTimeString().slice(0, 5); };
  const fmtDur = (s) => { s = Math.max(0, Math.round(s)); const m = Math.floor(s / 60); return m >= 60 ? `${Math.floor(m / 60)}h ${m % 60}m` : m ? `${m}m ${s % 60}s` : `${s}s`; };

  async function api(method, url, body, opts) {
    const headers = Object.assign({"X-CSRF-Token": P.csrf}, (opts && opts.headers) || {});
    let payload = body;
    if (body !== undefined && !(body instanceof Blob) && !(body instanceof ArrayBuffer)) {
      headers["Content-Type"] = "application/json";
      payload = JSON.stringify(body);
    }
    const res = await fetch(url, {method, headers, body: payload, credentials: "same-origin", signal: opts && opts.signal});
    if (res.status === 401) { location.href = "/login?next=" + encodeURIComponent(location.pathname); throw new Error("login"); }
    const type = res.headers.get("content-type") || "";
    const data = type.includes("json") ? await res.json() : await res.text();
    if (!res.ok) { const err = new Error((data && data.error) || (data && data.detail) || res.statusText); err.status = res.status; err.data = data; throw err; }
    return data;
  }

  function toast(msg, kind) {
    const r = $("#toastRoot");
    if (!r) return;
    r.innerHTML = `<div class="toast" role="status">${icon(kind === "error" ? "alert" : "check")}<span>${esc(msg)}</span></div>`;
    clearTimeout(toast.t);
    toast.t = setTimeout(() => { r.innerHTML = ""; }, kind === "error" ? 6000 : 3000);
  }

  function modal(html, onMount) {
    const root = $("#modalRoot");
    root.innerHTML = `<div class="scrim" id="scrim"><div class="modal" role="dialog" aria-modal="true">${html}</div></div>`;
    const close = () => { root.innerHTML = ""; document.removeEventListener("keydown", onKey); };
    const onKey = (e) => { if (e.key === "Escape") close(); };
    document.addEventListener("keydown", onKey);
    $("#scrim").addEventListener("click", (e) => { if (e.target.id === "scrim") close(); });
    $$("[data-close]", root).forEach((b) => b.addEventListener("click", close));
    if (onMount) onMount(root, close);
    const f = $("button, input, select", root); if (f) f.focus();
    return close;
  }

  function confirmBox(text, okLabel, danger) {
    return new Promise((resolve) => {
      modal(`<div class="modal-h"><h2>${esc(t("js_confirm"))}</h2></div>
        <div class="modal-b"><p style="margin:0">${esc(text)}</p>
        <div class="actions"><button class="btn btn-outline" data-close type="button">${esc(t("js_cancel"))}</button>
        <button class="btn ${danger ? "btn-accent" : "btn-dark"}" id="cOk" type="button">${esc(okLabel || t("js_ok"))}</button></div></div>`,
      (root, close) => {
        $$("[data-close]", root).forEach((b) => b.addEventListener("click", () => resolve(false)));
        $("#cOk", root).addEventListener("click", () => { close(); resolve(true); });
      });
    });
  }

  /* ---------------------------------------------------------------- shell */
  function shell() {
    const app = $("#app");
    const mb = $("#menuBtn");
    if (mb) mb.addEventListener("click", () => app.classList.toggle("nav-open"));
    const tb = $("#themeBtn");
    if (tb) tb.addEventListener("click", () => {
      const r = document.documentElement;
      const dark = r.dataset.theme ? r.dataset.theme === "dark" : matchMedia("(prefers-color-scheme: dark)").matches;
      r.dataset.theme = dark ? "light" : "dark";
      try { localStorage.setItem("theme", r.dataset.theme); } catch (e) {}
    });
    // dropdowns
    // Menus are positioned against the viewport so a scrolling table never clips them.
    const placeMenu = (btn, m) => {
      const r = btn.getBoundingClientRect(), gap = 6, pad = 8;
      m.style.position = "fixed";
      m.style.right = Math.max(pad, window.innerWidth - r.right) + "px";
      m.style.left = "auto";
      m.style.maxHeight = "";
      const h = m.offsetHeight, below = window.innerHeight - r.bottom - gap - pad, above = r.top - gap - pad;
      if (h <= below || below >= above) {
        m.style.top = (r.bottom + gap) + "px";
        if (h > below) m.style.maxHeight = below + "px";
      } else {
        m.style.top = Math.max(pad, r.top - gap - Math.min(h, above)) + "px";
        if (h > above) m.style.maxHeight = above + "px";
      }
    };
    let openDd = null;
    const closeMenus = (except) => {
      $$(".dropdown-menu").forEach((m) => { if (m !== except) m.hidden = true; });
      if (!except) openDd = null;
    };
    document.addEventListener("click", (e) => {
      const btn = e.target.closest("[data-dd]");
      if (btn) {
        const m = btn.nextElementSibling;
        closeMenus(m);
        m.hidden = !m.hidden;
        openDd = m.hidden ? null : [btn, m];
        if (openDd) placeMenu(btn, m);
        e.stopPropagation();
      } else if (!e.target.closest(".dropdown-menu")) closeMenus();
    });
    // Follow the button while the page or a table scrolls; close once the button is out of view.
    const followMenu = (e) => {
      if (!openDd || (e && e.target instanceof Element && e.target.closest(".dropdown-menu"))) return;
      const r = openDd[0].getBoundingClientRect();
      if (r.bottom < 0 || r.top > window.innerHeight || r.width === 0) closeMenus();
      else placeMenu(openDd[0], openDd[1]);
    };
    window.addEventListener("resize", followMenu);
    document.addEventListener("scroll", followMenu, true);
    // confirm on forms
    $$("form[data-confirm]").forEach((f) => f.addEventListener("submit", async (e) => {
      if (f.dataset.ok) return;
      e.preventDefault();
      if (await confirmBox(f.dataset.confirm, null, true)) { f.dataset.ok = "1"; f.submit(); }
    }));
    // styled radios/checkboxes
    $$(".radio input[type=radio]").forEach((inp) => inp.addEventListener("change", () => {
      $$(`input[name="${inp.name}"]`).forEach((o) => o.closest(".radio").classList.toggle("on", o.checked));
    }));
    $$(".radio input[type=checkbox]").forEach((inp) => inp.addEventListener("change", () => inp.closest(".radio").classList.toggle("on", inp.checked)));
    const ib = $("#infoBtn");
    if (ib) ib.addEventListener("click", infoModal);
    navLive();
  }

  let liveTimer = null;
  async function navLive() {
    try {
      const d = await api("GET", "/api/live");
      const n = d.clients.filter((c) => c.phase !== "done").length;
      const el = $("#liveCount");
      if (el) { el.textContent = n; el.hidden = n === 0; }
    } catch (e) { /* ignore */ }
    clearTimeout(liveTimer);
    liveTimer = setTimeout(navLive, 10000);
  }

  /* ---------------------------------------------------------------- info & updates */
  async function infoModal() {
    let info = {};
    try { info = await api("GET", "/api/info"); } catch (e) { info = {}; }
    let channel = info.channel || P.channel || "main";
    const render = () => `
      <div class="modal-h"><span class="osicon" style="width:44px;height:44px">${icon("info")}</span>
        <div style="flex:1"><h2>${esc(t("js_product"))}</h2><span class="note small">RHIS · ${esc((info.repo || "").replace("https://", ""))}</span></div>
        <button class="icon-btn" data-close type="button" aria-label="${esc(t("js_close"))}">${icon("x")}</button></div>
      <div class="modal-b">
        <dl class="kv"><dt>${esc(t("js_version"))}</dt><dd class="num">${esc(info.version || "")}</dd>
          <dt>${esc(t("js_commit"))}</dt><dd class="num">${esc(info.commit || "–")} · ${esc(info.branch || "")}</dd>
          <dt>${esc(t("js_installed"))}</dt><dd class="num">${esc(info.installed || "–")}</dd>
          <dt>${esc(t("js_os"))}</dt><dd>${esc(info.os || "")}</dd></dl>
        ${P.admin ? `<div class="field"><label>${icon("branch", 16)} ${esc(t("js_channel"))}</label><div class="radios">
          <label class="radio ${channel === "main" ? "on" : ""}"><input type="radio" name="ch" value="main" ${channel === "main" ? "checked" : ""} hidden><span class="rb"></span><span><b>${esc(t("js_ch_main"))}</b><p>${esc(t("js_ch_main_d"))}</p></span></label>
          <label class="radio ${channel === "test" ? "on" : ""}"><input type="radio" name="ch" value="test" ${channel === "test" ? "checked" : ""} hidden><span class="rb"></span><span><b>${esc(t("js_ch_test"))}</b><p>${esc(t("js_ch_test_d"))}</p></span></label>
        </div></div>
        <div class="callout" id="updMsg" hidden></div>
        <pre class="log" id="ulog" hidden></pre>
        <div class="actions"><button class="btn btn-outline" id="chk" type="button">${icon("refresh", 16)}${esc(t("js_check"))}</button>
          <button class="btn btn-accent" id="upd" type="button" hidden>${esc(t("js_update_now"))}</button></div>` : ""}
      </div>`;
    modal(render(), (root) => {
      const msg = $("#updMsg", root), upd = $("#upd", root), chk = $("#chk", root), log = $("#ulog", root);
      if (!chk) return;
      const show = (html, ok) => { msg.hidden = false; msg.className = "callout" + (ok ? " ok" : ""); msg.innerHTML = html; };
      $$("input[name=ch]", root).forEach((r) => r.addEventListener("change", () => {
        channel = r.value;
        $$("input[name=ch]", root).forEach((o) => o.closest(".radio").classList.toggle("on", o.checked));
        check();
      }));
      async function check() {
        chk.disabled = true; upd.hidden = true;
        show(`${icon("refresh")}<span>${esc(t("js_checking"))}</span>`);
        try {
          const r = await api("POST", "/api/update/check", {channel});
          const switching = channel !== (info.branch || P.channel);
          if (r.available || switching) {
            show(`${icon("info")}<span>${esc(switching ? t("js_switch_avail", {ch: channel, v: r.remote_version || "?"}) : t("js_update_avail", {v: r.remote_version || "?", n: r.behind || "?"}))}</span>`);
            upd.hidden = false;
            upd.textContent = switching ? t("js_switch_now", {ch: channel}) : t("js_update_now");
            const dot = $("#updDot"); if (dot) dot.hidden = switching;
          } else {
            show(`${icon("check")}<span>${esc(t("js_uptodate"))}</span>`, true);
            const dot = $("#updDot"); if (dot) dot.hidden = true;
          }
        } catch (e) { show(`${icon("alert")}<span>${esc(t("js_check_failed"))}: ${esc(e.message)}</span>`); }
        chk.disabled = false;
      }
      chk.addEventListener("click", check);
      upd.addEventListener("click", async () => {
        upd.disabled = true; chk.disabled = true; log.hidden = false; log.textContent = "";
        show(`${icon("refresh")}<span>${esc(t("js_updating"))}</span>`);
        try { await api("POST", "/api/update/run", {channel}); } catch (e) { show(`${icon("alert")}<span>${esc(e.message)}</span>`); upd.disabled = false; chk.disabled = false; return; }
        const started = Date.now();
        const poll = async () => {
          try {
            const r = await api("GET", "/api/update/log");
            log.textContent = r.log; log.scrollTop = log.scrollHeight;
            if (/^UPDATE OK/m.test(r.log)) { show(`${icon("check")}<span>${esc(t("js_updated", {v: r.version}))}</span>`, true); setTimeout(() => location.reload(), 2500); return; }
            if (/^UPDATE FAILED/m.test(r.log)) { show(`${icon("alert")}<span>${esc(t("js_update_failed"))}</span>`); chk.disabled = false; return; }
          } catch (e) { /* app restarting */ }
          if (Date.now() - started < 600000) setTimeout(poll, 1500);
        };
        setTimeout(poll, 1500);
      });
    });
  }

  /* ---------------------------------------------------------------- live clients */
  const PHASES = ["pxe", "ipxe", "menu", "loading", "installer", "done"];
  function phaseLabel(c) { return t("js_phase_" + c.phase); }
  function phasePill(c) {
    const cls = {loading: "warn", installer: "ok", done: "ok"}[c.phase] || "";
    return `<span class="pill ${cls}" ${cls ? "" : 'style="background:var(--hover);color:var(--muted)"'}>${esc(phaseLabel(c))}</span>`;
  }
  function clientCard(c) {
    const idx = PHASES.indexOf(c.phase);
    const steps = ["ipxe", "menu", "loading", "installer", "done"].map((p) => {
      const i = PHASES.indexOf(p);
      return `<div class="ph ${i < idx || c.phase === "done" ? "done" : i === idx ? "now" : ""}"><i></i><span>${esc(t("js_phase_" + p))}</span></div>`;
    }).join("");
    let prog = "";
    if (c.phase === "loading" || (c.phase === "installer" && c.expected && c.sent < c.expected)) {
      const pc = c.progress == null ? null : c.progress;
      prog = `<div class="prog"><div class="row2"><span>${esc(t("js_downloading"))}</span><span>${fmtBytes(c.sent)}${c.expected ? " / " + fmtBytes(c.expected) : ""}${pc != null ? " · " + pc.toFixed(0) + "%" : ""} · ${fmtMbit(c.speed * 8)} Mbit/s</span></div>
        <div class="bar"><i style="width:${pc != null ? pc : 100}%;${pc == null ? "opacity:.35" : ""}"></i></div></div>`;
    } else if (c.phase === "installer") {
      prog = `<div class="prog"><div class="row2"><span class="num" style="overflow-wrap:anywhere">${esc(c.file || t("js_installer_running"))}</span><span>${c.speed > 1000 ? fmtMbit(c.speed * 8) + " Mbit/s" : ""}</span></div><div class="bar"><i style="width:100%;opacity:.35"></i></div></div>`;
    }
    const hw = [c.manufacturer, c.product].filter(Boolean).join(" ");
    return `<div class="panel client"><div class="client-h"><span class="live-dot" ${c.phase === "done" ? 'style="animation:none;background:var(--muted)"' : ""}></span>
      <div class="who"><b>${esc(c.hostname || c.mac || c.ip)}</b><br><small>${esc([c.mac, c.ip].filter(Boolean).join(" · "))}</small></div>
      ${c.platform ? `<span class="chip yes">${esc(c.platform)}</span>` : ""}${phasePill(c)}</div>
      <div class="phases">${steps}</div>${prog}
      <dl class="kv"><dt>${esc(t("js_iso"))}</dt><dd>${esc(c.iso_name || "–")}${c.answer_name ? ` <span class="chip">${esc(c.answer_name)}</span>` : ""}</dd>
      <dt>${esc(t("js_started"))}</dt><dd class="num">${fmtClock(c.started)} · ${fmtDur(c.age)}</dd>
      ${hw ? `<dt>${esc(t("js_hardware"))}</dt><dd>${esc(hw)}</dd>` : ""}</dl></div>`;
  }

  /* ---------------------------------------------------------------- charts */
  function lineChart(el, series, opts) {
    const w = Math.max(280, el.clientWidth - 40), h = (opts && opts.height) || 200;
    const L = 46, B = 22, T = 8, R = 8;
    const maxV = Math.max(10, ...series.flatMap((s) => s.points.map((p) => p[1])));
    const nice = (v) => { const p = Math.pow(10, Math.floor(Math.log10(v))); for (const m of [1, 2, 2.5, 5, 10]) if (m * p >= v) return m * p; return v; };
    const top = nice(maxV);
    const n = Math.max(2, ...series.map((s) => s.points.length));
    const xs = (i) => L + (i * (w - L - R)) / (n - 1);
    const ys = (v) => T + (h - B - T) * (1 - v / top);
    const ticks = [0, 0.25, 0.5, 0.75, 1].map((f) => f * top);
    let svg = `<svg class="chart" width="${w}" height="${h}" viewBox="0 0 ${w} ${h}" role="img" aria-label="${esc(opts && opts.label || "chart")}">`;
    svg += `<g class="grid">${ticks.map((v) => `<line x1="${L}" x2="${w - R}" y1="${ys(v)}" y2="${ys(v)}"/><text class="lbl" x="${L - 8}" y="${ys(v) + 4}" text-anchor="end">${v >= 100 ? v.toFixed(0) : +v.toFixed(1)}</text>`).join("")}</g>`;
    series.forEach((s, si) => {
      if (!s.points.length) return;
      const off = n - s.points.length;
      const d = s.points.map((p, i) => `${i ? "L" : "M"}${xs(i + off).toFixed(1)},${ys(p[1]).toFixed(1)}`).join("");
      if (si === 0) svg += `<path class="area" d="${d}L${xs(n - 1)},${ys(0)}L${xs(off)},${ys(0)}Z"/>`;
      svg += `<path class="${si === 0 ? "ln" : "ln2"}" d="${d}"/>`;
      if (si === 0) { const last = s.points[s.points.length - 1]; svg += `<circle class="end" cx="${xs(n - 1)}" cy="${ys(last[1])}" r="4"/>`; }
    });
    if (opts && opts.xlabels) svg += `<text class="lbl" x="${L}" y="${h - 4}">${esc(opts.xlabels[0])}</text><text class="lbl" x="${w - R}" y="${h - 4}" text-anchor="end">${esc(opts.xlabels[1])}</text>`;
    el.innerHTML = svg + "</svg>";
  }

  function barChart(el, rows) {
    const w = Math.max(280, el.clientWidth - 40), h = 190, L = 46, B = 22, T = 8;
    const maxV = Math.max(1, ...rows.map((r) => r.v));
    const bw = (w - L) / rows.length;
    const ys = (v) => T + (h - B - T) * (1 - v / maxV);
    let svg = `<svg class="chart" width="${w}" height="${h}" viewBox="0 0 ${w} ${h}" role="img" aria-label="traffic">`;
    svg += `<g class="grid"><line x1="${L}" x2="${w}" y1="${ys(0)}" y2="${ys(0)}"/><line x1="${L}" x2="${w}" y1="${ys(maxV)}" y2="${ys(maxV)}"/></g>`;
    svg += `<text class="lbl" x="${L - 8}" y="${ys(maxV) + 4}" text-anchor="end">${esc(fmtBytes(maxV))}</text>`;
    rows.forEach((r, i) => {
      const x = L + i * bw + bw * 0.18, bh = Math.max(r.v ? 2 : 0, ys(0) - ys(r.v));
      svg += `<rect class="vbar" x="${x.toFixed(1)}" y="${(ys(0) - bh).toFixed(1)}" width="${(bw * 0.64).toFixed(1)}" height="${bh.toFixed(1)}" rx="3"><title>${esc(r.l)}: ${esc(fmtBytes(r.v))}</title></rect>`;
      if (i % 2 === 0 || rows.length < 8) svg += `<text class="lbl" x="${(x + bw * 0.32).toFixed(1)}" y="${h - 4}" text-anchor="middle">${esc(r.l)}</text>`;
    });
    el.innerHTML = svg + "</svg>";
  }

  /* ---------------------------------------------------------------- uploads */
  function uploader(box) {
    const chunk = Number(box.dataset.chunk) || 32 * 1024 * 1024;
    const folder = box.dataset.folder || "";
    const items = new Map();
    const pending = JSON.parse(box.dataset.pending || "[]");

    function row(it) {
      const pc = it.size ? (it.received / it.size) * 100 : 0;
      let status = "";
      if (it.state === "done") status = t("js_up_done");
      else if (it.state === "error") status = `<span class="status-err">${esc(it.error)}</span>`;
      else if (it.state === "waiting") status = t("js_up_waiting");
      else if (it.state === "stalled") status = t("js_up_reselect");
      else status = `${fmtBytes(it.received)} / ${fmtBytes(it.size)} · ${pc.toFixed(0)}%${it.speed ? " · " + fmtBytes(it.speed) + "/s" : ""}${it.eta ? " · " + fmtDur(it.eta) : ""}`;
      return `<div class="panel up" data-up="${esc(it.key)}"><span><b>${esc(it.name)}</b> <span class="note">→ /${esc(it.folder)}</span></span>
        <span style="display:flex;gap:8px;align-items:center"><small>${status}</small>
        ${it.state !== "done" ? `<button class="icon-btn" type="button" data-cancel="${esc(it.key)}" aria-label="${esc(t("js_cancel"))}">${icon("x", 16)}</button>` : ""}</span>
        <div class="bar"><i style="width:${pc}%"></i></div></div>`;
    }
    function draw() { box.innerHTML = Array.from(items.values()).map(row).join(""); }
    box.addEventListener("click", async (e) => {
      const b = e.target.closest("[data-cancel]");
      if (!b) return;
      const it = items.get(b.dataset.cancel);
      if (!it) return;
      it.cancelled = true;
      if (it.ctrl) it.ctrl.abort();
      if (it.id) { try { await api("DELETE", "/api/uploads/" + it.id); } catch (err) { /* ignore */ } }
      items.delete(it.key); draw();
    });

    pending.forEach((p) => {
      const key = "p" + p.id;
      items.set(key, {key, id: p.id, name: p.filename, folder: p.folder, size: p.size, received: p.received, state: "stalled"});
    });

    let queue = Promise.resolve();
    function add(file) {
      const key = file.name + ":" + file.size;
      for (const [k, v] of items) if (v.state === "stalled" && v.name === file.name && v.size === file.size) items.delete(k);
      const it = {key, file, name: file.name, folder, size: file.size, received: 0, state: "waiting"};
      items.set(key, it); draw();
      queue = queue.then(() => run(it));
    }
    async function run(it) {
      if (it.cancelled) return;
      it.state = "running"; draw();
      try {
        const r = await api("POST", "/api/uploads", {filename: it.name, size: it.size, folder: it.folder});
        it.id = r.id; it.received = r.received;
        let lastT = performance.now(), lastB = it.received, tries = 0;
        while (it.received < it.size && !it.cancelled) {
          const end = Math.min(it.size, it.received + chunk);
          const blob = it.file.slice(it.received, end);
          it.ctrl = new AbortController();
          try {
            const res = await api("PUT", `/api/uploads/${it.id}?offset=${it.received}`, blob, {signal: it.ctrl.signal, headers: {"Content-Type": "application/octet-stream"}});
            it.received = res.received; tries = 0;
          } catch (err) {
            if (it.cancelled) return;
            if (err.status === 409 && err.data && err.data.received != null) { it.received = err.data.received; continue; }
            if (++tries > 30) throw err;
            it.state = "running"; it.speed = 0; draw();
            await new Promise((ok) => setTimeout(ok, Math.min(30000, 1000 * tries)));
            try { const s = await api("GET", "/api/uploads/" + it.id); it.received = s.received; } catch (e2) { /* retry */ }
            continue;
          }
          const now = performance.now();
          if (now - lastT > 800) {
            it.speed = ((it.received - lastB) / (now - lastT)) * 1000;
            it.eta = it.speed ? (it.size - it.received) / it.speed : 0;
            lastT = now; lastB = it.received;
          }
          draw();
        }
        if (it.cancelled) return;
        it.state = "running"; it.speed = 0; it.eta = 0; draw();
        const done = await api("POST", `/api/uploads/${it.id}/complete`);
        it.state = "done"; draw();
        toast(t("js_up_finished", {name: it.name}));
        setTimeout(() => location.reload(), done.status === "ready" ? 900 : 2000);
      } catch (err) {
        it.state = "error";
        it.error = err.message === "exists" ? t("js_up_exists") : err.message === "disk" ? t("js_up_disk") : err.message;
        draw();
      }
    }
    draw();
    return {add};
  }

  /* ---------------------------------------------------------------- pages */
  const Page = {};

  Page.dashboard = function () {
    const body = $("#dashActive"), n = $("#dashActiveN");
    async function tick() {
      try {
        const d = await api("GET", "/api/live");
        const cs = d.clients;
        n.textContent = cs.length;
        body.innerHTML = cs.length ? cs.map((c) => `<tr><td style="width:24px"><span class="live-dot" style="display:inline-block"></span></td>
          <td><b>${esc(c.hostname || c.mac || c.ip)}</b></td><td>${c.platform ? `<span class="chip yes">${esc(c.platform)}</span>` : ""}</td>
          <td>${esc(c.iso_name || "–")}</td><td style="min-width:180px">${c.phase === "loading" && c.progress != null ? `<div class="prog"><div class="bar"><i style="width:${c.progress}%"></i></div></div>` : phasePill(c)}</td>
          <td class="num">${c.phase === "loading" && c.progress != null ? c.progress.toFixed(0) + "%" : ""}</td></tr>`).join("")
          : `<tr><td class="note">${esc(t("js_nolive"))}</td></tr>`;
      } catch (e) { /* ignore */ }
      setTimeout(tick, 2000);
    }
    tick();
  };

  Page.live = function () {
    const box = $("#clients"), empty = $("#liveEmpty");
    async function tick() {
      try {
        const d = await api("GET", "/api/live");
        box.innerHTML = d.clients.map(clientCard).join("");
        empty.hidden = d.clients.length > 0;
      } catch (e) { /* ignore */ }
      setTimeout(tick, 1500);
    }
    tick();
  };

  Page.stats = function () {
    const chart = $("#bwChart");
    const dayEl = $("#dayChart");
    const days = JSON.parse(dayEl.dataset.days || "[]");
    const drawDays = () => barChart(dayEl, days.map((d) => ({l: d.day, v: d.bytes})));
    drawDays();
    let last = null;
    async function tick() {
      try {
        const d = await api("GET", "/api/live");
        last = d;
        const bw = d.bandwidth;
        const now = bw.length ? bw[bw.length - 1] : {tx: 0, rx: 0};
        $("#bwNow").innerHTML = `${fmtMbit(now.tx)} <small style="font-size:.9rem">Mbit/s</small>`;
        $("#bwLink").textContent = t("js_of_link", {n: d.link_mbit});
        const peak = bw.reduce((m, p) => Math.max(m, p.tx), 0);
        $("#bwPeak").innerHTML = `${fmtMbit(peak)} <small style="font-size:.9rem">Mbit/s</small>`;
        $("#sentToday").textContent = fmtBytes(d.sent_today);
        $("#tasksN").textContent = d.tasks;
        $("#tasksSub").textContent = t("js_tasks_sub", {i: d.counts.installer, l: d.counts.loading});
        $("#tkMenu").textContent = d.counts.menu + d.counts.ipxe;
        $("#tkLoad").textContent = d.counts.loading;
        $("#tkInst").textContent = d.counts.installer;
        $("#tkUp").textContent = d.uploads.length;
        draw();
      } catch (e) { /* ignore */ }
      setTimeout(tick, 1000);
    }
    function draw() {
      if (!last) return;
      const bw = last.bandwidth;
      lineChart(chart, [{points: bw.map((p) => [p.t, p.tx / 1e6])}, {points: bw.map((p) => [p.t, p.rx / 1e6])}],
        {height: 220, label: "bandwidth", xlabels: [t("js_minutes_ago", {n: Math.round(bw.length / 60) || 5}), t("js_now")]});
    }
    window.addEventListener("resize", () => { draw(); drawDays(); });
    tick();
  };

  Page.library = function () {
    const box = $("#uploads");
    const up = uploader(box);
    const input = $("#file"), drop = $("#drop");
    input.addEventListener("change", () => { Array.from(input.files).forEach(up.add); input.value = ""; });
    ["dragenter", "dragover"].forEach((ev) => drop.addEventListener(ev, (e) => { e.preventDefault(); drop.classList.add("over"); }));
    ["dragleave", "drop"].forEach((ev) => drop.addEventListener(ev, (e) => { e.preventDefault(); drop.classList.remove("over"); }));
    drop.addEventListener("drop", (e) => Array.from(e.dataTransfer.files).filter((f) => /\.iso$/i.test(f.name)).forEach(up.add));
    window.addEventListener("beforeunload", (e) => { if ($$(".up").some((x) => !/done/.test(x.textContent) && x.querySelector("[data-cancel]"))) { /* uploads resume after reload */ } });

    $("#rescan").addEventListener("click", async (e) => {
      e.target.disabled = true;
      try { const r = await api("POST", "/api/library/scan"); toast(t("js_scan_result", r)); setTimeout(() => location.reload(), 800); }
      catch (err) { toast(err.message, "error"); e.target.disabled = false; }
    });
    const nf = $("#newFolder");
    $("#newFolderBtn").addEventListener("click", () => { nf.hidden = !nf.hidden; if (!nf.hidden) $("#newFolderName").focus(); });
    nf.addEventListener("submit", async (e) => {
      e.preventDefault();
      try { const r = await api("POST", "/api/folders", {path: $("#newFolderName").value}); location.href = "/library?folder=" + encodeURIComponent(r.folder); }
      catch (err) { toast(t("js_folder_invalid"), "error"); }
    });
    const df = $("#delFolder");
    if (df) df.addEventListener("click", async () => {
      try { await api("DELETE", "/api/folders?path=" + encodeURIComponent(df.dataset.folder)); location.href = "/library"; }
      catch (err) { toast(t("js_folder_not_empty"), "error"); }
    });
    $$("[data-rename]").forEach((inp) => {
      const save = async () => {
        if (inp.value === inp.defaultValue) return;
        try { await api("PATCH", "/api/isos/" + inp.dataset.rename, {name: inp.value}); inp.defaultValue = inp.value; toast(t("js_saved")); }
        catch (err) { toast(err.message, "error"); }
      };
      inp.addEventListener("change", save);
      inp.addEventListener("keydown", (e) => { if (e.key === "Enter") inp.blur(); });
    });
    $$("[data-toggle]").forEach((b) => b.addEventListener("click", async () => {
      const on = !b.classList.contains("on");
      try { await api("PATCH", "/api/isos/" + b.dataset.toggle, {enabled: on}); b.classList.toggle("on", on); b.setAttribute("aria-pressed", on); }
      catch (err) { toast(err.message, "error"); }
    }));
    $$("[data-method]").forEach((s) => s.addEventListener("change", async () => {
      try { await api("PATCH", "/api/isos/" + s.dataset.method, {method_override: s.value}); location.reload(); }
      catch (err) { toast(err.message, "error"); }
    }));
    $$("[data-move]").forEach((s) => s.addEventListener("change", async () => {
      try { await api("PATCH", "/api/isos/" + s.dataset.move, {folder: s.value}); location.reload(); }
      catch (err) { toast(t("js_move_failed"), "error"); }
    }));
    $$("[data-redetect]").forEach((b) => b.addEventListener("click", async () => {
      try { await api("PATCH", "/api/isos/" + b.dataset.redetect, {redetect: true}); location.reload(); }
      catch (err) { toast(err.message, "error"); }
    }));
    $$("[data-delete]").forEach((b) => b.addEventListener("click", async () => {
      if (!(await confirmBox(t("js_confirm_delete_iso", {name: b.dataset.name}), t("js_delete"), true))) return;
      try { await api("DELETE", "/api/isos/" + b.dataset.delete); location.reload(); }
      catch (err) { toast(err.message, "error"); }
    }));
  };

  Page.catalog = function () {
    const panel = $("#dlPanel"), list = $("#dlList");
    const folderOpts = $("#folderOpts").innerHTML;
    const errText = (e) => e.message === "exists" ? t("js_up_exists") : e.message === "name" ? t("js_cat_bad_name")
      : e.message === "url" ? t("js_cat_bad_url") : e.message === "checksum" ? t("js_cat_bad_sum") : e.message;

    function dlRow(d) {
      const pc = d.size ? (d.received / d.size) * 100 : 0;
      let status;
      if (d.status === "done") status = `<span class="pill ok">${esc(t("js_cat_done"))}</span>`;
      else if (d.status === "error") status = `<span class="status-err">${esc(d.error === "checksum mismatch" ? t("js_cat_mismatch") : d.error)}</span>`;
      else if (d.status === "cancelled") status = esc(t("js_cat_cancelled"));
      else if (d.status === "queued") status = esc(t("js_up_waiting"));
      else if (d.status === "verifying") status = esc(t("js_cat_verifying"));
      else status = `${fmtBytes(d.received)}${d.size ? " / " + fmtBytes(d.size) + " · " + pc.toFixed(0) + "%" : ""}${d.speed ? " · " + fmtBytes(d.speed) + "/s" : ""}`;
      const verified = d.status === "done" ? (d.expected ? " · " + t("js_cat_verified") : " · " + t("js_cat_unverified")) : "";
      const btns = [];
      if (d.status === "error" || d.status === "cancelled") btns.push(`<button class="icon-btn" type="button" data-retry="${d.id}" aria-label="${esc(t("js_cat_retry"))}" title="${esc(t("js_cat_retry"))}">${icon("refresh", 16)}</button>`);
      btns.push(`<button class="icon-btn" type="button" data-dl-x="${d.id}" aria-label="${esc(t("js_cancel"))}" title="${esc(t("js_cancel"))}">${icon("x", 16)}</button>`);
      return `<div class="up"><span><b>${esc(d.name)}</b> <span class="note">→ /${esc(d.folder)}</span></span>
        <span style="display:flex;gap:8px;align-items:center"><small>${status}${esc(verified)}</small>${btns.join("")}</span>
        <div class="bar"><i style="width:${d.status === "done" ? 100 : pc}%"></i></div></div>`;
    }
    const last = {};
    async function poll() {
      let busy = false;
      try {
        const r = await api("GET", "/api/downloads");
        const now = Date.now();
        r.downloads.forEach((d) => {
          const p = last[d.id];
          if (p && d.status === "downloading" && now > p.t) d.speed = Math.max(0, (d.received - p.b) / ((now - p.t) / 1000));
          last[d.id] = {t: now, b: d.received};
          if (["queued", "downloading", "verifying"].includes(d.status)) busy = true;
          if (d.status === "done" && p && p.s !== "done") markInLibrary(d.filename);
          last[d.id].s = d.status;
        });
        panel.hidden = !r.downloads.length;
        list.innerHTML = r.downloads.map(dlRow).join("");
      } catch (e) { /* ignore */ }
      setTimeout(poll, busy ? 1500 : 6000);
    }
    list.addEventListener("click", async (e) => {
      const x = e.target.closest("[data-dl-x]"), rt = e.target.closest("[data-retry]");
      try {
        if (x) await api("DELETE", "/api/downloads/" + x.dataset.dlX);
        if (rt) await api("POST", `/api/downloads/${rt.dataset.retry}/retry`);
      } catch (err) { toast(err.message, "error"); }
      if (x || rt) setTimeout(poll, 100);
    });

    const files = {};
    function markInLibrary(filename) {
      $$(".catcard").forEach((c) => { if (files[c.dataset.entry] === filename) c.querySelector("[data-chips]").insertAdjacentHTML("afterbegin", `<span class="chip yes">${esc(t("js_cat_in_lib"))}</span> `); });
    }
    $$(".catcard").forEach(async (card) => {
      const ver = $("[data-ver]", card), btn = $("[data-get]", card), chips = $("[data-chips]", card);
      try {
        const r = await api("GET", "/api/catalog/" + card.dataset.entry);
        files[card.dataset.entry] = r.filename;
        ver.textContent = `${r.version ? t("js_cat_version", {v: r.version}) + " · " : ""}${r.hash ? r.algo.toUpperCase() + " " + t("js_cat_checked") : t("js_cat_nosum")}`;
        if (r.pinned) chips.insertAdjacentHTML("beforeend", ` <span class="chip" title="${esc(t("js_cat_pinned_d"))}">${esc(t("js_cat_pinned"))}</span>`);
        if (r.in_library) chips.insertAdjacentHTML("afterbegin", `<span class="chip yes">${esc(t("js_cat_in_lib"))}</span> `);
        btn.disabled = false;
      } catch (e) {
        ver.innerHTML = `<span class="status-err">${esc(t("js_cat_unreachable"))}</span>`;
        ver.title = e.message;
      }
      btn.addEventListener("click", () => askFolder(card.dataset.name, card.dataset.folder, async (folder) => {
        await api("POST", `/api/catalog/${card.dataset.entry}/download`, {folder});
      }));
    });

    function askFolder(name, folder, go) {
      modal(`<div class="modal-h"><h2>${esc(name)}</h2></div><form class="modal-b" id="dlf">
        <div class="field"><label for="dl_folder">${esc(t("js_cat_folder"))}</label>
        <input class="input" id="dl_folder" list="dl_folders" value="${esc(folder)}"><datalist id="dl_folders">${folderOpts}</datalist>
        <span class="hint">${esc(t("js_cat_folder_hint"))}</span></div>
        <div class="actions"><button class="btn btn-outline" data-close type="button">${esc(t("js_cancel"))}</button>
        <button class="btn btn-accent" type="submit">${icon("download")}${esc(t("js_cat_start"))}</button></div></form>`,
      (root, close) => {
        $("#dlf", root).addEventListener("submit", async (e) => {
          e.preventDefault();
          try { await go($("#dl_folder", root).value.trim()); close(); toast(t("js_cat_queued")); poll(); }
          catch (err) { toast(errText(err), "error"); }
        });
      });
    }

    $("#fromUrl").addEventListener("click", () => {
      modal(`<div class="modal-h"><h2>${esc(t("js_cat_url_t"))}</h2></div><form class="modal-b" id="urlf">
        <div class="field"><label for="u_url">URL</label><input class="input" id="u_url" type="url" required placeholder="https://…/image.iso"></div>
        <div class="field"><label for="u_name">${esc(t("js_cat_filename"))}</label><input class="input" id="u_name" placeholder="${esc(t("js_cat_filename_ph"))}"></div>
        <div class="field"><label for="u_folder">${esc(t("js_cat_folder"))}</label><input class="input" id="u_folder" list="u_folders"><datalist id="u_folders">${folderOpts}</datalist></div>
        <div class="field"><label for="u_sum">${esc(t("js_cat_sum"))}</label><input class="input num" id="u_sum" placeholder="SHA-256 / SHA-512 / SHA-1 / MD5"><span class="hint">${esc(t("js_cat_sum_hint"))}</span></div>
        <div class="actions"><button class="btn btn-outline" data-close type="button">${esc(t("js_cancel"))}</button>
        <button class="btn btn-accent" type="submit">${icon("download")}${esc(t("js_cat_start"))}</button></div></form>`,
      (root, close) => {
        $("#urlf", root).addEventListener("submit", async (e) => {
          e.preventDefault();
          try {
            await api("POST", "/api/downloads", {url: $("#u_url", root).value.trim(), filename: $("#u_name", root).value.trim(),
              folder: $("#u_folder", root).value.trim(), checksum: $("#u_sum", root).value.trim()});
            close(); toast(t("js_cat_queued")); poll();
          } catch (err) { toast(errText(err), "error"); }
        });
      });
    });
    poll();
  };

  Page.logs = function () {
    const tabs = $$("#logTabs button"), head = $("#logHead"), body = $("#logBody"), q = $("#logQ"), all = $("#logAll");
    const pauseBtn = $("#logPause"), errBox = $("#logErr"), allWrap = $("#allWrap");
    let kind = "http", paused = false, timer = null, seq = 0;
    try { const k = sessionStorage.getItem("logKind"); if (k) kind = k; } catch (e) { /* ignore */ }
    const fmtTs = (ts) => { const d = new Date(ts * 1000); return d.toLocaleDateString([], {day: "2-digit", month: "2-digit"}) + " " + d.toTimeString().slice(0, 8); };
    const statusPill = (s) => `<span class="pill ${s < 300 ? "ok" : s < 400 ? "" : s < 500 ? "warn" : "off"}">${s}</span>`;
    const heads = {
      http: [t("js_log_time"), t("js_log_client"), t("js_log_status"), t("js_log_size"), t("js_log_path")],
      tftp: [t("js_log_time"), t("js_log_client"), t("js_log_file"), t("js_log_message")],
      dhcp: [t("js_log_time"), t("js_log_client"), "MAC", t("js_log_message")],
    };
    const rows = {
      http: (e) => `<td class="num">${fmtTs(e.ts)}</td><td class="num">${esc(e.ip)}</td><td>${statusPill(e.status)}</td><td class="num">${fmtBytes(e.bytes)}</td><td class="num" style="word-break:break-all">${esc(e.path)}</td>`,
      tftp: (e) => `<td class="num">${fmtTs(e.ts)}</td><td class="num">${esc(e.ip || "–")}</td><td>${e.file ? `<span class="pill ${e.ok ? "ok" : "off"}">${esc(e.file)}</span>` : "–"}</td><td class="small">${esc(e.message)}</td>`,
      dhcp: (e) => `<td class="num">${fmtTs(e.ts)}</td><td class="num">${esc(e.ip || "–")}</td><td class="num">${esc(e.mac || "–")}</td><td class="small">${esc(e.message)}</td>`,
    };
    function show() {
      tabs.forEach((b) => { const on = b.dataset.kind === kind; b.classList.toggle("on", on); b.setAttribute("aria-selected", on); });
      allWrap.hidden = kind !== "http";
      head.innerHTML = "<tr>" + heads[kind].map((h) => `<th>${esc(h)}</th>`).join("") + "</tr>";
    }
    async function load() {
      clearTimeout(timer);
      const my = ++seq;
      try {
        const r = await api("GET", `/api/logs?kind=${kind}&q=${encodeURIComponent(q.value)}&all=${all.checked ? 1 : 0}`);
        if (my !== seq) return;
        body.innerHTML = r.lines.length ? r.lines.map((e) => "<tr>" + rows[kind](e) + "</tr>").join("")
          : `<tr><td class="note" colspan="5">${esc(t("js_log_empty"))}</td></tr>`;
        const notes = [];
        if (r.error) notes.push(t("js_log_error") + ": " + r.error);
        if (kind === "dhcp" && !r.log_dhcp) notes.push(t("js_log_dhcp_off"));
        errBox.hidden = !notes.length; errBox.textContent = notes.join(" ");
      } catch (e) { /* retry */ }
      if (!paused) timer = setTimeout(load, 3000);
    }
    tabs.forEach((b) => b.addEventListener("click", () => {
      kind = b.dataset.kind; try { sessionStorage.setItem("logKind", kind); } catch (e) { /* ignore */ }
      show(); body.innerHTML = `<tr><td class="note">…</td></tr>`; load();
    }));
    let qt; q.addEventListener("input", () => { clearTimeout(qt); qt = setTimeout(load, 300); });
    all.addEventListener("change", load);
    pauseBtn.addEventListener("click", () => {
      paused = !paused;
      pauseBtn.setAttribute("aria-pressed", paused);
      $("span", pauseBtn).textContent = paused ? t("js_log_paused") : t("js_log_live");
      if (!paused) load(); else clearTimeout(timer);
    });
    show(); load();
  };

  Page.menu = function () {
    const box = $("#menuPreview"), raw = $("#menuScript");
    async function load(fw) {
      const script = await api("GET", "/api/menu/preview?platform=" + fw);
      raw.textContent = script;
      box.innerHTML = renderMenu(script, fw);
    }
    function renderMenu(script, fw) {
      // Show the first menu of the script the way iPXE draws it.
      const lines = script.split("\n");
      const out = [];
      let inMenu = false, first = true;
      for (const l of lines) {
        if (l.startsWith("menu ")) {
          if (inMenu) break;
          inMenu = true;
          const title = l.slice(5).replace("${fwname}", fw === "efi" ? "UEFI" : "Legacy BIOS").replace("${netX/ip}", "192.0.2.10");
          out.push(`<div class="row t">  ${esc(title)}</div><div class="row g">  ${"─".repeat(46)}</div>`);
        } else if (inMenu && l.startsWith("item --gap")) {
          const txt = l.replace(/^item --gap --\s?/, "");
          out.push(`<div class="row g">  ${esc(txt || " ")}</div>`);
        } else if (inMenu && l.startsWith("item ")) {
          const txt = l.replace(/^item \S+ /, "");
          out.push(`<div class="row ${first ? "sel" : ""}">  ${esc(txt)}</div>`);
          first = false;
        } else if (inMenu && l.startsWith("choose")) {
          const m = /--timeout \$\{menu-timeout\}/.test(l);
          const to = (script.match(/set menu-timeout (\d+)/) || [0, 0])[1] / 1000;
          if (m && to) out.push(`<div class="row g"> </div><div class="row g">  ${esc(t("js_menu_timeout", {n: to}))}</div>`);
          break;
        }
      }
      return out.join("");
    }
    $$("[data-fw]").forEach((b) => b.addEventListener("click", () => {
      $$("[data-fw]").forEach((x) => x.classList.toggle("on", x === b));
      load(b.dataset.fw);
    }));
    load("efi");
  };

  Page.answers = function () {
    $$("[data-del-answer]").forEach((b) => b.addEventListener("click", async () => {
      if (!(await confirmBox(t("js_confirm_delete", {name: b.dataset.name}), t("js_delete"), true))) return;
      await api("DELETE", "/api/answers/" + b.dataset.delAnswer); location.reload();
    }));
  };

  Page.answerWizard = function (editing) {
    const form = $("#wizForm");
    if (!form) return;
    let step = 1;
    const steps = $$(".wstep", form), nav = $$("#wsteps [data-step]");
    const back = $("#wBack"), next = $("#wNext"), save = $("#wSave"), wipe = $("#w_wipe");
    const iso = $("#w_iso");
    const fmt = () => iso.selectedOptions[0] ? iso.selectedOptions[0].dataset.fmt : "";
    function show() {
      steps.forEach((s) => { s.hidden = Number(s.dataset.s) !== step; });
      nav.forEach((b) => {
        const n = Number(b.dataset.step);
        b.classList.toggle("on", n === step); b.classList.toggle("done", n < step);
        b.querySelector(".n").textContent = n < step ? "✓" : n;
      });
      back.style.visibility = step > 1 ? "visible" : "hidden";
      next.hidden = step === 6; save.hidden = step !== 6;
    }
    function applyFormat() {
      const f = fmt();
      $$("[data-only]", form).forEach((el) => { el.hidden = !el.dataset.only.split(" ").includes(f); });
      $$("[data-not]", form).forEach((el) => { el.hidden = el.dataset.not.split(" ").includes(f); });
      const st = $("#w_network").value === "static" && f !== "autounattend";
      $$("[data-static]", form).forEach((el) => { el.hidden = !st; });
      const wipeOn = $("#w_win_disk") && $("#w_win_disk").value === "wipe";
      $$("[data-wipe]", form).forEach((el) => { el.hidden = !wipeOn; });
      const opt = iso.selectedOptions[0];
      $("#entryPreview").textContent = `${opt ? opt.textContent : ""} - ${$("#w_name").value || "…"} (unattended)`;
    }
    // Windows: editions read from the ISO; a plain index field when they are unknown
    const edSel = $("#w_win_edition"), edIdx = $("#w_win_index"), edHint = $("#w_edition_hint");
    let edIso = null;
    function fillEditions() {
      if (!edSel) return;
      const opt = iso.selectedOptions[0];
      if (!opt || opt.value === edIso) return;
      edIso = opt.value;
      let eds = [];
      try { eds = JSON.parse(opt.dataset.editions || "[]"); } catch (e) { eds = []; }
      edSel.innerHTML = "";
      eds.forEach((e) => {
        const o = document.createElement("option");
        o.value = e.index;
        o.textContent = `${e.index}. ${e.name}${e.lang ? " · " + e.lang : ""}${e.arch ? " · " + e.arch : ""}`;
        edSel.appendChild(o);
      });
      const want = edIdx.value || edSel.dataset.value;
      if (eds.some((e) => String(e.index) === String(want))) edSel.value = want;
      edSel.hidden = !eds.length; edIdx.hidden = !!eds.length; edHint.hidden = !!eds.length;
      if (eds.length) edIdx.value = edSel.value;
    }
    if (edSel) edSel.addEventListener("change", () => { edIdx.value = edSel.value; });
    iso.addEventListener("change", () => { edIso = null; fillEditions(); });
    fillEditions();
    let timer = null;
    function preview() {
      clearTimeout(timer);
      timer = setTimeout(async () => {
        const data = Object.fromEntries(new FormData(form).entries());
        data.reboot = form.reboot.checked;
        try {
          const r = await api("POST", "/api/answers/preview", data);
          $("#fmtLabel").textContent = r.label;
          $("#previewName").textContent = r.name;
          $("#wcode").textContent = r.text;
        } catch (e) { /* ignore */ }
      }, 250);
    }
    nav.forEach((b) => b.addEventListener("click", () => { step = Number(b.dataset.step); show(); }));
    back.addEventListener("click", () => { step = Math.max(1, step - 1); show(); });
    next.addEventListener("click", () => {
      if (step === 1 && !$("#w_name").value.trim()) { $("#w_name").focus(); $("#w_name").reportValidity(); return; }
      step = Math.min(6, step + 1); show();
    });
    wipe.addEventListener("change", () => { save.disabled = !wipe.checked; });
    form.addEventListener("input", () => { applyFormat(); preview(); });
    form.addEventListener("change", () => { applyFormat(); preview(); });
    form.addEventListener("keydown", (e) => { if (e.key === "Enter" && e.target.tagName === "INPUT") { e.preventDefault(); if (step < 6) next.click(); } });
    applyFormat(); show(); preview();
  };

  Page.machines = function () {
    const pick = $("#mf_pick"), mac = $("#mf_mac"), isoSel = $("#mf_iso"), ans = $("#mf_ans");
    if (pick) pick.addEventListener("change", () => { if (pick.value) mac.value = pick.value; });
    const modes = $("#mf_modes");
    function filter() {
      if (!isoSel) return;
      $$("option[data-iso]", ans).forEach((o) => { o.hidden = o.dataset.iso !== isoSel.value; if (o.hidden && o.selected) ans.value = ""; });
      ans.disabled = !isoSel.value;
      if (modes) modes.hidden = !isoSel.value;
    }
    if (isoSel) { isoSel.addEventListener("change", filter); filter(); }
    $$("[data-del-group]").forEach((b) => b.addEventListener("click", async () => {
      if (!(await confirmBox(t("js_confirm_delete", {name: b.dataset.name}), t("js_delete"), true))) return;
      await api("DELETE", "/api/groups/" + b.dataset.delGroup); location.reload();
    }));
    $$("[data-rearm-group]").forEach((b) => b.addEventListener("click", async () => {
      await api("POST", `/api/groups/${b.dataset.rearmGroup}/rearm`); location.reload();
    }));
    $$("[data-del-machine]").forEach((b) => b.addEventListener("click", async () => {
      if (!(await confirmBox(t("js_confirm_delete", {name: b.dataset.name}), t("js_delete"), true))) return;
      await api("DELETE", "/api/machines/" + b.dataset.delMachine); location.reload();
    }));
    $$("[data-rearm]").forEach((b) => b.addEventListener("click", async () => {
      await api("POST", `/api/machines/${b.dataset.rearm}/rearm`); location.reload();
    }));
  };

  Page.network = function () {
    const full = $("#fullDhcp");
    $$("input[name=dhcp_mode]").forEach((r) => r.addEventListener("change", () => { full.hidden = $("input[name=dhcp_mode]:checked").value !== "full"; }));
  };

  Page.users = function () {
    $$("[data-del-user]").forEach((b) => b.addEventListener("click", async () => {
      if (!(await confirmBox(t("js_confirm_delete", {name: b.dataset.name}), t("js_delete"), true))) return;
      await api("DELETE", "/api/users/" + b.dataset.delUser); location.reload();
    }));
  };

  Page.mail = function () {
    const btn = $("#testMail"), form = $("#mailForm");
    btn.addEventListener("click", async () => {
      const data = Object.fromEntries(new FormData(form).entries());
      data.verify = form.verify.checked ? "1" : "";
      data.to = $("#m_testto").value;
      btn.disabled = true;
      try { const r = await api("POST", "/api/mail/test", data); toast(t("js_mail_sent", {to: r.to})); }
      catch (e) { toast(e.message === "no_recipient" ? t("js_mail_no_to") : t("js_mail_failed") + ": " + e.message, "error"); }
      btn.disabled = false;
    });
  };

  Page.settings = function () {
    const sel = $("#a_theme");
    if (!sel) return;
    try { sel.value = localStorage.getItem("theme") || ""; } catch (e) { /* ignore */ }
    sel.addEventListener("change", () => {
      const r = document.documentElement;
      if (sel.value) r.dataset.theme = sel.value; else delete r.dataset.theme;
      try { if (sel.value) localStorage.setItem("theme", sel.value); else localStorage.removeItem("theme"); } catch (e) { /* ignore */ }
    });
  };

  window.PXEPage = Page;
  window.PXEUtil = {api, toast, t, fmtBytes};
  shell();
})();
