"use strict";

/* ---------- utilidades ---------- */
const $ = (sel, root = document) => root.querySelector(sel);
const app = $("#app");
const enc = (v) => encodeURIComponent(v);
const dec = (v) => decodeURIComponent(v);
const esc = (v) => String(v ?? "").replace(/[&<>"']/g, (c) => (
  { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]
));

const STATUS = {
  adequado: { label: "Adequado", icon: "✓", cls: "ok" },
  subdimensionado: { label: "Subdimensionado", icon: "▲", cls: "bad" },
  superdimensionado: { label: "Superdimensionado", icon: "▼", cls: "bad" },
  sem_configuracao: { label: "Sem configuração", icon: "○", cls: "neutral" },
  dados_insuficientes: { label: "Dados insuficientes", icon: "◌", cls: "neutral" },
  desvio: { label: "Com desvio", icon: "▲", cls: "bad" },
  pendente: { label: "Pendente", icon: "◌", cls: "neutral" },
};
const STATUS_OPTIONS = [
  ["", "Todos"], ["desvio", "Com desvio"], ["pendente", "Pendente"], ["adequado", "Adequado"],
  ["subdimensionado", "Subdimensionado"], ["superdimensionado", "Superdimensionado"],
  ["sem_configuracao", "Sem configuração"], ["dados_insuficientes", "Dados insuficientes"],
];
const DASH = { solid: "", dash: "6 4", dot: "2 3", suggest: "4 4" };
const pct = (v, total) => ((v / Math.max(total, 1)) * 100).toFixed(2);

const badge = (status) => {
  const s = STATUS[status] || { label: status, icon: "?", cls: "neutral" };
  return `<span class="badge ${s.cls}"><span aria-hidden="true">${s.icon}</span>${esc(s.label)}</span>`;
};
const fmtCpu = (v) => (v == null ? "—" : `${Math.round(v)}m`);
const fmtMem = (v) => {
  if (v == null) return "—";
  return v >= 1024 ? `${(v / 1024).toFixed(2).replace(".", ",")}Gi` : `${Math.round(v)}Mi`;
};
const fmtVal = (v, unit) => (unit === "m" ? fmtCpu(v) : fmtMem(v));
const fmtAdj = (v, unit) => {
  if (v == null) return "—";
  const sign = v > 0 ? "+" : v < 0 ? "−" : "";
  return `${sign}${fmtVal(Math.abs(v), unit)}`;
};
const fmtDate = (iso) => (iso ? new Date(iso).toLocaleString("pt-BR") : "—");
const fmtDay = (sec) => new Date(sec * 1000).toLocaleString("pt-BR", {
  day: "2-digit", month: "2-digit", hour: "2-digit", minute: "2-digit",
});
const fmtPct = (r) => `${Math.round(r * 100)}%`;

function toQuery(obj) {
  const p = new URLSearchParams();
  for (const [k, v] of Object.entries(obj)) {
    if (v !== "" && v != null && !(k === "sort" && v === "ajuste")) p.set(k, v);
  }
  return p.toString();
}
const options = (list, selected) => list.map(
  ([value, label]) => `<option value="${esc(value)}"${value === selected ? " selected" : ""}>${esc(label)}</option>`
).join("");

/* ---------- cabeçalho, banner e erros ---------- */
function setMeta(win, meta) {
  const parts = [];
  if (win) parts.push(`Janela: últimos ${win.days} dias, de ${fmtDate(win.start)} a ${fmtDate(win.end)}`);
  if (meta) parts.push(`Última coleta: ${fmtDate(meta.lastSuccessfulAt)}`);
  $("#meta").innerHTML = parts.map((p) => `<span>${esc(p)}</span>`).join("");
}
function setCrumbs(html) { $("#crumbs").innerHTML = html; }

