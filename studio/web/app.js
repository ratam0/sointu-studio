"use strict";
/* Sointu Studio – selainkäyttöliittymä (ei riippuvuuksia) */

const $ = (s, el = document) => el.querySelector(s);
const $$ = (s, el = document) => [...el.querySelectorAll(s)];
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const fmtDur = (s) => { s = Math.round(s || 0); return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}`; };
const fmtEta = (s) => { if (s == null) return "–"; s = Math.round(s); const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60); return h ? `${h} h ${m} min` : m ? `${m} min` : `${s} s`; };
const fmtLoss = (v) => v == null ? "–" : Number(v) < 0.01 ? Number(v).toExponential(1) : Number(v).toFixed(4);
const fmtTime = (t) => t ? new Date(t * 1000).toLocaleString("fi-FI", { day: "numeric", month: "numeric", hour: "2-digit", minute: "2-digit" }) : "";

const state = {
  projects: [], project: null, tab: "tracks", openTracks: new Set(),
  jobId: null, logOffset: 0, logJob: null, status: null, playing: null,
  section: "music", voices: [], voice: null, vtab: "data", jobsAll: [],
  rvcModels: [], aceSources: [], conversions: [], convSrc: "upload", convInput: null,
};
const audio = new Audio();
audio.preload = "none";

// ------------------------------------------------------------------ API
async function api(path, opts = {}) {
  const init = { method: opts.method || "GET", headers: {} };
  if (opts.body !== undefined) { init.headers["Content-Type"] = "application/json"; init.body = JSON.stringify(opts.body); }
  const r = await fetch(path, init);
  let data = null;
  try { data = await r.json(); } catch { /* tyhjä vastaus */ }
  if (!r.ok) throw new Error((data && (data.error || data.detail?.[0]?.msg || data.detail)) || `Virhe ${r.status}`);
  return data;
}

function toast(msg, err = false) {
  const t = $("#toast");
  t.textContent = msg;
  t.className = "toast show" + (err ? " err" : "");
  clearTimeout(toast._t);
  toast._t = setTimeout(() => t.className = "toast", err ? 6000 : 2800);
}
const guard = (fn) => async (...a) => { try { await fn(...a); } catch (e) { toast(e.message, true); } };

// ------------------------------------------------------------------ projektit
async function loadProjects() {
  state.projects = await api("/api/projects");
  if (state.section !== "music") return;
  const nav = $("#projectList");
  nav.innerHTML = state.projects.map((p) => `
    <button data-id="${p.id}" aria-current="${state.project?.id === p.id}">
      <span class="pn">${esc(p.name)}</span>
      <span class="pm">${p.tracks} kappaletta, ${p.runs} ajoa</span>
    </button>`).join("") || `<p class="muted" style="padding:0 10px;font-size:13px">Ei projekteja vielä.</p>`;
  $$("button", nav).forEach((b) => b.onclick = () => openProject(b.dataset.id));
}

const newProject = guard(async () => {
  const name = prompt("Projektin nimi (esim. artistinimi tai tyyli):");
  if (!name) return;
  const p = await api("/api/projects", { method: "POST", body: { name } });
  await loadProjects();
  await openProject(p.id);
});

async function openProject(id, keepTab = false) {
  state.project = await api(`/api/projects/${id}`);
  if (!keepTab) { state.tab = "tracks"; state.openTracks.clear(); }
  localStorageSet("lastProject", id);
  showSection("music");
  $$("#projectList button").forEach((b) => b.setAttribute("aria-current", b.dataset.id === id));
  renderProject();
  const jl = state.project.jobs || [];
  const pick = jl.find((j) => j.status === "running") || jl.find((j) => j.status === "queued") || jl[0];
  if (pick && !keepTab) selectJob(pick.id);
}

function localStorageSet(k, v) { try { localStorage.setItem(k, v); } catch { /* ei tallennusta */ } }
function localStorageGet(k) { try { return localStorage.getItem(k); } catch { return null; } }

async function refreshProject() {
  if (!state.project) return;
  try {
    const p = await api(`/api/projects/${state.project.id}`);
    const changed = JSON.stringify(p) !== JSON.stringify(state.project);
    state.project = p;
    if (changed) renderProject(true);
  } catch { /* hiljainen */ }
}

function renderProject(soft = false) {
  const p = state.project;
  if (!soft || document.activeElement !== $("#pName")) $("#pName").value = p.name;
  if (!soft || document.activeElement !== $("#pTag")) $("#pTag").value = p.trigger_tag || "";
  $("#pTagPos").value = p.tag_position || "prepend";
  $("#tabTracksCount").textContent = p.tracks.length ? `(${p.tracks.length})` : "";
  setTab(state.tab);
  renderTracks(soft);
  renderTrainForm();
  renderRuns();
  renderListen(soft);
  renderJobs();
}

function setTab(tab) {
  state.tab = tab;
  $$("#project .tabs button").forEach((b) => b.setAttribute("aria-selected", b.dataset.tab === tab));
  $$("#project .panel").forEach((el) => el.hidden = el.dataset.panel !== tab);
}

const patchProject = guard(async (body) => {
  state.project = await api(`/api/projects/${state.project.id}`, { method: "PATCH", body });
  renderProject(true);
  loadProjects();
});

// ------------------------------------------------------------------ kappaleet
function trackState(t) {
  if (!t.include) return `<span class="state off">ei mukana</span>`;
  return t.ready ? `<span class="state ok">valmis</span>` : `<span class="state todo">${esc(t.ready_reason)}</span>`;
}

function renderTracks(soft) {
  const p = state.project;
  const ready = p.tracks.filter((t) => t.ready).length;
  const total = p.tracks.reduce((a, t) => a + (t.include ? t.duration : 0), 0);
  $("#readiness").innerHTML = p.tracks.length
    ? `<b>${ready}/${p.tracks.length}</b> valmiina koulutukseen · ${fmtDur(total)} audiota`
    : "";
  const list = $("#trackList");
  // Älä piirrä avointa editoria uudelleen kesken kirjoittamisen
  const editing = list.contains(document.activeElement) && document.activeElement.closest(".tedit");
  if (soft && editing) {
    p.tracks.forEach((t) => {
      const row = $(`.track[data-id="${t.id}"]`, list);
      if (row) { $(".state", row).outerHTML = trackState(t); }
    });
    return;
  }
  if (!p.tracks.length) {
    list.innerHTML = `<p class="muted" style="padding:16px 4px">Ei kappaleita. Lataa vähintään 5 kappaletta – 10–50 antaa yleensä parhaan tuloksen.</p>`;
    return;
  }
  list.innerHTML = p.tracks.map((t) => {
    const open = state.openTracks.has(t.id);
    const instr = t.is_instrumental;
    return `
    <div class="track ${open ? "open" : ""} ${t.include ? "" : "off"}" data-id="${t.id}">
      <div class="trow">
        <button class="play ${state.playing === t.id ? "on" : ""}" data-act="play" aria-label="Soita ${esc(t.title)}">
          <svg viewBox="0 0 12 12">${state.playing === t.id ? '<rect x="2" y="1" width="3" height="10"/><rect x="7" y="1" width="3" height="10"/>' : '<path d="M2 1l9 5-9 5z"/>'}</svg>
        </button>
        <div style="min-width:0">
          <div class="tname">${esc(t.title)}</div>
          <div class="tcap">${t.caption ? esc(t.caption) : "<i>ei kuvausta</i>"}</div>
        </div>
        <span class="tnum">${fmtDur(t.duration)}${t.duration > 240 ? " ⚠" : ""}</span>
        <span class="tnum">${t.bpm ? t.bpm + " bpm" : "–"}</span>
        <span class="tnum">${esc(t.keyscale || "–")}</span>
        ${trackState(t)}
        <span class="chev" aria-hidden="true">›</span>
      </div>
      <div class="tedit">
        <div class="col">
          <label>Kuvaus (caption) – tyyli, soittimet, laulu, tunnelma
            <textarea data-f="caption" rows="2" maxlength="512">${esc(t.caption)}</textarea></label>
          <label>Genre <input data-f="genre" value="${esc(t.genre)}" maxlength="200"></label>
          <label ${instr ? "hidden" : ""}>Lyriikat – lisää rakennemerkinnät kuten [Verse], [Chorus], [Bridge]
            <textarea data-f="lyrics" rows="10" maxlength="4096">${esc(instr ? "" : t.lyrics)}</textarea></label>
          ${t.notes?.length ? `<ul class="notes">${t.notes.map((n) => `<li>${esc(n)}</li>`).join("")}</ul>` : ""}
        </div>
        <div class="col">
          <div class="seg" role="group" aria-label="Laulu vai instrumentaali">
            <button data-act="vocal" aria-pressed="${instr === false}">Laulua</button>
            <button data-act="instr" aria-pressed="${instr === true}">Instrumentaali</button>
          </div>
          <div class="row2">
            <label>BPM <input data-f="bpm" type="number" min="0" max="300" value="${t.bpm ?? ""}"></label>
            <label>Sävellaji <input data-f="keyscale" value="${esc(t.keyscale)}" placeholder="A minor"></label>
            <label>Tahtilaji
              <select data-f="timesignature">${["2", "3", "4", "6"].map((v) => `<option value="${v}" ${t.timesignature == v ? "selected" : ""}>${v === "6" ? "6/8" : v + "/4"}</option>`).join("")}</select></label>
            <label>Kieli <input data-f="language" value="${esc(t.language)}" placeholder="fi, en…" maxlength="10"></label>
          </div>
          <label>Nimi <input data-f="title" value="${esc(t.title)}"></label>
          <label class="chk"><input type="checkbox" data-f="include" ${t.include ? "checked" : ""}> Mukana koulutuksessa</label>
          <div class="saved" data-saved></div>
          <button class="btn ghost danger small" data-act="del" style="align-self:flex-start">Poista kappale</button>
        </div>
      </div>
    </div>`;
  }).join("");
}

const saveTrack = guard(async (tid, body, el) => {
  const t = await api(`/api/projects/${state.project.id}/tracks/${tid}`, { method: "PATCH", body });
  const i = state.project.tracks.findIndex((x) => x.id === tid);
  state.project.tracks[i] = t;
  const row = $(`.track[data-id="${tid}"]`);
  if (row) {
    $(".state", row).outerHTML = trackState(t);
    $(".tcap", row).innerHTML = t.caption ? esc(t.caption) : "<i>ei kuvausta</i>";
    $(".tname", row).textContent = t.title;
    row.classList.toggle("off", !t.include);
    const s = $("[data-saved]", row);
    if (s) { s.textContent = "Tallennettu"; setTimeout(() => s.textContent = "", 1500); }
  }
  renderTrainSummary();
});

function bindTrackList() {
  const list = $("#trackList");
  list.addEventListener("click", guard(async (e) => {
    const track = e.target.closest(".track");
    if (!track) return;
    const tid = track.dataset.id;
    const act = e.target.closest("[data-act]")?.dataset.act;
    if (act === "play") { togglePlay(tid); return; }
    if (act === "vocal" || act === "instr") {
      await saveTrack(tid, { is_instrumental: act === "instr" });
      renderTracks(); return;
    }
    if (act === "del") {
      if (!confirm("Poistetaanko kappale projektista?")) return;
      await api(`/api/projects/${state.project.id}/tracks/${tid}`, { method: "DELETE" });
      await refreshProject(); renderTracks(); return;
    }
    if (e.target.closest(".trow")) {
      state.openTracks.has(tid) ? state.openTracks.delete(tid) : state.openTracks.add(tid);
      track.classList.toggle("open");
    }
  }));
  list.addEventListener("change", (e) => {
    const f = e.target.dataset.f;
    if (!f) return;
    const tid = e.target.closest(".track").dataset.id;
    let v = e.target.type === "checkbox" ? e.target.checked : e.target.value;
    if (f === "bpm") v = v === "" ? 0 : parseInt(v, 10);
    saveTrack(tid, { [f]: v }, e.target);
  });
}

function togglePlay(tid) {
  if (state.playing === tid) { audio.pause(); state.playing = null; }
  else {
    audio.src = `/api/projects/${state.project.id}/tracks/${tid}/audio`;
    audio.play().catch(() => toast("Toisto epäonnistui", true));
    state.playing = tid;
  }
  renderTracks();
}
audio.onended = () => { state.playing = null; renderTracks(); };

// ------------------------------------------------------------------ lataus
function bindUpload() {
  const drop = $("#drop"), input = $("#fileInput");
  ["dragenter", "dragover"].forEach((ev) => drop.addEventListener(ev, (e) => { e.preventDefault(); drop.classList.add("over"); }));
  ["dragleave", "drop"].forEach((ev) => drop.addEventListener(ev, () => drop.classList.remove("over")));
  drop.addEventListener("drop", (e) => { e.preventDefault(); upload(e.dataTransfer.files); });
  input.addEventListener("change", () => { upload(input.files); input.value = ""; });
}

function upload(files) {
  if (!files?.length || !state.project) return;
  const fd = new FormData();
  [...files].forEach((f) => fd.append("files", f));
  const bar = $("#upbar"); bar.hidden = false;
  const xhr = new XMLHttpRequest();
  xhr.open("POST", `/api/projects/${state.project.id}/upload`);
  xhr.upload.onprogress = (e) => { if (e.lengthComputable) $("div", bar).style.width = `${(e.loaded / e.total) * 100}%`; };
  xhr.upload.onload = () => toast("Muunnetaan 48 kHz WAV-muotoon…");
  xhr.onload = async () => {
    bar.hidden = true; $("div", bar).style.width = "0";
    let d = {}; try { d = JSON.parse(xhr.responseText); } catch { /* */ }
    if (xhr.status !== 200) return toast(d.error || "Lataus epäonnistui", true);
    toast(`${d.added} kappaletta lisätty` + (d.errors.length ? ` · ${d.errors.length} virhettä` : ""), d.errors.length > 0);
    if (d.errors.length) console.warn(d.errors), alert("Osa tiedostoista ohitettiin:\n\n" + d.errors.join("\n"));
    await refreshProject(); renderTracks(); loadProjects();
  };
  xhr.onerror = () => { bar.hidden = true; toast("Lataus katkesi", true); };
  xhr.send(fd);
}

const analyze = guard(async () => {
  const stages = [["meta", "#stMeta"], ["lyrics", "#stLyrics"], ["caption", "#stCaption"]].filter(([, s]) => $(s).checked).map(([k]) => k);
  const j = await api(`/api/projects/${state.project.id}/analyze`, {
    method: "POST", body: { stages, only_missing: !$("#anForce").checked, language: $("#anLang").value || null },
  });
  toast("Analyysi lisätty työjonoon");
  selectJob(j.id);
  await refreshProject();
});

// ------------------------------------------------------------------ koulutus
function renderTrainForm() {
  const sel = $("#variantSel");
  const vs = state.status?.variants || [];
  const cur = sel.value;
  sel.innerHTML = vs.map((v) => `<option value="${v.id}" ${v.installed ? "" : "disabled"}>${esc(v.label)}${v.installed ? "" : " – ei ladattu"}</option>`).join("");
  const def = vs.find((v) => v.id === "base" && v.installed) ? "base" : "turbo";
  sel.value = cur && vs.find((v) => v.id === cur && v.installed) ? cur : def;
  renderTrainSummary();
}

function suggestEpochs(n) { return n <= 10 ? 400 : n <= 50 ? 150 : 80; }

function renderTrainSummary() {
  const p = state.project; if (!p) return;
  const ready = p.tracks.filter((t) => t.ready).length;
  const f = $("#trainForm");
  const sug = suggestEpochs(ready);
  if (!f.dataset.touched) { f.epochs.value = sug; f.save_every.value = Math.max(5, Math.round(sug / 8)); }
  $("#epochHint").textContent = ready
    ? `${ready} kappaletta → suositus noin ${sug} epochia (1–10 kpl: 200–500, 10–50 kpl: 100–200, 50+: 50–100). Seuraa loss-käyrää ja vertaa checkpointeja kuuntelemalla.`
    : "Yksikään kappale ei ole vielä valmis. Täytä kuvaukset ja lyriikat Kappaleet-välilehdellä.";
  $("#trainSummary").textContent = ready ? `${ready} kappaletta mukana` : "";
  $("#trainBtn").disabled = !ready;
}

const startTraining = guard(async (e) => {
  e.preventDefault();
  const f = e.target, num = (n) => Number(f[n].value);
  const body = {
    name: f.run_name.value, variant: f.variant.value, epochs: num("epochs"), save_every: num("save_every"),
    vram_profile: f.vram_profile.value, lr: num("lr"), rank: num("rank"), alpha: num("alpha"),
    dropout: num("dropout"), batch_size: num("batch_size"), grad_accum: num("grad_accum"),
    attention: f.attention.value, max_duration: num("max_duration"), seed: num("seed"),
  };
  const j = await api(`/api/projects/${state.project.id}/train`, { method: "POST", body });
  toast("Koulutus lisätty työjonoon");
  f.run_name.value = "";
  selectJob(j.id);
  await refreshProject();
});

function sparkline(points, w = 220, h = 56, emptyText = "ei loss-dataa vielä") {
  if (points) points = [...new Map(points.map((p) => [p[0], p])).values()].sort((a, b) => a[0] - b[0]);
  if (!points || points.length < 2) return `<svg class="spark" viewBox="0 0 ${w} ${h}"><text x="0" y="${h / 2}" fill="currentColor" font-size="12" opacity=".5">${esc(emptyText)}</text></svg>`;
  const xs = points.map((p) => p[0]), ys = points.map((p) => p[1]);
  const x0 = Math.min(...xs), x1 = Math.max(...xs), y0 = Math.min(...ys), y1 = Math.max(...ys);
  const px = (x) => ((x - x0) / (x1 - x0 || 1)) * (w - 4) + 2, py = (y) => h - 4 - ((y - y0) / (y1 - y0 || 1)) * (h - 8);
  const d = points.map((p, i) => `${i ? "L" : "M"}${px(p[0]).toFixed(1)},${py(p[1]).toFixed(1)}`).join("");
  return `<svg class="spark" viewBox="0 0 ${w} ${h}" role="img" aria-label="Loss-käyrä ${ys[0].toFixed(3)} → ${ys[ys.length - 1].toFixed(3)}"><path d="${d}" fill="none" stroke="var(--trace)" stroke-width="1.6"/></svg>`;
}

const STATUS_FI = { queued: "jonossa", running: "käynnissä", done: "valmis", failed: "epäonnistui", stopped: "pysäytetty", interrupted: "keskeytyi" };

function renderRuns() {
  const p = state.project, el = $("#runList");
  if (!p.runs?.length) { el.innerHTML = `<p class="muted">Ei ajoja vielä.</p>`; return; }
  el.innerHTML = p.runs.map((r) => {
    const job = p.jobs.find((j) => j.id === r.job_id);
    const st = job?.status || r.status;
    const prog = job?.progress || {};
    const ep = st === "running" ? `${prog.epoch || 0}/${prog.max_epochs || r.params.epochs} epochia` : `${r.epochs_done ?? 0}/${r.params.epochs} epochia`;
    const ckpts = (r.checkpoints || []).map((c) => `
      <span class="ck"><span>${esc(c.label)}</span>
        <button data-act="listen" data-run="${r.id}" data-ck="${c.id}">Kuuntele</button>
        <a href="/api/projects/${p.id}/runs/${r.id}/download/${encodeURIComponent(c.id)}" download>Lataa</a></span>`).join("");
    const canResume = st !== "running" && st !== "queued" && (r.checkpoints || []).some((c) => c.resumable);
    return `
    <div class="run" data-run="${r.id}">
      <div class="run-head">
        <span class="run-name">${esc(r.name)}</span>
        <span class="badge ${st}">${STATUS_FI[st] || st}</span>
        <span class="run-meta">${esc(r.variant)} · ${r.samples} kpl · rank ${r.params.rank} · ${ep}${r.last_loss != null ? ` · loss ${fmtLoss(r.last_loss)}` : ""} · ${fmtTime(r.created)}</span>
        <span class="acts">
          ${job && (st === "running" || st === "queued") ? `<button class="btn small" data-act="watch" data-job="${job.id}">Seuraa</button>` : ""}
          ${canResume ? `<button class="btn small" data-act="resume" data-run="${r.id}">Jatka koulutusta</button>` : ""}
          ${st !== "running" ? `<button class="btn ghost danger small" data-act="delrun" data-run="${r.id}">Poista</button>` : ""}
        </span>
      </div>
      <div class="run-body">
        ${sparkline(r.loss_curve, 220, 56, st === "running" ? "käyrä päivittyy työjonossa" : "ei loss-dataa vielä")}
        <div class="ckpts">${ckpts || `<span class="muted" style="font-size:13px">Checkpointit ilmestyvät tähän koulutuksen edetessä (joka ${r.params.save_every}. epoch).</span>`}</div>
      </div>
    </div>`;
  }).join("");
}

function bindRuns() {
  $("#runList").addEventListener("click", guard(async (e) => {
    const b = e.target.closest("[data-act]"); if (!b) return;
    const pid = state.project.id;
    if (b.dataset.act === "watch") selectJob(b.dataset.job);
    if (b.dataset.act === "listen") {
      setTab("listen");
      $("#loraSel").value = `${b.dataset.run}|${b.dataset.ck}`;
      state.loraTouched = true;
    }
    if (b.dataset.act === "resume") {
      const n = prompt("Montako epochia lisää?", "100"); if (!n) return;
      const j = await api(`/api/projects/${pid}/runs/${b.dataset.run}/resume`, { method: "POST", body: { extra_epochs: parseInt(n, 10) } });
      selectJob(j.id); await refreshProject();
    }
    if (b.dataset.act === "delrun") {
      if (!confirm("Poistetaanko ajo ja kaikki sen checkpointit?")) return;
      await api(`/api/projects/${pid}/runs/${b.dataset.run}`, { method: "DELETE" });
      await refreshProject();
    }
  }));
}

// ------------------------------------------------------------------ kuuntelu
function renderListen(soft) {
  const p = state.project;
  const sel = $("#loraSel"), cur = sel.value;
  const opts = [`<option value="">Ei adapteria (perusmalli turbo)</option>`];
  (p.runs || []).forEach((r) => (r.checkpoints || []).forEach((c) =>
    opts.push(`<option value="${r.id}|${c.id}">${esc(r.name)} – ${esc(c.label)}</option>`)));
  sel.innerHTML = opts.join("");
  if (state.loraTouched && [...sel.options].some((o) => o.value === cur)) sel.value = cur;
  else if (sel.options.length > 1) sel.selectedIndex = 1;

  const cf = $("#captionFrom"), curCf = cf.value;
  cf.innerHTML = `<option value="">Kopioi kappaleesta…</option>` + p.tracks.filter((t) => t.caption)
    .map((t) => `<option value="${t.id}">${esc(t.title)}</option>`).join("");
  cf.value = curCf;

  const list = $("#sampleList");
  if (soft && list.contains(document.activeElement)) return;
  const playing = $$("audio", list).some((a) => !a.paused);
  if (soft && playing) return;
  list.innerHTML = (p.samples || []).map((s) => `
    <div class="sample">
      <div class="sample-head">
        <span class="sample-cap">${esc(s.caption)}</span>
        <span class="sample-meta">${s.run_name ? `${esc(s.run_name)} / ${esc(s.checkpoint)} · voimakkuus ${s.lora_scale}` : "perusmalli"} · ${Math.round(s.duration)} s · ${fmtTime(s.created)}</span>
        <button class="btn ghost danger small" data-del="${s.id}">Poista</button>
      </div>
      ${s.files.map((f, i) => `<audio controls preload="none" src="/api/projects/${p.id}/samples/${encodeURIComponent(f)}"></audio>
        <span class="sample-meta">siemen ${s.seeds?.[i] ?? "?"} · <a href="/api/projects/${p.id}/samples/${encodeURIComponent(f)}" download>lataa</a>
        ${s.instrumental ? "" : ` · <button class="linkbtn" data-own="${esc(f)}">Laula omalla äänellä</button>`}</span>`).join("")}
    </div>`).join("") || `<p class="muted">Generoidut näytteet näkyvät tässä.</p>`;
}

function bindListen() {
  const f = $("#genForm");
  f.lora.onchange = () => { state.loraTouched = true; };
  f.lora_scale.oninput = () => $("#scaleOut").textContent = Number(f.lora_scale.value).toFixed(2);
  f.instrumental.onchange = () => $("#lyricsWrap").hidden = f.instrumental.checked;
  $("#captionFrom").onchange = (e) => {
    const t = state.project.tracks.find((x) => x.id === e.target.value); if (!t) return;
    f.caption.value = t.caption;
    f.instrumental.checked = !!t.is_instrumental; $("#lyricsWrap").hidden = !!t.is_instrumental;
    f.lyrics.value = t.is_instrumental ? "" : t.lyrics;
    if (t.language && t.language !== "unknown" && [...f.language.options].some((o) => o.value === t.language)) f.language.value = t.language;
    e.target.value = "";
  };
  f.onsubmit = guard(async (e) => {
    e.preventDefault();
    const [run_id, checkpoint] = (f.lora.value || "|").split("|");
    const body = {
      run_id: run_id || null, checkpoint: checkpoint || null, caption: f.caption.value,
      lyrics: f.lyrics.value, instrumental: f.instrumental.checked, add_trigger: f.add_trigger.checked,
      duration: Number(f.duration.value), language: f.language.value, count: Number(f.count.value),
      seed: Number(f.seed.value), lora_scale: Number(f.lora_scale.value),
    };
    const j = await api(`/api/projects/${state.project.id}/sample`, { method: "POST", body });
    toast("Näyte lisätty työjonoon");
    selectJob(j.id);
  });
  $("#sampleList").addEventListener("click", guard(async (e) => {
    const own = e.target.dataset.own;
    if (own) { openConvertWith({ project_id: state.project.id, file: own }); return; }
    const id = e.target.dataset.del; if (!id) return;
    if (!confirm("Poistetaanko näyte?")) return;
    await api(`/api/projects/${state.project.id}/samples/${id}`, { method: "DELETE" });
    await refreshProject();
  }));
}

// ------------------------------------------------------------------ työjono
const isTrain = (j) => j.kind === "train" || j.kind === "voice_train";

function renderJobs() {
  const jobsArr = state.jobsAll || [];
  const el = $("#jobList");
  el.innerHTML = jobsArr.map((j) => {
    const pr = j.progress || {};
    let sub = STATUS_FI[j.status] || j.status;
    if (j.status === "running") {
      if (isTrain(j) && pr.max_epochs && pr.epoch) sub = `epoch ${pr.epoch || 0}/${pr.max_epochs}${pr.last_loss != null ? ` · loss ${fmtLoss(pr.last_loss)}` : ""}`;
      else if (pr.n) sub = `${pr.stage || ""} ${pr.i}/${pr.n} ${pr.title ? "· " + pr.title : ""}`;
      else sub = j.step_title;
    }
    if (j.status === "failed" && j.error) sub = j.error;
    return `<button class="job" data-id="${j.id}" aria-current="${state.jobId === j.id}">
      <span class="job-title"><span class="dot ${j.status}"></span>${esc(j.title)}</span>
      <span class="job-sub">${esc(sub)} · ${fmtTime(j.created)}</span></button>`;
  }).join("") || `<p class="muted" style="padding:0 16px 14px;font-size:13px">Analyysit, koulutukset ja näytteet ajetaan GPU:lla yksi kerrallaan.</p>`;
  $$(".job", el).forEach((b) => b.onclick = () => selectJob(b.dataset.id));
}

function selectJob(id) {
  if (state.jobId !== id) { state.jobId = id; state.logOffset = 0; $("#jdLog").textContent = ""; }
  $("#jobDetail").hidden = false;
  $$(".job").forEach((b) => b.setAttribute("aria-current", b.dataset.id === id));
  pollJob();
}

let jobBusy = false;
async function pollJob() {
  if (!state.jobId || jobBusy) return;
  jobBusy = true;
  try {
    const id = state.jobId;
    const [j, log] = await Promise.all([api(`/api/jobs/${id}`), api(`/api/jobs/${id}/log?offset=${state.logOffset}`)]);
    if (id !== state.jobId) return;
    renderJobDetail(j);
    if (log.text) {
      const pre = $("#jdLog");
      const atBottom = pre.scrollHeight - pre.scrollTop - pre.clientHeight < 40;
      pre.textContent += log.text.split("\n").filter((l) => !l.startsWith("@@")).join("\n");
      if (pre.textContent.length > 300000) pre.textContent = pre.textContent.slice(-200000);
      if (atBottom) pre.scrollTop = pre.scrollHeight;
    }
    state.logOffset = log.offset;
  } catch { /* */ } finally { jobBusy = false; }
}

function renderJobDetail(j) {
  $("#jdTitle").textContent = j.title;
  const pr = j.progress || {};
  let step = `${STATUS_FI[j.status] || j.status}`;
  if (j.status === "queued") step += j.queue_position ? ` · sijalla ${j.queue_position}` : "";
  if (j.status === "running") step += ` · vaihe ${j.step_index + 1}/${j.step_count}: ${pr.stage && !isTrain(j) ? pr.stage : j.step_title}${pr.title && !isTrain(j) ? ` (${pr.title})` : ""}`;
  if (j.error) step += ` · ${j.error}`;
  $("#jdStep").textContent = step;
  $("#jdStop").hidden = !(j.status === "running" || j.status === "queued");

  let pct = 0;
  if (j.status === "done") pct = 100;
  else if (isTrain(j) && pr.max_epochs && pr.epoch) pct = (100 * (pr.epoch || 0)) / pr.max_epochs;
  else if (pr.n) pct = (100 * Math.max(0, (pr.i || 0) - 0.5)) / pr.n;
  else if (j.step_count) pct = (100 * j.step_index) / j.step_count;
  $("#jdBar").style.width = `${Math.min(100, pct)}%`;
  $(".bigbar").classList.toggle("done", j.status === "done");

  const stats = [];
  if (isTrain(j)) {
    stats.push(["Epoch", pr.max_epochs ? `${pr.epoch || 0}/${pr.max_epochs}` : "–"]);
    stats.push(["Loss", fmtLoss(pr.last_loss)]);
    stats.push(["Arvio jäljellä", j.status === "running" ? fmtEta(pr.eta_secs) : "–"]);
  } else if (pr.n) {
    stats.push(["Edistyminen", `${pr.i}/${pr.n}`], ["Kesto", fmtEta((j.finished || Date.now() / 1000) - (j.started || Date.now() / 1000))]);
  } else if (j.started) {
    stats.push(["Kesto", fmtEta((j.finished || Date.now() / 1000) - j.started)]);
  }
  $("#jdStats").innerHTML = stats.map(([k, v]) => `<div><b>${esc(v)}</b>${esc(k)}</div>`).join("");

  const chart = $("#lossChart");
  const show = isTrain(j) && (pr.steps?.length > 1 || pr.epochs?.length > 1);
  chart.hidden = !show;
  if (show) drawLoss(chart, pr.steps || [], pr.epochs || [], pr.max_epochs);
}

function cssVar(n) { return getComputedStyle(document.documentElement).getPropertyValue(n).trim(); }

/* Loss-käyrä: askelkohtainen loss vaaleana, epoch-keskiarvo vahvana viivana */
function drawLoss(canvas, steps, epochs) {
  const dpr = window.devicePixelRatio || 1, w = canvas.clientWidth, h = canvas.clientHeight;
  canvas.width = w * dpr; canvas.height = h * dpr;
  const c = canvas.getContext("2d"); c.scale(dpr, dpr); c.clearRect(0, 0, w, h);
  const pad = { l: 44, r: 10, t: 10, b: 20 };
  const ys = [...steps.map((p) => p[1]), ...epochs.map((p) => p[1])].filter(Number.isFinite).sort((a, b) => a - b);
  if (!ys.length) return;
  const y0 = ys[Math.floor(ys.length * 0.01)], y1 = ys[Math.floor(ys.length * 0.98)] || ys[ys.length - 1];
  const lo = Math.max(y0 >= 0 ? 0 : -Infinity, y0 - (y1 - y0) * 0.08), hi = y1 + (y1 - y0) * 0.08 || y0 + 1;
  const sx = steps.length ? steps[steps.length - 1][0] : 1;
  const ex = epochs.length ? epochs[epochs.length - 1][0] : 1;
  const Y = (v) => pad.t + (1 - (Math.min(hi, Math.max(lo, v)) - lo) / (hi - lo)) * (h - pad.t - pad.b);
  const ink2 = cssVar("--ink-2"), line = cssVar("--line"), trace = cssVar("--trace");
  c.font = "11px Instrument Sans, sans-serif"; c.fillStyle = ink2; c.strokeStyle = line; c.lineWidth = 1;
  for (let i = 0; i <= 3; i++) {
    const v = lo + ((hi - lo) * i) / 3, y = Y(v);
    c.beginPath(); c.moveTo(pad.l, y); c.lineTo(w - pad.r, y); c.stroke();
    c.fillText(v.toFixed(3), 4, y + 4);
  }
  const plot = (pts, xmax, color, width, alpha) => {
    if (pts.length < 2) return;
    c.globalAlpha = alpha; c.strokeStyle = color; c.lineWidth = width; c.beginPath();
    pts.forEach(([x, v], i) => { const X = pad.l + (x / xmax) * (w - pad.l - pad.r); i ? c.lineTo(X, Y(v)) : c.moveTo(X, Y(v)); });
    c.stroke(); c.globalAlpha = 1;
  };
  plot(steps, sx, trace, 1, 0.3);
  plot(epochs, ex, trace, 2, 1);
  c.fillStyle = ink2;
  c.fillText(epochs.length ? `epoch ${ex}` : `step ${sx}`, w - pad.r - 64, h - 5);
}

// ------------------------------------------------------------------ tila
async function pollStatus() {
  try {
    const s = await api("/api/status");
    state.status = s;
    const g = s.gpu;
    if (g) {
      $(".gpu-name").textContent = g.name;
      $("#gpuMem").style.width = `${(100 * g.mem_used) / g.mem_total}%`;
      $("#gpuMeta").textContent = `${(g.mem_used / 1024).toFixed(1)} / ${(g.mem_total / 1024).toFixed(0)} GB · ${g.util} % · ${g.temp} °C · levyä ${s.disk_free_gb} GB`;
    } else {
      $("#gpuMeta").textContent = `GPU-tietoja ei saatavilla · levyä ${s.disk_free_gb} GB`;
    }
    $("#queueInfo").textContent = s.active_job ? `käynnissä${s.queued ? `, ${s.queued} jonossa` : ""}` : s.queued ? `${s.queued} jonossa` : "vapaa";
    if (state.project && !$("#variantSel").options.length) renderTrainForm();
  } catch { /* */ }
}

// ------------------------------------------------------------------ osiot
const NEW_LABEL = { music: "Uusi projekti", voice: "Uusi äänimalli" };

function showSection(sec) {
  state.section = sec;
  localStorageSet("section", sec);
  $$(".sections button").forEach((b) => b.setAttribute("aria-current", b.dataset.section === sec));
  const has = { music: !!state.project, voice: !!state.voice };
  $$("[data-sec]").forEach((el) => {
    const s = el.dataset.sec;
    if (s !== sec) { el.hidden = true; return; }
    if (el.id === "welcome") el.hidden = has.music;
    else if (el.id === "project") el.hidden = !has.music;
    else if (el.id === "voiceWelcome") el.hidden = has.voice;
    else if (el.id === "voice") el.hidden = !has.voice;
    else el.hidden = false;
  });
  $("#newProject").hidden = sec === "convert";
  $("#newProject").textContent = NEW_LABEL[sec] || "";
  $("#projectList").hidden = sec === "convert";
  renderSideList();
}

function renderSideList() {
  const nav = $("#projectList");
  if (state.section === "music") {
    nav.innerHTML = state.projects.map((p) => `
      <button data-id="${p.id}" aria-current="${state.project?.id === p.id}">
        <span class="pn">${esc(p.name)}</span><span class="pm">${p.tracks} kappaletta, ${p.runs} ajoa</span>
      </button>`).join("") || `<p class="muted side-empty">Ei projekteja vielä.</p>`;
    $$("button", nav).forEach((b) => b.onclick = () => openProject(b.dataset.id));
  } else if (state.section === "voice") {
    nav.innerHTML = state.voices.map((v) => `
      <button data-id="${v.id}" aria-current="${state.voice?.id === v.id}">
        <span class="pn">${esc(v.name)}</span><span class="pm">${v.files} tiedostoa, ${v.models} mallia</span>
      </button>`).join("") || `<p class="muted side-empty">Ei äänimalleja vielä.</p>`;
    $$("button", nav).forEach((b) => b.onclick = () => openVoice(b.dataset.id));
  }
}

const newItem = guard(async () => {
  if (state.section === "voice") {
    const name = prompt("Äänimallin nimi (esim. oma nimesi tai laulutyyli):");
    if (!name) return;
    const v = await api("/api/voices", { method: "POST", body: { name } });
    await loadVoices();
    await openVoice(v.id);
  } else {
    await newProject();
  }
});

// ------------------------------------------------------------------ äänimallit
async function loadVoices() {
  state.voices = await api("/api/voices");
  if (state.section === "voice") renderSideList();
}

async function openVoice(id, keep = false) {
  state.voice = await api(`/api/voices/${id}`);
  if (!keep) state.vtab = "data";
  localStorageSet("lastVoice", id);
  showSection("voice");
  renderVoice();
}

async function refreshVoice() {
  if (!state.voice) return;
  try {
    const v = await api(`/api/voices/${state.voice.id}`);
    const changed = JSON.stringify(v) !== JSON.stringify(state.voice);
    state.voice = v;
    if (changed) renderVoice(true);
  } catch { /* */ }
}

function setVTab(tab) {
  state.vtab = tab;
  $$("#voice .tabs button").forEach((b) => b.setAttribute("aria-selected", b.dataset.vtab === tab));
  $$("#voice .vpanel").forEach((el) => el.hidden = el.dataset.vpanel !== tab);
}

function suggestVoiceEpochs(mins) { return mins < 10 ? 300 : mins < 30 ? 200 : 150; }

function renderVoice(soft = false) {
  const v = state.voice, st = v.stats;
  if (!soft || document.activeElement !== $("#vName")) $("#vName").value = v.name;
  $("#vMeta").textContent = `${v.files.length} tiedostoa · ${fmtDur(st.raw_secs)} raakaa ääntä`;
  $("#vTabCount").textContent = v.files.length ? `(${v.files.length})` : "";
  setVTab(state.vtab);

  // Minuuttimittari: tavoite 10–30 min puhdasta laulua, asteikko 0–60 min
  const mins = st.clean_secs / 60;
  $("#minVal").textContent = fmtDur(st.clean_secs);
  $("#minFill").style.width = `${Math.min(100, (mins / 60) * 100)}%`;
  $("#minRaw").style.width = `${Math.min(100, (st.raw_secs / 3600) * 100)}%`;
  const unprepared = st.included - st.prepared;
  $("#minHint").textContent = !v.files.length ? "· lataa äänitteitä aloittaaksesi"
    : unprepared > 0 ? `· ${unprepared} tiedostoa valmistelematta (raakaa ääntä yhteensä ${fmtDur(st.raw_secs)})`
    : mins < 1 ? "· liian vähän koulutukseen" : mins < 10 ? "· toimii, mutta 10+ min antaa selvästi paremman tuloksen"
    : mins <= 30 ? "· hyvä määrä" : "· runsaasti dataa, koulutus kestää pidempään";

  renderVoiceFiles(soft);
  const f = $("#vTrainForm");
  const sug = suggestVoiceEpochs(mins);
  if (!f.dataset.touched) { f.epochs.value = sug; f.save_every.value = Math.max(5, Math.round(sug / 12 / 5) * 5); }
  $("#vEpochHint").textContent = st.clean_secs >= 60
    ? `${mins.toFixed(1)} min laulua → suositus noin ${sug} epochia. Malli tallennetaan joka ${f.save_every.value}. epoch, joten voit verrata vaiheita ja valita parhaan. Ylikoulutettu malli alkaa kuulostaa metalliselta.`
    : "Valmistele ensin vähintään minuutti laulua Data-välilehdellä.";
  $("#vTrainSummary").textContent = st.clean_secs >= 60 ? `${st.prepared} tiedostoa · ${mins.toFixed(1)} min` : "";
  $("#vTrainBtn").disabled = st.clean_secs < 60;
  renderVoiceRuns();
  renderJobs();
}

const FILE_STATE = (f) => !f.include ? `<span class="state off">ei mukana</span>`
  : f.prepared ? `<span class="state ok">${fmtDur(f.clean_secs)} laulua</span>`
  : `<span class="state todo">valmistelematta</span>`;

function renderVoiceFiles(soft) {
  const v = state.voice, list = $("#vFileList");
  if (soft && $$("audio", list).some((a) => !a.paused)) return;
  if (!v.files.length) { list.innerHTML = `<p class="muted" style="padding:16px 4px">Ei tiedostoja.</p>`; return; }
  list.innerHTML = v.files.map((f) => `
    <div class="vfile ${f.include ? "" : "off"}" data-id="${f.id}">
      <div class="vrow">
        <div style="min-width:0">
          <div class="tname">${esc(f.filename)}</div>
          <div class="tcap">${fmtDur(f.duration)} · ${[f.separate && "erotellaan taustasta", f.karaoke && "taustalaulut pois", f.dereverb && "kaiku pois"].filter(Boolean).join(", ") || "ei käsittelyä"}</div>
        </div>
        ${FILE_STATE(f)}
        <span class="vacts">
          <label class="chip"><input type="checkbox" data-f="separate" ${f.separate ? "checked" : ""}> Erota taustasta</label>
          <label class="chip"><input type="checkbox" data-f="karaoke" ${f.karaoke ? "checked" : ""}> Taustalaulut pois</label>
          <label class="chip"><input type="checkbox" data-f="dereverb" ${f.dereverb ? "checked" : ""}> Kaiku pois</label>
          <label class="chip"><input type="checkbox" data-f="include" ${f.include ? "checked" : ""}> Mukana</label>
          <button class="btn ghost danger small" data-act="del" aria-label="Poista ${esc(f.filename)}">Poista</button>
        </span>
      </div>
      <div class="vplay">
        <span class="muted">alkuperäinen</span><audio controls preload="none" src="/api/voices/${v.id}/files/${f.id}/audio?kind=raw"></audio>
        ${f.prepared ? `<span class="muted">puhdas</span><audio controls preload="none" src="/api/voices/${v.id}/files/${f.id}/audio?kind=clean&t=${f.clean_secs}"></audio>` : ""}
      </div>
      ${f.notes?.length ? `<ul class="notes">${f.notes.map((n) => `<li>${esc(n)}</li>`).join("")}</ul>` : ""}
    </div>`).join("");
}

function bindVoice() {
  $("#newVoice2").onclick = newItem;
  $$("#voice .tabs button").forEach((b) => b.onclick = () => setVTab(b.dataset.vtab));
  $("#vName").onchange = guard(async (e) => {
    state.voice = await api(`/api/voices/${state.voice.id}`, { method: "PATCH", body: { name: e.target.value } });
    renderVoice(true); loadVoices();
  });
  $("#vDelete").onclick = guard(async () => {
    if (!confirm(`Poistetaanko äänimalli "${state.voice.name}" kaikkine tiedostoineen ja koulutettuine malleineen?`)) return;
    await api(`/api/voices/${state.voice.id}`, { method: "DELETE" });
    state.voice = null; await loadVoices(); showSection("voice");
  });
  const list = $("#vFileList");
  list.addEventListener("change", guard(async (e) => {
    const k = e.target.dataset.f; if (!k) return;
    const fid = e.target.closest(".vfile").dataset.id;
    state.voice = await api(`/api/voices/${state.voice.id}/files/${fid}`, { method: "PATCH", body: { [k]: e.target.checked } });
    renderVoice();
  }));
  list.addEventListener("click", guard(async (e) => {
    if (e.target.dataset.act !== "del") return;
    const fid = e.target.closest(".vfile").dataset.id;
    if (!confirm("Poistetaanko tiedosto?")) return;
    await api(`/api/voices/${state.voice.id}/files/${fid}`, { method: "DELETE" });
    await refreshVoice(); renderVoice();
  }));
  bindDrop($("#vDrop"), $("#vFileInput"), (files) => uploadFiles(files, `/api/voices/${state.voice.id}/upload`, $("#vUpbar"),
    async () => { await refreshVoice(); renderVoice(); loadVoices(); }));
  $("#prepareBtn").onclick = guard(async () => {
    const j = await api(`/api/voices/${state.voice.id}/prepare`, { method: "POST", body: { only_missing: !$("#vForce").checked } });
    toast("Valmistelu lisätty työjonoon"); selectJob(j.id); await refreshVoice();
  });
  const tf = $("#vTrainForm");
  tf.addEventListener("input", (e) => { if (["epochs", "save_every"].includes(e.target.name)) tf.dataset.touched = "1"; });
  tf.onsubmit = guard(async (e) => {
    e.preventDefault();
    const body = { name: tf.run_name.value, epochs: Number(tf.epochs.value), save_every: Number(tf.save_every.value),
      sample_rate: Number(tf.sample_rate.value), batch_size: Number(tf.batch_size.value), vocoder: tf.vocoder.value };
    const j = await api(`/api/voices/${state.voice.id}/train`, { method: "POST", body });
    toast("Koulutus lisätty työjonoon"); tf.run_name.value = ""; selectJob(j.id); await refreshVoice();
  });
  $("#vRunList").addEventListener("click", guard(async (e) => {
    const b = e.target.closest("[data-act]"); if (!b) return;
    const vid = state.voice.id, rid = b.dataset.run;
    if (b.dataset.act === "watch") selectJob(b.dataset.job);
    if (b.dataset.act === "try") openConvertWith({ model: `${vid}|${rid}|${b.dataset.w}` });
    if (b.dataset.act === "resume") {
      const n = prompt("Montako epochia lisää?", "100"); if (!n) return;
      const j = await api(`/api/voices/${vid}/runs/${rid}/resume`, { method: "POST", body: { extra_epochs: parseInt(n, 10) } });
      selectJob(j.id); await refreshVoice();
    }
    if (b.dataset.act === "index") {
      const j = await api(`/api/voices/${vid}/runs/${rid}/index`, { method: "POST" }); selectJob(j.id);
    }
    if (b.dataset.act === "delrun") {
      if (!confirm("Poistetaanko ajo ja kaikki sen mallitiedostot?")) return;
      await api(`/api/voices/${vid}/runs/${rid}`, { method: "DELETE" }); await refreshVoice();
    }
  }));
}

function renderVoiceRuns() {
  const v = state.voice, el = $("#vRunList");
  if (!v.runs?.length) { el.innerHTML = `<p class="muted">Ei koulutusajoja vielä.</p>`; return; }
  el.innerHTML = v.runs.map((r) => {
    const job = state.jobsAll.find((j) => j.id === r.job_id) || v.jobs.find((j) => j.id === r.job_id);
    const st = job?.status || r.status, pr = job?.progress || {};
    const ep = st === "running" ? `${pr.epoch || 0}/${pr.max_epochs || r.params.epochs}` : `${r.epochs_done || 0}/${r.params.epochs}`;
    const best = r.best?.epoch;
    const nearest = best != null && r.weights.length
      ? r.weights.reduce((a, w) => Math.abs(w.epoch - best) < Math.abs(a.epoch - best) ? w : a, r.weights[0]).file : null;
    const ws = r.weights.map((w) => `
      <span class="ck ${w.file === nearest ? "best" : ""}"><span>Epoch ${w.epoch}${w.file === nearest ? " · lähin parasta" : ""}</span>
        <button data-act="try" data-run="${r.id}" data-w="${esc(w.file)}">Kokeile</button>
        <a href="/api/voices/${v.id}/runs/${r.id}/download/${encodeURIComponent(w.file)}" download>Lataa</a></span>`).join("");
    const canResume = st !== "running" && st !== "queued" && (r.epochs_done || 0) > 0;
    return `
    <div class="run">
      <div class="run-head">
        <span class="run-name">${esc(r.name)}</span>
        <span class="badge ${st}">${STATUS_FI[st] || st}</span>
        <span class="run-meta">${(r.clean_secs / 60).toFixed(1)} min laulua · ${r.params.sample_rate / 1000} kHz · ${ep} epochia${best != null ? ` · alin loss epochissa ${best}` : ""} · ${fmtTime(r.created)}${r.weights.length && !r.has_index && st !== "running" ? " · indeksi puuttuu" : ""}</span>
        <span class="acts">
          ${job && (st === "running" || st === "queued") ? `<button class="btn small" data-act="watch" data-job="${job.id}">Seuraa</button>` : ""}
          ${r.weights.length && !r.has_index && st !== "running" && st !== "queued" ? `<button class="btn small" data-act="index" data-run="${r.id}">Rakenna indeksi</button>` : ""}
          ${canResume ? `<button class="btn small" data-act="resume" data-run="${r.id}">Jatka koulutusta</button>` : ""}
          ${st !== "running" ? `<button class="btn ghost danger small" data-act="delrun" data-run="${r.id}">Poista</button>` : ""}
        </span>
      </div>
      <div class="run-body">
        ${sparkline(r.loss_curve, 220, 56, st === "running" ? "käyrä päivittyy työjonossa" : "ei loss-dataa vielä")}
        <div class="ckpts">${ws || `<span class="muted" style="font-size:13px">Mallitiedostot ilmestyvät tähän joka ${r.params.save_every}. epoch.</span>`}</div>
      </div>
    </div>`;
  }).join("");
}

// ------------------------------------------------------------------ yhteinen lataus
function bindDrop(drop, input, onFiles) {
  ["dragenter", "dragover"].forEach((ev) => drop.addEventListener(ev, (e) => { e.preventDefault(); drop.classList.add("over"); }));
  ["dragleave", "drop"].forEach((ev) => drop.addEventListener(ev, () => drop.classList.remove("over")));
  drop.addEventListener("drop", (e) => { e.preventDefault(); onFiles(e.dataTransfer.files); });
  input.addEventListener("change", () => { onFiles(input.files); input.value = ""; });
}

function uploadFiles(files, url, bar, done, field = "files") {
  if (!files?.length) return;
  const fd = new FormData();
  [...files].forEach((f) => fd.append(field, f));
  bar.hidden = false;
  const xhr = new XMLHttpRequest();
  xhr.open("POST", url);
  xhr.upload.onprogress = (e) => { if (e.lengthComputable) $("div", bar).style.width = `${(e.loaded / e.total) * 100}%`; };
  xhr.upload.onload = () => toast("Muunnetaan WAV-muotoon…");
  xhr.onload = async () => {
    bar.hidden = true; $("div", bar).style.width = "0";
    let d = {}; try { d = JSON.parse(xhr.responseText); } catch { /* */ }
    if (xhr.status !== 200) return toast(d.error || "Lataus epäonnistui", true);
    if (d.added !== undefined) {
      toast(`${d.added} tiedostoa lisätty` + (d.errors?.length ? ` · ${d.errors.length} ohitettu` : ""), d.errors?.length > 0);
      if (d.errors?.length) alert("Osa tiedostoista ohitettiin:\n\n" + d.errors.join("\n"));
    }
    await done(d);
  };
  xhr.onerror = () => { bar.hidden = true; toast("Lataus katkesi", true); };
  xhr.send(fd);
}

// ------------------------------------------------------------------ muunnos
async function loadConvertData() {
  const [models, sources, convs] = await Promise.all([api("/api/rvc/models"), api("/api/conversions/sources"), api("/api/conversions")]);
  state.rvcModels = models; state.aceSources = sources; state.conversions = convs;
  renderConvert();
}

function renderConvert(soft = false) {
  const ms = $("#modelSel"), cur = ms.value;
  ms.innerHTML = state.rvcModels.length
    ? state.rvcModels.map((m) => `<option value="${m.voice_id}|${m.run_id}|${esc(m.weight)}">${esc(m.voice)} – ${esc(m.run)} – epoch ${m.epoch}${m.best != null && Math.abs(m.best - m.epoch) <= 0 ? " (alin loss)" : ""}${m.has_index ? "" : " · ei indeksiä"}</option>`).join("")
    : `<option value="">Ei koulutettuja äänimalleja – kouluta ensin Äänimallit-osiossa</option>`;
  if ([...ms.options].some((o) => o.value === (state.pendingModel || cur))) ms.value = state.pendingModel || cur;
  state.pendingModel = null;

  const as = $("#aceSel"), curA = as.value;
  as.innerHTML = state.aceSources.length
    ? state.aceSources.map((x) => `<option value="${x.project_id}|${esc(x.file)}">${esc(x.project)}: ${esc(x.caption.slice(0, 70))}${x.instrumental ? " (instrumentaali)" : ""} · ${fmtTime(x.created)}</option>`).join("")
    : `<option value="">Ei ACE-näytteitä – generoi niitä Musiikki-osion Kuuntele-välilehdellä</option>`;
  const want = state.pendingAce || curA;
  if ([...as.options].some((o) => o.value === want)) as.value = want;
  state.pendingAce = null;
  updateAcePreview();
  setConvSrc(state.convSrc);
  $("#convBtn").disabled = !state.rvcModels.length;

  const list = $("#convList");
  if (soft && $$("audio", list).some((a) => !a.paused)) return;
  const OUT = [["mix", "Valmis miksaus"], ["vocals", "Muunnettu laulu"], ["instrumental", "Tausta"], ["source_vocals", "Alkuperäinen laulu"]];
  list.innerHTML = state.conversions.map((c) => {
    const job = state.jobsAll.find((j) => j.id === c.job_id);
    const st = job?.status || c.status;
    const p = c.params;
    return `<div class="sample">
      <div class="sample-head">
        <span class="sample-cap">${esc(c.source_label)} → ${esc(c.voice)}${c.epoch ? `, epoch ${c.epoch}` : ""}</span>
        <span class="badge ${st}">${STATUS_FI[st] || st}</span>
        <span class="sample-meta">sävel ${p.pitch > 0 ? "+" : ""}${p.pitch} · indeksi ${p.index_rate} · suojaus ${p.protect} · ${fmtTime(c.created)}</span>
        <button class="btn ghost danger small" data-cdel="${c.id}">Poista</button>
      </div>
      ${OUT.filter(([k]) => c.outputs?.[k]).map(([k, label]) => `
        <div class="outrow"><span class="muted">${label}</span>
          <audio controls preload="none" src="/api/conversions/${c.id}/files/${encodeURIComponent(c.outputs[k])}"></audio>
          <a href="/api/conversions/${c.id}/files/${encodeURIComponent(c.outputs[k])}" download>lataa</a>
          ${k === "mix" && c.outputs.mix_flac ? `<a href="/api/conversions/${c.id}/files/${encodeURIComponent(c.outputs.mix_flac)}" download>FLAC</a>` : ""}</div>`).join("")}
    </div>`;
  }).join("") || `<p class="muted">Muunnetut biisit näkyvät tässä.</p>`;
}

function updateAcePreview() {
  const v = $("#aceSel").value, a = $("#acePreview");
  if (!v) { a.removeAttribute("src"); a.hidden = true; return; }
  const [pid, file] = v.split("|");
  const src = `/api/projects/${pid}/samples/${encodeURIComponent(file)}`;
  if (a.getAttribute("src") !== src) a.src = src;
  a.hidden = false;
}

function setConvSrc(src) {
  state.convSrc = src;
  $$("#convForm [data-src]").forEach((b) => b.setAttribute("aria-pressed", b.dataset.src === src));
  $("#srcUpload").hidden = src !== "upload";
  $("#srcAce").hidden = src !== "ace";
  $("#convHint").textContent = src === "upload"
    ? (state.convInput ? `Valittu: ${state.convInput.name} (${fmtDur(state.convInput.duration)})` : "Lataa ensin tiedosto.")
    : "ACE-näytteen laulu erotellaan ja korvataan sinun äänelläsi.";
}

function openConvertWith(opts) {
  if (opts.model) state.pendingModel = opts.model;
  if (opts.project_id) { state.pendingAce = `${opts.project_id}|${opts.file}`; state.convSrc = "ace"; }
  showSection("convert");
  loadConvertData();
}

function bindConvert() {
  const f = $("#convForm");
  $$("#convForm [data-src]").forEach((b) => b.onclick = () => setConvSrc(b.dataset.src));
  $("#aceSel").onchange = updateAcePreview;
  $$("#convForm input[type=range]").forEach((r) => {
    const out = $(`output[data-for="${r.name}"]`, f);
    const upd = () => out.textContent = r.name.endsWith("_db") ? `${Number(r.value) > 0 ? "+" : ""}${r.value} dB`
      : r.name === "pitch" ? `${Number(r.value) > 0 ? "+" : ""}${r.value}` : Number(r.value).toFixed(2);
    r.oninput = upd; upd();
  });
  bindDrop($("#cDrop"), $("#cFileInput"), (files) => uploadFiles([files[0]], "/api/conversions/input", $("#cUpbar"), (d) => {
    state.convInput = d; $("#cFileLabel").textContent = d.name; setConvSrc("upload");
  }, "file"));
  f.onsubmit = guard(async (e) => {
    e.preventDefault();
    const [voice_id, run_id, weight] = (f.model.value || "||").split("|");
    if (!weight) throw new Error("Valitse äänimalli");
    const body = { voice_id, run_id, weight, separate: f.separate.checked, dereverb: f.dereverb.checked,
      pitch: Number(f.pitch.value), index_rate: Number(f.index_rate.value), protect: Number(f.protect.value),
      vocal_gain_db: Number(f.vocal_gain_db.value), inst_gain_db: Number(f.inst_gain_db.value), f0_method: f.f0_method.value };
    if (state.convSrc === "upload") {
      if (!state.convInput) throw new Error("Lataa ensin äänitiedosto");
      Object.assign(body, { source: "upload", input_id: state.convInput.input_id });
    } else {
      const [project_id, file] = ($("#aceSel").value || "|").split("|");
      if (!file) throw new Error("Valitse ACE-näyte");
      Object.assign(body, { source: "ace", project_id, file });
    }
    const j = await api("/api/conversions", { method: "POST", body });
    toast("Muunnos lisätty työjonoon"); selectJob(j.id); await loadConvertData();
  });
  $("#convList").addEventListener("click", guard(async (e) => {
    const id = e.target.dataset.cdel; if (!id) return;
    if (!confirm("Poistetaanko muunnos?")) return;
    await api(`/api/conversions/${id}`, { method: "DELETE" }); await loadConvertData();
  }));
}

// ------------------------------------------------------------------ käynnistys
function bindAll() {
  $("#newProject").onclick = newItem;
  $("#newProject2").onclick = newProject;
  $$(".sections button").forEach((b) => b.onclick = async () => {
    const sec = b.dataset.section;
    showSection(sec);
    if (sec === "convert") loadConvertData();
    if (sec === "voice" && !state.voice && state.voices.length) {
      const last = localStorageGet("lastVoice");
      openVoice(state.voices.some((v) => v.id === last) ? last : state.voices[0].id);
    }
    if (sec === "music" && !state.project && state.projects.length) {
      const last = localStorageGet("lastProject");
      openProject(state.projects.some((p) => p.id === last) ? last : state.projects[0].id);
    }
  });
  $$("#project .tabs button").forEach((b) => b.onclick = () => setTab(b.dataset.tab));
  $("#pName").onchange = (e) => patchProject({ name: e.target.value });
  $("#pTag").onchange = (e) => patchProject({ trigger_tag: e.target.value });
  $("#pTagPos").onchange = (e) => patchProject({ tag_position: e.target.value });
  $("#pDelete").onclick = guard(async () => {
    if (!confirm(`Poistetaanko projekti "${state.project.name}" kaikkine kappaleineen, ajoineen ja näytteineen?`)) return;
    await api(`/api/projects/${state.project.id}`, { method: "DELETE" });
    state.project = null; await loadProjects(); showSection("music");
  });
  $("#analyzeBtn").onclick = analyze;
  $("#trainForm").onsubmit = startTraining;
  $("#trainForm").addEventListener("input", (e) => { if (e.target.name === "epochs" || e.target.name === "save_every") e.target.form.dataset.touched = "1"; });
  $("#jdStop").onclick = guard(async () => {
    if (!confirm("Pysäytetäänkö työ? Koulutuksessa jo tallennetut checkpointit säilyvät.")) return;
    await api(`/api/jobs/${state.jobId}/stop`, { method: "POST" });
    toast("Pysäytetään…");
  });
  bindTrackList(); bindUpload(); bindRuns(); bindListen(); bindVoice(); bindConvert();
}

async function pollJobs() {
  try {
    const all = await api("/api/jobs");
    const prevBusy = state.jobsAll.filter((j) => j.status === "running" || j.status === "queued").map((j) => j.id).join();
    state.jobsAll = all;
    renderJobs();
    const busy = all.filter((j) => j.status === "running" || j.status === "queued");
    const busyIds = busy.map((j) => j.id).join();
    const changed = busyIds !== prevBusy;
    // Päivitetään avoin näkymä, kun sen työt etenevät tai päättyvät
    if (state.section === "music" && state.project && (changed || busy.some((j) => j.project_id === state.project.id))) refreshProject();
    if (state.section === "voice" && state.voice && (changed || busy.some((j) => j.project_id === state.voice.id))) refreshVoice();
    if (state.section === "convert" && (changed || busy.some((j) => j.project_id === "convert"))) {
      const [convs, models] = await Promise.all([api("/api/conversions"), changed ? api("/api/rvc/models") : state.rvcModels]);
      if (JSON.stringify(convs) !== JSON.stringify(state.conversions) || changed) {
        state.conversions = convs; state.rvcModels = models; renderConvert(true);
      }
    }
    if (changed) { loadProjects().then(() => state.section === "music" && renderSideList()); loadVoices(); }
  } catch { /* */ }
}

(async function init() {
  bindAll();
  await pollStatus();
  await Promise.all([loadProjects(), loadVoices(), pollJobs()]);
  const sec = localStorageGet("section") || "music";
  const lastP = localStorageGet("lastProject"), lastV = localStorageGet("lastVoice");
  if (state.projects.length) await openProject(state.projects.some((p) => p.id === lastP) ? lastP : state.projects[0].id);
  if (state.voices.length) await openVoice(state.voices.some((v) => v.id === lastV) ? lastV : state.voices[0].id);
  showSection(sec);
  if (sec === "convert") loadConvertData();
  const run = state.jobsAll.find((j) => j.status === "running");
  if (run) selectJob(run.id);
  setInterval(pollStatus, 4000);
  setInterval(pollJobs, 2500);
  setInterval(pollJob, 2000);
})();
