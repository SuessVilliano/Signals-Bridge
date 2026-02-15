/**
 * Signal Bridge — Dashboard Application
 * Single-page app consuming the Signal Bridge REST API
 */

// ============================================================================
// State
// ============================================================================

const state = {
  currentPage: 'dashboard',
  signals: { items: [], total: 0, offset: 0, limit: 50 },
  providers: [],
  dashboardDays: 30,
  analyticsDays: 30,
  providersDays: 30,
  selectedProvider: null,
  charts: {},
};

// ============================================================================
// API Helpers
// ============================================================================

const API_BASE = '';  // Same origin

async function api(path, options = {}) {
  try {
    const res = await fetch(`${API_BASE}${path}`, {
      headers: { 'Content-Type': 'application/json', ...options.headers },
      ...options,
    });
    if (!res.ok) {
      const err = await res.json().catch(() => ({ detail: res.statusText }));
      throw new Error(err.detail || `HTTP ${res.status}`);
    }
    return await res.json();
  } catch (e) {
    console.error(`API error [${path}]:`, e);
    throw e;
  }
}

// ============================================================================
// Navigation
// ============================================================================

function navigate(page) {
  state.currentPage = page;

  // Update sidebar
  document.querySelectorAll('.nav-item').forEach(el => {
    el.classList.toggle('active', el.dataset.page === page);
  });

  // Update pages
  document.querySelectorAll('.page').forEach(el => {
    el.classList.toggle('active', el.id === `page-${page}`);
  });

  // Close mobile sidebar
  document.getElementById('sidebar').classList.remove('open');

  // Load data for the page
  switch (page) {
    case 'dashboard': loadDashboard(); break;
    case 'signals': loadSignals(); break;
    case 'analytics': loadAnalyticsProviderList(); break;
    case 'providers': loadProviders(); break;
    case 'integrations': loadIntegrations(); break;
  }

  // Update URL hash
  window.location.hash = page;
}

function toggleSidebar() {
  document.getElementById('sidebar').classList.toggle('open');
}

// ============================================================================
// Dashboard
// ============================================================================

async function loadDashboard() {
  loadDashboardKPIs();
  loadDashboardActiveSignals();
  loadDashboardRecentSignals();
  loadDashboardTopProviders();
}

async function loadDashboardKPIs() {
  try {
    // Get all signals count
    const allSignals = await api('/api/v1/signals?limit=1');
    document.getElementById('kpi-total').textContent = allSignals.total;

    // Get active signals
    const active = await api('/api/v1/signals/active/list?limit=1');
    document.getElementById('kpi-active').textContent = active.total;
    document.getElementById('active-signals-badge').textContent = active.total;

    // Try to get leaderboard for aggregate stats
    try {
      const leaderboard = await api(`/api/v1/reports/leaderboard?days=${state.dashboardDays}`);
      if (leaderboard.entries && leaderboard.entries.length > 0) {
        const avgWinRate = leaderboard.entries.reduce((s, e) => s + e.win_rate, 0) / leaderboard.entries.length;
        const totalR = leaderboard.entries.reduce((s, e) => s + e.total_r_value, 0);
        document.getElementById('kpi-winrate').textContent = avgWinRate.toFixed(1) + '%';
        document.getElementById('kpi-rvalue').textContent = totalR >= 0 ? '+' + totalR.toFixed(1) + 'R' : totalR.toFixed(1) + 'R';
      } else {
        document.getElementById('kpi-winrate').textContent = '—';
        document.getElementById('kpi-rvalue').textContent = '—';
      }
    } catch {
      document.getElementById('kpi-winrate').textContent = '—';
      document.getElementById('kpi-rvalue').textContent = '—';
    }
  } catch (e) {
    console.error('Failed to load dashboard KPIs:', e);
  }
}

async function loadDashboardActiveSignals() {
  const container = document.getElementById('dashboard-active-signals');
  try {
    const data = await api('/api/v1/signals/active/list?limit=8');
    if (!data.items || data.items.length === 0) {
      container.innerHTML = `
        <div class="empty-state">
          <i class="fas fa-signal"></i>
          <h4>No Active Signals</h4>
          <p>Signals will appear here when they are pending or active</p>
        </div>`;
      return;
    }

    container.innerHTML = `
      <table class="data-table">
        <thead>
          <tr>
            <th>Symbol</th>
            <th>Direction</th>
            <th>Entry</th>
            <th>Status</th>
            <th>RR</th>
          </tr>
        </thead>
        <tbody>
          ${data.items.map(s => `
            <tr onclick="openSignalDetail('${s.id}')">
              <td><span class="symbol-tag">${s.symbol}</span></td>
              <td>${directionBadge(s.direction)}</td>
              <td style="font-variant-numeric:tabular-nums">${formatPrice(s.entry_price)}</td>
              <td>${statusBadge(s.status)}</td>
              <td>${rrBadge(s.rr_ratio)}</td>
            </tr>
          `).join('')}
        </tbody>
      </table>`;
  } catch (e) {
    container.innerHTML = `<div class="empty-state"><p>Failed to load active signals</p></div>`;
  }
}