function showBanner(meta) {
  const el = $("#banner");
  if (meta && meta.stale) {
    el.textContent = `Dados desatualizados. Última coleta bem-sucedida em ${fmtDate(meta.lastSuccessfulAt)}. Motivo: ${meta.error}`;
    el.hidden = false;
  } else {
    el.hidden = true;
    el.textContent = "";
  }
}

async function api(path) {
  const res = await fetch(path, { headers: { Accept: "application/json" } });
  const body = await res.json().catch(() => ({}));
  if (!res.ok) {
    throw new Error([body.error, body.detail].filter(Boolean).join(": ") || `HTTP ${res.status}`);
  }
  showBanner(body.meta);
  return body;
}

/* ---------- visão geral ---------- */
const nsCard = (n) => `
  <a class="card" href="#/ns/${enc(n.name)}">
    <div class="card-head">
      <h3>${esc(n.name)}</h3>
      <span class="count">${n.workloads} workloads</span>
    </div>
    <div class="stack" role="img" aria-label="${n.counts.adequado} adequados, ${n.counts.desvio} com desvio, ${n.counts.pendente} pendentes">
      <span class="s-ok" data-w="${pct(n.counts.adequado, n.workloads)}"></span>
      <span class="s-bad" data-w="${pct(n.counts.desvio, n.workloads)}"></span>
      <span class="s-neutral" data-w="${pct(n.counts.pendente, n.workloads)}"></span>
    </div>
    <ul class="legend-inline">
      <li><span class="dot ok"></span>${n.counts.adequado} adequados</li>
      <li><span class="dot bad"></span>${n.counts.desvio} com desvio</li>
      <li><span class="dot neutral"></span>${n.counts.pendente} pendentes</li>
    </ul>
    <dl class="kv">
      <dt>Requests a reduzir</dt><dd>CPU ${fmtCpu(n.capacity.cpu.reclaim)} · Memória ${fmtMem(n.capacity.memory.reclaim)}</dd>
      <dt>Requests a acrescentar</dt><dd>CPU ${fmtCpu(n.capacity.cpu.add)} · Memória ${fmtMem(n.capacity.memory.add)}</dd>
    </dl>
  </a>`;

async function renderOverview() {
  const data = await api("api/overview");
  setMeta(data.window, data.meta);
  setCrumbs("<strong>Visão geral</strong>");
  const q = data.quality;
  const retention = q.retentionSeconds == null
    ? "não identificada"
    : `${Math.round(q.retentionSeconds / 86400)} dias${q.retentionSufficient === false ? " (menor que a janela)" : ""}`;
  return `
    <h1>Visão geral por namespace</h1>
    <p class="muted">Recomendações apenas para consulta. Os ajustes passam pelo fluxo de deploy ou GitOps da equipe.</p>
    <section class="cards">
      ${data.namespaces.length ? data.namespaces.map(nsCard).join("")
        : '<p class="panel">Nenhum workload com métricas na janela analisada.</p>'}
    </section>
    <section class="panel">
      <h2>Qualidade dos dados</h2>
      <ul class="quality">
        <li>Retenção do Prometheus: ${esc(retention)}</li>
        <li>Workloads analisados: ${q.workloads}. Recentes (histórico menor que a janela): ${q.recentWorkloads}.</li>
        <li>Containers com lacunas de coleta: ${q.gapContainers}.</li>
        <li>Itens sem configuração: ${q.withoutConfig}. Com dados insuficientes: ${q.insufficientData}.</li>
        <li>Containers com throttling de CPU: ${q.throttledContainers}. Com OOMKilled: ${q.oomContainers}.</li>
      </ul>
    </section>`;
}

/* ---------- namespace ---------- */
function topItem(w) {
  return w.items
    .filter((i) => i.adjustment != null)
    .sort((a, b) => Math.abs(b.adjustment) - Math.abs(a.adjustment))[0];
}

