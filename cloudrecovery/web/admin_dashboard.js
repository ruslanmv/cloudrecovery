/**
 * CloudRecovery Admin Dashboard
 * Real-time monitoring and control interface for autopilot recovery operations
 */

let sessionId = null;
let sessionToken = null;
let ws = null;
let reconnectInterval = null;
let adminId = null;

// Parse URL parameters
function parseUrlParams() {
    const params = new URLSearchParams(window.location.search);
    sessionToken = params.get('token');

    // Extract session ID from path
    const pathParts = window.location.pathname.split('/');
    const monitorIndex = pathParts.indexOf('monitor');
    if (monitorIndex !== -1 && pathParts.length > monitorIndex + 1) {
        sessionId = pathParts[monitorIndex + 1];
    }
}

// Initialize dashboard
async function init() {
    parseUrlParams();

    if (!sessionId || !sessionToken) {
        showError('Invalid session URL. Please use the link provided in the notification email.');
        return;
    }

    // Generate admin ID (could be replaced with actual auth)
    adminId = 'admin-' + Math.random().toString(36).substr(2, 9);

    try {
        // Authenticate and load session
        const response = await fetch(`/api/monitoring/session/${sessionId}`, {
            headers: {
                'Authorization': `Bearer ${sessionToken}`
            }
        });

        if (!response.ok) {
            throw new Error('Session not found or expired');
        }

        const session = await response.json();

        // Display session info
        displaySession(session);

        // Connect to WebSocket for real-time updates
        connectWebSocket();

        // Hide loading, show dashboard
        document.getElementById('loading').style.display = 'none';
        document.getElementById('dashboard').style.display = 'block';

        // Start polling for updates
        startPolling();

    } catch (error) {
        console.error('Failed to load session:', error);
        showError(error.message);
    }
}

// Display session information
function displaySession(session) {
    document.getElementById('sessionId').textContent = session.session_id;
    document.getElementById('sessionCreated').textContent = formatTimestamp(session.created_at);
    document.getElementById('sessionExpires').textContent = formatTimestamp(session.expires_at);

    // Service info
    const serviceInfo = document.getElementById('serviceInfo');
    serviceInfo.innerHTML = `
        <div class="info-item">
            <div class="info-label">Service Name</div>
            <div class="info-value">${session.service_name}</div>
        </div>
        <div class="info-item">
            <div class="info-label">Service Type</div>
            <div class="info-value">${session.service_type.toUpperCase()}</div>
        </div>
        <div class="info-item">
            <div class="info-label">Priority</div>
            <div class="info-value">${session.priority.toUpperCase()}</div>
        </div>
        <div class="info-item">
            <div class="info-label">Plan ID</div>
            <div class="info-value" style="font-size: 12px; word-break: break-all;">${session.plan_id}</div>
        </div>
    `;

    // Update status if emergency stopped
    if (session.emergency_stopped) {
        updateStatus('stopped', `Stopped by ${session.stopped_by}`);
    }
}

// Connect to WebSocket for real-time updates
function connectWebSocket() {
    const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
    const wsUrl = `${protocol}//${window.location.host}/ws/monitoring/${sessionId}?token=${sessionToken}&admin_id=${adminId}`;

    ws = new WebSocket(wsUrl);

    ws.onopen = () => {
        console.log('WebSocket connected');
        addLog('info', 'Connected to monitoring session');
    };

    ws.onmessage = (event) => {
        const data = JSON.parse(event.data);
        handleWebSocketMessage(data);
    };

    ws.onerror = (error) => {
        console.error('WebSocket error:', error);
        addLog('error', 'WebSocket connection error');
    };

    ws.onclose = () => {
        console.log('WebSocket closed');
        addLog('warning', 'Connection lost, attempting to reconnect...');

        // Attempt to reconnect after 5 seconds
        setTimeout(connectWebSocket, 5000);
    };
}

