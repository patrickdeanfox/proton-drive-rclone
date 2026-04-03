/* ═══════════════════════════════════════════════════════════
   AI Organizer — shared JS utilities
   ═══════════════════════════════════════════════════════════ */

/** Shorthand for getElementById */
function el(id) { return document.getElementById(id); }

/** POST JSON helper */
async function postJSON(url, data) {
    try {
        const r = await fetch(url, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(data),
        });
        return await r.json();
    } catch (e) {
        return { ok: false, error: e.message };
    }
}

/** PUT JSON helper */
async function putJSON(url, data) {
    try {
        const r = await fetch(url, {
            method: 'PUT',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(data),
        });
        return await r.json();
    } catch (e) {
        return { ok: false, error: e.message };
    }
}

/** Show a toast notification */
function showToast(message, type) {
    type = type || 'info';
    const container = document.getElementById('toast-container');
    if (!container) return;
    const toast = document.createElement('div');
    toast.className = 'toast toast-' + type;
    toast.textContent = message;
    container.appendChild(toast);
    setTimeout(() => {
        toast.style.opacity = '0';
        toast.style.transform = 'translateX(100%)';
        toast.style.transition = 'all 0.3s ease';
        setTimeout(() => toast.remove(), 300);
    }, 4000);
}

/** Format bytes to human-readable */
function formatBytes(bytes) {
    if (!bytes) return '0 B';
    const units = ['B', 'KB', 'MB', 'GB', 'TB'];
    let i = 0;
    while (bytes >= 1024 && i < units.length - 1) { bytes /= 1024; i++; }
    return bytes.toFixed(1) + ' ' + units[i];
}