async function renderNamespace(ns, qs) {
  const params = new URLSearchParams(qs);
  const filters = {
    q: params.get("q") || "",
    status: params.get("status") || "",
    resource: params.get("resource") || "",
    sort: params.get("sort") || "ajuste",
  };
  const query = toQuery(filters);
  const data = await api(`api/namespaces/${enc(ns)}?${query}`);
  setMeta(data.window, data.meta);
  setCrumbs(`<a href="#/">Visão geral</a> / <strong>${esc(ns)}</strong>`);
  const t = data.totals;
  const suffix = query ? `?${query}` : "";

  const rows = data.workloads.map((w) => {
    const top = topItem(w);
    return `
      <tr>
        <td><a href="#/ns/${enc(ns)}/w/${enc(w.kind)}/${enc(w.name)}${suffix}">${esc(w.name)}</a>
            <div class="muted small">${esc(w.kind)}</div></td>
        <td>${badge(w.status)}</td>
        <td class="num">${w.replicas}</td>
        <td>${top ? `${esc(top.label)} <strong>${fmtAdj(top.adjustment, top.unit)}</strong>` : "—"}</td>
        <td class="num" title="Maior razão entre consumo de referência e configuração atual">${w.pressure ? `${w.pressure.toFixed(2).replace(".", ",")}×` : "—"}</td>
        <td class="num">${fmtPct(w.coverage)}</td>
      </tr>`;
  }).join("");

  return `
    <header class="page-head">
      <div>
        <h1>Namespace ${esc(ns)}</h1>
        <p class="muted small">${data.window ? `Janela: últimos ${data.window.days} dias` : ""}</p>
      </div>
      <a class="button secondary" href="api/namespaces/${enc(ns)}/export.csv${suffix}" download>Exportar CSV</a>
    </header>

    <form id="filters" class="filters" role="search">
      <label>Buscar workload
        <input name="q" type="search" value="${esc(filters.q)}" placeholder="nome ou tipo">
      </label>
      <label>Recurso
        <select name="resource">${options([["", "Todos"], ["cpu", "CPU"], ["memory", "Memória"]], filters.resource)}</select>
      </label>
      <label>Status
        <select name="status">${options(STATUS_OPTIONS, filters.status)}</select>
      </label>
      <label>Ordenar por
        <select name="sort">${options([["ajuste", "Maior ajuste sugerido"], ["pressao", "Maior pressão observada"], ["nome", "Nome"]], filters.sort)}</select>
      </label>
    </form>

    <section class="cards">
      <div class="card static">
        <h3>CPU em requests</h3>
        <p>Pode reduzir <strong>${fmtCpu(t.capacity.cpu.reclaim)}</strong></p>
        <p>Precisa acrescentar <strong>${fmtCpu(t.capacity.cpu.add)}</strong></p>
      </div>
      <div class="card static">
        <h3>Memória em requests</h3>
        <p>Pode reduzir <strong>${fmtMem(t.capacity.memory.reclaim)}</strong></p>
        <p>Precisa acrescentar <strong>${fmtMem(t.capacity.memory.add)}</strong></p>
      </div>
      <div class="card static">
        <h3>Status dos workloads exibidos</h3>
        <div class="counts">
          <span class="badge ok">✓ ${t.counts.adequado} adequados</span>
          <span class="badge bad">▲ ${t.counts.desvio} com desvio</span>
          <span class="badge neutral">◌ ${t.counts.pendente} pendentes</span>
        </div>
      </div>
    </section>
    <p class="muted small capacity-note">Totais dos workloads exibidos com os filtros atuais, estimados para o número atual de
      réplicas ativas. Itens sem configuração não entram nos totais.</p>

    <div class="table-wrap">
      <table class="grid">
        <thead>
          <tr>
            <th>Workload</th><th>Status</th><th class="num">Réplicas</th>
            <th>Maior ajuste sugerido</th><th class="num">Pressão</th><th class="num">Cobertura</th>
          </tr>
        </thead>
        <tbody>${rows || '<tr><td colspan="6">Nenhum workload corresponde aos filtros.</td></tr>'}</tbody>
      </table>
    </div>`;
}