async function loadDashboardRecentSignals() {
  const container = document.getElementById('dashboard-recent-signals');
  try {
    const data = await api('/api/v1/signals?limit=10');
    if (!data.items || data.items.length === 0) {
      container.innerHTML = `
        <div class="empty-state">
          <i class="fas fa-inbox"></i>
          <h4>No Signals Yet</h4>
          <p>Send signals via the TradingView webhook to get started</p>
        </div>`;
      return;
    }

    container.innerHTML = `
      <table class="data-table">
        <thead>
          <tr>
            <th>Symbol</th>
            <th>Direction</th>
            <th>Entry</th>
            <th>SL</th>
            <th>TP1</th>
            <th>Status</th>
            <th>RR</th>
            <th>Time</th>
          </tr>
        </thead>
        <tbody>
          ${data.items.map(s => `
            <tr onclick="openSignalDetail('${s.id}')">
              <td><span class="symbol-tag">${s.symbol}</span></td>
              <td>${directionBadge(s.direction)}</td>
              <td style="font-variant-numeric:tabular-nums">${formatPrice(s.entry_price)}</td>
              <td style="font-variant-numeric:tabular-nums;color:var(--red)">${formatPrice(s.sl)}</td>
              <td style="font-variant-numeric:tabular-nums;color:var(--green)">${formatPrice(s.tp1)}</td>
              <td>${statusBadge(s.status)}</td>
              <td>${rrBadge(s.rr_ratio)}</td>
              <td style="color:var(--text-muted);font-size:12px">${timeAgo(s.entry_time)}</td>
            </tr>
          `).join('')}
        </tbody>
      </table>`;
  } catch (e) {
    container.innerHTML = `<div class="empty-state"><p>Failed to load recent signals</p></div>`;
  }
}

async function loadDashboardTopProviders() {
  const container = document.getElementById('dashboard-top-providers');
  try {
    const data = await api(`/api/v1/reports/leaderboard?days=${state.dashboardDays}&limit=5`);
    if (!data.entries || data.entries.length === 0) {
      container.innerHTML = `
        <div class="empty-state">
          <i class="fas fa-users"></i>
          <h4>No Provider Data</h4>
          <p>Providers with closed trades will appear here</p>
        </div>`;
      return;
    }

    container.innerHTML = data.entries.map(e => `
      <div class="webhook-item" style="cursor:pointer" onclick="navigate('analytics');setTimeout(()=>{document.getElementById('analytics-provider').value='${e.provider_id}';loadAnalytics()},100)">
        <div class="webhook-info">
          <span class="leaderboard-rank ${e.rank===1?'gold':e.rank===2?'silver':e.rank===3?'bronze':''}">${e.rank}</span>
          <div>
            <div class="webhook-name">${escapeHtml(e.provider_name)}</div>
            <div class="webhook-url">${e.total_trades} trades · ${e.win_rate}% win rate</div>
          </div>
        </div>
        <div style="text-align:right">
          <div style="font-size:14px;font-weight:600;color:${e.total_r_value >= 0 ? 'var(--green)' : 'var(--red)'}">
            ${e.total_r_value >= 0 ? '+' : ''}${e.total_r_value.toFixed(1)}R
          </div>
        </div>
      </div>
    `).join('');
  } catch (e) {
    container.innerHTML = `<div class="empty-state"><p>No provider data available</p></div>`;
  }
}

// ============================================================================
// Signals Page
// ============================================================================

let debounceTimer = null;
function debounceLoadSignals() {
  clearTimeout(debounceTimer);
  debounceTimer = setTimeout(loadSignals, 400);
}

async function loadSignals(offset = 0) {
  const container = document.getElementById('signals-table-container');
  container.innerHTML = '<div class="loading-spinner"><div class="spinner"></div></div>';

  const status = document.getElementById('filter-status').value;
  const symbol = document.getElementById('filter-symbol').value.trim().toUpperCase();
  const providerId = document.getElementById('filter-provider').value;

  let url = `/api/v1/signals?limit=50&offset=${offset}`;
  if (status) url += `&status=${status}`;
  if (symbol) url += `&symbol=${symbol}`;
  if (providerId) url += `&provider_id=${providerId}`;

  try {
    const data = await api(url);
    state.signals = data;

    if (!data.items || data.items.length === 0) {
      container.innerHTML = `
        <div class="empty-state">
          <i class="fas fa-search"></i>
          <h4>No Signals Found</h4>
          <p>Try adjusting your filters or send some signals first</p>
        </div>`;
      return;
    }

    container.innerHTML = `
      <table class="data-table">
        <thead>
          <tr>
            <th>Symbol</th>
            <th>Direction</th>
            <th>Entry</th>
            <th>SL</th>
            <th>TP1</th>
            <th>Status</th>
            <th>RR Ratio</th>
            <th>Time</th>
          </tr>
        </thead>
        <tbody>
          ${data.items.map(s => `
            <tr onclick="openSignalDetail('${s.id}')">
              <td><span class="symbol-tag">${escapeHtml(s.symbol)}</span></td>
              <td>${directionBadge(s.direction)}</td>
              <td style="font-variant-numeric:tabular-nums">${formatPrice(s.entry_price)}</td>
              <td style="font-variant-numeric:tabular-nums;color:var(--red)">${formatPrice(s.sl)}</td>
              <td style="font-variant-numeric:tabular-nums;color:var(--green)">${formatPrice(s.tp1)}</td>
              <td>${statusBadge(s.status)}</td>
              <td>${rrBadge(s.rr_ratio)}</td>
              <td style="color:var(--text-muted);font-size:12px">${timeAgo(s.entry_time)}</td>
            </tr>
          `).join('')}
        </tbody>
      </table>
      <div class="pagination">
        <button onclick="loadSignals(${Math.max(0, offset - 50)})" ${offset === 0 ? 'disabled' : ''}>
          <i class="fas fa-chevron-left"></i> Prev
        </button>
        <span class="page-info">Showing ${offset + 1}–${Math.min(offset + 50, data.total)} of ${data.total}</span>
        <button onclick="loadSignals(${offset + 50})" ${offset + 50 >= data.total ? 'disabled' : ''}>
          Next <i class="fas fa-chevron-right"></i>
        </button>
      </div>`;
  } catch (e) {
    container.innerHTML = `<div class="empty-state"><p>Failed to load signals: ${escapeHtml(e.message)}</p></div>`;
  }
}

