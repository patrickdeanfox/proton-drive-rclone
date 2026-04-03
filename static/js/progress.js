/* ═══════════════════════════════════════════════════════════════════
   Progress Tracking via WebSocket (Socket.IO)
   Provides real-time progress updates for all operations.
   ═══════════════════════════════════════════════════════════════════ */

const ProgressManager = {
    socket: null,
    connected: false,
    listeners: {},   // operation_id -> [callback, ...]
    globalListeners: [],  // callbacks for ALL events

    init() {
        if (this.socket) return;
        try {
            this.socket = io('/progress', {
                transports: ['websocket', 'polling'],
                reconnection: true,
                reconnectionDelay: 1000,
                reconnectionAttempts: 10,
            });

            this.socket.on('connect', () => {
                this.connected = true;
                console.log('[Progress] WebSocket connected');
            });

            this.socket.on('disconnect', () => {
                this.connected = false;
                console.log('[Progress] WebSocket disconnected');
            });

            // Progress events
            this.socket.on('progress_update', (data) => this._dispatch('progress_update', data));
            this.socket.on('operation_started', (data) => this._dispatch('operation_started', data));
            this.socket.on('operation_completed', (data) => this._dispatch('operation_completed', data));
            this.socket.on('operation_failed', (data) => this._dispatch('operation_failed', data));
            this.socket.on('migration_log', (data) => this._dispatch('migration_log', data));
        } catch (e) {
            console.warn('[Progress] WebSocket init failed:', e);
        }
    },

    _dispatch(event, data) {
        const opId = data.operation_id || data.migration_id || '';

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

        // Tell server to join room
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
   Creates and manages progress bar UI elements.
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
            <div class="progress-header">
                <span class="progress-title">${options.title || 'Operation'}</span>
                <span class="progress-status" id="${id}-status">Starting...</span>
            </div>
            <div class="progress-bar-wrap">
                <div class="progress-bar-bg">
                    <div class="progress-bar-fill" id="${id}-fill" style="width:0%"></div>
                </div>
                <span class="progress-pct" id="${id}-pct">0%</span>
            </div>
            <div class="progress-meta">
                ${showElapsed ? `<span id="${id}-elapsed" class="progress-meta-item">⏱ 0s</span>` : ''}
                ${showEta ? `<span id="${id}-eta" class="progress-meta-item">ETA: calculating...</span>` : ''}
                ${showSpeed ? `<span id="${id}-speed" class="progress-meta-item">Speed: --</span>` : ''}
                <span id="${id}-detail" class="progress-meta-item"></span>
            </div>
            <div class="progress-message" id="${id}-msg"></div>
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

            // Color the bar based on status
            if (fill) {
                if (data.status === 'completed') fill.classList.add('completed');
                else if (data.status === 'failed') fill.classList.add('failed');
                else fill.classList.remove('completed', 'failed');
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