/* ---------- detalhe do workload ---------- */
function chartSeries(c, res, consumption) {
  const cfg = (key) => (c.config[key] || []).map((ch) => [Date.parse(ch.at) / 1000, ch.value]);
  const suggested = (kind) => c.items.find((i) => i.resource === res && i.kind === kind)?.suggested ?? null;
  return [
    { label: "Consumo (réplica mais carregada)", color: "#1D4ED8", style: "solid", points: consumption },
    { label: "Request", color: "#102A43", style: "dash", step: true, points: cfg(`${res}_request`) },
    { label: "Limit", color: "#475569", style: "dot", step: true, points: cfg(`${res}_limit`) },
    { label: "Sugerido (request)", color: "#1D4ED8", style: "suggest", value: suggested("request") },
  ];
}

function chartSvg(series, unit) {
  const W = 720, H = 240, L = 64, R = 16, T = 14, B = 30;
  const consumption = series[0].points || [];
  if (!consumption.length) return '<p class="muted">Sem série de consumo no período.</p>';
  const pts = series.flatMap((s) => s.points || []);
  const values = pts.map((p) => p[1]).concat(series.filter((s) => s.value != null).map((s) => s.value));
  const x0 = Math.min(...pts.map((p) => p[0]));
  const x1 = Math.max(x0 + 1, ...pts.map((p) => p[0]));
  const y1 = Math.max(...values, 1) * 1.1;
  const X = (t) => L + ((t - x0) / (x1 - x0)) * (W - L - R);
  const Y = (v) => T + (1 - v / y1) * (H - T - B);

  let grid = "";
  for (let i = 0; i <= 4; i++) {
    const v = (y1 * i) / 4;
    grid += `<line class="grid-line" x1="${L}" x2="${W - R}" y1="${Y(v)}" y2="${Y(v)}"/>`;
    grid += `<text class="axis" x="${L - 6}" y="${Y(v) + 4}" text-anchor="end">${fmtVal(v, unit)}</text>`;
  }
  [[0, "start"], [0.5, "middle"], [1, "end"]].forEach(([f, anchor]) => {
    const t = x0 + (x1 - x0) * f;
    grid += `<text class="axis" x="${X(t)}" y="${H - 8}" text-anchor="${anchor}">${fmtDay(t)}</text>`;
  });

  const paths = series.map((s) => {
    if (s.value != null) {
      return `<line x1="${L}" x2="${W - R}" y1="${Y(s.value)}" y2="${Y(s.value)}" stroke="${s.color}" `
        + `stroke-width="2" stroke-dasharray="${DASH[s.style]}"><title>${esc(s.label)}: ${fmtVal(s.value, unit)}</title></line>`
        + `<text class="direct" x="${W - R}" y="${Y(s.value) - 6}" text-anchor="end">Sugerido ${fmtVal(s.value, unit)}</text>`;
    }
    const p = s.points || [];
    if (!p.length) return "";
    let d;
    if (s.step) {
      d = `M${X(p[0][0]).toFixed(1)},${Y(p[0][1]).toFixed(1)}`;
      for (let i = 1; i < p.length; i++) d += `H${X(p[i][0]).toFixed(1)}V${Y(p[i][1]).toFixed(1)}`;
      d += `H${X(x1).toFixed(1)}`;
    } else {
      d = p.map((q, i) => `${i ? "L" : "M"}${X(q[0]).toFixed(1)},${Y(q[1]).toFixed(1)}`).join("");
    }
    return `<path d="${d}" fill="none" stroke="${s.color}" stroke-width="2" stroke-dasharray="${DASH[s.style]}"/>`;
  }).join("");

  return `<svg class="chart" viewBox="0 0 ${W} ${H}" role="img" aria-label="Gráfico de ${unit}">${grid}${paths}</svg>`;
}