// ============================================================================
// Signal Detail Modal
// ============================================================================

async function openSignalDetail(signalId) {
  const overlay = document.getElementById('signal-modal');
  const body = document.getElementById('modal-body');
  overlay.classList.add('visible');
  body.innerHTML = '<div class="loading-spinner"><div class="spinner"></div></div>';

  try {
    const s = await api(`/api/v1/signals/${signalId}`);
    document.getElementById('modal-title').textContent = `${s.symbol} ${s.direction} — ${s.status}`;

    body.innerHTML = `
      <!-- Price Levels -->
      <div class="price-levels">
        <div class="price-level entry">
          <div class="level-label">Entry</div>
          <div class="level-value">${formatPrice(s.entry_price)}</div>
        </div>
        <div class="price-level sl">
          <div class="level-label">Stop Loss</div>
          <div class="level-value">${formatPrice(s.sl)}</div>
        </div>
        <div class="price-level tp">
          <div class="level-label">TP1</div>
          <div class="level-value">${formatPrice(s.tp1)}</div>
        </div>
        ${s.tp2 ? `<div class="price-level tp"><div class="level-label">TP2</div><div class="level-value">${formatPrice(s.tp2)}</div></div>` : ''}
        ${s.tp3 ? `<div class="price-level tp"><div class="level-label">TP3</div><div class="level-value">${formatPrice(s.tp3)}</div></div>` : ''}
      </div>

      <!-- Detail Grid -->
      <div class="signal-detail-grid">
        <div class="detail-item">
          <label>Status</label>
          <div class="value">${statusBadge(s.status)}</div>
        </div>
        <div class="detail-item">
          <label>Direction</label>
          <div class="value">${directionBadge(s.direction)}</div>
        </div>
        <div class="detail-item">
          <label>Asset Class</label>
          <div class="value">${s.asset_class}</div>
        </div>
        <div class="detail-item">
          <label>RR Ratio</label>
          <div class="value">${rrBadge(s.rr_ratio)}</div>
        </div>
        <div class="detail-item">
          <label>Risk Distance</label>
          <div class="value">${s.risk_distance ? s.risk_distance.toFixed(2) : '—'}</div>
        </div>
        <div class="detail-item">
          <label>Strategy</label>
          <div class="value">${s.strategy_name || '—'}</div>
        </div>
        <div class="detail-item">
          <label>Entry Time</label>
          <div class="value" style="font-size:12px">${formatDateTime(s.entry_time)}</div>
        </div>
        <div class="detail-item">
          <label>Closed At</label>
          <div class="value" style="font-size:12px">${s.closed_at ? formatDateTime(s.closed_at) : '—'}</div>
        </div>
        ${s.exit_price ? `
        <div class="detail-item">
          <label>Exit Price</label>
          <div class="value">${formatPrice(s.exit_price)}</div>
        </div>` : ''}
        ${s.close_reason ? `
        <div class="detail-item">
          <label>Close Reason</label>
          <div class="value">${s.close_reason}</div>
        </div>` : ''}
      </div>

      ${s.validation_warnings && s.validation_warnings.length > 0 ? `
      <div style="background:var(--yellow-bg);border:1px solid rgba(234,179,8,0.2);border-radius:8px;padding:10px 14px;margin-bottom:16px">
        <div style="font-size:11px;font-weight:600;color:var(--yellow);margin-bottom:4px">Validation Warnings</div>
        ${s.validation_warnings.map(w => `<div style="font-size:12px;color:var(--text-secondary)">${escapeHtml(w)}</div>`).join('')}
      </div>` : ''}

      ${s.validation_errors && s.validation_errors.length > 0 ? `
      <div style="background:var(--red-bg);border:1px solid rgba(239,68,68,0.2);border-radius:8px;padding:10px 14px;margin-bottom:16px">
        <div style="font-size:11px;font-weight:600;color:var(--red);margin-bottom:4px">Validation Errors</div>
        ${s.validation_errors.map(e => `<div style="font-size:12px;color:var(--text-secondary)">${escapeHtml(e)}</div>`).join('')}
      </div>` : ''}

      <!-- Event Timeline -->
      <h4 style="font-size:13px;font-weight:600;margin-bottom:12px">Event Timeline</h4>
      ${s.events && s.events.length > 0 ? `
        <div class="event-timeline">
          ${s.events.map(ev => `
            <div class="timeline-event ${getEventClass(ev.event_type)}">
              <div class="event-type">${ev.event_type}</div>
              <div class="event-meta">
                ${ev.price ? `Price: ${formatPrice(ev.price)} · ` : ''}
                ${ev.source} · ${formatDateTime(ev.event_time)}
              </div>
            </div>
          `).join('')}
        </div>
      ` : '<div style="color:var(--text-muted);font-size:13px">No events recorded yet</div>'}
    `;
  } catch (e) {
    body.innerHTML = `<div class="empty-state"><p>Failed to load signal details: ${escapeHtml(e.message)}</p></div>`;
  }
}

