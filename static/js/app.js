/* Sistem Pemantauan Sampah Cerdas ITERA – interaksi & pembaruan real-time */
(() => {
  const $ = (s, r = document) => r.querySelector(s);
  const $$ = (s, r = document) => [...r.querySelectorAll(s)];
  const APP = window.APP || {};
  const LABEL = { penuh: "Penuh", kosong: "Kosong", berserakan: "Berserakan", sebagian: "Terisi sebagian", bersih: "Bersih", offline: "CCTV offline" };
  const BULAN = ["Jan", "Feb", "Mar", "Apr", "Mei", "Jun", "Jul", "Agu", "Sep", "Okt", "Nov", "Des"];
  const svg = (p, s = 22) => `<svg class="ic" width="${s}" height="${s}" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${p}</svg>`;
  const IC = {
    alert: svg('<path d="M12 3 2 20.5h20z"/><path d="M12 10v4.5"/><circle cx="12" cy="17.3" r=".6" fill="currentColor"/>'),
    info: svg('<circle cx="12" cy="12" r="9"/><path d="M12 11v5.5"/><circle cx="12" cy="7.8" r=".6" fill="currentColor"/>'),
    check: svg('<circle cx="12" cy="12" r="9"/><path d="m8 12.3 2.8 2.7L16 9.5"/>'),
    plug: svg('<path d="M9 3v5M15 3v5"/><path d="M6 8h12v3a6 6 0 0 1-12 0z"/><path d="M12 17v4"/>'),
    wa: svg('<path d="M20.5 11.6a8.5 8.5 0 0 1-12.6 7.4L3.5 20.5 5 16.3A8.5 8.5 0 1 1 20.5 11.6z"/><path d="M9 8.5c0 3.5 2.6 6.5 6.5 6.5l.9-1.6-2-1-1 .9c-1.2-.5-2.2-1.5-2.7-2.7l.9-1-1-2z" fill="currentColor" stroke="none"/>', 22),
  };
  const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const parseTs = (s) => (s ? new Date(s.replace(" ", "T")) : null);
  const hhmm = (s) => { const d = parseTs(s); return d ? `${String(d.getHours()).padStart(2, "0")}:${String(d.getMinutes()).padStart(2, "0")} WIB` : "–"; };
  const fullDate = (d) => `${d.getDate()} ${BULAN[d.getMonth()]} ${d.getFullYear()} ${d.toLocaleTimeString("id-ID", { hour12: false }).replace(/\./g, ":")} WIB`;
  const setF = (name, val) => $$(`[data-f="${name}"]`).forEach((el) => { el.textContent = val; });

  /* ---------- umum */
  const toast = (msg) => {
    const t = $("#toast"); if (!t) return;
    t.textContent = msg; t.classList.add("show");
    clearTimeout(t._h); t._h = setTimeout(() => t.classList.remove("show"), 2600);
  };
  $$("[data-open-side]").forEach((b) => b.addEventListener("click", () => document.body.classList.add("side-open")));
  $$("[data-close-side]").forEach((b) => b.addEventListener("click", () => document.body.classList.remove("side-open")));
  document.addEventListener("keydown", (e) => { if (e.key === "Escape") { document.body.classList.remove("side-open"); $$("details.usermenu[open]").forEach((d) => d.removeAttribute("open")); } });
  document.addEventListener("click", (e) => {
    $$("details.usermenu[open]").forEach((d) => { if (!d.contains(e.target)) d.removeAttribute("open"); });
    const c = e.target.closest("[data-confirm]");
    if (c && !confirm(c.dataset.confirm)) e.preventDefault();
  });

  const post = (url, body) => fetch(url, {
    method: "POST", headers: { "Content-Type": "application/json", "X-CSRF": APP.csrf }, body: JSON.stringify(body || {}),
  });

  /* ---------- toggle notifikasi */
  $$("[data-notify-toggle]").forEach((cb) => cb.addEventListener("change", async () => {
    const on = cb.checked;
    $$("[data-notify-toggle]").forEach((o) => { o.checked = on; });
    paintWa(on);
    try {
      const r = await post("/api/notify-toggle", { enabled: on });
      if (!r.ok) throw 0;
      toast(on ? "Notifikasi WhatsApp otomatis diaktifkan" : "Notifikasi WhatsApp otomatis dinonaktifkan");
    } catch {
      $$("[data-notify-toggle]").forEach((o) => { o.checked = !on; });
      paintWa(!on); toast("Gagal menyimpan. Periksa koneksi ke server.");
    }
  }));
  function paintWa(on) {
    setF("wa", on ? "Aktif" : "Nonaktif");
    $("#kpiWa")?.classList.toggle("off", !on);
  }

  /* ---------- CCTV */
  const cctv = $("#cctv");
  const img = cctv && $(".cctv-img", cctv);
  if (img) {
    img.addEventListener("error", () => setTimeout(() => { img.src = APP.frameUrl + "?t=" + Date.now(); }, 3000));
    $("[data-fullscreen]", cctv)?.addEventListener("click", () => {
      if (document.fullscreenElement) document.exitFullscreen(); else cctv.requestFullscreen?.();
    });
    const tick = () => setF("clock", fullDate(new Date()));
    tick(); setInterval(tick, 1000);
  }
  const showRoi = $("#showRoi");

  /* gambar CCTV mengisi panel penuh (cover) tanpa merusak posisi kotak deteksi:
     kotak overlay ikut diukur dari ukuran gambar asli */
  let natural = [960, 441];
  function fitFrame() {
    const stage = $(".cctv-stage"), frame = $(".cctv-frame");
    if (!stage || !frame) return;
    const [iw, ih] = img.naturalWidth ? [img.naturalWidth, img.naturalHeight] : natural;
    stage.style.aspectRatio = document.fullscreenElement ? "auto" : `${iw} / ${ih}`;
    const sw = stage.clientWidth, sh = stage.clientHeight;
    const s = (document.fullscreenElement ? Math.min : Math.max)(sw / iw, sh / ih);
    const w = iw * s, h = ih * s;
    Object.assign(frame.style, { width: w + "px", height: h + "px", left: (sw - w) / 2 + "px", top: (sh - h) / 2 + "px" });
  }
  if (img) {
    img.addEventListener("load", fitFrame);
    new ResizeObserver(fitFrame).observe($(".cctv-stage"));
    document.addEventListener("fullscreenchange", () => setTimeout(fitFrame, 50));
  }

  function drawOverlay(st) {
    const ov = $("[data-overlay]"); if (!ov) return;
    let html = "";
    if (showRoi?.checked && st.roi) {
      const [x1, y1, x2, y2] = st.roi;
      html += `<div class="roi" style="left:${x1 * 100}%;top:${y1 * 100}%;width:${(x2 - x1) * 100}%;height:${(y2 - y1) * 100}%"><i>ROI</i></div>`;
    }
    if (st.online) for (const b of st.boxes || []) {
      const [x1, y1, x2, y2] = b.box;
      const bad = b.kind === "penuh" || b.kind === "berserakan";
      const below = y1 < 0.12;
      const txt = b.kind === "penuh" ? `Sampah Penuh (${st.fullness != null ? Math.round(st.fullness) : Math.round(b.conf * 100)}%)`
        : `Sampah ${LABEL[b.kind] || b.kind} (${Math.round(b.conf * 100)}%)`;
      html += `<div class="bbox ${bad ? "" : "ok"} ${b.kind === "berserakan" ? "litter" : ""} ${below ? "below" : ""}" style="left:${x1 * 100}%;top:${y1 * 100}%;width:${(x2 - x1) * 100}%;height:${(y2 - y1) * 100}%"><b>${esc(txt)}</b></div>`;
    }
    ov.innerHTML = html;
    // label berserakan cukup satu (pada kotak paling kanan) agar tidak menutupi tong sampah
    const lit = $$(".bbox.litter", ov).sort((a, b) => parseFloat(a.style.left) - parseFloat(b.style.left));
    lit.slice(0, -1).forEach((el) => el.querySelector("b")?.remove());
    lit.at(-1)?.classList.add("tag-right");
    $("[data-offline]")?.toggleAttribute("hidden", !!st.online);
    $$("[data-live]").forEach((l) => { l.classList.toggle("off", !st.online); l.textContent = st.online ? "Live" : "Offline"; });
  }

  /* ---------- dashboard state */
  let trendChart = null, trendHours = 6, lastState = null;
  const tone = (el, t) => el && (el.dataset.tone = t);

  function render(st) {
    lastState = st;
    if (st.frame_size && img) { natural = st.frame_size; fitFrame(); }
    $$("[data-pending]").forEach((d) => d.toggleAttribute("hidden", !st.pending));
    drawOverlay(st);
    setF("cam_name", st.camera.name); setF("cam_loc", st.camera.location);

    const bin = st.online ? st.bin_state : null;
    const status = !st.online ? "CCTV offline" : bin === "sebagian" ? "Terisi" : bin ? LABEL[bin] : "Menunggu";
    setF("status", status);
    tone($("#kpiStatus"), !st.online ? "neutral" : bin === "penuh" ? "red" : bin === "kosong" ? "green" : bin ? "amber" : "neutral");
    const fl = st.fullness;
    setF("fullness", st.online && fl != null ? `${Math.round(fl)}%` : "–");
    const m = $("#fullMeter");
    if (m) {
      const v = st.online && fl != null ? fl : 0;
      m.firstElementChild.style.width = v + "%"; m.setAttribute("aria-valuenow", Math.round(v));
      m.classList.toggle("mid", v < st.thresholds.full && v > st.thresholds.empty);
      m.classList.toggle("low", v <= st.thresholds.empty);
    }
    const area = st.online ? st.area_state : null;
    setF("area", !st.online ? "Tidak terpantau" : area ? LABEL[area] : "Menunggu deteksi");
    tone($("#kpiArea"), area === "berserakan" ? "red" : area === "bersih" ? "green" : "neutral");
    paintWa(st.notify_enabled);
    $$("[data-notify-toggle]").forEach((cb) => { if (document.activeElement !== cb) cb.checked = st.notify_enabled; });

    // klasifikasi
    $$(".cls").forEach((c) => {
      const k = c.dataset.cls;
      const on = st.online && ((k === "penuh" && bin === "penuh") || (k === "kosong" && bin === "kosong") || (k === "berserakan" && area === "berserakan"));
      c.classList.toggle("on", on);
    });

    // statistik hari ini
    setF("today_penuh", st.today.penuh); setF("today_berserakan", st.today.berserakan); setF("today_kosong", st.today.kosong);
    setF("last_sent", st.last_sent ? hhmm(st.last_sent) : "belum ada");
    const r = st.recipients;
    setF("recipient", r.length ? `${r[0].phone_number} (${r[0].recipient_name})${r.length > 1 ? ` +${r.length - 1} lainnya` : ""}` : "Belum ada penerima");

    // halaman CCTV
    setF("cam_status", st.online ? "Online" : "Offline");
    setF("mode", { simulasi: "Simulasi (demo)", yolo: "YOLO", eksternal: "AI eksternal", "kamera (tanpa model)": "Kamera tanpa model" }[st.mode] || st.mode);
    setF("confidence", st.confidence != null ? `${Math.round(st.confidence * 100)}%` : "–");
    setF("last_update", st.last_update ? fullDate(parseTs(st.last_update)) : "–");
    const bl = $("#boxList");
    if (bl) bl.innerHTML = st.online && st.boxes.length
      ? st.boxes.map((b) => `<tr><td><span class="tag k-${b.kind}">${esc(LABEL[b.kind] || b.kind)}</span></td><td class="num">${Math.round(b.conf * 100)}%</td></tr>`).join("")
      : `<tr><td colspan="2" class="empty">${st.online ? "Belum ada objek terdeteksi." : "Kamera offline."}</td></tr>`;

    renderAlerts(st.alerts);
    renderTrend(st);
    renderSim(st);
    checkAlerts(st);
  }

  function renderAlerts(list) {
    const ul = $("#alerts"); if (!ul) return;
    if (!list.length) { ul.innerHTML = `<li class="empty">Belum ada kejadian yang tercatat.</li>`; return; }
    ul.innerHTML = list.map((a, i) => {
      const k = a.detection_type;
      const ic = k === "penuh" ? `<span class="i-red">${IC.alert}</span>` : k === "berserakan" ? `<span class="i-amber">${IC.info}</span>`
        : k === "kosong" ? `<span class="i-green">${IC.check}</span>` : `<span class="i-grey">${IC.plug}</span>`;
      const title = { penuh: "Sampah penuh", berserakan: "Sampah berserakan", kosong: "Sampah kosong", offline: "CCTV offline" }[k] || k;
      const pc = a.fullness_percentage != null ? Math.round(a.fullness_percentage) + "%" : "–";
      const pcl = k === "penuh" ? "red" : k === "berserakan" ? "amber" : k === "kosong" ? "green" : "grey";
      const wa = a.wa_status ? (a.wa_status === "failed" ? `<span class="wa-bad" title="WhatsApp gagal">${IC.wa}</span>` : `<span class="wa-ok" title="WhatsApp ${esc(a.wa_status)}">${IC.wa}</span>`) : `<span class="wa-none" title="Tidak dikirim">${IC.wa}</span>`;
      const hot = i === 0 && a.action_status === "menunggu";
      return `<li class="${hot ? "hot" : ""}">${ic}<a class="a-title" href="/riwayat/${a.id}">${title}${a.action_status === "menunggu" ? "" : a.action_status === "selesai" ? " · ditangani" : ""}</a><span class="pill ${pcl}">${pc}</span><span class="a-time">${hhmm(a.detected_at)}</span>${wa}</li>`;
    }).join("");
  }

  function renderTrend(st) {
    const cv = $("#trendChart"); if (!cv || !window.Chart) return;
    const pts = st.trend.map((p) => ({ x: parseTs(p.t).getTime(), y: p.v }));
    $("[data-trend-empty]")?.toggleAttribute("hidden", pts.length > 0);
    if (!trendChart) {
      trendChart = new Chart(cv, {
        type: "line",
        data: { datasets: [{ data: pts, borderColor: "#c22029", borderWidth: 2, pointRadius: 0, pointHoverRadius: 4, tension: .25, fill: true,
          backgroundColor: (c) => { const g = c.chart.ctx.createLinearGradient(0, 0, 0, c.chart.height); g.addColorStop(0, "rgba(194,32,41,.28)"); g.addColorStop(1, "rgba(194,32,41,0)"); return g; } }] },
        options: {
          animation: false, maintainAspectRatio: false, parsing: false,
          plugins: { legend: { display: false }, tooltip: { callbacks: { title: (i) => new Date(i[0].parsed.x).toLocaleTimeString("id-ID", { hour: "2-digit", minute: "2-digit" }) + " WIB", label: (i) => ` Kepenuhan ${i.parsed.y}%` } } },
          scales: {
            x: { type: "linear", ticks: { maxTicksLimit: 6, color: "#6a717c", callback: (v) => new Date(v).toLocaleTimeString("id-ID", { hour: "2-digit", minute: "2-digit" }) }, grid: { display: false } },
            y: { min: 0, max: 100, ticks: { stepSize: 25, color: "#6a717c", callback: (v) => v + "%" }, grid: { color: "#eef0f2" } },
          },
        },
      });
    }
    trendChart.data.datasets[0].data = pts;
    trendChart.update("none");
  }

  $$("[data-hours]").forEach((b) => b.addEventListener("click", () => {
    $$("[data-hours]").forEach((o) => o.classList.toggle("on", o === b));
    trendHours = +b.dataset.hours; poll();
  }));
  showRoi?.addEventListener("change", () => lastState && drawOverlay(lastState));

  let polling = false;
  async function poll() {
    if (polling) return; polling = true;
    try {
      const r = await fetch(`/api/state?jam=${trendHours}`, { cache: "no-store" });
      if (r.status === 401) { location.href = "/login"; return; }
      if (r.ok) render(await r.json());
    } catch { /* server sedang restart: coba lagi di siklus berikutnya */ }
    finally { polling = false; }
  }
  // semua halaman admin ikut memantau, supaya alert muncul di mana pun admin berada
  if (APP.page && APP.page !== "None") { poll(); setInterval(poll, 2500); }

  /* ---------- simulasi */
  const SIM_LABEL = { penuh: "Penuh", sebagian: "Terisi sebagian", kosong: "Kosong", berserakan: "Berserakan", penuh_berserakan: "Penuh & berserakan", offline: "CCTV offline" };
  function renderSim(st) {
    if (APP.page !== "simulasi") return;
    const sim = st.sim;
    const a = sim && sim.active;
    $("[data-sim-name]") && ($("[data-sim-name]").textContent = a ? a.name : "Simulasi tidak aktif");
    const tag = $("[data-sim-tag]");
    if (tag) { tag.textContent = a ? SIM_LABEL[a.condition] : ""; tag.className = "tag" + (a ? " k-" + a.condition.split("_")[0] : ""); }
    const auto = sim && sim.mode === "auto";
    $("[data-sim-cycle]")?.toggleAttribute("hidden", !auto);
    $("[data-sim-manual]")?.toggleAttribute("hidden", !sim || auto);
    if (auto && sim.next_in != null) {
      $("[data-sim-next]").textContent = sim.next_in + " detik";
      $("[data-sim-bar]").style.width = Math.max(0, 100 - (sim.next_in / sim.cycle) * 100) + "%";
    }
    $$(".sim-card[data-sim-id]").forEach((c) => c.classList.toggle("now", !!a && +c.dataset.simId === a.id));
  }
  const fileIn = $('.upload input[type="file"]');
  fileIn?.addEventListener("change", () => { $("[data-file-label]").textContent = fileIn.files[0]?.name || "Pilih foto tempat sampah (JPG/PNG)"; });

  /* ---------- alert pop-up real-time */
  const ALERT_TXT = {
    penuh: ["Sampah penuh terdeteksi", "red", IC.alert],
    berserakan: ["Sampah berserakan terdeteksi", "amber", IC.info],
    kosong: ["Tempat sampah kembali kosong", "green", IC.check],
    offline: ["CCTV terputus", "grey", IC.plug],
  };
  let lastAlertId = null;
  const ssGet = () => { try { return +sessionStorage.getItem("lastAlertId") || null; } catch { return null; } };
  const ssSet = (v) => { try { sessionStorage.setItem("lastAlertId", v); } catch { } };
  let audioCtx = null;
  document.addEventListener("pointerdown", () => { try { audioCtx = audioCtx || new (window.AudioContext || window.webkitAudioContext)(); } catch { } }, { once: true });
  function beep(urgent) {
    if (!audioCtx) return;
    try {
      const t = audioCtx.currentTime;
      (urgent ? [880, 660] : [660]).forEach((f, i) => {
        const o = audioCtx.createOscillator(), g = audioCtx.createGain();
        o.frequency.value = f; o.type = "sine";
        g.gain.setValueAtTime(0.0001, t + i * 0.18); g.gain.exponentialRampToValueAtTime(0.12, t + i * 0.18 + 0.02);
        g.gain.exponentialRampToValueAtTime(0.0001, t + i * 0.18 + 0.16);
        o.connect(g).connect(audioCtx.destination); o.start(t + i * 0.18); o.stop(t + i * 0.18 + 0.17);
      });
    } catch { }
  }
  function popAlert(a, cam) {
    const stack = $("#alertStack"); if (!stack) return;
    const [title, tone, ic] = ALERT_TXT[a.detection_type] || [a.detection_type, "grey", IC.info];
    const el = document.createElement("div");
    el.className = `alert-pop t-${tone}`;
    el.setAttribute("role", "alert");
    const pct = a.fullness_percentage != null && a.detection_type !== "berserakan" ? `<span class="pill ${tone}">${Math.round(a.fullness_percentage)}%</span>` : "";
    el.innerHTML = `<span class="ap-ic">${ic}</span>
      <div class="ap-body"><b>${esc(title)}</b>
        <p>${esc(cam.name)} · ${esc(cam.location)} · ${hhmm(a.detected_at)}</p>
        <div class="ap-act"><a class="btn sm" href="/riwayat/${a.id}">Lihat detail</a>${pct}
        ${a.wa_status ? `<span class="ap-wa">${IC.wa} WhatsApp ${a.wa_status === "failed" ? "gagal" : "terkirim"}</span>` : ""}</div></div>
      <button class="ap-x" aria-label="Tutup">×</button><i class="ap-timer"></i>`;
    const close = () => { el.classList.add("out"); setTimeout(() => el.remove(), 250); };
    el.querySelector(".ap-x").addEventListener("click", close);
    let left = 9000, t0 = Date.now(), timer = setTimeout(close, left);
    el.addEventListener("mouseenter", () => { clearTimeout(timer); left -= Date.now() - t0; el.classList.add("hold"); });
    el.addEventListener("mouseleave", () => { t0 = Date.now(); timer = setTimeout(close, left); el.classList.remove("hold"); });
    stack.prepend(el);
    $$(".alert-pop", stack).slice(4).forEach((x) => x.remove());
    beep(tone === "red" || tone === "amber");
    if (document.hidden) {
      const old = document.title; document.title = "⚠ " + title;
      setTimeout(() => { document.title = old; }, 6000);
    }
  }
  function checkAlerts(st) {
    const list = st.alerts || [];
    const maxId = list.reduce((m, a) => Math.max(m, a.id), 0);
    if (lastAlertId === null) {
      const seen = ssGet();
      lastAlertId = seen != null && seen <= maxId ? seen : maxId; // kejadian saat pindah halaman tetap muncul
    }
    list.filter((a) => a.id > lastAlertId).reverse().slice(-3).forEach((a) => popAlert(a, st.camera));
    if (maxId > lastAlertId) lastAlertId = maxId;
    ssSet(lastAlertId);
  }

  /* ---------- laporan */
  const rd = $("#reportData");
  if (rd && window.Chart) {
    const d = JSON.parse(rd.textContent);
    const lbl = d.days.map((s) => { const t = new Date(s + "T00:00"); return `${t.getDate()} ${BULAN[t.getMonth()]}`; });
    const base = { maintainAspectRatio: false, animation: false, plugins: { legend: { position: "bottom", labels: { boxWidth: 12, color: "#444b56" } } } };
    const grid = { color: "#eef0f2" };
    new Chart($("#perDay"), { type: "bar", data: { labels: lbl, datasets: [
      { label: "Penuh", data: d.series.penuh, backgroundColor: "#c22029", borderRadius: 3 },
      { label: "Berserakan", data: d.series.berserakan, backgroundColor: "#d9960f", borderRadius: 3 },
      { label: "Kosong", data: d.series.kosong, backgroundColor: "#1c9a52", borderRadius: 3 }] },
      options: { ...base, scales: { x: { stacked: true, grid: { display: false } }, y: { stacked: true, beginAtZero: true, ticks: { precision: 0 }, grid } } } });
    new Chart($("#fillChart"), { type: "line", data: { labels: lbl, datasets: [
      { label: "Rata-rata", data: d.fill_avg, borderColor: "#c22029", backgroundColor: "rgba(194,32,41,.12)", fill: true, tension: .3, spanGaps: true },
      { label: "Maksimum", data: d.fill_max, borderColor: "#b8954a", borderDash: [5, 4], tension: .3, spanGaps: true, pointRadius: 2 }] },
      options: { ...base, scales: { y: { min: 0, max: 100, ticks: { callback: (v) => v + "%" }, grid }, x: { grid: { display: false } } } } });
    new Chart($("#perHour"), { type: "bar", data: { labels: d.hours.map((_, h) => String(h).padStart(2, "0")), datasets: [
      { label: "Kejadian penuh & berserakan", data: d.hours, backgroundColor: d.hours.map((v) => v === Math.max(...d.hours) && v > 0 ? "#a3161e" : "#e6a5a8"), borderRadius: 3 }] },
      options: { ...base, plugins: { legend: { display: false } }, scales: { y: { beginAtZero: true, ticks: { precision: 0 }, grid }, x: { grid: { display: false }, ticks: { maxRotation: 0, autoSkip: true, maxTicksLimit: 12 } } } } });
  }
  const pf = $("#periodForm");
  if (pf) {
    $$('input[name="periode"]', pf).forEach((r) => r.addEventListener("change", () => {
      if (r.value !== "kustom") pf.submit();
      $$(".seg label", pf).forEach((l) => l.classList.toggle("on", l.contains(r)));
    }));
    $$('input[type="date"]', pf).forEach((i) => i.addEventListener("change", () => { $('input[value="kustom"]', pf).checked = true; $$(".seg label", pf).forEach((l) => l.classList.toggle("on", l.contains($('input[value="kustom"]', pf)))); }));
  }

  /* ---------- pengaturan */
  const pform = $("[data-provider-form]");
  if (pform) {
    const sel = $("[data-provider]", pform);
    const upd = () => { pform.dataset.show = sel.value; };
    sel.addEventListener("change", upd); upd();
  }
  const roiBox = $("[data-roi-box]");
  if (roiBox) {
    const upd = () => {
      const v = Object.fromEntries($$("[data-roi]").map((i) => [i.dataset.roi, Math.max(0, Math.min(100, +i.value || 0))]));
      Object.assign(roiBox.style, { left: v.x1 + "%", top: v.y1 + "%", width: Math.max(0, v.x2 - v.x1) + "%", height: Math.max(0, v.y2 - v.y1) + "%" });
    };
    $$("[data-roi]").forEach((i) => i.addEventListener("input", upd)); upd();
  }
})();
