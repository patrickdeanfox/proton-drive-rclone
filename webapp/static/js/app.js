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

function fileIcon(name, isDir) {
    if (isDir) return '📁';
    const ext = name.split('.').pop().toLowerCase();
    const map = {
        pdf: '📕', doc: '📄', docx: '📄', txt: '📝', md: '📝',
        jpg: '🖼️', jpeg: '🖼️', png: '🖼️', gif: '🖼️', svg: '🖼️', webp: '🖼️',
        mp4: '🎬', mkv: '🎬', avi: '🎬', mov: '🎬',
        mp3: '🎵', flac: '🎵', wav: '🎵', ogg: '🎵',
        zip: '📦', tar: '📦', gz: '📦', '7z': '📦', rar: '📦',
        py: '🐍', js: '📜', ts: '📜', html: '🌐', css: '🎨',
        sh: '⚙️', json: '📋', yml: '📋', yaml: '📋',
    };
    return map[ext] || '📄';
}

/* ─── Toast Notifications ────────────────────────────────────────── */

function showToast(message, type = 'info') {
    const container = document.getElementById('toast-container');
    if (!container) return;
    const toast = document.createElement('div');
    toast.className = `toast ${type}`;
    toast.textContent = message;
    container.appendChild(toast);
    setTimeout(() => { toast.style.opacity = '0'; toast.style.transform = 'translateX(100px)'; setTimeout(() => toast.remove(), 300); }, 4000);
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

// Close modals on overlay click
document.addEventListener('click', e => {
    if (e.target.classList.contains('modal-overlay')) {
        e.target.classList.remove('active');
    }
});

/* ═══════════════════════════════════════════════════════════════════
   Dashboard
   ═══════════════════════════════════════════════════════════════════ */

async function loadDashboard() {
    const statusEl = document.getElementById('dashboard-stats');
    const historyEl = document.getElementById('dashboard-history');
    if (!statusEl) return;

    statusEl.innerHTML = '<div class="loading-overlay"><span class="spinner"></span> Loading status...</div>';

    try {
        const status = await api('/status');
        statusEl.innerHTML = `
            <div class="stat-card">
                <div class="stat-label">rclone</div>
                <div class="stat-value ${status.rclone_installed ? 'success' : 'danger'}">${status.rclone_installed ? '✓ Installed' : '✗ Not Found'}</div>
                <div class="stat-sub">${status.rclone_version || 'Install rclone to get started'}</div>
            </div>
            <div class="stat-card">
                <div class="stat-label">Remote</div>
                <div class="stat-value ${status.remote_connected ? 'success' : 'warning'}">${status.remote_connected ? '● Connected' : '● Offline'}</div>
                <div class="stat-sub">${status.remote_name}:</div>
            </div>
            <div class="stat-card">
                <div class="stat-label">Mount</div>
                <div class="stat-value ${status.mount_active ? 'success' : 'warning'}">${status.mount_active ? '● Active' : '○ Inactive'}</div>
                <div class="stat-sub">${status.mount_dir}</div>
            </div>
            <div class="stat-card">
                <div class="stat-label">Local Files</div>
                <div class="stat-value">${status.local_files.toLocaleString()}</div>
                <div class="stat-sub">${status.local_size} in ${status.sync_dir}</div>
            </div>
            <div class="stat-card">
                <div class="stat-label">Active Schedules</div>
                <div class="stat-value">${status.active_schedules}</div>
                <div class="stat-sub">automated sync jobs</div>
            </div>
        `;
    } catch (e) {
        statusEl.innerHTML = `<div class="stat-card"><div class="stat-value danger">Error loading status</div><div class="stat-sub">${e.message}</div></div>`;
    }

    // Load history
    if (historyEl) {
        try {
            const history = await api('/sync-history');
            if (history.length === 0) {
                historyEl.innerHTML = '<div class="empty-state"><p>No sync history yet. Run a sync or set up a schedule.</p></div>';
            } else {
                historyEl.innerHTML = history.slice(0, 10).map(h => `
                    <div class="history-entry">
                        <span class="history-dot ${h.success ? 'success' : 'error'}"></span>
                        <span class="history-time">${formatDate(h.timestamp)}</span>
                        <span class="history-name">${h.job_name || 'Manual Sync'}</span>
                        <span class="badge ${h.success ? 'badge-success' : 'badge-danger'}">${h.success ? 'OK' : 'Failed'}</span>
                    </div>
                `).join('');
            }
        } catch { historyEl.innerHTML = '<div class="empty-state"><p>Could not load history</p></div>'; }
    }
}

/* ═══════════════════════════════════════════════════════════════════
   Sync Configs (Folders)
   ═══════════════════════════════════════════════════════════════════ */

let syncConfigs = [];

async function loadSyncConfigs() {
    const container = document.getElementById('sync-configs-list');
    if (!container) return;

    try {
        syncConfigs = await api('/sync-configs');
        renderSyncConfigs(container);
    } catch (e) {
        container.innerHTML = `<div class="empty-state"><p>Error: ${e.message}</p></div>`;
    }
}

function renderSyncConfigs(container) {
    if (syncConfigs.length === 0) {
        container.innerHTML = `
            <div class="empty-state">
                <div class="empty-icon">📂</div>
                <p>No sync configurations yet. Create one to start syncing folders.</p>
                <button class="btn btn-primary" onclick="openModal('config-modal')">+ New Sync Config</button>
            </div>`;
        return;
    }

    container.innerHTML = `
        <div class="table-wrap">
            <table>
                <thead>
                    <tr>
                        <th>Name</th>
                        <th>Local Path</th>
                        <th>Remote Path</th>
                        <th>Direction</th>
                        <th>Status</th>
                        <th>Actions</th>
                    </tr>
                </thead>
                <tbody>
                    ${syncConfigs.map(c => `
                        <tr>
                            <td><strong>${escHtml(c.name)}</strong></td>
                            <td style="font-family:var(--font-mono);font-size:12px;">${escHtml(c.local_path)}</td>
                            <td style="font-family:var(--font-mono);font-size:12px;">${escHtml(c.remote_path || '/')}</td>
                            <td><span class="badge badge-accent">${directionLabel(c.direction)}</span></td>
                            <td><span class="badge ${c.enabled ? 'badge-success' : 'badge-warning'}">${c.enabled ? 'Active' : 'Disabled'}</span></td>
                            <td>
                                <div class="btn-group">
                                    <button class="btn btn-sm btn-primary" onclick="runSync('${c.id}')" title="Run Now">▶ Sync</button>
                                    <button class="btn btn-sm btn-ghost" onclick="editConfig('${c.id}')" title="Edit">✏️</button>
                                    <button class="btn btn-sm btn-danger" onclick="deleteConfig('${c.id}')" title="Delete">🗑</button>
                                </div>
                            </td>
                        </tr>
                    `).join('')}
                </tbody>
            </table>
        </div>`;
}

function directionLabel(d) {
    return { bisync: '⇄ Bisync', push: '→ Push', pull: '← Pull' }[d] || d;
}

function escHtml(s) {
    const d = document.createElement('div');
    d.textContent = s || '';
    return d.innerHTML;
}

/* ─── Config Modal ───────────────────────────────────────────────── */

let editingConfigId = null;

function openNewConfig() {
    editingConfigId = null;
    document.getElementById('config-name').value = '';
    document.getElementById('config-local-path').value = '';
    document.getElementById('config-remote-path').value = '';
    document.getElementById('config-direction').value = 'bisync';
    document.getElementById('config-excludes').value = '';
    document.getElementById('config-modal-title').textContent = '📂 New Sync Configuration';
    openModal('config-modal');
    loadLocalFolderPicker('');
    loadRemoteFolderPicker('');
}

function editConfig(id) {
    const c = syncConfigs.find(x => x.id === id);
    if (!c) return;
    editingConfigId = id;
    document.getElementById('config-name').value = c.name;
    document.getElementById('config-local-path').value = c.local_path;
    document.getElementById('config-remote-path').value = c.remote_path;
    document.getElementById('config-direction').value = c.direction;
    document.getElementById('config-excludes').value = c.exclude_patterns || '';
    document.getElementById('config-modal-title').textContent = '✏️ Edit Sync Configuration';
    openModal('config-modal');
    loadLocalFolderPicker(c.local_path);
    loadRemoteFolderPicker(c.remote_path);
}

async function saveConfig() {
    const data = {
        name: document.getElementById('config-name').value.trim() || 'Untitled',
        local_path: document.getElementById('config-local-path').value.trim(),
        remote_path: document.getElementById('config-remote-path').value.trim(),
        direction: document.getElementById('config-direction').value,
        exclude_patterns: document.getElementById('config-excludes').value.trim(),
    };

    if (!data.local_path) {
        showToast('Please specify a local path', 'error');
        return;
    }

    try {
        if (editingConfigId) {
            await api(`/sync-configs/${editingConfigId}`, { method: 'PUT', body: data });
            showToast('Configuration updated', 'success');
        } else {
            await api('/sync-configs', { method: 'POST', body: data });
            showToast('Configuration created', 'success');
        }
        closeModal('config-modal');
        loadSyncConfigs();
    } catch (e) {
        showToast('Error saving config: ' + e.message, 'error');
    }
}

async function deleteConfig(id) {
    if (!confirm('Delete this sync configuration?')) return;
    try {
        await api(`/sync-configs/${id}`, { method: 'DELETE' });
        showToast('Configuration deleted', 'success');
        loadSyncConfigs();
    } catch (e) {
        showToast('Error: ' + e.message, 'error');
    }
}

async function runSync(id) {
    showToast('Sync started...', 'info');
    try {
        await api(`/sync-configs/${id}/run`, { method: 'POST' });
    } catch (e) {
        showToast('Error: ' + e.message, 'error');
    }
}

/* ─── Folder Pickers ─────────────────────────────────────────────── */

async function loadLocalFolderPicker(initialPath) {
    const el = document.getElementById('local-folder-tree');
    if (!el) return;
    const path = initialPath || '';
    el.innerHTML = '<div class="loading-overlay"><span class="spinner"></span></div>';
    try {
        const data = await api(`/browse/local/tree?path=${encodeURIComponent(path || '~')}`);
        let html = '';
        if (data.parent) {
            html += `<div class="folder-tree-item folder-tree-back" onclick="loadLocalFolderPicker('${escAttr(data.parent)}')">
                <span class="ft-icon">⬆️</span> ../ (up)
            </div>`;
        }
        html += `<div class="folder-tree-item selected" onclick="document.getElementById('config-local-path').value='${escAttr(data.path)}'">
            <span class="ft-icon">📂</span> <strong>${escHtml(data.path)}</strong> (select this)
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
            <span class="ft-icon">☁️</span> <strong>${escHtml(data.path || '/ (root)')}</strong> (select this)
        </div>`;
        data.dirs.forEach(d => {
            html += `<div class="folder-tree-item" onclick="loadRemoteFolderPicker('${escAttr(d.path)}'); document.getElementById('config-remote-path').value='${escAttr(d.path)}'">
                <span class="ft-icon">📁</span> ${escHtml(d.name)}
            </div>`;
        });
        el.innerHTML = html;
    } catch { el.innerHTML = '<div style="padding:12px;color:var(--text-muted)">Could not load remote folders (is rclone configured?)</div>'; }
}

function escAttr(s) {
    return (s || '').replace(/'/g, "\\'").replace(/"/g, '&quot;');
}

/* ═══════════════════════════════════════════════════════════════════
   Schedules
   ═══════════════════════════════════════════════════════════════════ */

let schedules = [];

async function loadSchedules() {
    const container = document.getElementById('schedules-list');
    if (!container) return;

    try {
        schedules = await api('/schedules');
        syncConfigs = await api('/sync-configs');
        renderSchedules(container);
    } catch (e) {
        container.innerHTML = `<div class="empty-state"><p>Error: ${e.message}</p></div>`;
    }
}

function renderSchedules(container) {
    if (schedules.length === 0) {
        container.innerHTML = `
            <div class="empty-state">
                <div class="empty-icon">🕐</div>
                <p>No schedules yet. Create one to automate your syncs.</p>
                <button class="btn btn-primary" onclick="openNewSchedule()">+ New Schedule</button>
            </div>`;
        return;
    }

    container.innerHTML = `
        <div class="table-wrap">
            <table>
                <thead>
                    <tr>
                        <th>Name</th>
                        <th>Sync Config</th>
                        <th>Schedule</th>
                        <th>Next Run</th>
                        <th>Enabled</th>
                        <th>Actions</th>
                    </tr>
                </thead>
                <tbody>
                    ${schedules.map(s => {
                        const cfg = syncConfigs.find(c => c.id === s.config_id);
                        return `
                        <tr>
                            <td><strong>${escHtml(s.name)}</strong></td>
                            <td>${cfg ? escHtml(cfg.name) : '<em style="color:var(--text-muted)">unknown</em>'}</td>
                            <td><span class="badge badge-accent">${scheduleDesc(s)}</span></td>
                            <td style="font-size:12px;color:var(--text-secondary)">${s.next_run ? formatRelative(s.next_run) : '—'}</td>
                            <td>
                                <label class="toggle">
                                    <input type="checkbox" ${s.enabled ? 'checked' : ''} onchange="toggleSchedule('${s.id}')">
                                    <span class="slider"></span>
                                </label>
                            </td>
                            <td>
                                <div class="btn-group">
                                    <button class="btn btn-sm btn-ghost" onclick="editSchedule('${s.id}')">✏️</button>
                                    <button class="btn btn-sm btn-danger" onclick="deleteSchedule('${s.id}')">🗑</button>
                                </div>
                            </td>
                        </tr>`;
                    }).join('')}
                </tbody>
            </table>
        </div>`;
}

function scheduleDesc(s) {
    if (s.schedule_type === 'interval') return `Every ${s.interval_minutes}m`;
    if (s.schedule_type === 'daily') return `Daily at ${s.daily_time}`;
    if (s.schedule_type === 'cron') return `Cron: ${s.cron_expression}`;
    return s.schedule_type;
}

/* ─── Schedule Modal ─────────────────────────────────────────────── */

let editingScheduleId = null;

function openNewSchedule() {
    editingScheduleId = null;
    document.getElementById('sched-name').value = '';
    document.getElementById('sched-config').value = '';
    document.getElementById('sched-type').value = 'interval';
    document.getElementById('sched-interval').value = '30';
    document.getElementById('sched-daily-time').value = '02:00';
    document.getElementById('sched-cron').value = '0 * * * *';
    document.getElementById('sched-modal-title').textContent = '🕐 New Schedule';
    updateScheduleFields();
    populateConfigDropdown();
    openModal('schedule-modal');
}

function editSchedule(id) {
    const s = schedules.find(x => x.id === id);
    if (!s) return;
    editingScheduleId = id;
    document.getElementById('sched-name').value = s.name;
    document.getElementById('sched-type').value = s.schedule_type;
    document.getElementById('sched-interval').value = s.interval_minutes || 30;
    document.getElementById('sched-daily-time').value = s.daily_time || '02:00';
    document.getElementById('sched-cron').value = s.cron_expression || '0 * * * *';
    document.getElementById('sched-modal-title').textContent = '✏️ Edit Schedule';
    updateScheduleFields();
    populateConfigDropdown(s.config_id);
    openModal('schedule-modal');
}

function populateConfigDropdown(selectedId) {
    const sel = document.getElementById('sched-config');
    sel.innerHTML = '<option value="">— Select a sync config —</option>';
    syncConfigs.forEach(c => {
        sel.innerHTML += `<option value="${c.id}" ${c.id === selectedId ? 'selected' : ''}>${escHtml(c.name)} (${escHtml(c.local_path)})</option>`;
    });
}

function updateScheduleFields() {
    const type = document.getElementById('sched-type').value;
    document.getElementById('sched-interval-group').style.display = type === 'interval' ? '' : 'none';
    document.getElementById('sched-daily-group').style.display = type === 'daily' ? '' : 'none';
    document.getElementById('sched-cron-group').style.display = type === 'cron' ? '' : 'none';
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

    if (!data.config_id) {
        showToast('Please select a sync configuration', 'error');
        return;
    }

    try {
        if (editingScheduleId) {
            await api(`/schedules/${editingScheduleId}`, { method: 'PUT', body: data });
            showToast('Schedule updated', 'success');
        } else {
            await api('/schedules', { method: 'POST', body: data });
            showToast('Schedule created', 'success');
        }
        closeModal('schedule-modal');
        loadSchedules();
    } catch (e) {
        showToast('Error: ' + e.message, 'error');
    }
}

async function deleteSchedule(id) {
    if (!confirm('Delete this schedule?')) return;
    try {
        await api(`/schedules/${id}`, { method: 'DELETE' });
        showToast('Schedule deleted', 'success');
        loadSchedules();
    } catch (e) {
        showToast('Error: ' + e.message, 'error');
    }
}

async function toggleSchedule(id) {
    try {
        await api(`/schedules/${id}/toggle`, { method: 'POST' });
        loadSchedules();
    } catch (e) {
        showToast('Error: ' + e.message, 'error');
    }
}

/* ═══════════════════════════════════════════════════════════════════
   File Browser
   ═══════════════════════════════════════════════════════════════════ */

let localBrowserPath = '';
let remoteBrowserPath = '';

async function loadFileBrowser() {
    const localPane = document.getElementById('local-file-list');
    const remotePane = document.getElementById('remote-file-list');
    if (!localPane || !remotePane) return;

    // Get default paths from config
    try {
        const config = await api('/config');
        if (!localBrowserPath) localBrowserPath = config.SYNC_DIR || '';
        browseLocal(localBrowserPath);
        browseRemote(remoteBrowserPath);
    } catch {
        browseLocal('');
        browseRemote('');
    }
}

async function browseLocal(path) {
    localBrowserPath = path;
    const listEl = document.getElementById('local-file-list');
    const crumbEl = document.getElementById('local-breadcrumb');
    if (!listEl) return;

    listEl.innerHTML = '<div class="loading-overlay"><span class="spinner"></span></div>';

    try {
        const data = await api(`/browse/local?path=${encodeURIComponent(path)}`);
        if (data.error) {
            // Fallback to home directory
            if (path && path !== '/home') {
                browseLocal('/home');
                return;
            }
            listEl.innerHTML = `<div class="empty-state"><p>${escHtml(data.error)}</p></div>`;
            return;
        }
        localBrowserPath = data.path;

        // Breadcrumb
        if (crumbEl) {
            const parts = data.path.split('/').filter(Boolean);
            let crumbs = `<span class="crumb" onclick="browseLocal('/')">/</span>`;
            let acc = '';
            parts.forEach(p => {
                acc += '/' + p;
                const a = acc;
                crumbs += `<span class="sep">/</span><span class="crumb" onclick="browseLocal('${escAttr(a)}')">${escHtml(p)}</span>`;
            });
            crumbEl.innerHTML = crumbs;
        }

        // Entries
        let html = '';
        if (data.parent) {
            html += `<div class="file-entry dir" onclick="browseLocal('${escAttr(data.parent)}')">
                <span class="file-icon">⬆️</span>
                <span class="file-name">..</span>
                <span class="file-size"></span>
                <span class="file-date"></span>
            </div>`;
        }
        if (data.entries.length === 0) {
            html += '<div class="empty-state" style="padding:24px"><p>Empty directory</p></div>';
        }
        data.entries.forEach(e => {
            html += `<div class="file-entry ${e.is_dir ? 'dir' : ''}" ${e.is_dir ? `onclick="browseLocal('${escAttr(e.path)}')"` : ''}>
                <span class="file-icon">${fileIcon(e.name, e.is_dir)}</span>
                <span class="file-name">${escHtml(e.name)}</span>
                <span class="file-size">${e.is_dir ? '' : formatSize(e.size)}</span>
                <span class="file-date">${formatDate(e.modified)}</span>
            </div>`;
        });
        listEl.innerHTML = html;
    } catch (e) {
        listEl.innerHTML = `<div class="empty-state"><p>Error: ${e.message}</p></div>`;
    }
}

async function browseRemote(path) {
    remoteBrowserPath = path;
    const listEl = document.getElementById('remote-file-list');
    const crumbEl = document.getElementById('remote-breadcrumb');
    if (!listEl) return;

    listEl.innerHTML = '<div class="loading-overlay"><span class="spinner"></span></div>';

    try {
        const data = await api(`/browse/remote?path=${encodeURIComponent(path)}`);
        if (data.error) {
            listEl.innerHTML = `<div class="empty-state"><p>${escHtml(data.error)}</p><p style="font-size:12px;margin-top:8px;color:var(--text-muted)">Make sure rclone is installed and configured with a Proton Drive remote.</p></div>`;
            return;
        }
        remoteBrowserPath = data.path === '/' ? '' : data.path;

        // Breadcrumb
        if (crumbEl) {
            const displayPath = data.path || '/';
            const parts = displayPath.split('/').filter(Boolean);
            let crumbs = `<span class="crumb" onclick="browseRemote('')">☁️ /</span>`;
            let acc = '';
            parts.forEach(p => {
                acc += (acc ? '/' : '') + p;
                const a = acc;
                crumbs += `<span class="sep">/</span><span class="crumb" onclick="browseRemote('${escAttr(a)}')">${escHtml(p)}</span>`;
            });
            crumbEl.innerHTML = crumbs;
        }

        // Entries
        let html = '';
        if (data.parent !== null && data.parent !== undefined) {
            html += `<div class="file-entry dir" onclick="browseRemote('${escAttr(data.parent || '')}')">
                <span class="file-icon">⬆️</span>
                <span class="file-name">..</span>
                <span class="file-size"></span>
                <span class="file-date"></span>
            </div>`;
        }
        if (data.entries.length === 0) {
            html += '<div class="empty-state" style="padding:24px"><p>Empty directory</p></div>';
        }
        data.entries.forEach(e => {
            html += `<div class="file-entry ${e.is_dir ? 'dir' : ''}" ${e.is_dir ? `onclick="browseRemote('${escAttr(e.path)}')"` : ''}>
                <span class="file-icon">${fileIcon(e.name, e.is_dir)}</span>
                <span class="file-name">${escHtml(e.name)}</span>
                <span class="file-size">${e.is_dir ? '' : formatSize(e.size)}</span>
                <span class="file-date">${formatDate(e.modified)}</span>
            </div>`;
        });
        listEl.innerHTML = html;
    } catch (e) {
        listEl.innerHTML = `<div class="empty-state"><p>Error loading remote: ${e.message}</p></div>`;
    }
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
    // Determine page and load data
    const path = window.location.pathname;
    if (path === '/' || path === '') loadDashboard();
    if (path === '/folders') loadSyncConfigs();
    if (path === '/schedules') loadSchedules();
    if (path === '/browser') loadFileBrowser();

    // Refresh dashboard periodically
    if (path === '/' || path === '') {
        setInterval(loadDashboard, 30000);
    }
});