function closeModal(event) {
  if (event && event.target !== event.currentTarget) return;
  document.getElementById('signal-modal').classList.remove('visible');
}

// ============================================================================
// Analytics Page
// ============================================================================

async function loadAnalyticsProviderList() {
  const select = document.getElementById('analytics-provider');
  try {
    const providers = await api('/api/v1/providers');
    state.providers = providers;

    // Keep current selection
    const current = select.value;
    select.innerHTML = '<option value="">Select Provider</option>';
    providers.forEach(p => {
      select.innerHTML += `<option value="${p.id}" ${p.id === current ? 'selected' : ''}>${escapeHtml(p.name)}</option>`;
    });

    if (current) loadAnalytics();
  } catch (e) {
    console.error('Failed to load providers for analytics:', e);
  }
}

async function loadAnalytics() {
  const providerId = document.getElementById('analytics-provider').value;
  const content = document.getElementById('analytics-content');
  const panels = document.getElementById('analytics-panels');

  if (!providerId) {
    content.style.display = '';
    panels.style.display = 'none';
    return;
  }

  content.style.display = 'none';
  panels.style.display = '';
  state.selectedProvider = providerId;

  // Load all analytics data in parallel
  Promise.all([
    loadAnalyticsKPIs(providerId),
    loadEquityCurve(providerId),
    loadWinLossChart(providerId),
  ]).catch(e => console.error('Analytics load error:', e));
}

async function loadAnalyticsKPIs(providerId) {
  const container = document.getElementById('analytics-kpis');
  try {
    const report = await api(`/api/v1/reports/performance?provider_id=${providerId}&days=${state.analyticsDays}`);
    const m = report.metrics;

    container.innerHTML = `
      <div class="kpi-card">
        <div class="kpi-label"><i class="fas fa-chart-bar"></i> Closed Trades</div>
        <div class="kpi-value">${m.closed_trades}</div>
      </div>
      <div class="kpi-card">
        <div class="kpi-label"><i class="fas fa-bullseye"></i> Win Rate</div>
        <div class="kpi-value ${m.win_rate >= 50 ? 'green' : 'red'}">${m.win_rate}%</div>
        <div class="progress-bar">
          <div class="fill green" style="width:${m.win_rate}%"></div>
        </div>
      </div>
      <div class="kpi-card">
        <div class="kpi-label"><i class="fas fa-chart-line"></i> Total R-Value</div>
        <div class="kpi-value ${m.total_r_value >= 0 ? 'green' : 'red'}">
          ${m.total_r_value >= 0 ? '+' : ''}${m.total_r_value}R
        </div>
      </div>
      <div class="kpi-card">
        <div class="kpi-label"><i class="fas fa-balance-scale"></i> Profit Factor</div>
        <div class="kpi-value ${m.profit_factor >= 1 ? 'green' : 'red'}">${m.profit_factor}</div>
      </div>
      <div class="kpi-card">
        <div class="kpi-label"><i class="fas fa-trophy"></i> Expectancy</div>
        <div class="kpi-value ${m.expectancy_per_trade >= 0 ? 'green' : 'red'}">${m.expectancy_per_trade}R</div>
      </div>
      <div class="kpi-card">
        <div class="kpi-label"><i class="fas fa-fire"></i> Best Win</div>
        <div class="kpi-value green">${m.largest_win_rr ? '+' + m.largest_win_rr + 'R' : '—'}</div>
      </div>
    `;

    // Performance metrics panel
    document.getElementById('performance-metrics').innerHTML = `
      <div class="metric-row">
        <span class="metric-label">Total Trades</span>
        <span class="metric-value">${m.total_trades}</span>
      </div>
      <div class="metric-row">
        <span class="metric-label">Winning Trades</span>
        <span class="metric-value" style="color:var(--green)">${m.winning_trades}</span>
      </div>
      <div class="metric-row">
        <span class="metric-label">Losing Trades</span>
        <span class="metric-value" style="color:var(--red)">${m.losing_trades}</span>
      </div>
      <div class="metric-row">
        <span class="metric-label">Win Rate</span>
        <span class="metric-value">${m.win_rate}%</span>
      </div>
      <div class="metric-row">
        <span class="metric-label">Average RR Ratio</span>
        <span class="metric-value">${m.avg_rr_ratio}</span>
      </div>
      <div class="metric-row">
        <span class="metric-label">Consecutive Wins</span>
        <span class="metric-value" style="color:var(--green)">${m.consecutive_wins}</span>
      </div>
      <div class="metric-row">
        <span class="metric-label">Consecutive Losses</span>
        <span class="metric-value" style="color:var(--red)">${m.consecutive_losses}</span>
      </div>
    `;

    // Risk metrics panel
    document.getElementById('risk-metrics').innerHTML = `
      <div class="metric-row">
        <span class="metric-label">Profit Factor</span>
        <span class="metric-value ${m.profit_factor >= 1 ? '' : 'style=color:var(--red)'}">${m.profit_factor}</span>
      </div>
      <div class="metric-row">
        <span class="metric-label">Total R-Value</span>
        <span class="metric-value" style="color:${m.total_r_value >= 0 ? 'var(--green)' : 'var(--red)'}">
          ${m.total_r_value >= 0 ? '+' : ''}${m.total_r_value}R
        </span>
      </div>
      <div class="metric-row">
        <span class="metric-label">Expectancy per Trade</span>
        <span class="metric-value">${m.expectancy_per_trade}R</span>
      </div>
      <div class="metric-row">
        <span class="metric-label">Largest Win</span>
        <span class="metric-value" style="color:var(--green)">${m.largest_win_rr ? '+' + m.largest_win_rr + 'R' : '—'}</span>
      </div>
      <div class="metric-row">
        <span class="metric-label">Largest Loss</span>
        <span class="metric-value" style="color:var(--red)">-${m.largest_loss_rr}R</span>
      </div>
      <div class="metric-row">
        <span class="metric-label">Loss Rate</span>
        <span class="metric-value">${m.loss_rate}%</span>
      </div>
    `;
  } catch (e) {
    container.innerHTML = `<div class="empty-state"><p>Failed to load performance data</p></div>`;
  }
}

