/* ═══════════════════════════════════════════════════════════════════
   Progress Tracking via WebSocket (Socket.IO)
   Provides real-time progress updates for all operations.
   ═══════════════════════════════════════════════════════════════════ */

const ProgressManager = {
    socket: null,
    connected: false,
    listeners: {},        // operation_id -> [callback, ...]
    globalListeners: [],  // callbacks for ALL events
    syncListeners: [],    // callbacks for sync_progress events
    _reconnectCount: 0,

    init() {
        if (this.socket) return;
        try {
            this.socket = io('/progress', {
                transports: ['websocket', 'polling'],
                reconnection: true,
                reconnectionDelay: 500,
                reconnectionDelayMax: 5000,
                reconnectionAttempts: Infinity,
                timeout: 10000,
            });

            this.socket.on('connect', () => {
                this.connected = true;
                this._reconnectCount = 0;
                this._updateWsIndicator(true);
                console.log('[WS] Connected');
            });

            this.socket.on('disconnect', () => {
                this.connected = false;
                this._updateWsIndicator(false);
                console.log('[WS] Disconnected');
            });

            this.socket.on('reconnect_attempt', () => {
                this._reconnectCount++;
            });

            // Progress events
            this.socket.on('progress_update', (data) => this._dispatch('progress_update', data));
            this.socket.on('operation_started', (data) => this._dispatch('operation_started', data));
            this.socket.on('operation_completed', (data) => this._dispatch('operation_completed', data));
            this.socket.on('operation_failed', (data) => this._dispatch('operation_failed', data));
            this.socket.on('migration_log', (data) => this._dispatch('migration_log', data));

            // Sync-specific events (broadcast from _run_rclone_streaming)
            this.socket.on('sync_progress', (data) => {
                this.syncListeners.forEach(cb => {
                    try { cb('sync_progress', data); } catch(e) { console.error(e); }
                });
                this._dispatch('sync_progress', data);
            });
            this.socket.on('sync_started', (data) => {
                this.syncListeners.forEach(cb => {
                    try { cb('sync_started', data); } catch(e) { console.error(e); }
                });
                this._dispatch('sync_started', data);
            });
            this.socket.on('sync_completed', (data) => {
                this.syncListeners.forEach(cb => {
                    try { cb('sync_completed', data); } catch(e) { console.error(e); }
                });
                this._dispatch('sync_completed', data);
            });
        } catch (e) {
            console.warn('[WS] Init failed:', e);
        }
    },

    _updateWsIndicator(connected) {
        const dot = document.getElementById('ws-dot');
        const label = document.getElementById('ws-label');
        if (dot) {
            dot.className = 'ws-dot' + (connected ? ' connected' : '');
        }
        if (label) {
            label.textContent = connected ? 'Live' : 'Reconnecting...';
        }
    },

    _dispatch(event, data) {
        const opId = data.operation_id || data.migration_id || data.config_id || '';

        // Notify operation-specific listeners
        if (opId && this.listeners[opId]) {
            this.listeners[opId].forEach(cb => {
                try { cb(event, data); } catch(e) { console.error(e); }
            });
        }

        // Notify global listeners
        this.globalListeners.forEach(cb => {
            try { cb(event, data); } catch(e) { console.error(e); }
        });
    },

    subscribe(operationId, callback) {
        if (!this.listeners[operationId]) {
            this.listeners[operationId] = [];
        }
        this.listeners[operationId].push(callback);
        if (this.socket) {
            this.socket.emit('subscribe', { operation_id: operationId });
        }
    },

    unsubscribe(operationId) {
        delete this.listeners[operationId];
        if (this.socket) {
            this.socket.emit('unsubscribe', { operation_id: operationId });
        }
    },

    onSync(callback) {
        this.syncListeners.push(callback);
    },

    onAny(callback) {
        this.globalListeners.push(callback);
    },

    removeGlobalListener(callback) {
        this.globalListeners = this.globalListeners.filter(cb => cb !== callback);
    },
};

// Auto-init when DOM is ready
document.addEventListener('DOMContentLoaded', () => {
    ProgressManager.init();
});

/* ═══════════════════════════════════════════════════════════════════
   Progress Bar Component
   ═══════════════════════════════════════════════════════════════════ */