function chartBlock(title, series, unit) {
  const legend = series.map((s) => {
    const hasData = s.value != null || (s.points && s.points.length);
    return `<li><svg class="swatch" viewBox="0 0 28 8" aria-hidden="true">`
      + `<line x1="0" x2="28" y1="4" y2="4" stroke="${s.color}" stroke-width="2.5" stroke-dasharray="${DASH[s.style]}"/>`
      + `</svg>${esc(s.label)}${hasData ? "" : ' <span class="muted">(sem valor)</span>'}</li>`;
  }).join("");
  return `<figure class="chart-block"><figcaption>${esc(title)}</figcaption>${chartSvg(series, unit)}`
    + `<ul class="legend">${legend}</ul></figure>`;
}

/* escala: consumo de referência (faixa), valor atual (marca escura) e sugerido (marca tracejada) */
function gauge(it) {
  const vals = [it.current, it.reference, it.suggested].filter((v) => v != null);
  if (!vals.length) return "";
  const max = Math.max(...vals) * 1.15 || 1;
  const pos = (v) => (v == null ? null : Math.min(100, (v / max) * 100).toFixed(2));
  const ref = pos(it.reference), cur = pos(it.current), sug = pos(it.suggested);
  const label = `Atual ${fmtVal(it.current, it.unit)}; consumo de referência ${fmtVal(it.reference, it.unit)}; sugerido ${fmtVal(it.suggested, it.unit)}`;
  return `<div class="gauge" role="img" aria-label="${esc(label)}">`
    + (ref != null ? `<span class="gauge-ref" data-w="${ref}"></span>` : "")
    + (sug != null ? `<span class="gauge-sug" data-left="${sug}"></span>` : "")
    + (cur != null ? `<span class="gauge-cur" data-left="${cur}"></span>` : "")
    + "</div>";
}

/* larguras e posições via CSSOM: o CSP do nginx bloqueia atributos style inline */
function applyLayout() {
  app.querySelectorAll("[data-w]").forEach((el) => { el.style.width = `${el.dataset.w}%`; });
  app.querySelectorAll("[data-left]").forEach((el) => { el.style.left = `${el.dataset.left}%`; });
}

function itemRow(c, it, id) {
  const cfgKey = `${it.resource}_${it.kind}`;
  const changes = c.config[cfgKey] || [];
  const history = changes.length
    ? changes.map((ch) => `${fmtDate(ch.at)}: ${fmtVal(ch.value, it.unit)}`).join("; ")
    : "sem valor configurado no período";
  return `
    <tr data-row="${id}" tabindex="0" aria-expanded="false">
      <td>${esc(c.name)}</td>
      <td>${esc(it.label)}${gauge(it)}</td>
      <td class="num">${fmtVal(it.current, it.unit)}</td>
      <td class="num">${fmtVal(it.reference, it.unit)}</td>
      <td class="num">${fmtVal(it.suggested, it.unit)}</td>
      <td class="num">${fmtAdj(it.adjustment, it.unit)}</td>
      <td>${badge(it.status)}</td>
    </tr>
    <tr hidden>
      <td colspan="7">
        <p><strong>Método:</strong> ${esc(it.method)}</p>
        <p><strong>Justificativa:</strong> ${esc(it.justification)}</p>
        <p><strong>Histórico de ${esc(it.label)}:</strong> ${esc(history)}</p>
      </td>
    </tr>`;
}