async function loadEquityCurve(providerId) {
  try {
    const data = await api(`/api/v1/reports/equity-curve?provider_id=${providerId}&days=${state.analyticsDays}`);

    // Destroy existing chart
    if (state.charts.equity) state.charts.equity.destroy();

    const ctx = document.getElementById('equity-chart').getContext('2d');
    const points = data.points || [];

    if (points.length === 0) {
      ctx.canvas.parentElement.innerHTML = '<div class="empty-state"><p>No equity data available</p></div>';
      return;
    }

    const labels = points.map(p => {
      const d = new Date(p.timestamp);
      return d.toLocaleDateString('en-US', { month: 'short', day: 'numeric' });
    });
    const values = points.map(p => p.cumulative_r_value);

    // Gradient fill
    const gradient = ctx.createLinearGradient(0, 0, 0, 280);
    const isPositive = values[values.length - 1] >= 0;
    if (isPositive) {
      gradient.addColorStop(0, 'rgba(34, 197, 94, 0.2)');
      gradient.addColorStop(1, 'rgba(34, 197, 94, 0)');
    } else {
      gradient.addColorStop(0, 'rgba(239, 68, 68, 0.2)');
      gradient.addColorStop(1, 'rgba(239, 68, 68, 0)');
    }

    state.charts.equity = new Chart(ctx, {
      type: 'line',
      data: {
        labels,
        datasets: [{
          label: 'Cumulative R-Value',
          data: values,
          borderColor: isPositive ? '#22c55e' : '#ef4444',
          backgroundColor: gradient,
          fill: true,
          tension: 0.3,
          pointRadius: points.length > 30 ? 0 : 3,
          pointHoverRadius: 5,
          pointBackgroundColor: isPositive ? '#22c55e' : '#ef4444',
          borderWidth: 2,
        }]
      },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        plugins: {
          legend: { display: false },
          tooltip: {
            backgroundColor: '#1e2235',
            borderColor: '#2a2f42',
            borderWidth: 1,
            titleColor: '#e4e6f0',
            bodyColor: '#8b8fa3',
            padding: 10,
            callbacks: {
              label: (ctx) => `${ctx.parsed.y >= 0 ? '+' : ''}${ctx.parsed.y.toFixed(2)}R`
            }
          }
        },
        scales: {
          x: {
            grid: { color: 'rgba(42, 47, 66, 0.3)' },
            ticks: { color: '#5c6078', font: { size: 11 }, maxTicksLimit: 10 },
          },
          y: {
            grid: { color: 'rgba(42, 47, 66, 0.3)' },
            ticks: {
              color: '#5c6078',
              font: { size: 11 },
              callback: (v) => `${v >= 0 ? '+' : ''}${v}R`
            },
          }
        },
        interaction: { intersect: false, mode: 'index' },
      }
    });
  } catch (e) {
    console.error('Failed to load equity curve:', e);
  }
}

