# CloudRecovery Autopilot Setup Guide

This guide will help you set up the CloudRecovery Autopilot system with email notifications, automated recovery, and admin monitoring capabilities.

## Table of Contents

1. [Overview](#overview)
2. [Prerequisites](#prerequisites)
3. [Installation](#installation)
4. [Configuration](#configuration)
5. [Running the Daemon](#running-the-daemon)
6. [Monitoring Dashboard](#monitoring-dashboard)
7. [Troubleshooting](#troubleshooting)

## Overview

CloudRecovery Autopilot provides:

- **Automated Monitoring**: Continuous health checks for websites, databases, and services
- **Email Alerts**: Instant notifications to admins when services go down
- **Automated Recovery**: Safe, AI-assisted recovery with safety controls
- **Real-time Monitoring**: Admin dashboard to watch recovery in real-time
- **Emergency Stop**: Ability to halt any dangerous operations immediately

## Prerequisites

- Python 3.11 or 3.12
- Linux server (Ubuntu 20.04+ or RHEL 8+ recommended)
- SMTP server access (Gmail, SendGrid, or corporate email)
- Root or sudo access for system service installation

## Installation

### 1. Install CloudRecovery

```bash
# Clone the repository
git clone https://github.com/ruslanmv/cloudrecovery.git
cd cloudrecovery

# Install using pip
pip install -e .

# Or install with uv (faster)
uv pip install -e .
```

### 2. Verify Installation

```bash
cloudrecovery --version
```

## Configuration

### 1. Create Environment File

Copy the example environment file and customize it:

```bash
cp .env.example .env
nano .env  # or use your preferred editor
```

### 2. Configure Email Notifications

Edit `.env` and set your SMTP credentials:

```bash
# For Gmail
SMTP_HOST=smtp.gmail.com
SMTP_PORT=587
SMTP_USE_TLS=true
SMTP_USERNAME=your-email@gmail.com
SMTP_PASSWORD=your-app-password  # Use App Password, not regular password

# Admin emails (comma-separated)
ADMIN_EMAILS=admin1@example.com,admin2@example.com
```

**Gmail Setup:**
1. Go to Google Account Settings → Security
2. Enable 2-Factor Authentication
3. Generate an App Password (Select "Mail" and your device)
4. Use the generated password in `.env`

**SendGrid Setup:**
```bash
SMTP_HOST=smtp.sendgrid.net
SMTP_PORT=587
SMTP_USERNAME=apikey
SMTP_PASSWORD=your-sendgrid-api-key
```

### 3. Configure Services to Monitor

Edit `config/autopilot_config.yaml`:

```yaml
monitoring:
  # Website monitoring
  website:
    enabled: true
    urls:
      - url: "https://your-website.example.com"
        check_interval_s: 30
      - url: "https://your-api.example.com/health"
        check_interval_s: 30

  # PostgreSQL monitoring
  postgresql:
    enabled: true
    connection_string: "${POSTGRESQL_URL}"
    check_interval_s: 60

  # MCP server monitoring
  mcp:
    enabled: true
    process_name: "mcp-server"
    check_interval_s: 30
```

### 4. Configure Safety Settings

Set safety thresholds in `config/autopilot_config.yaml`:

```yaml
autopilot:
  enabled: true
  auto_approve_safe: true  # Auto-approve read-only operations
  max_safety_level: "medium"  # Maximum allowed without approval
  require_approval_above: "low"  # Require approval above this level
```

**Safety Levels:**
- `safe`: Read-only operations (ls, cat, status checks)
- `low`: Service restarts, log rotation
- `medium`: Configuration changes, database updates with WHERE clause
- `high`: Database operations, destructive changes
- `critical`: System-wide operations (never auto-approved)

## Running the Daemon

### Method 1: Run as Systemd Service (Recommended for Production)

1. Create systemd service file:

```bash
sudo nano /etc/systemd/system/cloudrecovery-autopilot.service
```

2. Add the following content:

```ini
[Unit]
Description=CloudRecovery Autopilot Daemon
After=network.target
Wants=network-online.target

[Service]
Type=simple
User=cloudrecovery
Group=cloudrecovery
WorkingDirectory=/opt/cloudrecovery
EnvironmentFile=/opt/cloudrecovery/.env
ExecStart=/usr/local/bin/cloudrecovery agent --config /etc/cloudrecovery/autopilot_config.yaml
Restart=always
RestartSec=10
StandardOutput=journal
StandardError=journal

# Security hardening
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ProtectHome=true
ReadWritePaths=/var/log/cloudrecovery

[Install]
WantedBy=multi-user.target
```

3. Create user and directories:

```bash
sudo useradd -r -s /bin/false cloudrecovery
sudo mkdir -p /opt/cloudrecovery /etc/cloudrecovery /var/log/cloudrecovery
sudo cp config/autopilot_config.yaml /etc/cloudrecovery/
sudo cp .env /opt/cloudrecovery/
sudo chown -R cloudrecovery:cloudrecovery /opt/cloudrecovery /var/log/cloudrecovery
```

4. Enable and start the service:

```bash
sudo systemctl daemon-reload
sudo systemctl enable cloudrecovery-autopilot
sudo systemctl start cloudrecovery-autopilot
```

5. Check status:

```bash
sudo systemctl status cloudrecovery-autopilot
sudo journalctl -u cloudrecovery-autopilot -f
```

### Method 2: Run in Docker

```bash
# Build image
docker build -t cloudrecovery:latest .

# Run container
docker run -d \
  --name cloudrecovery-autopilot \
  --restart unless-stopped \
  -v $(pwd)/config:/etc/cloudrecovery \
  -v $(pwd)/.env:/app/.env \
  -e CLOUDRECOVERY_AGENT_CONFIG=/etc/cloudrecovery/autopilot_config.yaml \
  cloudrecovery:latest
```

### Method 3: Run Manually (Development/Testing)

```bash
# Load environment variables
export $(cat .env | xargs)

# Run agent
cloudrecovery agent --config config/autopilot_config.yaml
```

## Monitoring Dashboard

### Access the Dashboard

When a service goes down and recovery is initiated, admins receive an email with a monitoring link:

```
🔍 Monitor AI Recovery
View real-time recovery progress and stop dangerous operations

[Open Monitoring Dashboard]
```

### Dashboard Features

1. **Real-time Action Tracking**
   - See each recovery step as it executes
   - View command outputs live
   - Safety level indicators

2. **Approval Controls**
   - Approve or reject pending actions
   - Require approval for risky operations
   - View safety assessments

3. **Emergency Stop**
   - Immediately halt all operations
   - Prevents execution of pending actions
   - Requires confirmation and reason

4. **Activity Log**
   - Real-time log stream
   - Color-coded severity levels
   - Timestamps for all events

5. **Connected Admins**
   - See who else is monitoring
   - Multiple admins can observe simultaneously

### Dashboard URL Format

```
https://your-server.example.com/monitor/{session_id}?token={session_token}
```

The URL is automatically generated and included in email notifications.

## Testing the Setup

### 1. Test Email Notifications

```python
from cloudrecovery.notifications import EmailNotifier, NotificationConfig, NotificationEvent, NotificationPriority

# Create config
config = NotificationConfig(
    smtp_host="smtp.gmail.com",
    smtp_port=587,
    smtp_username="your-email@gmail.com",
    smtp_password="your-app-password",
    smtp_from_email="cloudrecovery@example.com",
    admin_emails=["admin@example.com"],
)

# Create notifier
notifier = EmailNotifier(config)

# Send test notification
event = NotificationEvent(
    event_id="test-001",
    priority=NotificationPriority.HIGH,
    service_name="Test Service",
    service_type="website",
    status="down",
    title="Test Alert",
    message="This is a test notification from CloudRecovery Autopilot",
)

# Send (async)
import asyncio
asyncio.run(notifier.send_notification(event))
```

### 2. Test Recovery Engine

```python
from cloudrecovery.autopilot import RecoveryEngine, SafetyMonitor, RecoveryPlan, RecoveryAction
from cloudrecovery.autopilot.safety_monitor import SafetyLevel

# Create safety monitor
safety = SafetyMonitor(max_safety_level=SafetyLevel.MEDIUM)

# Create recovery engine
engine = RecoveryEngine(safety_monitor=safety)

# Create test plan
plan = RecoveryPlan(
    service_name="nginx",
    service_type="website",
    actions=[
        RecoveryAction(
            description="Check nginx status",
            command="systemctl status nginx",
            safety_level=SafetyLevel.SAFE,
        ),
    ],
)

# Execute (async)
import asyncio
results = asyncio.run(engine.execute_recovery(plan))
print(results)
```

### 3. Test Monitoring Dashboard

1. Start the server:
   ```bash
   cloudrecovery ui --host 0.0.0.0 --port 8787
   ```

2. Create a test session (Python):
   ```python
   from cloudrecovery.autopilot import SessionManager

   manager = SessionManager(base_url="http://localhost:8787")
   session = manager.create_session(plan, priority="high")

   print(f"Dashboard URL: {session.get_session_url('http://localhost:8787')}")
   ```

3. Open the URL in a browser to see the dashboard

## Troubleshooting

### Email Not Sending

**Problem**: No emails received

**Solutions**:
1. Check SMTP credentials in `.env`
2. Verify firewall allows outbound port 587
3. Check spam folder
4. Enable debug logging:
   ```bash
   LOG_LEVEL=DEBUG cloudrecovery agent --config config/autopilot_config.yaml
   ```

### Service Not Detected as Down

**Problem**: Service is down but no alert sent

**Solutions**:
1. Check monitoring configuration in `autopilot_config.yaml`
2. Verify polling interval is appropriate
3. Check service URL/connection string
4. Review logs for errors:
   ```bash
   tail -f /var/log/cloudrecovery/autopilot.log
   ```

### Recovery Actions Not Executing

**Problem**: Recovery doesn't start automatically

**Solutions**:
1. Verify `autopilot.enabled: true` in config
2. Check safety level settings
3. Review blocked commands list
4. Check if emergency stop is active

### Dashboard Not Loading

**Problem**: Monitoring dashboard shows error

**Solutions**:
1. Verify server is running
2. Check session hasn't expired
3. Verify token is correct in URL
4. Check browser console for errors

### Permission Denied Errors

**Problem**: Commands fail with permission errors

**Solutions**:
1. Verify service user has necessary permissions
2. Add sudo rules if needed:
   ```bash
   cloudrecovery ALL=(ALL) NOPASSWD: /bin/systemctl restart nginx
   ```
3. Review security settings in systemd service file

## Security Best Practices

1. **Use Environment Variables**: Never commit `.env` to version control
2. **Restrict Permissions**: Run daemon as non-root user when possible
3. **Enable TLS**: Use HTTPS for dashboard and API
4. **Rotate Tokens**: Periodically change authentication tokens
5. **Monitor Logs**: Review audit logs regularly
6. **Test Recovery Plans**: Test in staging before production
7. **Set Conservative Safety Levels**: Start with low `max_safety_level`
8. **Use Approval Workflow**: Require approval for risky operations

## Next Steps

- Review [Usage Examples](examples/)
- Read [Best Practices](BEST_PRACTICES.md)
- Configure [Custom Recovery Plans](RECOVERY_PLANS.md)
- Set up [Monitoring Integration](MONITORING.md)

## Support

For issues or questions:
- GitHub Issues: https://github.com/ruslanmv/cloudrecovery/issues
- Documentation: https://github.com/ruslanmv/cloudrecovery/docs