function podsTable(pods) {
  const rows = pods.map((p) => `
    <tr>
      <td>${esc(p.pod)}</td>
      <td>${p.active ? "Ativa" : "Encerrada"}</td>
      <td class="num">${fmtCpu(p.cpuRef)}</td>
      <td class="num">${fmtCpu(p.cpuPeak)}</td>
      <td class="num">${fmtCpu(p.cpuSuggested)}</td>
      <td class="num">${fmtMem(p.memRef)}</td>
      <td class="num">${fmtMem(p.memPeak)}</td>
      <td class="num">${fmtMem(p.memSuggested)}</td>
      <td class="num">${fmtPct(p.coverage)}</td>
      <td class="num">${p.oomEvents}</td>
      <td class="num">${p.throttlingPct.toFixed(1).replace(".", ",")}%</td>
    </tr>`).join("");
  return `
    <div class="table-wrap">
      <table class="grid compact">
        <thead>
          <tr>
            <th>Réplica</th><th>Situação</th><th class="num">CPU referência</th><th class="num">CPU pico</th>
            <th class="num">Sugerido CPU</th><th class="num">Memória referência</th><th class="num">Memória pico</th>
            <th class="num">Sugerido memória</th><th class="num">Cobertura</th><th class="num">OOM</th>
            <th class="num">Throttling</th>
          </tr>
        </thead>
        <tbody>${rows}</tbody>
      </table>
    </div>`;
}

function containerSection(c, ci, charts) {
  const chart = charts.find((x) => x.container === c.name) || { cpu: { consumption: [] }, memory: { consumption: [] } };
  const rows = c.items.map((it, idx) => itemRow(c, it, `row-${ci}-${idx}`)).join("");
  return `
    <section class="panel" aria-label="Container ${esc(c.name)}">
      <header class="panel-head">
        <h2>Container <code>${esc(c.name)}</code></h2>
        <span class="muted small">Reinícios: ${c.restarts}; OOMKilled no período: ${c.oomEvents} intervalo(s); throttling de CPU: ${c.throttlingPct.toFixed(1).replace(".", ",")}%</span>
      </header>
      <div class="table-wrap">
        <table class="grid">
          <thead>
            <tr>
              <th>Container</th><th>Recurso</th><th class="num">Atual</th><th class="num">Consumo de referência</th>
              <th class="num">Sugerido</th><th class="num">Ajuste</th><th>Status</th>
            </tr>
          </thead>
          <tbody>${rows}</tbody>
        </table>
      </div>
      <div class="gauge-key" aria-label="Legenda da escala">
        <span><i class="key-ref"></i>Consumo de referência</span>
        <span><i class="key-cur"></i>Atual</span>
        <span><i class="key-sug"></i>Sugerido</span>
        <span>Selecione uma linha para ver a justificativa e o histórico.</span>
      </div>
      <div class="charts">
        ${chartBlock("CPU (m)", chartSeries(c, "cpu", chart.cpu.consumption), "m")}
        ${chartBlock("Memória (Mi)", chartSeries(c, "memory", chart.memory.consumption), "Mi")}
      </div>
      <h3>Réplicas</h3>
      ${podsTable(c.pods)}
      <h3>Trecho YAML para revisão</h3>
      <div class="yaml-wrap">
        <pre class="yaml" id="yaml-${ci}">${esc(c.yaml)}</pre>
        <button class="button secondary" type="button" data-copy="yaml-${ci}">Copiar YAML</button>
      </div>
    </section>`;
}

async function renderDetail(ns, kind, name, qs) {
  const data = await api(`api/namespaces/${enc(ns)}/workloads/${enc(kind)}/${enc(name)}`);
  const w = data.workload;
  setMeta(null, data.meta);
  const back = `#/ns/${enc(ns)}${qs ? `?${qs}` : ""}`;
  setCrumbs(`<a href="#/">Visão geral</a> / <a href="${back}">${esc(ns)}</a> / <strong>${esc(name)}</strong>`);
  return `
    <a class="back" href="${back}">Voltar para ${esc(ns)}</a>
    <header class="page-head">
      <div>
        <h1>${esc(name)}</h1>
        <p class="muted small">${esc(w.kind)}, namespace ${esc(ns)}</p>
      </div>
      ${badge(w.status)}
    </header>
    <section class="cards">
      <div class="card static"><h3>Réplicas ativas</h3><p class="big">${w.replicas}</p></div>
      <div class="card static">
        <h3>Histórico disponível</h3>
        <p class="big">${Math.round(w.historyHours)} h</p>
        ${w.recent ? '<p class="warn">Workload recente: histórico menor que a janela analisada.</p>' : ""}
      </div>
      <div class="card static"><h3>Cobertura da coleta</h3><p class="big">${fmtPct(w.coverage)}</p></div>
    </section>
    ${w.containers.map((c, ci) => containerSection(c, ci, data.charts)).join("")}`;
}