async function loadWinLossChart(providerId) {
  try {
    const report = await api(`/api/v1/reports/performance?provider_id=${providerId}&days=${state.analyticsDays}`);
    const m = report.metrics;

    // Destroy existing chart
    if (state.charts.winloss) state.charts.winloss.destroy();

    const ctx = document.getElementById('winloss-chart').getContext('2d');

    if (m.closed_trades === 0) {
      ctx.canvas.parentElement.innerHTML = '<div class="empty-state"><p>No closed trades</p></div>';
      return;
    }

    state.charts.winloss = new Chart(ctx, {
      type: 'doughnut',
      data: {
        labels: ['Wins', 'Losses'],
        datasets: [{
          data: [m.winning_trades, m.losing_trades],
          backgroundColor: ['#22c55e', '#ef4444'],
          borderColor: ['#1a1d29', '#1a1d29'],
          borderWidth: 3,
          hoverOffset: 4,
        }]
      },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        cutout: '70%',
        plugins: {
          legend: {
            position: 'bottom',
            labels: {
              color: '#8b8fa3',
              font: { size: 12 },
              padding: 16,
              usePointStyle: true,
              pointStyleWidth: 10,
            }
          },
          tooltip: {
            backgroundColor: '#1e2235',
            borderColor: '#2a2f42',
            borderWidth: 1,
            titleColor: '#e4e6f0',
            bodyColor: '#8b8fa3',
          }
        }
      }
    });

    // Stats below chart
    document.getElementById('winloss-stats').innerHTML = `
      <div style="display:flex;justify-content:space-around;margin-top:12px">
        <div style="text-align:center">
          <div style="font-size:20px;font-weight:700;color:var(--green)">${m.winning_trades}</div>
          <div style="font-size:11px;color:var(--text-muted)">Wins</div>
        </div>
        <div style="text-align:center">
          <div style="font-size:20px;font-weight:700;color:var(--red)">${m.losing_trades}</div>
          <div style="font-size:11px;color:var(--text-muted)">Losses</div>
        </div>
      </div>
    `;
  } catch (e) {
    console.error('Failed to load win/loss chart:', e);
  }
}

// ============================================================================
// Providers Page
// ============================================================================

async function loadProviders() {
  loadLeaderboard();
  loadProviderCards();
}

async function loadLeaderboard() {
  const container = document.getElementById('leaderboard-container');
  try {
    const data = await api(`/api/v1/reports/leaderboard?days=${state.providersDays}&limit=20`);

    if (!data.entries || data.entries.length === 0) {
      container.innerHTML = `
        <div class="empty-state">
          <i class="fas fa-trophy"></i>
          <h4>No Leaderboard Data</h4>
          <p>Providers need closed trades to appear on the leaderboard</p>
        </div>`;
      return;
    }

    container.innerHTML = `
      <table class="data-table">
        <thead>
          <tr>
            <th>Rank</th>
            <th>Provider</th>
            <th>Trades</th>
            <th>Win Rate</th>
            <th>Total R</th>
            <th>Expectancy</th>
          </tr>
        </thead>
        <tbody>
          ${data.entries.map(e => `
            <tr>
              <td>
                <span class="leaderboard-rank ${e.rank===1?'gold':e.rank===2?'silver':e.rank===3?'bronze':''}">
                  ${e.rank}
                </span>
              </td>
              <td style="font-weight:600">${escapeHtml(e.provider_name)}</td>
              <td>${e.total_trades}</td>
              <td>
                <span style="color:${e.win_rate >= 50 ? 'var(--green)' : 'var(--red)'}">${e.win_rate}%</span>
              </td>
              <td>
                <span style="font-weight:600;color:${e.total_r_value >= 0 ? 'var(--green)' : 'var(--red)'}">
                  ${e.total_r_value >= 0 ? '+' : ''}${e.total_r_value.toFixed(1)}R
                </span>
              </td>
              <td>
                <span style="color:${e.expectancy >= 0 ? 'var(--green)' : 'var(--red)'}">
                  ${e.expectancy >= 0 ? '+' : ''}${e.expectancy.toFixed(4)}R
                </span>
              </td>
            </tr>
          `).join('')}
        </tbody>
      </table>`;
  } catch (e) {
    container.innerHTML = `<div class="empty-state"><p>Failed to load leaderboard</p></div>`;
  }
}

async function loadProviderCards() {
  const container = document.getElementById('providers-grid');
  try {
    const providers = await api('/api/v1/providers');

    if (!providers || providers.length === 0) {
      container.innerHTML = `
        <div class="empty-state" style="grid-column:1/-1">
          <i class="fas fa-users"></i>
          <h4>No Providers</h4>
          <p>Create a provider via the API to get started</p>
        </div>`;
      return;
    }

    container.innerHTML = providers.map(p => `
      <div class="kpi-card" style="cursor:pointer" onclick="navigate('analytics');setTimeout(()=>{document.getElementById('analytics-provider').value='${p.id}';loadAnalytics()},100)">
        <div class="kpi-label">
          <i class="fas fa-user"></i>
          ${escapeHtml(p.name)}
        </div>
        <div style="display:flex;justify-content:space-between;align-items:end;margin-top:8px">
          <div>
            <div style="font-size:22px;font-weight:700">${p.total_signals || 0}</div>
            <div style="font-size:11px;color:var(--text-muted)">Total Signals</div>
          </div>
          <div style="text-align:right">
            <div style="font-size:16px;font-weight:600;color:var(--blue)">${p.active_signals || 0}</div>
            <div style="font-size:11px;color:var(--text-muted)">Active</div>
          </div>
        </div>
        <div style="font-size:11px;color:var(--text-muted);margin-top:8px">
          ${p.is_active ? '<span style="color:var(--green)">Active</span>' : '<span style="color:var(--red)">Inactive</span>'}
          · Since ${new Date(p.created_at).toLocaleDateString()}
        </div>
      </div>
    `).join('');
  } catch (e) {
    container.innerHTML = `<div class="empty-state" style="grid-column:1/-1"><p>Failed to load providers</p></div>`;
  }
}