// Handle WebSocket messages
function handleWebSocketMessage(data) {
    switch (data.type) {
        case 'action_update':
            updateAction(data.action);
            break;
        case 'action_complete':
            updateAction(data.action);
            addLog('success', `Action completed: ${data.action.description}`);
            break;
        case 'action_failed':
            updateAction(data.action);
            addLog('error', `Action failed: ${data.action.description} - ${data.action.error}`);
            break;
        case 'approval_required':
            updateAction(data.action);
            addLog('warning', `Approval required for: ${data.action.description}`);
            break;
        case 'emergency_stopped':
            updateStatus('stopped', `Emergency stop by ${data.admin_id}`);
            addLog('error', `EMERGENCY STOP activated by ${data.admin_id}`);
            break;
        case 'admin_connected':
            updateAdminList(data.admins);
            addLog('info', `Admin ${data.admin_id} connected`);
            break;
        case 'admin_disconnected':
            updateAdminList(data.admins);
            addLog('info', `Admin ${data.admin_id} disconnected`);
            break;
        case 'log':
            addLog(data.level, data.message);
            break;
    }
}

// Start polling for updates
async function startPolling() {
    // Poll every 5 seconds for actions and results
    setInterval(async () => {
        try {
            const response = await fetch(`/api/monitoring/session/${sessionId}/actions`, {
                headers: {
                    'Authorization': `Bearer ${sessionToken}`
                }
            });

            if (response.ok) {
                const actions = await response.json();
                displayActions(actions);
            }
        } catch (error) {
            console.error('Polling error:', error);
        }
    }, 5000);
}

// Display recovery actions
function displayActions(actions) {
    const container = document.getElementById('actionsContainer');

    if (!actions || actions.length === 0) {
        container.innerHTML = '<p style="color: #6b7280; text-align: center; padding: 20px;">No actions yet...</p>';
        return;
    }

    container.innerHTML = actions.map(action => {
        const statusClass = action.status.replace('_', '-');
        const statusText = action.status.replace('_', ' ').toUpperCase();

        let buttons = '';
        if (action.status === 'waiting_approval') {
            buttons = `
                <div class="action-buttons">
                    <button class="btn btn-approve" onclick="approveAction('${action.action_id}')">
                        ✓ Approve
                    </button>
                    <button class="btn btn-reject" onclick="rejectAction('${action.action_id}')">
                        ✗ Reject
                    </button>
                </div>
            `;
        }

        let output = '';
        if (action.output) {
            output = `<div class="action-output">${escapeHtml(action.output)}</div>`;
        }
        if (action.error) {
            output = `<div class="action-output" style="color: #ef4444;">${escapeHtml(action.error)}</div>`;
        }

        return `
            <div class="action-item ${statusClass}">
                <div class="action-header">
                    <div class="action-title">${action.description}</div>
                    <div class="action-status" style="background: var(--status-color);">${statusText}</div>
                </div>
                <div class="action-command">${escapeHtml(action.command)}</div>
                ${output}
                ${buttons}
            </div>
        `;
    }).join('');
}

// Update a single action
function updateAction(action) {
    // This will be called from WebSocket updates
    // For now, we rely on polling to refresh the full list
}

// Update overall status
function updateStatus(status, message) {
    const badge = document.getElementById('overallStatus');

    if (status === 'stopped') {
        badge.className = 'status-badge status-stopped';
        badge.textContent = 'Emergency Stopped';

        // Disable emergency stop button
        const btn = document.querySelector('.btn-emergency');
        btn.disabled = true;
        btn.textContent = '🛑 STOPPED';
        btn.style.opacity = '0.5';
        btn.style.cursor = 'not-allowed';
    } else if (status === 'warning') {
        badge.className = 'status-badge status-warning';
        badge.textContent = 'Warning';
    }
}

// Update admin list
function updateAdminList(admins) {
    const container = document.getElementById('adminList');

    if (!admins || admins.length === 0) {
        container.innerHTML = '<span style="color: #6b7280; font-size: 14px;">No admins connected</span>';
        return;
    }

    container.innerHTML = admins.map(admin =>
        `<span class="admin-badge">${admin}</span>`
    ).join('');
}

