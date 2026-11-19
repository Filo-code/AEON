/**
 * AEON by Rekt v2.2 — Complete Trading Intelligence Dashboard
 * Full multi-page UI, TP1/TP2 partial exit badges, Copy-Trading 1d/7d/30d,
 * Equity SVG + interactive tooltip, AntiRug, Survival & Cervello AI.
 */

const API_ENDPOINT = '/api/state';
const REFRESH_INTERVAL = 2500;

let FX = 1;
let lastState = null;
let equityRange = 'all';
let copyTimeframe = '1d';
let currentPage = 'overview';

// ── Formatters & Helpers ──────────────────────────────────────────────────
const esc = s => String(s ?? '').replace(/[&<>"]/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));
const fmt = (x, d = 2) => Number(x || 0).toLocaleString('en-US', { minimumFractionDigits: d, maximumFractionDigits: d });
const usd = (x, d = 2, sign = false) => (x < 0 ? '-' : (sign && x > 0 ? '+' : '')) + '€' + fmt(Math.abs(x) / FX, d);
const pc = (x, d = 1) => `${x > 0 ? '+' : ''}${fmt(x, d)}%`;
const tok = t => '$' + esc(String(t || '?').split(' /')[0]);
const dur = s => {
  s = Math.max(0, s | 0);
  const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60);
  return h ? `${h}h ${m}m` : `${m}m`;
};
const hhmmss = ts => ts ? new Date(ts * 1000).toLocaleTimeString('it-IT', { hour: '2-digit', minute: '2-digit', second: '2-digit' }) : '—';

const PALETTE = ['#3ddc84', '#2dd4bf', '#3b82f6', '#8b5cf6', '#f5a623', '#ec4899', '#f97316', '#14b8a6'];
const tokenColor = t => {
  let h = 0;
  for (const c of String(t || '?')) h = (h * 31 + c.charCodeAt(0)) >>> 0;
  return PALETTE[h % PALETTE.length];
};
const avatar = t => {
  const sym = esc(String(t || '?').replace('$', '')[0] || '?').toUpperCase();
  return `<span class="token-avatar" style="background:${tokenColor(t)}">${sym}</span>`;
};
const slipBadge = src => src === 'jupiter'
  ? '<span class="badge badge-success" title="Quote reale Jupiter">J</span>'
  : '<span class="badge badge-neutral" title="Slippage stimato">s</span>';

function tpBadges(obj) {
  if (!obj) return '';
  let out = '';
  if (obj.tp1_hit || obj.tp1_taken) out += '<span class="tp-pill">TP1</span>';
  if (obj.tp2_hit || obj.tp2_taken) out += '<span class="tp-pill">TP2</span>';
  return out;
}

// ── Particle Background ───────────────────────────────────────────────────
function initParticles() {
  const canvas = document.getElementById('particles');
  if (!canvas || window.matchMedia('(prefers-reduced-motion: reduce)').matches) return;
  const ctx = canvas.getContext('2d');
  let w, h;
  const resize = () => { w = canvas.width = window.innerWidth; h = canvas.height = window.innerHeight; };
  resize();
  window.addEventListener('resize', resize);

  const particles = Array.from({ length: 30 }, () => ({
    x: Math.random() * w, y: Math.random() * h,
    r: Math.random() * 1.4 + 0.4,
    dx: (Math.random() - 0.5) * 0.16, dy: (Math.random() - 0.5) * 0.16,
    op: Math.random() * 0.16 + 0.04
  }));

  function draw() {
    ctx.clearRect(0, 0, w, h);
    for (const p of particles) {
      p.x += p.dx; p.y += p.dy;
      if (p.x < 0 || p.x > w) p.dx *= -1;
      if (p.y < 0 || p.y > h) p.dy *= -1;
      ctx.beginPath();
      ctx.arc(p.x, p.y, p.r, 0, Math.PI * 2);
      ctx.fillStyle = `rgba(61, 220, 132, ${p.op})`;
      ctx.fill();
    }
    requestAnimationFrame(draw);
  }
  draw();
}

// ── Navigation & UI Interactions ──────────────────────────────────────────
const PAGE_META = {
  overview:     ['Panoramica', 'Equity · Posizioni · Agenti · Telemetria'],
  portfolio:    ['Portfolio', 'Allocazione asset, cassa ed esposizione'],
  positions:    ['Posizioni Aperte', 'Monitoraggio live TP1/TP2, SL e wallet tracking'],
  history:      ['Storico Operazioni', 'Log completo trade chiusi, motivi e slippage'],
  strategies:   ['Strategie & Cervello AI', 'Parametri dinamici, TP/SL e auto-calibrazione'],
  agents:       ['Agenti AI', 'Carico operativo e segnali in tempo reale'],
  scanner:      ['Scanner & Discovery', 'Flusso unificato Pump.fun e GMGN'],
  intelligence: ['Intelligence & AntiRug', 'Copy-Trading 1G/7G/30G, Birdeye, RugCheck e Jupiter'],
  risk:         ['Gestione Rischio', 'Sopravvivenza, Drawdown e Profilo di rischio'],
  health:       ['Stato Sistema', 'RPC, API, Watchdog, Costi e Latenze'],
  logs:         ['Eventi & Log', 'Feed cronologico operazioni e diagnosi'],
  settings:     ['Impostazioni', 'Configurazione attiva e parametri bot']
};

function switchPage(page) {
  currentPage = page;
  document.querySelectorAll('.page').forEach(p => p.classList.add('hidden'));
  const target = document.getElementById(`page-${page}`);
  if (target) target.classList.remove('hidden');

  document.querySelectorAll('[data-page]').forEach(el => {
    el.classList.toggle('active', el.dataset.page === page);
  });

  const [title, desc] = PAGE_META[page] || [page, ''];
  const tEl = document.getElementById('pageTitle');
  const dEl = document.getElementById('pageDescription');
  if (tEl) tEl.textContent = title;
  if (dEl) dEl.textContent = desc;

  if (lastState) renderSecondaryPage(page, lastState);
}

function initUI() {
  document.querySelectorAll('[data-page]').forEach(link => {
    link.addEventListener('click', e => {
      e.preventDefault();
      switchPage(link.dataset.page);
      document.getElementById('sidebar')?.classList.remove('open');
      document.getElementById('profileMenu')?.classList.add('hidden');
    });
  });

  document.getElementById('sidebarToggle')?.addEventListener('click', () => {
    document.getElementById('sidebar')?.classList.toggle('open');
  });

  const profBtn = document.getElementById('profileBtn');
  const profMenu = document.getElementById('profileMenu');
  profBtn?.addEventListener('click', e => {
    e.stopPropagation();
    profMenu?.classList.toggle('hidden');
  });
  document.addEventListener('click', () => profMenu?.classList.add('hidden'));

  // Equity range tabs
  document.querySelectorAll('#equityTabs .tab').forEach(tab => {
    tab.addEventListener('click', () => {
      document.querySelectorAll('#equityTabs .tab').forEach(t => t.classList.remove('active'));
      tab.classList.add('active');
      equityRange = tab.dataset.range || 'all';
      if (lastState) renderEquityChart(lastState);
    });
  });

  // Copy-Trading timeframe tabs (1d / 7d / 30d)
  document.querySelectorAll('#copyTfTabs .tab').forEach(tab => {
    tab.addEventListener('click', () => {
      document.querySelectorAll('#copyTfTabs .tab').forEach(t => t.classList.remove('active'));
      tab.classList.add('active');
      copyTimeframe = tab.dataset.tf || '1d';
      if (lastState) renderIntelligence(lastState);
    });
  });

  setInterval(() => {
    const el = document.getElementById('headerTime');
    if (el) el.textContent = new Date().toLocaleTimeString('it-IT');
  }, 1000);
}

// ── Header & Sidebar Status ───────────────────────────────────────────────
function renderHeaderStatus(v) {
  const rpcDot = document.querySelector('#solanaStatus .rpc-dot');
  const rpcVal = document.getElementById('rpcStatus');
  const aeonDot = document.getElementById('aeonStatusDot');
  const aeonVal = document.getElementById('aeonStatus');
  const lastUpd = document.getElementById('lastUpdated');
  const posCount = document.getElementById('navPosCount');
  const alertBanner = document.getElementById('globalAlertBanner');

  // ── Canonical RPC status with response freshness verification ──
  const rh = v.rpc_health || {};
  const isAlive = Boolean(v.live && !v.dead);
  if (rpcDot) {
    rpcDot.classList.remove('offline', 'warning');
    if (rh.state === 'degraded') rpcDot.classList.add('warning');
    else if (rh.state === 'healthy') { /* healthy green dot */ }
    else rpcDot.classList.add('offline');
  }
  if (rpcVal) {
    if (v.dead) {
      rpcVal.textContent = 'Capitale Esaurito';
    } else if (rh.state === 'healthy') {
      const latency = rh.latency_ms ? ' ' + rh.latency_ms + 'ms' : '';
      rpcVal.textContent = 'Connesso' + latency;
    } else if (rh.state === 'degraded') {
      rpcVal.textContent = 'Degradato (' + (rh.errors || 0) + ' err)';
    } else if (rh.state === 'down') {
      rpcVal.textContent = 'Disconnesso';
    } else {
      rpcVal.textContent = 'In attesa (UNKNOWN)';
    }
  }

  if (aeonDot) aeonDot.className = `status-dot ${isAlive ? 'live' : 'paper'}`;
  if (aeonVal) aeonVal.textContent = v.dead ? 'MORTO' : (v.paused ? 'In pausa' : (isAlive ? 'Live' : 'Fermo'));
  if (lastUpd) lastUpd.textContent = v.clock || hhmmss(v.now);
  if (posCount) posCount.textContent = (v.positions || []).length;

  if (alertBanner) {
    if (v.dead) {
      alertBanner.classList.remove('hidden');
      alertBanner.textContent = '💀 BOT FERMO PER CAPITALE ESAURITO — Nessun nuovo ordine verrà aperto.';
    } else if (v.paused) {
      alertBanner.classList.remove('hidden');
      alertBanner.textContent = '⏸ MODALITÀ PAUSA ATTIVA — Monitoraggio posizioni aperte in corso.';
    } else {
      alertBanner.classList.add('hidden');
    }
  }
}

// ── KPI Cards ─────────────────────────────────────────────────────────────
function renderKPIs(v) {
  // Use canonical backend metrics exclusively — no client-side fallbacks or arithmetic
  const set = (id, text) => { const el = document.getElementById(id); if (el) el.textContent = text; };
  set('kpiStartingCapital', v.start != null ? usd(v.start) : 'N/A');
  set('kpiCurrentEquity', v.equity != null ? usd(v.equity) : 'N/A');
  set('kpiTotalPnl', v.total_pnl != null ? usd(v.total_pnl, 2, true) : 'N/A');
  set('kpiRealizedPnl', v.realized_pnl != null ? usd(v.realized_pnl, 2, true) : 'N/A');
  set('kpiUnrealizedPnl', v.unrealized_pnl != null ? usd(v.unrealized_pnl, 2, true) : 'N/A');
  set('kpiLiquidity', v.cash != null ? usd(v.cash) : 'N/A');

  const pctEl = document.getElementById('kpiTotalPnlPct');
  const pnlIcon = document.getElementById('kpiPnlIcon');
  const pnlBar = document.getElementById('kpiPnlBar');
  const isPos = v.total_pnl != null ? v.total_pnl >= 0 : false;

  if (pctEl) {
    pctEl.textContent = v.return_pct != null ? pc(v.return_pct, 2) : 'N/A';
    pctEl.className = `kpi-change ${isPos ? 'positive' : 'negative'}`;
  }
  if (pnlBar) pnlBar.style.background = isPos ? 'linear-gradient(90deg,#3ddc84,#2dd4bf)' : 'linear-gradient(90deg,#ff4d5e,#f5a623)';
  if (pnlIcon) {
    pnlIcon.className = `kpi-icon ${isPos ? 'positive' : 'warning'}`;
    pnlIcon.style.color = isPos ? 'var(--positive)' : 'var(--negative)';
  }
}

// ── Interactive Equity Chart (SVG) ────────────────────────────────────────
function renderEquityChart(v) {
  const svg = document.getElementById('equityChart');
  const tooltip = document.getElementById('equityTooltip');
  if (!svg) return;

  const W = svg.clientWidth || 760;
  const H = 245;
  svg.setAttribute('viewBox', `0 0 ${W} ${H}`);

  const rawPts = (v.equity_curve || []).slice();
  const now = v.now || (Date.now() / 1000);
  const cutoffMap = { '1d': 86400, '7d': 86400 * 7, '30d': 86400 * 30, 'all': Infinity };
  const span = cutoffMap[equityRange] || Infinity;
  const pts = span !== Infinity ? rawPts.filter(p => (now - p[0]) <= span) : rawPts;

  // Never fabricate equity history points. If insufficient, show explicit empty state.
  if (!pts || pts.length < 2) {
    svg.innerHTML = `
      <rect width="100%" height="100%" fill="none"/>
      <text x="50%" y="46%" text-anchor="middle" fill="var(--text-tertiary)" font-family="var(--font-mono)" font-size="12.5">
        In attesa di dati storici di equity sufficienti...
      </text>
      <text x="50%" y="58%" text-anchor="middle" fill="var(--text-muted)" font-family="var(--font-mono)" font-size="11">
        ${pts.length === 1 ? '1 punto registrato — registrazione curva in corso' : 'Nessun punto registrato'}
      </text>
    `;
    const curEl = document.getElementById('equityCurrent');
    const chgEl = document.getElementById('equityChange');
    const updEl = document.getElementById('equityUpdated');
    if (curEl) curEl.textContent = v.equity != null ? usd(v.equity) : 'N/A';
    if (chgEl) chgEl.textContent = v.return_pct != null ? `${usd(v.total_pnl, 2, true)} (${pc(v.return_pct, 2)})` : 'N/A';
    if (updEl) updEl.textContent = `Agg: ${v.clock || hhmmss(v.now)}`;
    return;
  }

  const t0 = pts[0][0];
  const t1 = Math.max(now, t0 + 10);
  const realizedPts = pts.filter(p => p.length > 2 && p[2] != null);

  const allY = pts.map(p => p[1]).concat([v.start], realizedPts.map(p => p[2]));
  let lo = Math.min(...allY);
  let hi = Math.max(...allY);
  const margin = (hi - lo || Math.max((v.start || 100) * 0.02, 1)) * 0.14;
  lo -= margin;
  hi += margin;

  const X = t => padL + ((t - t0) / (t1 - t0)) * (W - padL - padR);
  const Y = y => H - padB - ((y - lo) / (hi - lo)) * (H - padT - padB);

  const pathMain = pts.map((p, i) => `${i === 0 ? 'M' : 'L'}${X(p[0]).toFixed(1)} ${Y(p[1]).toFixed(1)}`).join(' ');
  const pathReal = realizedPts.length > 1
    ? realizedPts.map((p, i) => `${i === 0 ? 'M' : 'L'}${X(p[0]).toFixed(1)} ${Y(p[2]).toFixed(1)}`).join(' ')
    : '';

  const last = pts[pts.length - 1];
  const isUp = v.equity >= v.start;
  const col = isUp ? '#3ddc84' : '#ff4d5e';
  const baseY = Y(v.start).toFixed(1);

  svg.innerHTML = `
    <defs>
      <linearGradient id="aeonEqGrad" x1="0" y1="0" x2="0" y2="1">
        <stop offset="0%" stop-color="${col}" stop-opacity="0.30"/>
        <stop offset="100%" stop-color="${col}" stop-opacity="0.0"/>
      </linearGradient>
    </defs>
    <line x1="${padL}" x2="${W - padR}" y1="${baseY}" y2="${baseY}" stroke="rgba(148,163,184,0.22)" stroke-dasharray="4 4" stroke-width="1"/>
    <path d="${pathMain} L${X(last[0]).toFixed(1)} ${H - padB} L${X(pts[0][0]).toFixed(1)} ${H - padB} Z" fill="url(#aeonEqGrad)"/>
    ${pathReal ? `<path d="${pathReal}" fill="none" stroke="rgba(226,232,240,0.45)" stroke-width="1.3" stroke-dasharray="3 3"/>` : ''}
    <path d="${pathMain}" fill="none" stroke="${col}" stroke-width="2.4" stroke-linejoin="round"/>
    <line id="eqCrosshair" x1="0" x2="0" y1="${padT}" y2="${H - padB}" stroke="rgba(255,255,255,0.20)" stroke-width="1" visibility="hidden"/>
    <circle cx="${X(last[0]).toFixed(1)}" cy="${Y(last[1]).toFixed(1)}" r="4.5" fill="#050608" stroke="${col}" stroke-width="2.5"/>
  `;

  svg.onmousemove = e => {
    if (!tooltip) return;
    const rect = svg.getBoundingClientRect();
    const relX = Math.max(padL, Math.min(W - padR, ((e.clientX - rect.left) / rect.width) * W));
    const targetT = t0 + ((relX - padL) / (W - padL - padR)) * (t1 - t0);
    let nearest = pts[0], bestDiff = Infinity;
    for (const p of pts) {
      const d = Math.abs(p[0] - targetT);
      if (d < bestDiff) { bestDiff = d; nearest = p; }
    }
    const cx = X(nearest[0]), cy = Y(nearest[1]);
    const cross = document.getElementById('eqCrosshair');
    if (cross) { cross.setAttribute('x1', cx); cross.setAttribute('x2', cx); cross.setAttribute('visibility', 'visible'); }
    tooltip.style.left = `${(cx / W) * 100}%`;
    tooltip.style.top = `${Math.max(22, (cy / H) * 100)}%`;
    tooltip.innerHTML = `<b>${usd(nearest[1])}</b><br><span style="color:var(--text-tertiary);font-size:11px">${hhmmss(nearest[0])}</span>`;
    tooltip.classList.add('visible');
  };
  svg.onmouseleave = () => {
    tooltip?.classList.remove('visible');
    document.getElementById('eqCrosshair')?.setAttribute('visibility', 'hidden');
  };

  const pnlPct = v.return_pct != null ? pc(v.return_pct, 2) : "N/A";
  const curEl = document.getElementById('equityCurrent');
  const chgEl = document.getElementById('equityChange');
  const updEl = document.getElementById('equityUpdated');
  if (curEl) curEl.textContent = usd(v.equity);
  if (chgEl) {
    chgEl.textContent = v.total_pnl != null ? `${usd(v.total_pnl, 2, true)} (${pnlPct})` : "N/A";
    chgEl.className = `chart-change ${isUp ? 'positive' : 'negative'}`;
  }
  if (updEl) updEl.textContent = `Agg: ${v.clock || hhmmss(v.now)}`;
}

// ── Portfolio Allocation Donut ────────────────────────────────────────────
function renderAllocation(v) {
  const svg = document.getElementById('allocationChart');
  const legend = document.getElementById('allocationLegend');
  const totalEl = document.getElementById('allocationTotal');
  if (!svg || !legend) return;

  const cash = Math.max(0, v.cash || 0);
  const positions = v.positions || [];
  const slices = [{ name: 'Liquidità (Cash)', value: cash, color: '#3ddc84' }];

  positions.forEach((p, idx) => {
    const val = Math.max(0, (p.cost_usd || 0) + (p.upnl || 0));
    if (val > 0) {
      slices.push({
        name: `$${String(p.name || '?').split(' /')[0]}`,
        value: val,
        color: PALETTE[(idx + 1) % PALETTE.length]
      });
    }
  });

  const total = slices.reduce((acc, s) => acc + s.value, 0) || 1;
  if (totalEl) totalEl.textContent = usd(v.equity);

  const R = 74, C = 2 * Math.PI * R;
  let offset = 0;
  const circles = slices.map(s => {
    const len = (s.value / total) * C;
    const el = `<circle cx="92" cy="92" r="${R}" fill="none" stroke="${s.color}" stroke-width="16"
      stroke-dasharray="${Math.max(0, len - 2).toFixed(2)} ${C.toFixed(2)}"
      stroke-dashoffset="${(-offset).toFixed(2)}" transform="rotate(-90 92 92)"/>`;
    offset += len;
    return el;
  }).join('');

  svg.setAttribute('viewBox', '0 0 184 184');
  svg.innerHTML = `<circle cx="92" cy="92" r="${R}" fill="none" stroke="rgba(255,255,255,0.05)" stroke-width="16"/>${circles}`;

  legend.innerHTML = slices.slice(0, 5).map(s => {
    const pct = (s.value / total) * 100;
    return `<li>
      <span class="allocation-color" style="background:${s.color}"></span>
      <span class="allocation-name" style="flex:1">${esc(s.name)}</span>
      <span class="allocation-value">${usd(s.value)}</span>
      <span class="allocation-pct">${fmt(pct, 1)}%</span>
    </li>`;
  }).join('');
}

// ── Performance Stats ─────────────────────────────────────────────────────
function renderPerformance(v) {
  const set = (id, text) => { const el = document.getElementById(id); if (el) el.textContent = text; };
  const completedCount = v.completed_trades_count != null ? v.completed_trades_count : (v.trades || 0);
  const wrNum = v.completed_win_rate != null ? v.completed_win_rate : v.win_rate;
  const wr = wrNum != null ? fmt(wrNum, 1) + '%' : '—';
  set('winRateBadge', 'WR ' + wr);
  set('perfTotalTrades', fmt(completedCount, 0));
  set('perfWinRate', wr);
  set('perfOpenPos', fmt((v.positions || []).length, 0));

  let peak = v.start || 0, maxDD = 0;
  for (const p of (v.equity_curve || [])) {
    if (p[1] > peak) peak = p[1];
    if (peak > 0) {
      const dd = ((peak - p[1]) / peak) * 100;
      if (dd > maxDD) maxDD = dd;
    }
  }
  set('perfMaxDD', maxDD > 0 ? `-${fmt(maxDD, 1)}%` : '0.0%');
  set('perfBestTrade', v.best ? `${usd(v.best.pnl_usd, 2, true)} (${tok(v.best.token)})` : '€0.00');
  set('perfWorstTrade', v.worst ? `${usd(v.worst.pnl_usd, 2, true)} (${tok(v.worst.token)})` : '€0.00');
}

// ── Trading Activity & Open Positions Table (con TP1/TP2) ─────────────────
function renderTradingTable(v) {
  const tbody = document.getElementById('tradingTableBody');
  if (!tbody) return;

  const openRows = (v.positions || []).map(p => {
    const upnl = p.upnl || 0;
    const stratLabel = p.entry_type === 'volume_spike' ? 'Volume Spike' : `Copy (${p.timeframe || copyTimeframe})`;
    return `<tr>
      <td class="text-muted">${esc(p.entry_clock || '—')}</td>
      <td><div class="token-cell">${avatar(p.name)}<span class="token-symbol">${tok(p.name)}</span><span class="badge badge-primary">OPEN</span></div></td>
      <td><span class="type-badge buy">BUY</span></td>
      <td class="num">${usd(p.cost_usd)}</td>
      <td class="num">${fmt(p.held_min, 0)}m</td>
      <td class="num ${upnl >= 0 ? 'text-positive' : 'text-negative'}">${usd(upnl, 2, true)} (${pc(p.chg_pct, 1)})</td>
      <td><span class="strategy-badge">${esc(stratLabel)}</span>${tpBadges(p)}</td>
      <td>${slipBadge(p.costs?.buy_slip_src)}</td>
    </tr>`;
  });

  const closedRows = (v.closed || []).slice(0, 12).map(t => {
    const pnl = t.pnl_usd || 0;
    return `<tr>
      <td class="text-muted">${esc(t.exit_clock || '—')}</td>
      <td><div class="token-cell">${avatar(t.token)}<span class="token-symbol">${tok(t.token)}</span></div></td>
      <td><span class="type-badge sell">SELL</span></td>
      <td class="num">${usd(t.cost_usd)}</td>
      <td class="num">${fmt((t.hold_sec || 0) / 60, 0)}m</td>
      <td class="num ${pnl >= 0 ? 'text-positive' : 'text-negative'}">${usd(pnl, 2, true)} (${pc(t.pnl_pct, 1)})</td>
      <td><span class="strategy-badge">${esc(t.reason_it || t.entry_type || '—')}</span>${tpBadges(t)}</td>
      <td>${slipBadge(t.sell_slip_src)}</td>
    </tr>`;
  });

  const allRows = [...openRows, ...closedRows];
  tbody.innerHTML = allRows.length
    ? allRows.join('')
    : '<tr><td colspan="8" class="text-muted center" style="padding:26px">In attesa di segnali di mercato…</td></tr>';
}

// ── 8 Grok / AEON Agents ──────────────────────────────────────────────────
function renderAgents(v) {
  const grid = document.getElementById('agentsGrid');
  const badge = document.getElementById('agentsRunning');
  if (!grid) return;

  const agents = v.agents || [];
  if (badge) badge.textContent = `${v.running || agents.filter(a => !a.idle).length} active · ${fmt(v.ops_per_min || 0, 0)} ops/m`;

  if (!agents.length) {
    grid.innerHTML = '<div class="text-muted" style="padding:12px">Nessun agente attivo</div>';
    return;
  }

  grid.innerHTML = agents.map(a => `
    <div class="agent-card ${a.active ? 'active' : ''}" style="--card-color:${esc(a.color || '#3ddc84')}">
      <div class="agent-header">
        <span class="agent-icon" style="background:${esc(a.color)}22;color:${esc(a.color)}">●</span>
        <span class="agent-label">${esc(a.label)}</span>
        <span class="agent-sub">${esc(a.sub || '')}</span>
        <span class="agent-pct" style="color:${esc(a.color)}">${a.pct ?? 0}%</span>
      </div>
      <div class="agent-last" title="${esc(a.last)}">${esc(a.last || 'idle')}</div>
      <div class="agent-bar">
        <div class="agent-bar-fill" style="width:${Math.min(100, Math.max(0, a.pct || 0))}%;background:${esc(a.color)}"></div>
      </div>
    </div>
  `).join('');
}

// ── Intelligence, Copy-Trading 1d/7d/30d & Cervello AI ────────────────────
function renderIntelligence(v) {
  const pGrid = document.getElementById('providersGrid');
  const smPanel = document.getElementById('smartmoneyPanel');
  if (!pGrid || !smPanel) return;

  const sources = v.sources || [];
  pGrid.innerHTML = sources.map(s => {
    const online = s.connected && s.enabled;
    const statusCls = !s.enabled ? 'disabled' : (online ? 'online' : (s.state === 'error' ? 'error' : 'offline'));
    return `<div class="provider-card">
      <div class="provider-icon ${statusCls}">${esc((s.label || 'S')[0])}</div>
      <div class="provider-info">
        <div class="provider-name">${esc(s.label)}</div>
        <div class="provider-status">${online ? 'LIVE stream' : esc(s.state || 'offline')}</div>
      </div>
      <div class="provider-received">${fmt(s.received || 0, 0)} ev</div>
    </div>`;
  }).join('');

  const ar = v.antirug || {};
  const st = ar.stats || {};
  const brn = v.brain || {};
  const sv = v.survival || {};

  // Estrazione ranking wallet copy-trading multi-timeframe (1d/7d/30d) se esposto dal backend
  const topWalletsObj = v.top_traders || v.smart_money || {};
  const tfList = Array.isArray(topWalletsObj)
    ? topWalletsObj
    : (topWalletsObj[copyTimeframe] || topWalletsObj['1d'] || []);

  const walletsHtml = tfList.length
    ? `<div style="display:flex;flex-direction:column;gap:6px;margin-top:6px">
        ${tfList.slice(0, 4).map((w, i) => `
          <div class="kv-row" style="font-family:var(--font-mono);font-size:11.5px;padding:4px 8px;background:rgba(255,255,255,0.02);border-radius:6px">
            <span>#${i + 1} <b>${esc((w.wallet || w.address || '?').slice(0, 6))}…${esc((w.wallet || w.address || '').slice(-4))}</b></span>
            <span class="text-muted">WR ${fmt(w.win_rate || 0, 0)}% · ${w.trades || 0}t</span>
            <span class="${(w.pnl_usd || 0) >= 0 ? 'text-positive' : 'text-negative'}">${usd(w.pnl_usd || 0, 0, true)}</span>
          </div>`).join('')}
      </div>`
    : `<div class="text-muted mono" style="font-size:11.5px;margin-top:4px">
        Ranking wallet ${copyTimeframe.toUpperCase()} sincronizzato con Birdeye/Vybe (${(v.by_type?.copy_top_traders || {}).n || 0} copy eseguiti)
      </div>`;

  smPanel.innerHTML = `
    <div class="sm-summary">
      <div class="sm-summary-item">
        <span class="sm-summary-label">AntiRug (${esc(ar.mode_label || (ar.mode || 'off').toUpperCase())})</span>
        <span class="sm-summary-value">${st.evaluated || 0} scan · ${(ar.mode === 'active') ? (st.block || 0) + ' bloccati (ACTIVE)' : ((ar.mode === 'shadow') ? (st.block || 0) + ' osservati (SHADOW)' : 'Disattivato (OFF)')}</span>
      </div>
      <div class="sm-summary-item">
        <span class="sm-summary-label">Next Size</span>
        <span class="sm-summary-value">${usd(sv.next_size_usd || 0)} (${fmt(sv.size_pct_eff || 0, 1)}%)</span>
      </div>
      <div class="sm-summary-item">
        <span class="sm-summary-label">Cervello AI</span>
        <span class="sm-summary-value">${brn.mode === 'validated' ? 'ValidatedBrain' : 'Legacy'} · tra ${brn.next_in ?? '—'} trade</span>
      </div>
    </div>
    <div>
      <div class="sm-rank-title">Top Wallet Copy-Trading (${copyTimeframe.toUpperCase()})</div>
      ${walletsHtml}
    </div>
    <div>
      <div class="sm-rank-title">Ultima diagnosi Cervello</div>
      <div style="font-family:var(--font-mono);font-size:11.5px;color:var(--text-secondary);line-height:1.5;background:rgba(0,0,0,0.28);padding:10px 12px;border-radius:8px;border:1px solid var(--border)">
        ${esc(brn.diagnosis || 'Raccolta telemetria in corso: la prima auto-calibrazione parte dopo 10 trade chiusi.')}
      </div>
    </div>
  `;
}

// ── System Health & Costs ─────────────────────────────────────────────────
function renderHealth(v) {
  const grid = document.getElementById('healthGrid');
  if (!grid) return;

  const sv = v.survival || {};
  const jup = v.jupiter || {};
  const totalFees = v.fees_total != null ? usd(v.fees_total) : "N/A";

  const items = [
    {
      label: 'Sopravvivenza',
      val: `${usd(v.equity - (sv.death_line_usd || 0))} sopra soglia`,
      cls: v.dead ? 'danger' : 'ok', icon: '♥'
    },
    {
      label: 'Quote Jupiter',
      val: jup.enabled ? `OK ${jup.ok || 0} · Err ${jup.fail || 0}` : 'Disattivato',
      cls: (jup.fail || 0) > (jup.ok || 0) ? 'warning' : 'ok', icon: '⚡'
    },
    {
      label: 'Fee + Slippage',
      val: `${totalFees} + ${v.slip_cost != null ? usd(v.slip_cost) : "N/A"}`,
      cls: 'ok', icon: '◈'
    },
    {
      label: 'Uptime & Watchdog',
      val: `${dur(v.uptime_sec)} · restart ${v.meta?.restarts || 0}`,
      cls: (v.stall_total_min || 0) > 5 ? 'warning' : 'ok', icon: '⏱'
    }
  ];

  grid.innerHTML = items.map(it => `
    <div class="health-item">
      <div class="health-icon ${it.cls}">${it.icon}</div>
      <div class="health-info">
        <div class="health-label">${esc(it.label)}</div>
        <div class="health-value">${esc(it.val)}</div>
      </div>
    </div>
  `).join('');
}

// ── Complete Secondary Pages Renderer (Tutte le 11 sottopagine) ───────────
function renderSecondaryPage(page, v) {
  if (page === 'portfolio') {
    const el = document.getElementById('portfolio-content');
    if (!el) return;
    const bt = (v.strategies || []).length > 0
      ? v.strategies.map(s => {
          const wr = s.win_rate != null ? fmt(s.win_rate, 1) + '%' : '—';
          const statusPill = s.enabled
            ? '<span class="badge badge-success" style="font-size:9px">ON</span>'
            : '<span class="badge badge-neutral" style="font-size:9px">OFF</span>';
          const openTag = s.open_positions ? ` <span class="text-muted" style="font-size:11px">${s.open_positions} aperte</span>` : '';
          return `<div class="kv-row">
            <span>${esc(s.name)} ${statusPill}${openTag}</span>
            <span class="mono">${s.trades}T · WR ${wr} · <b class="${(s.realized_pnl || 0) >= 0 ? 'text-positive' : 'text-negative'}">${usd(s.realized_pnl || 0, 2, true)}</b></span>
          </div>`;
        }).join('')
      : Object.entries(v.by_type || {}).map(([k, e]) =>
          `<div class="kv-row"><span>${k === 'volume_spike' ? 'Volume Spike' : k.replace(/_/g,' ')}</span><span class="mono">${e.n}T · <b class="${e.pnl >= 0 ? 'text-positive' : 'text-negative'}">${usd(e.pnl, 2, true)}</b></span></div>`
        ).join('') || '<div class="text-muted">Nessun dato per strategia</div>';

    const br = Object.entries(v.by_reason || {}).map(([k, e]) =>
      `<div class="kv-row"><span>${esc(k)}</span><span class="mono">${e.n} · <b class="${e.pnl >= 0 ? 'text-positive' : 'text-negative'}">${usd(e.pnl, 2, true)}</b></span></div>`
    ).join('') || '<div class="text-muted">Nessun dato di uscita</div>';

    el.innerHTML = `
      <div class="subpage-grid">
        <div class="data-panel" style="padding:18px">
          <h3 style="margin:0 0 12px">Riepilogo Capitale</h3>
          <div class="kv-row"><span>Capitale iniziale</span><b class="mono">${usd(v.start)}</b></div>
          <div class="kv-row"><span>Cassa disponibile</span><b class="mono">${usd(v.cash)}</b></div>
          <div class="kv-row"><span>Costo base posizioni</span><b class="mono">${usd(v.invested_capital || 0)}</b></div>
          <div class="kv-row"><span>Valore di mercato aperte</span><b class="mono">${usd(v.open_value || 0)}</b></div>
          <div class="kv-row"><span>PnL non realizzato</span><b class="mono ${(v.unrealized_pnl || 0) >= 0 ? 'text-positive' : 'text-negative'}">${usd(v.unrealized_pnl || 0, 2, true)}</b></div>
          <div class="kv-row"><span>Equity totale</span><b class="mono text-positive">${usd(v.equity)}</b></div>
          <div class="kv-row"><span>Moltiplicatore</span><b class="mono">${fmt(v.mult || 1, 2)}x</b></div>
        </div>
        <div class="data-panel" style="padding:18px">
          <h3 style="margin:0 0 12px">Performance per Strategia</h3>
          ${bt}
          <h4 style="margin:16px 0 8px">Breakdown Motivi Uscita</h4>
          ${br}
        </div>
      </div>`;
  }

  if (page === 'positions') {
    const el = document.getElementById('positions-content');
    if (!el) return;
    const rows = (v.positions || []).map(p => `<tr>
      <td>${avatar(p.name)} <b>${tok(p.name)}</b></td>
      <td>${p.entry_type === 'volume_spike' ? 'Volume Spike' : 'Copy Trader'}${tpBadges(p)} ${p.track?.n ? `<span class="text-muted">(${p.track.sold || 0}/${p.track.n} wallet usciti)</span>` : ''}</td>
      <td>${esc(p.entry_clock)}</td>
      <td class="num">${usd(p.cost_usd)}</td>
      <td class="num ${p.chg_pct >= 0 ? 'text-positive' : 'text-negative'}">${pc(p.chg_pct, 2)}</td>
      <td class="num ${p.upnl >= 0 ? 'text-positive' : 'text-negative'}">${usd(p.upnl, 2, true)}</td>
      <td class="num">${fmt(p.held_min, 0)} min</td>
      <td>${slipBadge(p.costs?.buy_slip_src)}</td>
    </tr>`).join('') || '<tr><td colspan="8" class="center text-muted" style="padding:28px">Nessuna posizione aperta al momento</td></tr>';

    el.innerHTML = `<div class="data-panel"><div class="panel-header"><h3>Posizioni Aperte (${(v.positions || []).length})</h3></div>
      <div class="table-container" style="max-height:none"><table class="table">
      <thead><tr><th>Token</th><th>Strategia & TP</th><th>Entrata</th><th class="num">Size</th><th class="num">Var %</th><th class="num">uPnL</th><th class="num">Durata</th><th>Slip</th></tr></thead>
      <tbody>${rows}</tbody></table></div></div>`;
  }

  if (page === 'history') {
    const el = document.getElementById('history-content');
    if (!el) return;
    const rows = (v.closed || []).map(t => {
      const isPartial = t.is_partial;
      const dispPnl = isPartial ? (t.pnl_usd || 0) : (t.cumulative_pnl_display != null ? t.cumulative_pnl_display : (t.pnl_usd || 0));
      const dispPct = isPartial ? (t.pnl_pct || 0) : (t.cumulative_pnl_pct != null ? t.cumulative_pnl_pct : (t.pnl_pct || 0));
      const pnlCls = dispPnl >= 0 ? 'text-positive' : 'text-negative';
      const rowStyle = isPartial ? ' style="opacity:0.72"' : '';
      const typePill = isPartial
        ? '<span class="badge badge-warning" style="font-size:10px;padding:1px 5px">PARZIALE</span> '
        : '';
      return `<tr${rowStyle}>
        <td>${esc(t.exit_clock)}</td>
        <td>${typePill}<b>${tok(t.token)}</b></td>
        <td>${esc(t.reason_it || '—')}${tpBadges(t)}</td>
        <td class="num">${usd(t.cost_usd || 0)}</td>
        <td class="num ${pnlCls}">${usd(dispPnl, 2, true)}</td>
        <td class="num ${pnlCls}">${pc(dispPct, 1)}</td>
        <td class="num">${fmt(t.buy_slip_pct || 0, 1)}% / ${fmt(t.sell_slip_pct || 0, 1)}%</td>
        <td class="num">${fmt((t.hold_sec || 0) / 60, 0)}m</td>
      </tr>`;
    }).join('') || '<tr><td colspan="8" class="center text-muted" style="padding:28px">Nessun trade chiuso</td></tr>';

    el.innerHTML = `<div class="data-panel"><div class="panel-header"><h3>Storico Completo Trade (${v.trades || 0})</h3></div>
      <div class="table-container" style="max-height:none"><table class="table">
      <thead><tr><th>Uscita</th><th>Token</th><th>Motivo Uscita</th><th class="num">Size</th><th class="num">PnL</th><th class="num">%</th><th class="num">Slip B/S</th><th class="num">Durata</th></tr></thead>
      <tbody>${rows}</tbody></table></div></div>`;
  }

  if (page === 'strategies') {
    const el = document.getElementById('strategies-content');
    if (!el) return;
    const r = v.exit_rules || {}, vs = r.volume_spike || {}, cp = r.copy_top_traders || {};
    const changes = ((v.brain_info?.changes || v.brain?.changes || [])).slice().reverse().map(c => {
      const cls = c.classification || (c.rejected ? 'REJECTED' : 'UNKNOWN');
      const badgeCls = cls === 'ACCEPTED' ? 'badge-success' : (cls === 'REJECTED' ? 'badge-danger' : 'badge-neutral');
      return `<div class="kv-row mono" style="font-size:12px">
        <span class="text-muted">${hhmmss(c.ts)}</span>
        <span>${esc(c.text || c.param)}</span>
        <span class="badge ${badgeCls}" style="font-size:9.5px;padding:1px 5px">${cls}</span>
      </div>`;
    }).join('') || '<div class="text-muted">Nessuna modifica automatica registrata</div>';

    el.innerHTML = `
      <div class="subpage-grid">
        <div class="data-panel" style="padding:18px">
          <h3 style="margin:0 0 10px">Regole Volume Spike</h3>
          <div class="kv-row"><span>Stop Loss</span><b class="mono text-negative">-${fmt((vs.stop_loss_pct || 0) * 100, 0)}%</b></div>
          <div class="kv-row"><span>Take Profit</span><b class="mono text-positive">+${fmt((vs.take_profit_pct || 0) * 100, 0)}%</b></div>
          <div class="kv-row"><span>Trailing Stop</span><b class="mono">${vs.trailing_stop_pct ? fmt(vs.trailing_stop_pct * 100, 0) + '% (att. +' + fmt((vs.trailing_activate_pct || 0) * 100, 0) + '%)' : 'Off'}</b></div>
          <div class="kv-row"><span>Durata Max</span><b class="mono">${fmt(vs.max_hold_minutes || 0, 0)} min</b></div>
        </div>
        <div class="data-panel" style="padding:18px">
          <h3 style="margin:0 0 10px">Regole Copy Top Traders</h3>
          <div class="kv-row"><span>Stop Loss</span><b class="mono text-negative">-${fmt((cp.stop_loss_pct || 0) * 100, 0)}%</b></div>
          <div class="kv-row"><span>Take Profit</span><b class="mono text-positive">+${fmt((cp.take_profit_pct || 0) * 100, 0)}%</b></div>
          <div class="kv-row"><span>Follow Wallet Exit</span><b class="mono">${cp.follow_wallets ? '>' + fmt((cp.sold_majority_pct || 0) * 100, 0) + '% usciti' : 'Off'}</b></div>
          <div class="kv-row"><span>Durata Max</span><b class="mono">${fmt(cp.max_hold_minutes || 0, 0)} min</b></div>
        </div>
      </div>
      <div class="data-panel" style="padding:18px">
        <h3 style="margin:0 0 10px">🧠 Diagnosi & Auto-Calibrazione Cervello AI</h3>
        ${(() => {
          const bi = v.brain_info || {};
          return `<div style="display:grid;grid-template-columns:1fr 1fr;gap:4px 12px;margin-bottom:12px">
            <div class="kv-row"><span>Modalità</span><b class="mono">${esc(bi.mode || '—')}</b></div>
            <div class="kv-row"><span>Fase</span><b class="mono">${esc(bi.phase || '—')}</b></div>
            <div class="kv-row"><span>Record analizzati</span><b class="mono">${bi.records || 0}</b></div>
            <div class="kv-row"><span>Prossima analisi</span><b class="mono">tra ${bi.next_in ?? '—'} trade</b></div>
            <div class="kv-row"><span>Modifiche accettate (ACCEPTED)</span><b class="mono text-positive">${bi.accepted_count || 0}</b></div>
            <div class="kv-row"><span>Modifiche rifiutate (REJECTED)</span><b class="mono text-negative">${bi.rejected_count || 0}</b></div>
            <div class="kv-row"><span>Esito sconosciuto (UNKNOWN)</span><b class="mono text-muted">${bi.unknown_count || 0}</b></div>
            ${bi.pending_count ? `<div class="kv-row"><span>In verifica (PENDING)</span><b class="mono text-warning">${bi.pending_count}</b></div>` : ''}
          </div>`;
        })()}
        <pre style="white-space:pre-wrap;font-family:var(--font-mono);font-size:12px;color:var(--text-primary);background:rgba(0,0,0,0.32);padding:14px;border-radius:10px;margin:0 0 16px">${esc(v.brain_info?.diagnosis || v.brain?.diagnosis || 'In attesa di 10 trade chiusi per la prima diagnosi...')}</pre>
        <h4 style="margin:0 0 8px">Storico Modifiche Parametri</h4>
        ${changes}
      </div>`;
  }

  if (page === 'agents') {
    const el = document.getElementById('agents-content');
    if (!el) return;
    el.innerHTML = `<div class="data-panel"><div class="panel-header"><h3>Tutti gli Agenti (${(v.agents || []).length})</h3><span class="badge badge-success">${fmt(v.ops_per_min || 0, 0)} ops/min · ${fmt(v.signals || 0, 0)} segnali</span></div>
      <div class="agents-grid" style="grid-template-columns:repeat(auto-fit,minmax(260px,1fr))">
        ${(v.agents || []).map(a => `
          <div class="agent-card ${a.active ? 'active' : ''}" style="--card-color:${esc(a.color || '#3ddc84')}">
            <div class="agent-header">
              <span class="agent-icon" style="background:${esc(a.color)}22;color:${esc(a.color)}">●</span>
              <span class="agent-label">${esc(a.label)}</span>
              <span class="agent-sub">${esc(a.sub || '')} · q${a.n || 0}</span>
              <span class="agent-pct" style="color:${esc(a.color)}">${a.pct ?? 0}%</span>
            </div>
            <div class="agent-last">${esc(a.last || 'idle')}</div>
            <div class="agent-bar"><div class="agent-bar-fill" style="width:${a.pct || 0}%;background:${esc(a.color)}"></div></div>
          </div>`).join('')}
      </div></div>`;
  }

  if (page === 'scanner') {
    const el = document.getElementById('scanner-content');
    if (!el) return;
    const events = [];
    (v.gmgn?.recent_events || []).forEach(e => events.push({ ...e, source: 'GMGN' }));
    (v.pumpfun?.recent_events || []).forEach(e => events.push({ ...e, source: 'PUMPFUN' }));
    events.sort((a, b) => (b.received_at || 0) - (a.received_at || 0));

    const rows = events.slice(0, 35).map(e => `<tr>
      <td class="text-muted">${hhmmss(e.received_at)}</td>
      <td><span class="badge badge-neutral">${esc(e.source)}</span></td>
      <td><b>$${esc(e.token_symbol || e.token_name || 'TOKEN')}</b> <span class="text-muted">${esc((e.token_mint || '').slice(0, 8))}…</span></td>
      <td>${esc(e.event_type || '—')}</td>
      <td class="num">${e.liquidity_usd ? '$' + fmt(e.liquidity_usd, 0) : '—'}</td>
      <td class="num">${e.volume_usd ? '$' + fmt(e.volume_usd, 0) : '—'}</td>
    </tr>`).join('') || '<tr><td colspan="6" class="center text-muted" style="padding:28px">In attesa di nuovi token dagli stream…</td></tr>';

    el.innerHTML = `<div class="data-panel"><div class="panel-header"><h3>Live Token Discovery (Pump.fun + GMGN)</h3><span class="badge badge-primary">${events.length} recenti</span></div>
      <div class="table-container" style="max-height:none"><table class="table">
      <thead><tr><th>Ora</th><th>Fonte</th><th>Token / Mint</th><th>Evento</th><th class="num">Liquidità</th><th class="num">Volume</th></tr></thead>
      <tbody>${rows}</tbody></table></div></div>`;
  }

  if (page === 'intelligence') {
    const el = document.getElementById('intelligence-content');
    if (!el) return;
    const ar = v.antirug || {};
    const st = ar.stats || {};
    const recent = (ar.recent || []).slice().reverse().map(r => `
      <div class="data-panel" style="padding:14px;margin-bottom:10px">
        <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:6px">
          <b>$${esc(r.name || '?')} <span class="text-muted">(${esc((r.time || '').slice(11, 16))})</span></b>
          <span class="badge ${(r.effective_cls || (r.decision==='ALLOW'?'success':r.decision==='BLOCK'?'danger':'warning')) === 'success' ? 'badge-success' : ((r.effective_cls||'danger') === 'danger' ? 'badge-danger' : 'badge-warning')}">${esc(r.effective_action || r.decision)} · Risk ${r.risk ?? '?'}</span>
        </div>
        <div class="mono text-secondary" style="font-size:11.5px">
          Motivo: ${esc((r.hard_blocks || []).join(', ') || r.reason || 'OK')} ·
          Top10: ${r.birdeye?.top10_pct ?? '—'}% · LP Lock: ${r.rugcheck?.lp_locked_pct ?? '—'}% ·
          Sell Slip Jupiter: ${r.jupiter?.estimated_slippage_sell_pct ?? '—'}%
        </div>
      </div>
    `).join('') || '<div class="data-panel" style="padding:20px" class="text-muted">Nessuna scansione AntiRug recente</div>';

    el.innerHTML = `
      <div class="subpage-grid">
        <div class="data-panel" style="padding:18px">
          <h3 style="margin:0 0 10px">🛡 Statistiche AntiRug (${esc(ar.mode_label || (ar.mode || 'off').toUpperCase())})</h3>\n          <div class="kv-row"><span>Stato operativo</span><b class="mono">${esc(ar.mode_label || 'OFF')} — ${esc(ar.mode_desc || 'Disattivato')}</b></div>
          <div class="kv-row"><span>Token analizzati</span><b class="mono">${st.evaluated || 0}</b></div>
          <div class="kv-row"><span>Autorizzati / Bloccati</span><b class="mono">${st.allow || 0} / ${st.block || 0}</b></div>
          <div class="kv-row"><span>Rischi critici</span><b class="mono text-negative">${st.critical || 0}</b></div>
        </div>
      </div>
      ${recent}`;
  }

  if (page === 'risk') {
    const el = document.getElementById('risk-content');
    if (!el) return;
    const sv = v.survival || {}, rp = v.risk_profile || {};
    el.innerHTML = `
      <div class="subpage-grid">
        <div class="data-panel" style="padding:18px">
          <h3 style="margin:0 0 10px">Sopravvivenza Capitale</h3>
          <div class="kv-row"><span>Equity attuale</span><b class="mono">${usd(v.equity)}</b></div>
          <div class="kv-row"><span>Soglia morte</span><b class="mono text-negative">${usd(sv.death_line_usd || 0)}</b></div>
          <div class="kv-row"><span>Margine di sicurezza</span><b class="mono text-positive">${usd(v.equity - (sv.death_line_usd || 0))}</b></div>
          <div class="kv-row"><span>Size prossimo trade</span><b class="mono">${usd(sv.next_size_usd || 0)} (${fmt(sv.size_pct_eff || 0, 1)}%)</b></div>
          <div class="kv-row"><span>Max posizioni simultanee</span><b class="mono">${sv.max_open || 'Nessun limite'}</b></div>
        </div>
        <div class="data-panel" style="padding:18px">
          <h3 style="margin:0 0 10px">Profilo di Rischio Attivo</h3>
          <div class="kv-row"><span>Profilo</span><b class="mono">${esc(rp.label || 'Standard')}</b></div>
          <div class="kv-row"><span>Origine</span><span class="mono">${esc(rp.source || 'config')}</span></div>
          <p class="text-secondary" style="font-size:12px;margin-top:10px">${esc(rp.desc || 'Gestione dinamica basata su equity corrente e drawdown.')}</p>
        </div>
      </div>`;
  }

  if (page === 'health') {
    const el = document.getElementById('health-content');
    if (!el) return;
    const srcHtml = (v.sources || []).map(s => `
      <div class="kv-row">
        <span><b>${esc(s.label)}</b> (${esc(s.state || '—')})</span>
        <span class="mono">${s.connected ? '🟢 Connesso' : '🔴 Disconnesso'} · ${s.received || 0} eventi</span>
      </div>`).join('');

    el.innerHTML = `
      <div class="subpage-grid">
        <div class="data-panel" style="padding:18px">
          <h3 style="margin:0 0 10px">Stato Data Sources</h3>
          ${srcHtml}
        </div>
        <div class="data-panel" style="padding:18px">
          <h3 style="margin:0 0 10px">Costi & Esecuzione</h3>
          <div class="kv-row"><span>Fee DEX totali</span><b class="mono">${usd(v.fees_dex || 0)}</b></div>
          <div class="kv-row"><span>Fee Rete Solana</span><b class="mono">${usd(v.fees_net || 0)}</b></div>
          <div class="kv-row"><span>Costo Slippage</span><b class="mono">${usd(v.slip_cost || 0)}</b></div>
          <div class="kv-row"><span>Slippage medio Buy / Sell</span><b class="mono">${fmt(v.avg_buy_slip || 0, 1)}% / ${fmt(v.avg_sell_slip || 0, 1)}%</b></div>
        </div>
      </div>`;
  }

  if (page === 'logs') {
    const el = document.getElementById('logs-content');
    if (!el) return;
    const rows = (v.events || []).map(e => `<tr>
      <td class="text-muted">${esc(e.clock || hhmmss(e.ts))}</td>
      <td><span class="badge ${e.kind === 'FAIL' || e.kind === 'DEAD' ? 'badge-danger' : 'badge-neutral'}">${esc(e.kind)}</span></td>
      <td><b>${tok(e.token)}</b></td>
      <td>${esc(e.text || '')}</td>
      <td class="num ${e.pnl >= 0 ? 'text-positive' : 'text-negative'}">${e.pnl != null ? usd(e.pnl, 2, true) : '—'}</td>
    </tr>`).join('') || '<tr><td colspan="5" class="center text-muted" style="padding:24px">Nessun evento registrato</td></tr>';

    el.innerHTML = `<div class="data-panel"><div class="panel-header"><h3>Feed Eventi Live</h3></div>
      <div class="table-container" style="max-height:none"><table class="table">
      <thead><tr><th>Ora</th><th>Tipo</th><th>Token</th><th>Dettaglio</th><th class="num">PnL</th></tr></thead>
      <tbody>${rows}</tbody></table></div></div>`;
  }

  if (page === 'settings') {
    const el = document.getElementById('settings-content');
    if (!el) return;
    const cfg = v.config || {};
    const rows = Object.entries(cfg).map(([k, val]) =>
      `<div class="kv-row"><span class="mono text-secondary">${esc(k)}</span><b class="mono">${esc(JSON.stringify(val))}</b></div>`
    ).join('') || '<div class="text-muted">Nessun parametro di configurazione esposto</div>';

    el.innerHTML = `<div class="data-panel" style="padding:18px"><h3 style="margin:0 0 12px">Parametri Attivi Sessione</h3>${rows}</div>`;
  }
}

// ── Live Toast Notifications ──────────────────────────────────────────────
const SEEN_EVENTS = new Set();
let toastBooted = false;

function processToasts(v) {
  const container = document.getElementById('toastContainer');
  if (!container) return;

  const evs = (v.events || []).slice().reverse();
  for (const e of evs) {
    const id = `${e.ts}_${e.kind}_${e.token}`;
    if (SEEN_EVENTS.has(id)) continue;
    SEEN_EVENTS.add(id);
    if (!toastBooted) continue;

    const cls = e.kind === 'SELL' ? (e.pnl >= 0 ? 'toast-success' : 'toast-error')
              : (e.kind === 'FAIL' || e.kind === 'DEAD' ? 'toast-error' : 'toast-info');
    const title = e.kind === 'BUY' ? `Entrata ${tok(e.token)}`
                : e.kind === 'SELL' ? `Chiusura ${tok(e.token)} (${usd(e.pnl, 2, true)})`
                : e.kind === 'BRAIN' ? '🧠 Cervello AI aggiornato'
                : `${e.kind} ${tok(e.token)}`;

    const toastEl = document.createElement('div');
    toastEl.className = `toast ${cls}`;
    toastEl.innerHTML = `<div class="toast-content"><div class="toast-title">${title}</div><div class="toast-message">${esc(e.text || '')}</div></div>`;
    container.appendChild(toastEl);
    setTimeout(() => {
      toastEl.classList.add('toast-leaving');
      setTimeout(() => toastEl.remove(), 300);
    }, 5000);
  }
  toastBooted = true;
}

// ── Main Poll Loop ────────────────────────────────────────────────────────
async function pollState() {
  try {
    const resp = await fetch(API_ENDPOINT, { cache: 'no-store' });
    const v = await resp.json();
    if (!v.ok) return;

    FX = v.fx || 1;
    lastState = v;

    renderHeaderStatus(v);
    renderKPIs(v);
    renderEquityChart(v);
    renderAllocation(v);
    renderPerformance(v);
    renderTradingTable(v);
    renderAgents(v);
    renderIntelligence(v);
    renderHealth(v);
    processToasts(v);

    if (currentPage !== 'overview') renderSecondaryPage(currentPage, v);
    document.title = `€${fmt(v.equity / FX)} · AEON Trading Intelligence`;
  } catch (err) {
    document.querySelector('#solanaStatus .rpc-dot')?.classList.add('offline');
    const rpcVal = document.getElementById('rpcStatus');
    if (rpcVal) rpcVal.textContent = 'Errore rete';
  }
}

document.addEventListener('DOMContentLoaded', () => {
  initParticles();
  initUI();
  pollState();
  setInterval(pollState, REFRESH_INTERVAL);
});