function createProgressBar(containerId, options = {}) {
    const container = document.getElementById(containerId);
    if (!container) return null;

    const id = options.id || 'pb-' + Math.random().toString(36).substr(2, 6);
    const showEta = options.showEta !== false;
    const showSpeed = options.showSpeed !== false;
    const showElapsed = options.showElapsed !== false;

    container.innerHTML = `
        <div class="progress-tracker" id="${id}">
            <div class="progress-header" style="display:flex;justify-content:space-between;align-items:center;margin-bottom:8px">
                <span class="progress-title" style="font-weight:600;font-size:13px">${options.title || 'Operation'}</span>
                <span class="progress-status" id="${id}-status" style="font-size:12px;color:var(--text-muted)">Starting...</span>
            </div>
            <div style="display:flex;align-items:center;gap:10px;margin-bottom:8px">
                <div class="progress-bar-container" style="flex:1">
                    <div class="progress-bar-fill" id="${id}-fill" style="width:0%"></div>
                </div>
                <span class="progress-pct" id="${id}-pct" style="font-size:13px;font-weight:700;font-family:var(--font-mono);color:var(--proton-purple-light);min-width:40px;text-align:right">0%</span>
            </div>
            <div style="display:flex;gap:16px;flex-wrap:wrap;font-size:12px;color:var(--text-muted)">
                ${showElapsed ? `<span id="${id}-elapsed">⏱ 0s</span>` : ''}
                ${showEta ? `<span id="${id}-eta">ETA: calculating...</span>` : ''}
                ${showSpeed ? `<span id="${id}-speed">Speed: --</span>` : ''}
                <span id="${id}-detail"></span>
            </div>
            <div id="${id}-msg" style="margin-top:6px;font-size:12px;color:var(--text-secondary)"></div>
        </div>
    `;

    return {
        id,
        update(data) {
            const fill = document.getElementById(`${id}-fill`);
            const pct = document.getElementById(`${id}-pct`);
            const status = document.getElementById(`${id}-status`);
            const msg = document.getElementById(`${id}-msg`);
            const elapsed = document.getElementById(`${id}-elapsed`);
            const eta = document.getElementById(`${id}-eta`);
            const speed = document.getElementById(`${id}-speed`);
            const detail = document.getElementById(`${id}-detail`);

            if (fill) fill.style.width = `${data.percent || 0}%`;
            if (pct) pct.textContent = `${Math.round(data.percent || 0)}%`;
            if (status) status.textContent = data.status || 'running';
            if (msg && data.message) msg.textContent = data.message;

            if (elapsed && data.elapsed != null) {
                elapsed.textContent = `⏱ ${formatDuration(data.elapsed)}`;
            }
            if (eta && data.eta != null) {
                eta.textContent = `ETA: ${formatDuration(data.eta)}`;
            } else if (eta) {
                eta.textContent = data.percent >= 100 ? 'Done' : 'ETA: calculating...';
            }
            if (speed && data.speed != null) {
                const unit = data.unit === 'bytes' ? formatTransferSpeed(data.speed) :
                             `${data.speed.toFixed(1)} ${data.unit || 'items'}/s`;
                speed.textContent = `Speed: ${unit}`;
            }
            if (detail && data.current != null && data.total) {
                if (data.unit === 'bytes') {
                    detail.textContent = `${formatSizeCompact(data.current)} / ${formatSizeCompact(data.total)}`;
                } else {
                    detail.textContent = `${data.current} / ${data.total} ${data.unit || ''}`;
                }
            }

            if (fill) {
                if (data.status === 'completed') fill.classList.add('complete');
                else if (data.status === 'failed') fill.classList.add('error');
                else fill.classList.remove('complete', 'error');
            }
        },
        destroy() {
            container.innerHTML = '';
        }
    };
}

function formatDuration(seconds) {
    if (seconds == null || seconds < 0) return '--';
    seconds = Math.round(seconds);
    if (seconds === 0) return '0s';
    if (seconds < 60) return `${seconds}s`;
    const m = Math.floor(seconds / 60);
    const s = seconds % 60;
    if (m < 60) return `${m}m ${s}s`;
    const h = Math.floor(m / 60);
    return `${h}h ${m % 60}m`;
}

function formatSizeCompact(bytes) {
    if (!bytes || bytes === 0) return '0 B';
    const units = ['B', 'KB', 'MB', 'GB', 'TB'];
    let i = 0;
    while (bytes >= 1024 && i < units.length - 1) { bytes /= 1024; i++; }
    return `${bytes.toFixed(i > 0 ? 1 : 0)} ${units[i]}`;
}

function formatTransferSpeed(bytesPerSec) {
    if (!bytesPerSec) return '--';
    return formatSizeCompact(bytesPerSec) + '/s';
}
