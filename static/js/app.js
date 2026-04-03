/* ─── Utility Functions ───────────────────────────────────────────── */

function formatSize(bytes) {
    if (!bytes || bytes === 0) return '—';
    const units = ['B', 'KB', 'MB', 'GB', 'TB'];
    let i = 0;
    while (bytes >= 1024 && i < units.length - 1) { bytes /= 1024; i++; }
    return `${bytes.toFixed(i > 0 ? 1 : 0)} ${units[i]}`;
}

function formatDate(iso) {
    if (!iso) return '—';
    try {
        const d = new Date(iso);
        return d.toLocaleDateString('en-US', { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' });
    } catch { return '—'; }
}

function formatRelative(iso) {
    if (!iso) return 'N/A';
    try {
        const d = new Date(iso);
        const now = new Date();
        const diff = d - now;
        if (diff < 0) return 'past';
        const mins = Math.floor(diff / 60000);
        if (mins < 60) return `in ${mins}m`;
        const hrs = Math.floor(mins / 60);
        if (hrs < 24) return `in ${hrs}h`;
        return `in ${Math.floor(hrs / 24)}d`;
    } catch { return 'N/A'; }
}

function formatElapsed(ms) {
    const secs = Math.floor(ms / 1000);
    const m = Math.floor(secs / 60);
    const s = secs % 60;
    if (m > 0) return `${m}m ${s}s`;
    return `${s}s`;
}

function escHtml(s) {
    const d = document.createElement('div');
    d.textContent = s || '';
    return d.innerHTML;
}

function escAttr(s) {
    return (s || '').replace(/'/g, "\\'").replace(/"/g, '&quot;');
}

/* ─── Toast Notifications ────────────────────────────────────────── */

function showToast(message, type = 'info') {
    const container = document.getElementById('toast-container');
    if (!container) return;
    const toast = document.createElement('div');
    toast.className = `toast ${type}`;
    toast.innerHTML = `<i class="fas fa-${type === 'success' ? 'check-circle' : type === 'error' ? 'exclamation-circle' : type === 'warn' ? 'exclamation-triangle' : 'info-circle'}" style="margin-right:6px"></i>${escHtml(message)}`;
    container.appendChild(toast);
    setTimeout(() => {
        toast.style.opacity = '0';
        toast.style.transform = 'translateX(100px)';
        setTimeout(() => toast.remove(), 300);
    }, 4000);
}

/* ─── API Helper ─────────────────────────────────────────────────── */

async function api(path, opts = {}) {
    const url = path.startsWith('http') ? path : `/api${path}`;
    const res = await fetch(url, {
        headers: { 'Content-Type': 'application/json', ...opts.headers },
        ...opts,
        body: opts.body ? JSON.stringify(opts.body) : undefined,
    });
    return res.json();
}

/* ─── Modal Helpers ──────────────────────────────────────────────── */

function openModal(id) {
    document.getElementById(id)?.classList.add('active');
}

function closeModal(id) {
    document.getElementById(id)?.classList.remove('active');
}

document.addEventListener('click', e => {
    if (e.target.classList.contains('modal-overlay')) {
        e.target.classList.remove('active');
    }
});

/* ─── Tab Helpers ─────────────────────────────────────────────────── */

function switchTab(tabGroup, tabId) {
    // Deactivate all tabs in group
    document.querySelectorAll(`[data-tab-group="${tabGroup}"] .tab`).forEach(t => t.classList.remove('active'));
    document.querySelectorAll(`[data-tab-group="${tabGroup}"] .tab-content`).forEach(t => t.classList.remove('active'));
    // Activate selected
    document.querySelector(`[data-tab-group="${tabGroup}"] .tab[data-tab="${tabId}"]`)?.classList.add('active');
    document.getElementById(tabId)?.classList.add('active');
}

/* ═══════════════════════════════════════════════════════════════════
   Global Sync State Manager
   Uses both HTTP polling AND WebSocket for real-time updates.
   ═══════════════════════════════════════════════════════════════════ */

const SyncManager = {
    _pollTimer: null,
    _activeSyncs: {},

    init() {
        this.poll();
        this._pollTimer = setInterval(() => this.poll(), 2000);

        // Also listen for WebSocket sync events for faster updates
        if (typeof ProgressManager !== 'undefined') {
            ProgressManager.onSync((event, data) => {
                if (event === 'sync_progress' && data.config_id) {
                    if (this._activeSyncs[data.config_id]) {
                        this._activeSyncs[data.config_id].progress = {
                            ...this._activeSyncs[data.config_id].progress,
                            ...data.progress
                        };
                        this.updateGlobalIndicator(this._activeSyncs);
                    }
                } else if (event === 'sync_started' && data.config_id) {
                    // Immediately show sync as running
                    this._activeSyncs[data.config_id] = {
                        running: true,
                        success: null,
                        name: data.name || data.config_id,
                        progress: {},
                        ...this._activeSyncs[data.config_id],
                    };
                    this.updateGlobalIndicator(this._activeSyncs);
                } else if (event === 'sync_completed' && data.config_id) {
                    // Immediately mark sync as done (works for both early and final completion)
                    if (this._activeSyncs[data.config_id]) {
                        this._activeSyncs[data.config_id].running = false;
                        this._activeSyncs[data.config_id].success = data.success;
                        this._activeSyncs[data.config_id].progress = {
                            ...this._activeSyncs[data.config_id].progress,
                            percent: 100,
                        };
                    }
                    this.updateGlobalIndicator(this._activeSyncs);
                    // Trigger an immediate poll to refresh full state
                    setTimeout(() => this.poll(), 500);
                }
            });
        }

        window.addEventListener('beforeunload', (e) => {
            const running = Object.values(this._activeSyncs).filter(s => s.running);
            if (running.length > 0) {
                e.preventDefault();
                e.returnValue = 'A sync is currently running.';
            }
        });
    },

    async poll() {
        try {
            const data = await api('/active-syncs');
            this._activeSyncs = data;
            this.updateGlobalIndicator(data);
        } catch {
            // Silently ignore poll failures
        }
    },

    updateGlobalIndicator(syncs) {
        const running = Object.entries(syncs).filter(([_, s]) => s.running);
        const indicator = document.getElementById('global-sync-indicator');
        if (!indicator) return;

        if (running.length === 0) {
            // Check if any just completed (show briefly)
            const justCompleted = Object.entries(syncs).filter(([_, s]) => !s.running && s.success !== null);
            if (justCompleted.length > 0) {
                const [_, last] = justCompleted[justCompleted.length - 1];
                indicator.style.display = 'flex';
                indicator.innerHTML = `
                    <div class="global-sync-icon"><i class="fas fa-${last.success ? 'check-circle' : 'times-circle'}" style="color:${last.success ? 'var(--success)' : 'var(--danger)'}"></i></div>
                    <div class="global-sync-info">
                        <div class="global-sync-name">${escHtml(last.name || 'Sync')} — ${last.success ? 'Complete' : 'Failed'}</div>
                    </div>
                `;
                // Auto-hide after 5 seconds
                clearTimeout(this._hideTimer);
                this._hideTimer = setTimeout(() => { indicator.style.display = 'none'; }, 5000);
            } else {
                indicator.style.display = 'none';
            }
            return;
        }

        clearTimeout(this._hideTimer);
        indicator.style.display = 'flex';
        const [configId, syncInfo] = running[0];
        const pct = syncInfo.progress?.percent || 0;
        const name = syncInfo.name || 'Sync';
        const speed = syncInfo.progress?.speed || '';

        indicator.innerHTML = `
            <div class="global-sync-icon"><span class="spinner" style="width:14px;height:14px;border-width:2px"></span></div>
            <div class="global-sync-info">
                <div class="global-sync-name">${escHtml(name)} ${running.length > 1 ? `<span style="opacity:0.6">+${running.length - 1}</span>` : ''}</div>
                <div class="global-sync-bar-wrap">
                    <div class="global-sync-bar">
                        <div class="global-sync-bar-fill" style="width:${pct}%"></div>
                    </div>
                    <span class="global-sync-pct">${pct}%</span>
                </div>
                ${speed ? `<div style="font-size:10px;color:var(--text-muted);margin-top:2px">${escHtml(speed)}</div>` : ''}
            </div>
        `;
    },

    getActiveSyncs() { return this._activeSyncs; },
    isRunning(configId) { return this._activeSyncs[configId]?.running || false; },
};

/* ═══════════════════════════════════════════════════════════════════
   Dashboard
   ═══════════════════════════════════════════════════════════════════ */

async function loadDashboard() {
    const statusEl = document.getElementById('dashboard-stats');
    const historyEl = document.getElementById('dashboard-history');
    if (!statusEl) return;

    // Show skeleton while loading
    statusEl.innerHTML = Array(6).fill('<div class="stat-card"><div class="skeleton skeleton-title"></div><div class="skeleton skeleton-text" style="width:80%"></div></div>').join('');

    try {
        const status = await api('/status');
        statusEl.innerHTML = `
            <div class="stat-card">
                <div class="stat-label">rclone</div>
                <div class="stat-value ${status.rclone_installed ? 'success' : 'danger'}">${status.rclone_installed ? '<i class="fas fa-check-circle"></i> Installed' : '<i class="fas fa-times-circle"></i> Not Found'}</div>
                <div class="stat-sub">${status.rclone_version || 'Install rclone to get started'}</div>
            </div>
            <div class="stat-card">
                <div class="stat-label">Remote</div>
                <div class="stat-value ${status.remote_connected ? 'success' : 'warning'}">${status.remote_connected ? '<i class="fas fa-circle"></i> Connected' : '<i class="far fa-circle"></i> Offline'}</div>
                <div class="stat-sub">${escHtml(status.remote_name)}:</div>
            </div>
            <div class="stat-card">
                <div class="stat-label">Mount</div>
                <div class="stat-value ${status.mount_active ? 'success' : 'warning'}">${status.mount_active ? '<i class="fas fa-circle"></i> Active' : '<i class="far fa-circle"></i> Inactive'}</div>
                <div class="stat-sub">${escHtml(status.mount_dir)}</div>
            </div>
            <div class="stat-card">
                <div class="stat-label">Local Files</div>
                <div class="stat-value">${status.local_files.toLocaleString()}</div>
                <div class="stat-sub">${status.local_size} in sync dir</div>
            </div>
            <div class="stat-card">
                <div class="stat-label">Schedules</div>
                <div class="stat-value">${status.active_schedules}</div>
                <div class="stat-sub">automated jobs</div>
            </div>
            <div class="stat-card">
                <div class="stat-label">Running</div>
                <div class="stat-value ${status.running_syncs > 0 ? 'success' : ''}">${status.running_syncs}</div>
                <div class="stat-sub">active transfers</div>
            </div>
        `;
    } catch (e) {
        statusEl.innerHTML = `<div class="stat-card"><div class="stat-value danger">Error loading status</div><div class="stat-sub">${e.message}</div></div>`;
    }

    if (historyEl) {
        try {
            const history = await api('/sync-history');
            if (history.length === 0) {
                historyEl.innerHTML = '<div class="empty-state"><p>No sync history yet. Run a sync to get started.</p></div>';
            } else {
                historyEl.innerHTML = history.slice(0, 10).map(h => `
                    <div class="history-entry">
                        <span class="history-dot ${h.success ? 'success' : 'error'}"></span>
                        <span class="history-time">${formatDate(h.timestamp)}</span>
                        <span class="history-name">${escHtml(h.job_name || 'Manual Sync')}</span>
                        <span class="badge ${h.success ? 'badge-success' : 'badge-danger'}">${h.success ? 'OK' : 'Failed'}</span>
                    </div>
                `).join('');
            }
        } catch { historyEl.innerHTML = '<div class="empty-state"><p>Could not load history</p></div>'; }
    }
}

/* ═══════════════════════════════════════════════════════════════════
   Sync Configs
   ═══════════════════════════════════════════════════════════════════ */

let syncConfigs = [];

async function loadSyncConfigs() {
    try {
        syncConfigs = await api('/sync-configs');
    } catch (e) {
        console.error('Failed to load sync configs:', e);
    }
    return syncConfigs;
}

function directionLabel(d) {
    return {
        bisync: '<i class="fas fa-arrows-left-right"></i> Bisync',
        push: '<i class="fas fa-arrow-right"></i> Local → Cloud',
        pull: '<i class="fas fa-arrow-left"></i> Cloud → Local'
    }[d] || d;
}

/* ─── Config Modal ───────────────────────────────────────────────── */

let editingConfigId = null;

function openNewConfig() {
    editingConfigId = null;
    const el = (id) => document.getElementById(id);
    if (el('config-name')) el('config-name').value = '';
    if (el('config-local-path')) el('config-local-path').value = '';
    if (el('config-remote-path')) el('config-remote-path').value = '';
    if (el('config-direction')) el('config-direction').value = 'push';
    if (el('config-excludes')) el('config-excludes').value = '';
    if (el('config-modal-title')) el('config-modal-title').textContent = 'New Sync Configuration';
    openModal('config-modal');
    if (typeof loadLocalFolderPicker === 'function') loadLocalFolderPicker('');
    if (typeof loadRemoteFolderPicker === 'function') loadRemoteFolderPicker('');
}

function editConfig(id) {
    const c = syncConfigs.find(x => x.id === id);
    if (!c) return;
    editingConfigId = id;
    const el = (id) => document.getElementById(id);
    if (el('config-name')) el('config-name').value = c.name;
    if (el('config-local-path')) el('config-local-path').value = c.local_path;
    if (el('config-remote-path')) el('config-remote-path').value = c.remote_path;
    if (el('config-direction')) el('config-direction').value = c.direction;
    if (el('config-excludes')) el('config-excludes').value = c.exclude_patterns || '';
    if (el('config-modal-title')) el('config-modal-title').textContent = 'Edit Sync Configuration';
    openModal('config-modal');
}

async function saveConfig() {
    const data = {
        name: document.getElementById('config-name').value.trim() || 'Untitled',
        local_path: document.getElementById('config-local-path').value.trim(),
        remote_path: document.getElementById('config-remote-path').value.trim(),
        direction: document.getElementById('config-direction').value,
        exclude_patterns: document.getElementById('config-excludes').value.trim(),
    };
    if (!data.local_path) { showToast('Please specify a local path', 'error'); return; }

    try {
        if (editingConfigId) {
            await api(`/sync-configs/${editingConfigId}`, { method: 'PUT', body: data });
            showToast('Configuration updated', 'success');
        } else {
            await api('/sync-configs', { method: 'POST', body: data });
            showToast('Configuration created', 'success');
        }
        closeModal('config-modal');
        if (typeof refreshSyncTab === 'function') refreshSyncTab();
    } catch (e) {
        showToast('Error saving config: ' + e.message, 'error');
    }
}

async function deleteConfig(id) {
    if (!confirm('Delete this sync configuration and related schedules?')) return;
    try {
        await api(`/sync-configs/${id}`, { method: 'DELETE' });
        showToast('Configuration deleted', 'success');
        if (typeof refreshSyncTab === 'function') refreshSyncTab();
    } catch (e) {
        showToast('Error: ' + e.message, 'error');
    }
}

async function runSync(id) {
    showToast('Starting sync...', 'info');
    try {
        const res = await api(`/sync-configs/${id}/run`, { method: 'POST' });
        if (!res.success) showToast(res.message || 'Failed to start', 'error');
    } catch (e) {
        showToast('Error: ' + e.message, 'error');
    }
}

/* ═══════════════════════════════════════════════════════════════════
   Schedules
   ═══════════════════════════════════════════════════════════════════ */

let schedules = [];

async function loadSchedules() {
    try {
        schedules = await api('/schedules');
    } catch (e) {
        console.error('Failed to load schedules:', e);
    }
    return schedules;
}

function scheduleDesc(s) {
    if (s.schedule_type === 'interval') return `Every ${s.interval_minutes}m`;
    if (s.schedule_type === 'daily') return `Daily at ${s.daily_time}`;
    if (s.schedule_type === 'cron') return `Cron: ${s.cron_expression}`;
    return s.schedule_type;
}

let editingScheduleId = null;

function openNewSchedule() {
    editingScheduleId = null;
    const el = (id) => document.getElementById(id);
    if (el('sched-name')) el('sched-name').value = '';
    if (el('sched-config')) el('sched-config').value = '';
    if (el('sched-type')) el('sched-type').value = 'interval';
    if (el('sched-interval')) el('sched-interval').value = '30';
    if (el('sched-daily-time')) el('sched-daily-time').value = '02:00';
    if (el('sched-cron')) el('sched-cron').value = '0 * * * *';
    if (el('sched-modal-title')) el('sched-modal-title').textContent = 'New Schedule';
    updateScheduleFields();
    populateConfigDropdown();
    openModal('schedule-modal');
}

function editSchedule(id) {
    const s = schedules.find(x => x.id === id);
    if (!s) return;
    editingScheduleId = id;
    const el = (fid) => document.getElementById(fid);
    if (el('sched-name')) el('sched-name').value = s.name;
    if (el('sched-type')) el('sched-type').value = s.schedule_type;
    if (el('sched-interval')) el('sched-interval').value = s.interval_minutes || 30;
    if (el('sched-daily-time')) el('sched-daily-time').value = s.daily_time || '02:00';
    if (el('sched-cron')) el('sched-cron').value = s.cron_expression || '0 * * * *';
    if (el('sched-modal-title')) el('sched-modal-title').textContent = 'Edit Schedule';
    updateScheduleFields();
    populateConfigDropdown(s.config_id);
    openModal('schedule-modal');
}

function populateConfigDropdown(selectedId) {
    const sel = document.getElementById('sched-config');
    if (!sel) return;
    sel.innerHTML = '<option value="">— Select a sync config —</option>';
    syncConfigs.forEach(c => {
        sel.innerHTML += `<option value="${c.id}" ${c.id === selectedId ? 'selected' : ''}>${escHtml(c.name)} (${escHtml(c.local_path)})</option>`;
    });
}

function updateScheduleFields() {
    const type = document.getElementById('sched-type')?.value;
    const el = (id) => document.getElementById(id);
    if (el('sched-interval-group')) el('sched-interval-group').style.display = type === 'interval' ? '' : 'none';
    if (el('sched-daily-group')) el('sched-daily-group').style.display = type === 'daily' ? '' : 'none';
    if (el('sched-cron-group')) el('sched-cron-group').style.display = type === 'cron' ? '' : 'none';
}

async function saveSchedule() {
    const data = {
        name: document.getElementById('sched-name').value.trim() || 'Untitled Schedule',
        config_id: document.getElementById('sched-config').value,
        schedule_type: document.getElementById('sched-type').value,
        interval_minutes: parseInt(document.getElementById('sched-interval').value) || 30,
        daily_time: document.getElementById('sched-daily-time').value,
        cron_expression: document.getElementById('sched-cron').value.trim(),
        enabled: true,
    };
    if (!data.config_id) { showToast('Please select a sync configuration', 'error'); return; }

    try {
        if (editingScheduleId) {
            await api(`/schedules/${editingScheduleId}`, { method: 'PUT', body: data });
            showToast('Schedule updated', 'success');
        } else {
            await api('/schedules', { method: 'POST', body: data });
            showToast('Schedule created', 'success');
        }
        closeModal('schedule-modal');
        if (typeof refreshSchedulesTab === 'function') refreshSchedulesTab();
    } catch (e) {
        showToast('Error: ' + e.message, 'error');
    }
}

async function deleteSchedule(id) {
    if (!confirm('Delete this schedule?')) return;
    try {
        await api(`/schedules/${id}`, { method: 'DELETE' });
        showToast('Schedule deleted', 'success');
        if (typeof refreshSchedulesTab === 'function') refreshSchedulesTab();
    } catch (e) {
        showToast('Error: ' + e.message, 'error');
    }
}

async function toggleSchedule(id) {
    try {
        await api(`/schedules/${id}/toggle`, { method: 'POST' });
        if (typeof refreshSchedulesTab === 'function') refreshSchedulesTab();
    } catch (e) {
        showToast('Error: ' + e.message, 'error');
    }
}

/* ─── Folder Pickers ─────────────────────────────────────────────── */

async function loadLocalFolderPicker(initialPath) {
    const el = document.getElementById('local-folder-tree');
    if (!el) return;
    el.innerHTML = '<div class="loading-overlay"><span class="spinner"></span></div>';
    try {
        const data = await api(`/browse/local/tree?path=${encodeURIComponent(initialPath || '~')}`);
        let html = '';
        if (data.parent) {
            html += `<div class="folder-tree-item folder-tree-back" onclick="loadLocalFolderPicker('${escAttr(data.parent)}')">
                <span class="ft-icon">⬆️</span> ../ (up)
            </div>`;
        }
        html += `<div class="folder-tree-item selected" onclick="document.getElementById('config-local-path').value='${escAttr(data.path)}'">
            <span class="ft-icon">📂</span> <strong>${escHtml(data.path)}</strong> (select)
        </div>`;
        data.dirs.forEach(d => {
            html += `<div class="folder-tree-item" onclick="loadLocalFolderPicker('${escAttr(d.path)}'); document.getElementById('config-local-path').value='${escAttr(d.path)}'">
                <span class="ft-icon">📁</span> ${escHtml(d.name)}
            </div>`;
        });
        el.innerHTML = html;
    } catch { el.innerHTML = '<div style="padding:12px;color:var(--text-muted)">Could not load folders</div>'; }
}

async function loadRemoteFolderPicker(initialPath) {
    const el = document.getElementById('remote-folder-tree');
    if (!el) return;
    el.innerHTML = '<div class="loading-overlay"><span class="spinner"></span></div>';
    try {
        const data = await api(`/browse/remote/tree?path=${encodeURIComponent(initialPath || '')}`);
        let html = '';
        if (data.parent !== null && data.parent !== undefined) {
            html += `<div class="folder-tree-item folder-tree-back" onclick="loadRemoteFolderPicker('${escAttr(data.parent || '')}')">
                <span class="ft-icon">⬆️</span> ../ (up)
            </div>`;
        }
        html += `<div class="folder-tree-item selected" onclick="document.getElementById('config-remote-path').value='${escAttr(data.path === '/' ? '' : data.path)}'">
            <span class="ft-icon">☁️</span> <strong>${escHtml(data.path || '/ (root)')}</strong> (select)
        </div>`;
        data.dirs.forEach(d => {
            html += `<div class="folder-tree-item" onclick="loadRemoteFolderPicker('${escAttr(d.path)}'); document.getElementById('config-remote-path').value='${escAttr(d.path)}'">
                <span class="ft-icon">📁</span> ${escHtml(d.name)}
            </div>`;
        });
        el.innerHTML = html;
    } catch { el.innerHTML = '<div style="padding:12px;color:var(--text-muted)">Could not load remote folders</div>'; }
}

/* ─── Quick Actions ──────────────────────────────────────────────── */

async function doMount() {
    showToast('Mounting...', 'info');
    const r = await api('/actions/mount', { method: 'POST' });
    showToast(r.success ? 'Mounted successfully' : ('Mount failed: ' + (r.error || r.output)), r.success ? 'success' : 'error');
    loadDashboard();
}

async function doUnmount() {
    showToast('Unmounting...', 'info');
    const r = await api('/actions/unmount', { method: 'POST' });
    showToast(r.success ? 'Unmounted' : ('Unmount failed: ' + (r.error || r.output)), r.success ? 'success' : 'error');
    loadDashboard();
}

async function doHealthCheck() {
    showToast('Running health check...', 'info');
    const r = await api('/actions/health', { method: 'POST' });
    alert(r.output || 'Health check completed');
}

/* ─── Page Init ──────────────────────────────────────────────────── */

document.addEventListener('DOMContentLoaded', () => {
    SyncManager.init();

    const path = window.location.pathname;
    if (path === '/' || path === '') {
        loadDashboard();
        setInterval(loadDashboard, 30000);
    }
});