// ============================================================================
// Integrations Page
// ============================================================================

async function loadIntegrations() {
  loadWebhookProviderDropdown();
  loadWebhooks();
}

async function loadWebhookProviderDropdown() {
  const select = document.getElementById('webhook-provider');
  try {
    const providers = await api('/api/v1/providers');
    const current = select.value;
    select.innerHTML = '<option value="">Select provider...</option>';
    providers.forEach(p => {
      select.innerHTML += `<option value="${p.id}" ${p.id === current ? 'selected' : ''}>${escapeHtml(p.name)}</option>`;
    });
  } catch (e) {
    console.error('Failed to load providers for webhooks:', e);
  }
}

async function loadWebhooks() {
  const container = document.getElementById('webhooks-list');
  try {
    const data = await api('/api/v1/webhooks/outbound');

    if (!data.items || data.items.length === 0) {
      container.innerHTML = `
        <div class="empty-state">
          <i class="fas fa-link"></i>
          <h4>No Webhooks Configured</h4>
          <p>Add a webhook to start forwarding signal events</p>
        </div>`;
      return;
    }

    container.innerHTML = data.items.map(w => `
      <div class="webhook-item">
        <div class="webhook-info">
          <div>
            <div class="webhook-name">
              ${w.is_active ? '<span class="status-dot online" style="width:6px;height:6px"></span>' : '<span class="status-dot offline" style="width:6px;height:6px"></span>'}
              ${escapeHtml(w.name)}
            </div>
            <div class="webhook-url">${escapeHtml(w.url)}</div>
            <div style="margin-top:4px">
              ${w.event_types.map(t => `<span class="status-badge active" style="font-size:9px;padding:1px 6px;margin-right:2px">${t}</span>`).join('')}
            </div>
            ${w.consecutive_failures > 0 ? `<div style="color:var(--red);font-size:11px;margin-top:4px">${w.consecutive_failures} consecutive failures</div>` : ''}
          </div>
        </div>
        <div class="webhook-actions">
          <button class="btn btn-sm btn-danger" onclick="deleteWebhook('${w.id}')" title="Delete">
            <i class="fas fa-trash"></i>
          </button>
        </div>
      </div>
    `).join('');
  } catch (e) {
    container.innerHTML = `<div class="empty-state"><p>Failed to load webhooks</p></div>`;
  }
}

async function createWebhook(event) {
  event.preventDefault();

  const providerId = document.getElementById('webhook-provider').value;
  const name = document.getElementById('webhook-name').value;
  const url = document.getElementById('webhook-url').value;
  const headersRaw = document.getElementById('webhook-headers').value.trim();

  // Get selected event types
  const eventTypes = [];
  document.querySelectorAll('#event-type-checkboxes input[type=checkbox]:checked').forEach(cb => {
    eventTypes.push(cb.value);
  });

  if (eventTypes.length === 0) {
    showToast('Please select at least one event type', 'error');
    return;
  }

  let headers = null;
  if (headersRaw) {
    try {
      headers = JSON.parse(headersRaw);
    } catch {
      showToast('Invalid JSON in headers field', 'error');
      return;
    }
  }

  try {
    await api('/api/v1/webhooks/outbound', {
      method: 'POST',
      body: JSON.stringify({
        provider_id: providerId,
        name,
        url,
        event_types: eventTypes,
        headers,
      }),
    });

    showToast('Webhook created successfully!', 'success');
    document.getElementById('webhook-form').reset();
    loadWebhooks();
  } catch (e) {
    showToast(`Failed to create webhook: ${e.message}`, 'error');
  }
}

async function deleteWebhook(webhookId) {
  if (!confirm('Delete this webhook?')) return;

  try {
    await api(`/api/v1/webhooks/outbound/${webhookId}`, { method: 'DELETE' });
    showToast('Webhook deleted', 'info');
    loadWebhooks();
  } catch (e) {
    showToast(`Failed to delete webhook: ${e.message}`, 'error');
  }
}

// ============================================================================
// Health Check
// ============================================================================

async function checkHealth() {
  const dot = document.getElementById('health-dot');
  const text = document.getElementById('health-text');
  try {
    const data = await api('/health');
    if (data.status === 'ok') {
      dot.className = 'status-dot online';
      text.textContent = 'System Online';
    } else {
      dot.className = 'status-dot offline';
      text.textContent = 'System Issues';
    }
  } catch {
    dot.className = 'status-dot offline';
    text.textContent = 'Disconnected';
  }
}

// ============================================================================
// Provider filter dropdown (signals page)
// ============================================================================

async function loadProviderFilter() {
  const select = document.getElementById('filter-provider');
  try {
    const providers = await api('/api/v1/providers');
    providers.forEach(p => {
      select.innerHTML += `<option value="${p.id}">${escapeHtml(p.name)}</option>`;
    });
  } catch (e) {
    console.error('Failed to load provider filter:', e);
  }
}

// ============================================================================
// Utility Functions
// ============================================================================