// Add log entry
function addLog(level, message) {
    const container = document.getElementById('logContainer');
    const timestamp = new Date().toLocaleTimeString();

    const entry = document.createElement('div');
    entry.className = 'log-entry';
    entry.innerHTML = `
        <span class="log-timestamp">${timestamp}</span>
        <span class="log-level-${level}">[${level.toUpperCase()}]</span>
        <span class="log-message">${escapeHtml(message)}</span>
    `;

    container.insertBefore(entry, container.firstChild);

    // Keep only last 100 entries
    while (container.children.length > 100) {
        container.removeChild(container.lastChild);
    }
}

// Approve action
async function approveAction(actionId) {
    try {
        const response = await fetch(`/api/monitoring/action/${actionId}/approve`, {
            method: 'POST',
            headers: {
                'Authorization': `Bearer ${sessionToken}`,
                'Content-Type': 'application/json'
            },
            body: JSON.stringify({ admin_id: adminId })
        });

        if (response.ok) {
            addLog('success', `Action ${actionId} approved`);
        } else {
            throw new Error('Failed to approve action');
        }
    } catch (error) {
        console.error('Approve error:', error);
        addLog('error', `Failed to approve action: ${error.message}`);
    }
}

// Reject action
async function rejectAction(actionId) {
    const reason = prompt('Please provide a reason for rejection:');
    if (!reason) return;

    try {
        const response = await fetch(`/api/monitoring/action/${actionId}/reject`, {
            method: 'POST',
            headers: {
                'Authorization': `Bearer ${sessionToken}`,
                'Content-Type': 'application/json'
            },
            body: JSON.stringify({
                admin_id: adminId,
                reason: reason
            })
        });

        if (response.ok) {
            addLog('warning', `Action ${actionId} rejected: ${reason}`);
        } else {
            throw new Error('Failed to reject action');
        }
    } catch (error) {
        console.error('Reject error:', error);
        addLog('error', `Failed to reject action: ${error.message}`);
    }
}

// Emergency stop
async function emergencyStop() {
    const confirmed = confirm(
        '⚠️ EMERGENCY STOP\n\n' +
        'This will immediately halt ALL recovery operations.\n\n' +
        'Are you absolutely sure you want to proceed?'
    );

    if (!confirmed) return;

    const reason = prompt('Please provide a reason for emergency stop:');
    if (!reason) return;

    try {
        const response = await fetch(`/api/monitoring/session/${sessionId}/emergency-stop`, {
            method: 'POST',
            headers: {
                'Authorization': `Bearer ${sessionToken}`,
                'Content-Type': 'application/json'
            },
            body: JSON.stringify({
                admin_id: adminId,
                reason: reason
            })
        });

        if (response.ok) {
            updateStatus('stopped', `Stopped by ${adminId}`);
            addLog('error', `EMERGENCY STOP: ${reason}`);
            alert('Emergency stop activated. All operations have been halted.');
        } else {
            throw new Error('Failed to activate emergency stop');
        }
    } catch (error) {
        console.error('Emergency stop error:', error);
        addLog('error', `Failed to activate emergency stop: ${error.message}`);
        alert('Failed to activate emergency stop. Please try again or contact support.');
    }
}

// Show error
function showError(message) {
    document.getElementById('loading').style.display = 'none';
    document.getElementById('dashboard').style.display = 'none';
    document.getElementById('error').style.display = 'block';
    document.getElementById('errorMessage').textContent = message;
}

// Utility functions
function formatTimestamp(timestamp) {
    const date = new Date(timestamp);
    return date.toLocaleString();
}

function escapeHtml(text) {
    const div = document.createElement('div');
    div.textContent = text;
    return div.innerHTML;
}

// Initialize on load
window.addEventListener('DOMContentLoaded', init);

// Cleanup on unload
window.addEventListener('beforeunload', () => {
    if (ws) {
        ws.close();
    }
});
