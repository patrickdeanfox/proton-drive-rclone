/**
 * AppState – Persistent state management via localStorage
 * Keeps sync progress, operations, and settings alive across page navigations.
 */
const AppState = (() => {
    const PREFIX = 'pdrive_';

    function _get(key, fallback = null) {
        try {
            const raw = localStorage.getItem(PREFIX + key);
            return raw ? JSON.parse(raw) : fallback;
        } catch { return fallback; }
    }

    function _set(key, value) {
        try {
            localStorage.setItem(PREFIX + key, JSON.stringify(value));
        } catch (e) {
            console.warn('localStorage write failed:', e);
        }
    }

    function _remove(key) {
        localStorage.removeItem(PREFIX + key);
    }

    return {
        // Operations
        getOperations() { return _get('operations', {}); },
        saveOperation(op) {
            const ops = this.getOperations();
            ops[op.id] = op;
            _set('operations', ops);
        },
        removeOperation(opId) {
            const ops = this.getOperations();
            delete ops[opId];
            _set('operations', ops);
        },
        getActiveOperationIds() {
            const ops = this.getOperations();
            return Object.values(ops).filter(o => o.status === 'running').map(o => o.id);
        },

        // Current tracked op for the sync page
        getCurrentSyncOp() { return _get('current_sync_op', null); },
        setCurrentSyncOp(opId) { _set('current_sync_op', opId); },
        clearCurrentSyncOp() { _remove('current_sync_op'); },

        // Comparison result
        getComparisonResult() { return _get('comparison_result', null); },
        saveComparisonResult(result) { _set('comparison_result', result); },
        clearComparisonResult() { _remove('comparison_result'); },

        // Settings
        getSettings() { return _get('settings', { demoMode: true, navGuard: true, autoScroll: true }); },
        saveSettings(s) { _set('settings', s); },

        // Page-specific log cache (last N entries per page)
        getPageLogs(page) { return _get('page_logs_' + page, []); },
        savePageLogs(page, logs) {
            // Keep only last 200 per page
            const trimmed = logs.slice(-200);
            _set('page_logs_' + page, trimmed);
        },
        clearPageLogs(page) { _remove('page_logs_' + page); },

        // Has active operations?
        hasActiveOps() {
            return this.getActiveOperationIds().length > 0;
        },

        // Clear all
        clear() {
            const keys = [];
            for (let i = 0; i < localStorage.length; i++) {
                const k = localStorage.key(i);
                if (k && k.startsWith(PREFIX)) keys.push(k);
            }
            keys.forEach(k => localStorage.removeItem(k));
        }
    };
})();