/* ---------- roteamento ---------- */
function parseHash() {
  const raw = location.hash.replace(/^#/, "") || "/";
  const i = raw.indexOf("?");
  return { path: i < 0 ? raw : raw.slice(0, i), qs: i < 0 ? "" : raw.slice(i + 1) };
}

async function route() {
  const { path, qs } = parseHash();
  let m = path.match(/^\/ns\/([^/]+)\/w\/([^/]+)\/([^/]+)$/);
  if (m) return renderDetail(dec(m[1]), dec(m[2]), dec(m[3]), qs);
  m = path.match(/^\/ns\/([^/]+)$/);
  if (m) return renderNamespace(dec(m[1]), qs);
  return renderOverview();
}

let renderSeq = 0;
async function render() {
  const seq = ++renderSeq;
  const focused = document.activeElement && document.activeElement.name;
  let html;
  try {
    html = await route();
  } catch (err) {
    setCrumbs("");
    html = `<section class="panel error"><h2>Não foi possível carregar os dados</h2>
      <p>${esc(err.message)}</p>
      <p class="muted">Verifique PROMETHEUS_URL, a conectividade com o Prometheus e os logs do back-end.</p></section>`;
  }
  if (seq !== renderSeq) return; // navegação mais recente já assumiu a tela
  app.innerHTML = html;
  applyLayout();
  if (focused) {
    const el = app.querySelector(`[name="${focused}"]`);
    if (el) el.focus();
  }
}

/* ---------- interações (delegação de eventos) ---------- */
function toggleRow(row) {
  const detail = row.nextElementSibling;
  const open = detail.hidden;
  detail.hidden = !open;
  row.setAttribute("aria-expanded", String(open));
}

async function copyText(button) {
  const source = document.getElementById(button.dataset.copy);
  try {
    await navigator.clipboard.writeText(source.textContent);
    button.textContent = "Copiado";
  } catch (err) {
    button.textContent = "Não foi possível copiar";
  }
  setTimeout(() => { button.textContent = "Copiar YAML"; }, 2000);
}

function applyFilters(form) {
  const { path } = parseHash();
  const ns = dec(path.split("/")[2] || "");
  const fd = new FormData(form);
  const query = toQuery({
    q: fd.get("q").trim(), resource: fd.get("resource"), status: fd.get("status"), sort: fd.get("sort"),
  });
  location.hash = `#/ns/${enc(ns)}${query ? `?${query}` : ""}`;
}

let searchTimer = null;
app.addEventListener("click", (ev) => {
  const copy = ev.target.closest("[data-copy]");
  if (copy) return copyText(copy);
  const row = ev.target.closest("tr[data-row]");
  if (row && !ev.target.closest("a")) toggleRow(row);
});
app.addEventListener("keydown", (ev) => {
  if ((ev.key === "Enter" || ev.key === " ") && ev.target.matches("tr[data-row]")) {
    ev.preventDefault();
    toggleRow(ev.target);
  }
});
app.addEventListener("change", (ev) => {
  const form = ev.target.closest("#filters");
  if (form) applyFilters(form);
});
app.addEventListener("input", (ev) => {
  const form = ev.target.closest("#filters");
  if (form && ev.target.name === "q") {
    clearTimeout(searchTimer);
    searchTimer = setTimeout(() => applyFilters(form), 350);
  }
});
app.addEventListener("submit", (ev) => {
  if (ev.target.id === "filters") {
    ev.preventDefault();
    applyFilters(ev.target);
  }
});

window.addEventListener("hashchange", render);
render();
