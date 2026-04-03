/**
 * dashboard.js – Dashboard page logic
 */

let statusInterval = null;

document.addEventListener('DOMContentLoaded', () => {
    loadStatus();
    loadOperations();
    restoreLiveFeed();
    statusInterval = setInterval(loadStatus, 5000);

    // Listen for real-time events
    eventBus.addEventListener('ws:log', (e) => {
        const entry = e.detail;
        AppUI.appendToFeed('live-log-feed', AppUI.renderLogEntry(entry));
        cacheLiveFeedEntry(entry);
    });

    eventBus.addEventListener('ws:progress', (e) => {
        updateOperationInList(e.detail);
    });

    eventBus.addEventListener('ws:op_started', (e) => {
        loadOperations();
    });

    eventBus.addEventListener('ws:op_finished', (e) => {
        loadOperations();
        loadStatus();
    });
});

function loadStatus() {
    fetch('/api/status')
        .then(r => r.json())
        .then(data => {
            const rcloneEl = document.getElementById('status-rclone');
            rcloneEl.textContent = data.rclone_available ? 'Available' : 'Not Found';
            rcloneEl.className = 'badge ' + (data.rclone_available ? 'bg-success' : 'bg-danger');

            document.getElementById('status-ops').textContent = data.running_operations;
            document.getElementById('status-logs').textContent = data.log_count;
            document.getElementById('status-remotes').textContent = data.remotes.length;
        })
        .catch(() => {});
}

function loadOperations() {
    fetch('/api/operations')
        .then(r => r.json())
        .then(ops => {
            const container = document.getElementById('operations-list');
            const badge = document.getElementById('active-ops-badge');
            const running = ops.filter(o => o.status === 'running');
            badge.textContent = running.length;

            if (ops.length === 0) {
                container.innerHTML = `<div class="text-muted text-center py-4">
                    <i class="bi bi-inbox fs-1"></i>
                    <p class="mt-2">No operations yet. Use Quick Actions to start one.</p>
                </div>`;
                return;
            }

            // Show last 10 operations
            container.innerHTML = ops.slice(0, 10).map(op => {
                AppState.saveOperation(op); // persist
                return AppUI.renderOperationCard(op);
            }).join('');
        })
        .catch(() => {});
}

function updateOperationInList(data) {
    const card = document.querySelector(`.op-card[data-op-id="${data.op_id}"]`);
    if (card) {
        const bar = card.querySelector('.progress-bar');
        if (bar) {
            bar.style.width = data.progress + '%';
            bar.textContent = data.progress + '%';
        }
        const msg = card.querySelector('.small.text-muted');
        if (msg && data.message) {
            msg.textContent = data.message;
        }
    }
}

// Quick actions
function quickSync(direction) {
    const settings = AppState.getSettings();
    fetch('/api/sync/start', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
            direction,
            simulate: settings.demoMode !== false,
            source: settings.localPath || '~/ProtonDrive',
            dest: (settings.remoteName || 'protondrive') + ':'
        })
    }).then(r => r.json()).then(data => {
        AppUI.toast(`Sync started (${direction})`, 'info');
        AppState.setCurrentSyncOp(data.op_id);
        loadOperations();
    });
}

function quickBackup() {
    const settings = AppState.getSettings();
    fetch('/api/backup/start', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ simulate: settings.demoMode !== false })
    }).then(r => r.json()).then(() => {
        AppUI.toast('Backup started', 'info');
        loadOperations();
    });
}

function quickCleanup() {
    const settings = AppState.getSettings();
    fetch('/api/cleanup/start', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ simulate: settings.demoMode !== false })
    }).then(r => r.json()).then(() => {
        AppUI.toast('Cleanup started', 'info');
        loadOperations();
    });
}

function quickHealthCheck() {
    fetch('/api/health/check', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' }
    }).then(r => r.json()).then(() => {
        AppUI.toast('Health check started', 'info');
        loadOperations();
    });
}

// Live feed cache
function cacheLiveFeedEntry(entry) {
    const logs = AppState.getPageLogs('dashboard');
    logs.push(entry);
    AppState.savePageLogs('dashboard', logs);
}

function restoreLiveFeed() {
    const logs = AppState.getPageLogs('dashboard');
    if (logs.length > 0) {
        const feed = document.getElementById('live-log-feed');
        const placeholder = feed.querySelector('.text-muted.text-center');
        if (placeholder) placeholder.remove();
        logs.forEach(entry => {
            feed.insertAdjacentHTML('beforeend', AppUI.renderLogEntry(entry));
        });
        feed.scrollTop = feed.scrollHeight;
    }
}

function clearLiveFeed() {
    document.getElementById('live-log-feed').innerHTML =
        '<div class="text-muted text-center py-4 small">Waiting for activity...</div>';
    AppState.clearPageLogs('dashboard');
}
