/**
 * AEON by Rekt — Dashboard Application
 * Apple-inspired trading intelligence dashboard
 */

// ==========================================================================
// Configuration & Constants
// ==========================================================================

const BRAND = 'AEON by Rekt';

const LOGO = (size = 46) =>
  `<img class="logo" style="width:${size}px;height:${size}px;border-radius:12px" src="/static/logo/aeon-logo-${size}x${size}.png" alt="AEON by Rekt" />`;

const fmt = (x, d = 2) => Number(x || 0).toLocaleString('en-US', { minimumFractionDigits: d, maximumFractionDigits: d });

let FX = 1; // USD per EUR fixed at simulation start

const usd = (x, d = 2, sign = false) => (x < 0 ? '-' : (sign && x > 0 ? '+' : '')) + '€' + fmt(Math.abs(x) / FX, d);
const pc = (x, d = 0) => (x > 0 ? '+' : '') + fmt(x, d) + '%';
const esc = s => String(s ?? '').replace(/[&<>"]/g, c => ({ '&': '&', '<': '<', '>': '>', '"': '"' }[c]));

const COLORS = ['#e5484d', '#3b82f6', '#f59e0b', '#10b981', '#8b5cf6', '#ec4899', '#64748b', '#14b8a6', '#f97316', '#6366f1'];

const av = (t, s = 22) => {
  t = String(t || '?');
  let h = 0;
  for (const c of t) h = (h * 31 + c.charCodeAt(0)) >>> 0;
  return `<span class="avatar avatar-sm" style="width:${s}px;height:${s}px;font-size:${s/2}px;background:${COLORS[h % COLORS.length]}">${esc(t.replace('$', '')[0] || '?').toUpperCase()}</span>`;
};

const tok = t => '$' + esc(String(t || '?').split(' /')[0]);
const dur = s => { s = Math.max(0, s | 0); const h = Math.floor(s / 3600), m = Math.floor(s % 3600 / 60); return h ? `${h}h ${m}m` : `${m}m`; };

const agentIcon = c => `<span class="icon icon-sm icon-primary" style="background:${c}22;border-color:${c}66"><svg width="12" height="12" viewBox="0 0 46 46"><path d="M9 21c0-8 6.5-12.5 14.5-12.5S38 13 38 21v6.5c0 6.5-5 10-11 10H18c-5.5 0-9-3.5-9-9.5z" fill="${c}"/><ellipse cx="18.5" cy="22.5" rx="2.5" ry="5" transform="rotate(-28 18.5 22.5)" fill="#111"/><ellipse cx="28" cy="22.5" rx="2.5" ry="5" transform="rotate(-28 28 22.5)" fill="#111"/></svg></span>`;

// ==========================================================================
// Utility Functions
// ==========================================================================

const escHtml = s => String(s ?? '').replace(/[&<>"]/g, c => ({ '&': '&', '<': '<', '>': '>', '"': '"' }[c]));

const COLORS = ['#e5484d', '#3b82f6', '#f59e0b', '#10b981', '#8b5cf6', '#ec4899', '#64748b', '#14b8a6', '#f97316', '#6366f1'];

const av = (t, s = 22) => {
  t = String(t || '?');
  let h = 0;
  for (const c of t) h = (h * 31 + c.charCodeAt(0)) >>> 0;
  return `<span class="avatar avatar-sm" style="width:${s}px;height:${s}px;font-size:${s/2}px;background:${COLORS[h % COLORS.length]}">${esc(t.replace('$', '')[0] || '?').toUpperCase()}</span>`;
};

const tok = t => '$' + esc(String(t || '?').split(' /')[0]);
const dur = s => { s = Math.max(0, s | 0); const h = Math.floor(s / 3600), m = Math.floor(s % 3600 / 60); return h ? `${h}h ${m}m` : `${m}m`; };

const agentIcon = c => `<span class="icon icon-sm icon-primary" style="background:${c}22;border-color:${c}66"><svg width="12" height="12" viewBox="0 0 46 46"><path d="M9 21c0-8 6.5-12.5 14.5-12.5S38 13 38 21v6.5c0 6.5-5 10-11 10H18c-5.5 0-9-3.5-9-9.5z" fill="${c}"/><ellipse cx="18.5" cy="22.5" rx="2.5" ry="5" transform="rotate(-28 18.5 22.5)" fill="#111"/><ellipse cx="28" cy="22.5" rx="2.5" ry="5" transform="rotate(-28 28 22.5)" fill="#111"/></svg></span>`;

// ==========================================================================
// Source Status Badge
// ==========================================================================

function srcStatusBadge(s) {
  if (!s.enabled) return '<span class="badge badge-neutral">disabilitato</span>';
  if (s.connected && s.subscribed && s.receiving_events) return '<span class="badge badge-success">LIVE</span>';
  if (s.connected && s.subscribed) return '<span class="badge badge-warning">iscritto</span>';
  if (s.connected) return '<span class="badge badge-warning">connesso</span>';
  if (s.state === 'connecting') return '<span class="badge badge-warning">connessione…</span>';
  if (s.state === 'reconnecting') return '<span class="badge badge-danger">riconnessione</span>';
  if (s.state === 'error') return '<span class="badge badge-danger">errore</span>';
  if (s.state === 'polling') return '<span class="badge badge-success">polling</span>';
  if (s.state === 'idle') return '<span class="badge badge-neutral">idle</span>';
  return `<span class="badge badge-neutral">${esc(s.state)}</span>`;
}

// ==========================================================================
// Chart Rendering
// ==========================================================================

function chart(v) {
  const W = 480, H = 190, P = 8;
  let pts = v.equity_curve.slice();
  if (!pts.length) return `<svg viewBox="0 0 ${W} ${H}"></svg>`;
  if (pts.length === 1) pts.push([v.now, pts[0][1]]);
  pts[pts.length - 1] = [v.now, v.equity];
  const t0 = pts[0][0], t1 = Math.max(v.now, t0 + 60);

  // Realized balance line
  const rp = pts.filter(p => p.length > 2 && p[2] != null).map(p => [p[0], p[2]]);
  if (rp.length && v.realized_value != null) rp[rp.length - 1] = [v.now, v.realized_value];

  const ys = pts.map(p => p[1]).concat([v.start], rp.map(p => p[1]));
  let lo = Math.min(...ys), hi = Math.max(...ys);
  const pad = (hi - lo || 1) * 0.15;
  lo -= pad; hi += pad;

  const X = t => (t - t0) / (t1 - t0) * (W - 2 * 8) + 8;
  const Y = y => H - 6 - (y - lo) / (hi - lo) * (H - 26);

  const line = pts.map((p, i) => (i ? 'L' : 'M') + X(p[0]).toFixed(1) + ' ' + Y(p[1]).toFixed(1)).join(' ');
  const rline = rp.length > 1 ? rp.map((p, i) => (i ? 'L' : 'M') + X(p[0]).toFixed(1) + ' ' + Y(p[1]).toFixed(1)).join(' ') : '';
  const last = pts[pts.length - 1];
  const up = v.equity >= v.start;
  const col = up ? '#3ddc84' : '#ff4d5e';

  return `<svg viewBox="0 0 ${W} ${H}" preserveAspectRatio="none"><defs><linearGradient id="gf" x1="0" y1="0" x2="0" y2="1">
  <stop offset="0" stop-color="${col}" stop-opacity=".55"/><stop offset="1" stop-color="${col}" stop-opacity="0"/></linearGradient></defs>
  <line x1="0" x2="${W}" y1="${Y(v.start)}" y2="${Y(v.start)}" stroke="#4a4a50" stroke-dasharray="5 5" stroke-width="1.2"/>
  <path d="${line} L${X(last[0])} ${H} L${X(pts[0][0])} ${H}Z" fill="url(#gf)"/>
  <path d="${line}" fill="none" stroke="${col}" stroke-width="2.4" stroke-linejoin="round"/>
  ${rline ? `<path d="${rline}" fill="none" stroke="#d9d9dd" stroke-opacity=".75" stroke-width="1.4" stroke-dasharray="2 3" stroke-linejoin="round"/>` : ''}
  <circle cx="${X(last[0])}" cy="${Y(last[1])}" r="7" fill="#0a0a0b" stroke="#fff" stroke-width="2.6"/></svg>`;
}

// ==========================================================================
// Core Agents Visualization
// ==========================================================================

function core(v) {
  const W = 470, H = 250, cx = W / 2, cy = 205;
  let s = '';

  v.agents.forEach((a, i) => {
    const x = 30 + i * (W - 60) / 7, y = 34;
    const p = `M${x} ${y+16} C ${x} ${y+90}, ${cx+(x-cx)*.15} ${cy-80}, ${cx+(x-cx)*.08} ${cy-36}`;
    s += `<path d="${p}" fill="none" stroke="${a.color}" stroke-opacity="${a.idle?.18:.55}" stroke-width="1.6"/>`;
    for (const k of [.35, .62, .85]) {
      const px = x + (cx + (x-cx)*.08 - x) * k * k;
      const py = y + 16 + (cy - 36 - y - 16) * k;
      s += `<circle cx="${px}" cy="${py}" r="${a.idle?2:3.2}" fill="${a.color}" opacity="${a.idle?.3:.9}"/>`;
    }
    s += `<text x="${x}" y="12" fill="${a.color}" font-family="JBM,monospace" font-size="10" text-anchor="middle">${a.label}</text>
    <circle cx="${x}" cy="${y}" r="15" fill="#0d0d0f" stroke="${a.color}" stroke-opacity=".7" stroke-width="1.5"/>
    <g transform="translate(${x-8} ${y-8}) scale(.36)"><path d="M9 21c0-8 6.5-12.5 14.5-12.5S38 13 38 21v6.5c0 6.5-5 10-11 10H18c-5.5 0-9-3.5-9-9.5z" fill="${a.color}"/><ellipse cx="18.5" cy="22.5" rx="2.1" ry="4.2" transform="rotate(-28 18.5 22.5)" fill="#111"/><ellipse cx="28" cy="22.5" rx="2.1" ry="4.2" transform="rotate(-28 28 22.5)" fill="#111"/></g>`;
  });

  s += `<circle cx="${cx}" cy="${cy}" r="33" fill="#0d0d0f" stroke="#3ddc84" stroke-width="3"/>
  <circle cx="${cx}" cy="${cy}" r="40" fill="none" stroke="#3ddc84" stroke-opacity=".18" stroke-width="6"/>
  <g transform="translate(${cx-19} ${cy-19}) scale(.83)"><path d="M9 21c0-8 6.5-12.5 14.5-12.5S38 13 38 21v6.5c0 6.5-5 10-11 10H18c-5.5 0-9-3.5-9-9.5z" fill="#f2f2f2"/><ellipse cx="18.5" cy="22.5" rx="2.1" ry="4.2" transform="rotate(-28 18.5 22.5)" fill="#111"/><ellipse cx="28" cy="22.5" rx="2.1" ry="4.2" transform="rotate(-28 28 22.5)" fill="#111"/></g>
  <text x="${cx-70}" y="${cy+5}" fill="#8b8b92" font-family="JBM,monospace" font-size="12" text-anchor="end">8 → 1</text>
  <rect x="${cx+58}" y="${cy-10}" width="18" height="18" rx="4" fill="#3ddc84" opacity=".85"/><text x="${cx+82}" y="${cy+4}" fill="#3ddc84" font-family="JBM,monospace" font-size="12">EXECUTE</text>
  <text x="${cx}" y="${H-2}" fill="#f4f4f5" font-family="Inter,sans-serif" font-weight="700" font-size="13" text-anchor="middle">GROK CORE</text>`;

  return `<svg viewBox="0 0 ${W} ${H}">${s}</svg>`;
}

// ==========================================================================
// Phone Layout (Main View)
// ==========================================================================

function phone(v) {
  const [ip, dp] = fmt(v.equity / FX).split('.');
  const tk = (v.ticker.length ? v.ticker : [{ token: 'SOL', chg: 0 }]);
  const tkHtml = tk.map(t => `<span class="tk">${av(t.token)}${tok(t.token)} <span class="${t.chg >= 0 ? 'text-positive' : 'text-negative'}">${pc(t.chg)}</span></span>`).join('');

  const feed = v.events.slice(0, 3).map(e => {
    const k = e.kind === 'FAIL' ? '<span class="badge badge-danger">FAIL</span>' :
              e.kind === 'DEAD' ? '<span class="badge badge-danger">DEAD</span>' :
              e.kind === 'BRAIN' ? '<span class="badge badge-info">AI</span>' :
              `<span class="badge badge-neutral">${e.kind}</span>`;
    const tail = e.kind === 'SELL' ? `<span class="${e.pnl >= 0 ? 'text-positive' : 'text-negative'}">${usd(e.pnl, 2, true)}</span> <span class="text-muted">paper</span>` :
                  `<span class="text-muted">${escHtml(e.text)}</span>`;
    return `<div class="row"><span class="text-muted">${e.clock}</span>${k}${av(e.token)}<span>${tok(e.token)}</span><span class="text-muted">·</span>${tail}</div>`;
  }).join('') || '<div class="row text-muted">nessun trade ancora · in ascolto</div>';

  const ags = v.agents.map(a => `<div class="surface ${a.active ? 'surface-elevated' : ''}" style="${a.active ? `border-color:${a.color}` : ''}">
    <div class="t" style="display:flex;align-items:center;gap:6px;font-family:var(--font-mono);font-size:11px;color:var(--color-text-tertiary);white-space:nowrap">
      ${agentIcon(a.color)}<b style="color:${a.color}">${a.label}</b>
      <span class="s" style="color:var(--color-text-tertiary);flex:1 1 auto;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${a.sub} · q${a.n}</span>
      <span class="p" style="color:${a.color};font-weight:700;font-size:12px;flex:0 0 auto">${a.pct}%</span>
    </div>
    <div class="l" style="font-family:var(--font-mono);font-size:11.5px;color:#d9d9dd;margin:3px 0 6px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis">${esc(a.last)}</div>
    <div class="bar" style="height:4px;margin:0 -8px;background:#1d1d21"><i style="display:block;height:100%;width:${a.pct}%;background:${a.color};box-shadow:0 0 10px currentColor;transition:width .8s cubic-bezier(.2,.8,.2,1)"></i></div>
  </div>`).join('');

  const b = v.best, w = v.worst;
  const copies = (v.by_type?.copy_top_traders || {}).n || 0;
  const vols = (v.by_type?.volume_spike || {}).n || 0;

  return `
  <div class="hdr">${LOGO()}<span class="brand">${esc(BRAND)}</span><span class="badge badge-neutral">paper mode</span>
    <span class="live ${v.live && !v.dead ? '' : 'off'}"><span class="dot"></span>${v.dead ? '💀 Morto' : v.live ? (v.paused ? 'In pausa' : 'Live') : (v.status === 'stopped' ? 'Fermo' : 'Bloccato')}</span></div>
  <div class="tick" id="tick" data-k="${esc(tkHtml)}"></div>
  ${v.dead ? `<div class="badge badge-danger" style="display:block;text-align:center;padding:12px;margin:0 -14px">💀 MORTO · capitale esaurito ${v.survival?.dead_ts ? 'alle ' + new Date(v.survival.dead_ts * 1000).toLocaleTimeString('it-IT', { hour: '2-digit', minute: '2-digit' }) : ''} · nessun nuovo trade</div>` : ''}
  <div class="meta">PAPER · 3 sources · 8 agents · ${dur(v.uptime_sec)} in</div>
  <div class="balrow"><div class="bal">€${ip}<span class="dec">.${dp}</span></div>
    <span class="chg ${v.change_1h < 0 ? 'text-negative' : ''}">${usd(v.change_1h, 0, true)}</span></div>
  <div class="pills"><span class="pd">in €${fmt(v.start/FX, 0)} · ${v.trades} trades</span><span class="pw">${fmt(v.mult)}×</span></div>
  <div class="sub"><span class="${v.pnl >= 0 ? 'text-positive' : 'text-negative'}">${usd(v.pnl, 2, true)} · ${v.wins}W ${v.losses}L</span><span class="text-muted">${v.clock}</span></div>
  <div class="card card-padded">${chart(v)}<div class="axis" style="display:flex;justify-content:space-between;font-family:var(--font-mono);font-size:12px;color:var(--color-text-tertiary);padding:2px 14px 12px"><span>${v.start_clock}</span><span>— Totale (a mercato) &nbsp; ┄ Realizzato &nbsp; -- put in</span><span>${v.clock}</span></div></div>
  <div class="card card-padded feed">${feed}</div>
  <div class="card card-padded"><div class="ct" style="display:flex;align-items:center;justify-content:space-between;margin:2px 0 10px"><h3 style="margin:0;font-size:16px;font-weight:700;letter-spacing:-.2px">8 grok agents · live load</h3><span class="rt" style="font-family:var(--font-mono);font-size:12.5px;color:var(--color-positive);display:flex;align-items:center;gap:6px"><span class="dot"></span>${v.running} running · ${fmt(v.ops_per_min, 0)} ops/min</span></div>
    <div class="ag" style="display:grid;grid-template-columns:1fr 1fr;gap:8px">${ags}</div></div>
  <div class="card card-padded core"><div class="ct"><h3 style="margin:0;font-size:16px;font-weight:700;letter-spacing:-.2px">8 agents → grok core</h3><span class="rt" style="font-family:var(--font-mono);font-size:12.5px;color:var(--color-positive);display:flex;align-items:center;gap:6px">${fmt(v.signals, 0)} signals</span></div>${core(v)}</div>
  <div class="card card-padded recap"><div class="grab" style="width:46px;height:5px;border-radius:3px;background:#3a3a40;margin:-6px auto 14px"></div>
    <div class="rh" style="display:flex;gap:14px;align-items:center">${LOGO(58)}<div><h2 style="margin:0;font-size:24px">Night recap</h2><div class="mono text-muted" style="font-size:13px">${esc(BRAND.toLowerCase())} × paper · ${v.start_clock} → ${v.clock}</div></div></div>
    <div class="kv" style="display:flex;justify-content:space-between;align-items:baseline;margin-top:18px"><span class="text-muted">put in</span><b style="font-size:24px">€${fmt(v.start/FX)}</b></div>
    <div class="text-muted" style="margin-top:8px">session result</div>
    <div class="kv" style="display:flex;justify-content:space-between;align-items:baseline"><span class="${v.equity < v.start ? 'text-negative' : 'text-positive'}" style="font-weight:800;font-size:46px;letter-spacing:-1.5px">€${fmt(v.equity/FX)}</span><span class="pw" style="font-size:17px">${fmt(v.mult, 1)}×</span></div>
    <div class="inner" style="border:1px solid #2c2c31;border-radius:14px;padding:10px 14px;margin:12px 0;background:#141416"><div class="kv" style="padding:3px 0"><span>🐸 smart-money copies</span><span class="mono">${(v.by_type?.copy_top_traders || {}).n || 0}</span></div>
      <div class="kv text-muted" style="padding:3px 0 8px;border-bottom:1px solid #2c2c31"><span>trend / volume entries</span><span class="mono">${(v.by_type?.volume_spike || {}).n || 0}</span></div>
      <div class="kv" style="padding-top:8px"><b>session profit</b><b class="${v.pnl >= 0 ? 'text-positive' : 'text-negative'}" style="font-size:21px">${usd(v.pnl, 2, true)}</b></div></div>
    <div class="tiles" style="display:grid;grid-template-columns:repeat(4,1fr);gap:8px"><div class="tile" style="background:#232327;border-radius:12px;padding:9px 12px"><small style="color:var(--color-text-tertiary);font-size:12.5px">trades</small><div style="font:700 21px var(--font-mono);margin-top:3px">${v.trades}</div></div><div class="tile" style="background:#232327;border-radius:12px;padding:9px 12px"><small style="color:var(--color-text-tertiary);font-size:12.5px">coins</small><div style="font:700 21px var(--font-mono);margin-top:3px">${v.coins}</div></div>
      <div class="tile" style="background:#232327;border-radius:12px;padding:9px 12px"><small style="color:var(--color-text-tertiary);font-size:12.5px">sources</small><div style="font:700 21px var(--font-mono);margin-top:3px">3</div></div><div class="tile" style="background:#232327;border-radius:12px;padding:9px 12px"><small style="color:var(--color-text-tertiary);font-size:12.5px">agents</small><div style="font:700 21px var(--font-mono);margin-top:3px">8</div></div></div>
    <div class="mono" style="font-size:13.5px;line-height:1.85;margin-top:12px">
      ${v.best ? `best ${av(v.best.token, 18)} ${tok(v.best.token)} <span class="${v.best.pnl_usd >= 0 ? 'text-positive' : 'text-negative'}">${usd(v.best.pnl_usd, 2, true)}</span><br>` : ''}
      ${v.worst ? `worst ${av(v.worst.token, 18)} ${tok(v.worst.token)} <span class="${v.worst.pnl_usd >= 0 ? 'text-positive' : 'text-negative'}">${usd(v.worst.pnl_usd, 2, true)}</span><br>` : ''}
      win rate ${v.win_rate ?? '—'}% · TP/SL on every trade<br>
      <span class="text-muted">GMGN · Pump.fun · GeckoTerminal · DexScreener</span></div>
    <button class="btn btn-secondary btn-full">simulazione · nessun denaro reale</button></div>
  <div class="side-mobile">${side(v)}</div>`;
}

// ==========================================================================
// Side Layout (Desktop)
// ==========================================================================

function side(v) {
  const J = src => src === 'jupiter' ? '<span class="text-positive" title="slippage dal quote Jupiter (stima)">J</span>' : '<span class="text-muted" title="slippage stimato">s</span>';

  const pos = v.positions.map(p => `<tr><td>${av(p.name, 16)} ${tok(p.name)}</td><td>${p.entry_type === 'volume_spike' ? 'volume' : 'copy'}${p.track && p.track.n ? ` <span class="text-muted" title="top trader usciti / seguiti">${p.track.sold || 0}/${p.track.n}👛</span>` : ''} ${J((p.costs || {}).buy_slip_src)}</td><td>${p.entry_clock}</td>
    <td class="num">${usd(p.cost_usd)}</td><td class="num ${p.chg_pct >= 0 ? 'text-positive' : 'text-negative'}">${pc(p.chg_pct, 1)}</td><td class="num ${p.upnl >= 0 ? 'text-positive' : 'text-negative'}">${usd(p.upnl, 2, true)}</td><td class="num">${fmt(p.held_min, 0)}m</td></tr>`).join('') || '<tr><td colspan="7" class="text-muted">nessuna posizione aperta</td></tr>';

  const tr = v.closed.map(t => `<tr><td>${t.exit_clock}</td><td>${tok(t.token)}</td><td>${t.entry_type === 'volume_spike' ? 'volume' : 'copy'}</td><td class="num">${usd(t.cost_usd)}</td>
    <td class="num ${t.pnl_usd >= 0 ? 'text-positive' : 'text-negative'}">${usd(t.pnl_usd, 2, true)}</td><td class="num ${t.pnl_pct >= 0 ? 'text-positive' : 'text-negative'}">${pc(t.pnl_pct, 1)}</td><td>${esc(t.reason_it)}</td>
    <td class="num">${fmt(t.buy_slip_pct, 1)}${J(t.buy_slip_src)}/${fmt(t.sell_slip_pct, 1)}${J(t.sell_slip_src)}%</td><td class="num">${fmt(t.hold_sec / 60, 0)}m</td></tr>`).join('') || '<tr><td colspan="9" class="text-muted">nessun trade chiuso</td></tr>';

  const bt = Object.entries(v.by_type).map(([k, e]) => `<div class="kv"><span>${k === 'volume_spike' ? 'volume spike' : 'copy top traders'}</span><span>${e.n} · ${e.wins}W · <span class="${e.pnl >= 0 ? 'text-positive' : 'text-negative'}">${usd(e.pnl, 2, true)}</span></span></div>`).join('') || '<span class="text-muted">—</span>';

  const br = Object.entries(v.by_reason).map(([k, e]) => `<div class="kv"><span>${esc(k)}</span><span>${e.n} · <span class="${e.pnl >= 0 ? 'text-positive' : 'text-negative'}">${usd(e.pnl, 2, true)}</span></span></div>`).join('') || '<span class="text-muted">—</span>';

  const vy = v.vybe ? `${fmt(v.vybe.estimated_credits_used_this_month, 0)} usati · ${fmt(v.vybe.credits_used_today, 0)} oggi` : 'non configurato';
  const tg = v.telegram ? (v.telegram.dry ? 'modalità prova (nessun token)' : `attivo · ${v.telegram.sent} inviati · ${v.telegram.failed} falliti`) : '—';

  const sv = v.survival || {}, brn = v.brain || {};
  const svMode = { ok: '✅ vivo', paused: '⏸ in pausa', dead: '💀 MORTO' }[sv.mode] || sv.mode || '—';

  const survival = `<div class="card card-padded"><div class="ct"><h3>Sopravvivenza</h3><span class="rt" style="color:${v.dead ? 'var(--color-negative)' : 'var(--color-positive)'}">${svMode}</span></div>
   <div class="st grid2" style="display:grid;grid-template-columns:1fr 1fr;gap:10px"><div>
    <div class="kv"><span>capitale</span><b>${usd(v.equity)}</b></div>
    <div class="kv"><span>muore sotto</span><span>${usd(sv.death_line_usd || 0)}</span></div>
    <div class="kv"><span>distanza dalla morte</span><span>${usd(v.equity - (sv.death_line_usd || 0))}</span></div></div><div>
    <div class="kv"><span>prossimo trade</span><span>${usd(sv.next_size_usd || 0)} (${fmt(sv.size_pct_eff || 0, 1)}%)</span></div>
    <div class="kv"><span>trade minimo</span><span>${usd(sv.min_trade_usd || 0)}</span></div>
    <div class="kv"><span>limite posizioni</span><span>${sv.max_open || 'nessuno'}</span></div></div></div>
    <div class="text-muted mono" style="font-size:11.5px;margin-top:6px">Unica regola: quando i soldi finiscono il bot muore. EUR/USD fissato all'avvio: ${fmt(v.fx, 4)}</div></div>`;

  const chs = (brn.changes || []).slice().reverse().map(c => `<div class="chg-row" style="display:flex;gap:10px;font-family:var(--font-mono);font-size:12.5px;padding:5px 0;border-bottom:1px solid #1c1c20"><span class="text-muted">${new Date(c.ts * 1000).toLocaleString('it-IT', { day: '2-digit', month: '2-digit', hour: '2-digit', minute: '2-digit' })}</span><span>${esc(c.text)}</span></div>`).join('') || '<div class="text-muted mono" style="font-size:12.5px">nessuna modifica finora</div>';

  const brain = `<div class="card card-padded"><div class="ct"><h3>🧠 Cervello</h3><span class="rt">${brn.records || 0} trade · prossima analisi tra ${brn.next_in ?? '—'}</span></div>
    <div class="brain" style="white-space:pre-wrap;font-family:var(--font-mono);font-size:12.5px;line-height:1.6;color:#d9d9dd">${brn.diagnosis ? esc(brn.diagnosis) : 'Sto raccogliendo dati: la prima diagnosi arriva dopo 10 trade chiusi.'}</div>
    <div style="margin-top:10px" class="text-muted mono">storico modifiche</div><div class="scroll" style="max-height:220px;overflow:auto">${chs}</div></div>`;

  const ar = v.antirug;
  let antirug = '';
  if (!ar) {
    antirug = `<div class="card card-padded" id="antirug"><div class="ct"><h3>🛡 AntiRug</h3><span class="rt text-muted">OFF</span></div><div class="text-muted mono" style="font-size:12px">spento (ANTIRUG_MODE=off): nessun controllo, comportamento invariato</div></div>`;
  } else {
    const st = ar.stats || {}, cmp = ar.comparison || {}, lv = st.levels || {}, api = ar.api || {};
    const col = d => d === 'ALLOW' ? '#3ddc84' : d === 'BLOCK' ? '#ff4d5e' : '#f5b73b';

    const card = r => `<details><summary><span class="text-muted">${esc((r.time || '').slice(11, 16))}</span> $${esc(r.name || '?')} · <b style="color:${col(r.decision)}">${esc(r.decision)}</b> · rischio ${r.risk ?? '?'} (${esc(r.level || '')})</summary>
     <div class="text-muted mono" style="font-size:11.5px;line-height:1.5">motivo: ${esc((r.hard_blocks || []).join(', ') || r.reason || '')}<br>
     Birdeye: top10 ${r.birdeye?.top10_pct ?? '—'}% · top5 ${r.birdeye?.top5_pct ?? '—'}% · liquidità $${fmt(r.birdeye?.liquidity_usd || 0, 0)} ${r.birdeye?.uncertain ? '· incerto' : ''}<br>
     Jupiter: vendibile ${r.jupiter?.route_sell === false ? 'NO' : r.jupiter?.route_sell ? 'sì (' + esc(r.jupiter.sell_to || '') + ')' : '?'} · slippage vendita stimato ${r.jupiter?.estimated_slippage_sell_pct ?? '—'}% · impatto ${r.jupiter?.price_impact_sell_pct ?? '—'}%<br>
     RugCheck: mint ${r.rugcheck?.mint_authority == null ? '?' : r.rugcheck.mint_authority ? 'ATTIVA' : 'revocata'} · freeze ${r.rugcheck?.freeze_authority == null ? '?' : r.rugcheck.freeze_authority ? 'ATTIVA' : 'revocata'} · LP bloccata ${r.rugcheck?.lp_locked_pct ?? '—'}%<br>
     falliti: ${esc((r.failed || []).join(', ') || '—')} · non verificati: ${esc((r.unverified || []).join(', ') || '—')}<br>
     controlli obbligatori: ${Object.entries(r.mandatory || {}).map(([k, x]) => `<span style="color:${x === 'PASS' ? '#3ddc84' : x === 'FAIL' ? '#ff4d5e' : '#f5b73b'}">${esc(k)} ${esc(x)}</span>`).join(' · ') || '—'} · fiducia ${esc(r.confidence || '—')}</div></details>`;

    const cards = (ar.recent || []).slice().reverse().map(card).join('') || '<div class="text-muted">nessuna analisi finora</div>';
    const blocks = (ar.last_blocks || []).slice().reverse().map(r => `<div class="kv"><span>$${esc(r.name || '?')}</span><span>${esc((r.hard_blocks || []).join(', ') || r.reason || '')}</span></div>`).join('') || '<div class="text-muted">nessun blocco</div>';
    const dist = ['basso', 'moderato', 'elevato', 'molto elevato', 'critico'].map(k => `${k} ${lv[k] || 0}`).join(' · ');
    const apis = Object.entries(api).map(([k, a]) => !(a.ok || a.fail || a.skipped) ? `<div class="kv"><span>${esc(k)}</span><span class="text-muted">non usata / disattivata</span></div>` : `<div class="kv"><span>${esc(k)}</span><span style="color:${a.fail || a.skipped ? '#f5b73b' : 'var(--color-positive)'}">ok ${a.ok} · err ${a.fail}${a.skipped ? ' · saltate ' + a.skipped : ''} · ultimo ok ${esc((a.last_ok || '—').slice(11, 16))}</span></div>`).join('');

    const lastUpd = Object.values(api).map(a => a.last_ok).filter(Boolean).sort().pop();
    const cmpRow = (k, lab) => { const c = cmp[k] || {}; return `<tr><td>${lab}</td><td>${c.n || 0}</td><td>${c.avg_5m ?? '—'}</td><td>${c.avg_15m ?? '—'}</td><td>${c.avg_60m ?? '—'}</td><td>${c.hypo_60m ?? '—'}</td><td>${c.rug_like ?? 0}</td></tr>` };

    antirug = `<div class="card card-padded" id="antirug"><div class="ct"><h3>🛡 AntiRug</h3><span class="rt" style="color:${ar.mode === 'active' ? 'var(--color-positive)' : 'var(--color-warning)'}">${ar.mode === 'active' ? 'ACTIVE · blocca' : (ar.enforce && ar.enforce.length ? 'SHADOW + top10 applicato' : 'SHADOW · solo osservazione')}</span></div>
      <div class="st grid2"><div><div class="kv"><span>token analizzati</span><b>${st.evaluated || 0}</b></div><div class="kv"><span>autorizzati / bloccati</span><span>${st.allow || 0} / ${st.block || 0}</span></div><div class="kv"><span>non verificabili</span><span>${st.unknown || 0}</span></div></div>
      <div><div class="kv"><span>rischi critici</span><span style="color:var(--color-negative)">${st.critical || 0}</span></div><div class="kv"><span>entrate rifiutate</span><span>${(st.rejected_active || 0) + (st.rejected_enforced || 0)}</span></div><div class="kv"><span>controlli prezzo</span><span>${ar.pending_followups || 0} in corso · ${ar.completed || 0} finiti</span></div></div></div>
      <div class="text-muted mono" style="font-size:11.5px;margin-top:6px">distribuzione punteggi: ${dist}</div>
      <div class="text-muted mono" style="margin-top:8px">ultimi blocchi e motivi</div>${blocks}
      <div class="text-muted mono" style="margin-top:8px">schede token (Birdeye · Jupiter · RugCheck)</div><div class="scroll" style="max-height:240px;overflow:auto">${cards}</div>
      <div class="text-muted mono" style="font-size:11.5px;margin-top:8px">dopo la valutazione: variazione media prezzo % e trade ipotetico % a 60 min</div>
      <table><tr class="text-muted"><td>esito</td><td>n</td><td>+5m</td><td>+15m</td><td>+60m</td><td>ipot.</td><td>rug</td></tr>${cmpRow('BLOCK', 'bloccati')}${cmpRow('ALLOW', 'autorizzati')}${cmpRow('UNKNOWN', 'non verif.')}${cmp.ENFORCED ? cmpRow('ENFORCED', 'rifiutati (top10)') : ''}</table>
      <div class="text-muted mono" style="font-size:11.5px">rug evitati ${cmp.rugs_avoided ?? 0} · occasioni profittevoli perse ${cmp.profitable_missed ?? 0} · rug autorizzati ${cmp.rugs_allowed ?? 0}</div>
      <div class="text-muted mono" style="margin-top:8px">stato API · ultimo aggiornamento fonti ${esc((lastUpd || '—').replace('T', ' ').slice(0, 16))} UTC</div>${apis}</div>`;
  }

  const rp = v.risk_profile;
  const prof = rp ? `<div class="card card-padded" id="riskprofile"><div class="ct"><h3>🎚 Profilo di rischio</h3><span class="rt" style="color:${rp.name === 'aggressiva' ? '#ff8a3d' : rp.name === 'bilanciata' ? '#f5b73b' : 'var(--color-positive)'}">${esc(rp.label)}</span></div><div class="text-muted mono" style="font-size:12px">${esc(rp.desc)}<br>origine: ${esc(rp.source)}${rp.restart_needed ? ' · <b>AntiRug/Risk Manager al prossimo riavvio</b>' : ''}</div></div>` : '';

  const pv = v.price_provenance;
  const prov = pv ? `<div class="card card-padded" id="provenance"><div class="ct"><h3>🔎 Provenienza prezzi</h3><span class="rt text-muted">${v.session ? esc('sessione ' + v.session.name) : ''}</span></div><div class="text-muted mono" style="font-size:12px">VERIFICATI (Jupiter): ${pv.VERIFIED.n} trade · P&L $${fmt(pv.VERIFIED.pnl_usd, 2)}<br>STIMATI (modello): ${pv.ESTIMATED.n} trade · P&L $${fmt(pv.ESTIMATED.pnl_usd, 2)}<br>RIFIUTATI (prezzo non verificabile): ${pv.REJECTED.n} · salti non confermati ignorati: ${pv.REJECTED.unconfirmed_jumps_ignored}</div></div>` : '';

  const pf = v.pumpfun || {};
  const pfEvents = (pf.recent_events || []).slice(0, 8).map(e => `<div class="pump-event" style="display:grid;grid-template-columns:70px 1fr auto;gap:9px;align-items:center;padding:8px 0;border-top:1px solid rgba(148,163,184,.1);font:12px var(--font-mono)"><span class="text-muted">${new Date((e.received_at || 0) * 1000).toLocaleTimeString('it-IT', { hour: '2-digit', minute: '2-digit', second: '2-digit' })}</span><span><b>${esc(e.token_symbol || e.token_name || 'TOKEN')}</b> <span class="text-muted">${esc((e.token_mint || '').slice(0, 8))}…</span></span><span class="pump-tag ${e.event_type === 'migration' ? 'mig' : ''}" style="font-size:10px;letter-spacing:.08em;border:1px solid rgba(45,212,191,.25);border-radius:99px;padding:3px 7px;color:#5eead4">${e.event_type === 'migration' ? 'MIGRATED' : 'NEW TOKEN'}</span></div>`).join('') || '<div class="pump-empty" style="padding:12px 0;color:var(--color-text-tertiary);font:12px var(--font-mono)">In attesa di nuovi lanci e migrazioni…</div>';

  const pfCard = `<div class="card card-padded pump-card" style="border-color:rgba(45,212,191,.22);background:radial-gradient(ellipse at 90% 0,rgba(45,212,191,.10),transparent 48%),linear-gradient(145deg,#141b1d,#101216)"><div class="ct"><div class="pump-head" style="display:flex;align-items:center;gap:10px"><span class="pump-orb" style="width:11px;height:11px;border-radius:50%;background:#2dd4bf;box-shadow:0 0 18px #2dd4bf;animation:dotPulse 1.8s infinite;${pf.connected ? '' : 'background:#64748b;box-shadow:none'}"></span><h3>Pump.fun Intelligence</h3></div><span class="rt" style="color:${pf.connected ? '#2dd4bf' : '#94a3b8'}">${pf.connected ? 'LIVE' : esc(pf.state || 'OFFLINE')}</span></div><div class="st grid2"><div><div class="kv"><span>nuovi token</span><b>${pf.events_by_channel?.new_token || 0}</b></div><div class="kv"><span>migrazioni</span><b>${pf.events_by_channel?.migration || 0}</b></div></div><div><div class="kv"><span>eventi ricevuti</span><b>${pf.received || 0}</b></div><div class="kv"><span>ultimo evento</span><span>${pf.last_event_ts ? new Date(pf.last_event_ts * 1000).toLocaleTimeString('it-IT') : '—'}</span></div></div></div><div style="margin-top:10px">${pfEvents}</div><div class="disc" style="font-family:var(--font-mono);font-size:11.5px;color:var(--color-text-tertiary);text-align:center;margin-top:14px">PumpPortal · stream gratuiti create/migration · sola lettura · nessun trade stream</div></div>`;

  // Source cards
  const src = v.sources || [];
  const srcCards = src.map(s => `<div class="card card-padded src-card">
    <div class="ct"><h3>${esc(s.label)}</h3><span class="rt">${srcStatusBadge(s)}</span></div>
    <div class="st">
      <div class="kv"><span>abilitato</span><span class="${s.enabled ? 'text-positive' : 'text-muted'}">${s.enabled ? 'sì' : 'no'}</span></div>
      <div class="kv"><span>connesso</span><span class="${s.connected ? 'text-positive' : 'text-negative'}">${s.connected ? 'sì' : 'no'}</span></div>
      ${s.subscribed !== undefined ? `<div class="kv"><span>iscritto</span><span class="${s.subscribed ? 'text-positive' : 'text-negative'}">${s.subscribed ? 'sì' : 'no'}</span></div>` : ''}
      ${s.receiving_events !== undefined ? `<div class="kv"><span>riceve eventi</span><span class="${s.receiving_events ? 'text-positive' : 'text-negative'}">${s.receiving_events ? 'sì' : 'no'}</span></div>` : ''}
      <div class="kv"><span>eventi</span><span>${s.received || 0}</span></div>
      ${s.events_by_channel ? `<div class="kv"><span>per canale</span><span class="text-muted">${Object.entries(s.events_by_channel).map(([k, v]) => `${k}:${v}`).join(' · ')}</span></div>` : ''}
      <div class="kv"><span>ultimo evento</span><span>${s.last_event_ts ? new Date(s.last_event_ts * 1000).toLocaleTimeString('it-IT') : '—'}</span></div>
      ${s.last_error ? `<div class="kv"><span>ultimo errore</span><span class="text-negative">${esc(s.last_error.slice(0, 60))}</span></div>` : ''}
    </div>
  </div>`).join('');

  const srcSection = `<div class="card card-padded"><div class="ct"><h3>📡 Data Sources</h3></div><div class="src-grid" style="display:grid;grid-template-columns:repeat(auto-fit,minmax(280px,1fr));gap:10px">${srcCards}</div></div>`;

  // Unified Token Discovery
  const events = [];
  const gm = v.gmgn || {};
  (gm.recent_events || []).forEach(e => events.push({ ...e, source: 'gmgn' }));
  const pf2 = v.pumpfun || {};
  (pf2.recent_events || []).forEach(e => events.push({ ...e, source: 'pumpfun' }));

  events.sort((a, b) => (b.received_at || 0) - (a.received_at || 0));

  const discEvents = events.slice(0, 20).map(e => `<div class="disc-event" style="display:grid;grid-template-columns:auto auto 1fr auto auto auto auto auto auto;gap:12px;align-items:center;padding:8px 0;border-top:1px solid rgba(148,163,184,.1);font:12px var(--font-mono)"><span class="text-muted">${new Date((e.received_at || 0) * 1000).toLocaleTimeString('it-IT', { hour: '2-digit', minute: '2-digit', second: '2-digit' })}</span><span class="src-badge src-${e.source}" style="font-size:9px;font-weight:700;padding:2px 6px;border-radius:4px;background:var(--color-surface);border:1px solid var(--color-surface-border);text-transform:uppercase">${esc(e.source.toUpperCase())}</span><span><b>${esc(e.token_symbol || e.token_name || 'TOKEN')}</b> <span class="text-muted">${esc((e.token_mint || '').slice(0, 8))}…</span></span><span class="text-muted">${esc(e.event_type)}</span><span class="text-muted">${e.price_usd ? fmt(e.price_usd, 6) : '—'}</span><span class="text-muted">${e.liquidity_usd ? fmt(e.liquidity_usd, 0) + ' $' : '—'}</span><span class="text-muted">${e.volume_usd ? fmt(e.volume_usd, 0) + ' $' : '—'}</span><span class="${e.data_quality === 'complete' ? 'text-positive' : e.data_quality === 'partial' ? 'text-muted' : 'text-negative'}">${e.data_quality || '—'}</span><span class="${e.verification_status === 'verified' ? 'text-positive' : 'text-muted'}">${e.verification_status || '—'}</span></div>`).join('') || '<div class="disc-empty" style="padding:12px 0;color:var(--color-text-tertiary);font:12px var(--font-mono);text-align:center">Nessun evento di scoperta recente</div>';

  const discSection = `<div class="card card-padded"><div class="ct"><h3>🔍 Token Discovery</h3></div><div class="disc-grid" style="display:flex;flex-direction:column;gap:0">${discEvents}</div></div>`;

  return `${prof}${prov}${survival}${brain}${antirug}${pfCard}${srcSection}${discSection}
  <div class="card card-padded"><div class="ct"><h3>Stato bot</h3><span class="rt" style="color:${v.live ? 'var(--color-positive)' : 'var(--color-negative)'}"><span class="dot" style="${v.live ? '' : 'background:var(--color-negative)'}"></span>${v.live ? 'live' : v.status}</span></div>
   <div class="st grid2"><div>
    <div class="kv"><span>ultimo battito</span><span>${v.heartbeat_age}s fa</span></div>
    <div class="kv"><span>in esecuzione da</span><span>${dur(v.uptime_sec)}</span></div>
    <div class="kv"><span>riavvii</span><span>${v.meta.restarts || 0} (watchdog ${v.meta.watchdog_restarts || 0})</span></div>
    <div class="kv"><span>blocchi totali</span><span>${v.stall_total_min} min</span></div></div><div>
    <div class="kv"><span>coppie in lista</span><span>${v.watch}</span></div>
    <div class="kv"><span>coppie viste</span><span>${fmt(v.pools_seen, 0)}</span></div>
    <div class="kv"><span>Vybe crediti</span><span>${vy}</span></div>
    <div class="kv"><span>Telegram</span><span>${tg}</span></div></div></div></div>
  <div class="card card-padded"><div class="ct"><h3>Saldo</h3><span class="rt text-muted" style="color:var(--color-text-tertiary)">simulato</span></div><div class="st grid2"><div>
    <div class="kv"><span>partenza</span><span>${usd(v.start)}</span></div><div class="kv"><span>cassa</span><span>${usd(v.cash)}</span></div>
    <div class="kv"><span>posizioni aperte</span><span>${usd(v.open_value)}</span></div><div class="kv"><span>totale</span><b>${usd(v.equity)}</b></div></div><div>
    <div class="kv"><span>P&L</span><span class="${v.pnl >= 0 ? 'text-positive' : 'text-negative'}">${usd(v.pnl, 2, true)} (${pc(100 * (v.mult - 1), 1)})</span></div>
    <div class="kv"><span>realizzato</span><span class="${v.realized >= 0 ? 'text-positive' : 'text-negative'}">${usd(v.realized, 2, true)}</span></div>
    <div class="kv"><span>win rate</span><span>${v.win_rate ?? '—'}% (${v.wins}/${v.trades})</span></div>
    <div class="kv"><span>vendite fallite</span><span>${v.failed_exits}</span></div></div></div></div>
  <div class="card card-padded"><div class="ct"><h3>Posizioni aperte</h3><span class="rt">${v.positions.length}</span></div><div class="scroll" style="max-height:420px;overflow:auto"><table class="table-container">
    <tr><th>token</th><th>entrata</th><th>ora</th><th class="num">importo</th><th class="num">prezzo</th><th class="num">P&L</th><th class="num">tenuto</th></tr>${pos}</table></div></div>
  <div class="grid2"><div class="card card-padded"><div class="ct"><h3>Entrate per tipo</h3></div><div class="st">${bt}</div></div>
    <div class="card card-padded"><div class="ct"><h3>Motivi di uscita</h3></div><div class="st">${br}</div></div></div>
  <div class="card card-padded"><div class="ct"><h3>Costi (fee e slippage)</h3></div><div class="st grid2"><div>
    <div class="kv"><span>fee DEX</span><span>${usd(v.fees_dex)}</span></div><div class="kv"><span>fee rete</span><span>${usd(v.fees_net)}</span></div>
    <div class="kv"><span>fee totali</span><b>${usd(v.fees_dex + v.fees_net)}</b></div></div><div>
    <div class="kv"><span>slippage medio acquisto</span><span>${v.avg_buy_slip == null ? '—' : fmt(v.avg_buy_slip, 1) + '%'}</span></div>
    <div class="kv"><span>slippage medio vendita</span><span>${v.avg_sell_slip == null ? '—' : fmt(v.avg_sell_slip, 1) + '%'}</span></div>
    <div class="kv"><span>costo slippage</span><span>${usd(v.slip_cost)}</span></div></div></div>
    <div class="text-muted mono" style="font-size:11.5px;margin-top:6px">fonte slippage: <span class="text-positive">J</span> = quote Jupiter (stima) ${v.slip_src.jupiter || 0} · <span class="text-muted">s</span> = stimato ${v.slip_src.stimato || 0} (lati eseguiti) · quote Jupiter ${v.jupiter.enabled ? \`ok ${v.jupiter.ok || 0} · senza rotta ${v.jupiter.no_route || 0} · errori ${v.jupiter.fail || 0}\` : 'disattivate'}</div></div>
  ${rulesCard(v)}
  <div class="card card-padded"><div class="ct"><h3>Storico trade</h3><span class="rt">${v.trades}</span></div><div class="scroll"><table class="table-container">
    <tr><th>uscita</th><th>token</th><th>tipo</th><th class="num">importo</th><th class="num">P&L $</th><th class="num">%</th><th>motivo</th><th class="num">slip</th><th class="num">durata</th></tr>${tr}</table></div></div>
  <div class="disc" style="font-family:var(--font-mono);font-size:11.5px;color:var(--color-text-tertiary);text-align:center;margin-top:14px">PAPER TRADING · nessun wallet · nessuna chiave privata · nessun ordine reale</div>`;
}

function rulesCard(v) {
  const r = v.exit_rules || {}, vs = r.volume_spike, cp = r.copy_top_traders;
  if (!vs || !cp) return '';

  const line = x => `<div class="kv"><span>stop loss</span><span>-${fmt(100 * x.stop_loss_pct, 0)}%</span></div>
    <div class="kv"><span>take profit</span><span>${x.take_profit_pct ? '+' + fmt(100 * x.take_profit_pct, 0) + '%' : '—'}</span></div>
    <div class="kv"><span>trailing</span><span>${x.trailing_stop_pct ? fmt(100 * x.trailing_stop_pct, 0) + '% dopo +' + fmt(100 * x.trailing_activate_pct, 0) + '%' : 'off'}</span></div>
    <div class="kv"><span>durata max</span><span>${fmt(x.max_hold_minutes, 0)} min</span></div>`;

  const cd = v.config.reentry_cooldown_minutes || 0;
  return `<div class="card card-padded"><div class="ct"><h3>Regole di uscita</h3><span class="rt text-muted" style="color:var(--color-text-tertiary)">per tipo di entrata</span></div><div class="st grid2">
    <div><div class="text-muted mono" style="margin-bottom:4px">picco di volume</div>${line(vs)}<div class="kv"><span>ricompra stesso token</span><span>${cd ? 'dopo ' + fmt(cd, 0) + ' min' : 'subito'}</span></div></div>
    <div><div class="text-muted mono" style="margin-bottom:4px">copia top trader</div>${line(cp)}<div class="kv"><span>top trader vendono</span><span>${cp.follow_wallets ? 'esce se >' + fmt(100 * cp.sold_majority_pct, 0) + '%' : 'off'}</span></div><div class="kv"><span>ricompra</span><span>se ricomprano</span></div></div></div></div>`;
}

const TQ = [], SEEN = new Set();
let tInit = false, tBusy = false;

function toast(v) {
  const evs = v.events.slice().reverse();          // oldest -> newest
  for (const e of evs) {
    const id = e.ts + e.kind + e.token;
    if (SEEN.has(id)) continue;
    SEEN.add(id);
    if (tInit) TQ.push(e);
  }
  tInit = true;
  if (TQ.length > 5) TQ.splice(0, TQ.length - 5);
  if (SEEN.size > 500) { const keep = [...SEEN].slice(-200); SEEN.clear(); keep.forEach(x => SEEN.add(x)) }
  if (!tBusy) nextToast();
}

function nextToast() {
  const el = document.getElementById('toastContainer'), e = TQ.shift();
  if (!e) { tBusy = false; return }
  tBusy = true;

  const title = e.kind === 'BUY' ? `Bought ${tok(e.token)} (paper)` :
                e.kind === 'SELL' ? `Sold ${tok(e.token)} ${usd(e.pnl, 2, true)}` :
                e.kind === 'BRAIN' ? '🧠 Cervello: nuova analisi' :
                e.kind === 'DEAD' ? '💀 MORTO' :
                `⚠️ Sell failed ${tok(e.token)}`;

  el.innerHTML = `${LOGO(40)}<div style="flex:1;min-width:0"><b>${title}</b><span>${escHtml(e.kind === 'SELL' ? (e.text || '').replace(/_/g, ' ') : e.text)} · simulato</span></div>${av(e.token, 36)}`;
  el.classList.add('show');
  setTimeout(() => { el.classList.remove('show'); setTimeout(nextToast, 500) }, 6000);
}

async function tick() {
  try {
    const v = await (await fetch('/api/state', { cache: 'no-store' })).json();
    FX = v.fx || 1;
    if (!v.ok) { document.getElementById('phone').innerHTML = `<div class="err animate-fade-in">${esc(v.error)}</div>`; return }
    const keep = document.getElementById('tick');
    document.getElementById('phone').innerHTML = phone(v);
    const ph = document.getElementById('tick');
    if (keep && keep.dataset.k === ph.dataset.k) { ph.replaceWith(keep) } else { ph.innerHTML = (v.ticker.length >= 4) ? `<div class="tick-in">${ph.dataset.k}${ph.dataset.k}</div>` : `<div class="tick-in" style="animation:none">${ph.dataset.k}</div>` }
    document.getElementById('side').innerHTML = side(v);
    toast(v);
    document.title = `€${fmt(v.equity / FX)} · ${BRAND} paper`;
  } catch (e) { document.querySelector('.live')?.classList.add('off') }
}

tick();
setInterval(tick, 5000);