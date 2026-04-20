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
    toast.textContent = message;
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

/* ═══════════════════════════════════════════════════════════════════
   Global Sync State Manager
   Polls /api/active-syncs on every page and shows sidebar indicator.
   ═══════════════════════════════════════════════════════════════════ */

const SyncManager = {
    _pollTimer: null,
    _activeSyncs: {},

    init() {
        this.poll();
        this._pollTimer = setInterval(() => this.poll(), 2000);

        window.addEventListener('beforeunload', (e) => {
            const running = Object.values(this._activeSyncs).filter(s => s.running);
            if (running.length > 0) {
                e.preventDefault();
                e.returnValue = 'A sync is currently running. Are you sure you want to leave?';
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
            indicator.style.display = 'none';
            return;
        }

        indicator.style.display = 'flex';
        const [, syncInfo] = running[0];
        const pct = syncInfo.progress?.percent || 0;
        const name = syncInfo.name || 'Sync';

        indicator.innerHTML = `
            <div class="global-sync-icon"><span class="spinner" style="width:14px;height:14px;border-width:2px"></span></div>
            <div class="global-sync-info">
                <div class="global-sync-name">${escHtml(name)}</div>
                <div class="global-sync-bar-wrap">
                    <div class="global-sync-bar">
                        <div class="global-sync-bar-fill" style="width:${pct}%"></div>
                    </div>
                    <span class="global-sync-pct">${pct}%</span>
                </div>
            </div>
        `;
    },

    isRunning(configId) {
        return this._activeSyncs[configId]?.running || false;
    },
};

/* ─── Page Init ──────────────────────────────────────────────────── */

document.addEventListener('DOMContentLoaded', () => {
    SyncManager.init();
});