function statusBadge(status) {
  const map = {
    PENDING: 'pending',
    ACTIVE: 'active',
    TP1_HIT: 'tp1',
    TP2_HIT: 'tp2',
    TP3_HIT: 'tp3',
    SL_HIT: 'sl',
    CLOSED: 'closed',
    INVALID: 'invalid',
  };
  const cls = map[status] || 'closed';
  return `<span class="status-badge ${cls}">${status}</span>`;
}

function directionBadge(direction) {
  if (direction === 'LONG') {
    return '<span class="direction-badge long"><i class="fas fa-arrow-up"></i> LONG</span>';
  }
  return '<span class="direction-badge short"><i class="fas fa-arrow-down"></i> SHORT</span>';
}

function rrBadge(rr) {
  if (rr === null || rr === undefined) return '<span class="rr-value neutral">—</span>';
  const cls = rr >= 2 ? 'good' : rr >= 1 ? 'neutral' : 'bad';
  return `<span class="rr-value ${cls}">${rr.toFixed(2)}</span>`;
}

function formatPrice(price) {
  if (price === null || price === undefined) return '—';
  // Smart formatting based on price magnitude
  if (price >= 1000) return price.toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 });
  if (price >= 1) return price.toFixed(4);
  return price.toFixed(6);
}

function formatDateTime(iso) {
  if (!iso) return '—';
  const d = new Date(iso);
  return d.toLocaleString('en-US', {
    month: 'short', day: 'numeric', year: 'numeric',
    hour: '2-digit', minute: '2-digit',
  });
}

function timeAgo(iso) {
  if (!iso) return '—';
  const diff = Date.now() - new Date(iso).getTime();
  const mins = Math.floor(diff / 60000);
  if (mins < 1) return 'just now';
  if (mins < 60) return `${mins}m ago`;
  const hours = Math.floor(mins / 60);
  if (hours < 24) return `${hours}h ago`;
  const days = Math.floor(hours / 24);
  if (days < 7) return `${days}d ago`;
  return formatDateTime(iso);
}

function getEventClass(eventType) {
  if (eventType.includes('ENTRY')) return 'entry';
  if (eventType.includes('TP')) return 'tp';
  if (eventType.includes('SL')) return 'sl';
  if (eventType.includes('CLOSE') || eventType.includes('EXPIRED')) return 'close';
  return '';
}

function escapeHtml(str) {
  if (!str) return '';
  const div = document.createElement('div');
  div.textContent = str;
  return div.innerHTML;
}

function showToast(message, type = 'info') {
  const container = document.getElementById('toast-container');
  const toast = document.createElement('div');
  toast.className = `toast ${type}`;
  const icon = type === 'success' ? 'check-circle' : type === 'error' ? 'exclamation-circle' : 'info-circle';
  toast.innerHTML = `<i class="fas fa-${icon}"></i> ${escapeHtml(message)}`;
  container.appendChild(toast);
  setTimeout(() => {
    toast.style.opacity = '0';
    toast.style.transform = 'translateX(100%)';
    toast.style.transition = 'all 0.3s ease';
    setTimeout(() => toast.remove(), 300);
  }, 4000);
}

// ============================================================================
// Time Selector Handlers
// ============================================================================

function setupTimeSelectors() {
  // Dashboard time selector
  document.querySelectorAll('#dashboard-time-selector button').forEach(btn => {
    btn.addEventListener('click', () => {
      document.querySelectorAll('#dashboard-time-selector button').forEach(b => b.classList.remove('active'));
      btn.classList.add('active');
      state.dashboardDays = parseInt(btn.dataset.days);
      loadDashboardKPIs();
      loadDashboardTopProviders();
    });
  });

  // Analytics time selector
  document.querySelectorAll('#analytics-time-selector button').forEach(btn => {
    btn.addEventListener('click', () => {
      document.querySelectorAll('#analytics-time-selector button').forEach(b => b.classList.remove('active'));
      btn.classList.add('active');
      state.analyticsDays = parseInt(btn.dataset.days);
      if (state.selectedProvider) loadAnalytics();
    });
  });

  // Providers time selector
  document.querySelectorAll('#providers-time-selector button').forEach(btn => {
    btn.addEventListener('click', () => {
      document.querySelectorAll('#providers-time-selector button').forEach(b => b.classList.remove('active'));
      btn.classList.add('active');
      state.providersDays = parseInt(btn.dataset.days);
      loadLeaderboard();
    });
  });
}

// ============================================================================
// Keyboard shortcut: ESC to close modal
// ============================================================================

document.addEventListener('keydown', (e) => {
  if (e.key === 'Escape') closeModal();
});

// ============================================================================
// Initialization
// ============================================================================

async function init() {
  // Handle hash navigation
  const hash = window.location.hash.replace('#', '');
  if (['dashboard', 'signals', 'analytics', 'providers', 'integrations'].includes(hash)) {
    navigate(hash);
  } else {
    navigate('dashboard');
  }

  // Setup time selectors
  setupTimeSelectors();

  // Load provider filter
  loadProviderFilter();

  // Health check
  checkHealth();
  setInterval(checkHealth, 30000);

  // Auto-refresh active data every 60 seconds
  setInterval(() => {
    if (state.currentPage === 'dashboard') {
      loadDashboardKPIs();
      loadDashboardActiveSignals();
    }
  }, 60000);
}

// Start the app
init();
