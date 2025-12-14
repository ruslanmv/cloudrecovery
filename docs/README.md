# CloudRecovery Autopilot Documentation

Welcome to the CloudRecovery Autopilot documentation! This system provides automated monitoring, recovery, and admin oversight for critical services.

## 📚 Documentation Overview

### Getting Started

- **[Autopilot Setup Guide](AUTOPILOT_SETUP.md)** - Complete installation and configuration guide
- **[Best Practices](BEST_PRACTICES.md)** - Production recommendations and safety guidelines
- **[Examples](examples/)** - Code examples and configuration samples

### Key Features

#### 🔔 Email Notifications
- Instant alerts when services go down
- Configurable priority levels
- Rate limiting to prevent alert fatigue
- Beautiful HTML email templates with monitoring links

#### 🤖 Automated Recovery
- AI-assisted recovery plans
- Safety-first approach with multiple safety levels
- Automatic rollback on failures
- Support for websites, databases, and custom services

#### 🛡️ Safety Controls
- Command safety analysis before execution
- Blocked command list (destructive operations)
- Approval workflow for risky operations
- Emergency stop functionality

#### 📊 Real-time Monitoring Dashboard
- Watch AI recovery in real-time
- Approve or reject pending actions
- Emergency stop button
- Multi-admin support
- Live activity logs

## 🚀 Quick Start

### 1. Install

```bash
pip install cloudrecovery
```

### 2. Configure

```bash
# Copy environment template
cp .env.example .env

# Edit configuration
nano .env  # Add your SMTP credentials and admin emails
```

### 3. Run

```bash
# Start the daemon
cloudrecovery agent --config config/autopilot_config.yaml
```

### 4. Test

```bash
# Send test email
python -c "
from cloudrecovery.notifications import EmailNotifier, NotificationConfig
# ... (see examples/basic_usage.py)
"
```

## 📖 Documentation Index

### Setup & Configuration

| Document | Description |
|----------|-------------|
| [Autopilot Setup](AUTOPILOT_SETUP.md) | Installation, configuration, deployment |
| [Best Practices](BEST_PRACTICES.md) | Production guidelines and safety recommendations |

### Examples

| File | Description |
|------|-------------|
| [basic_usage.py](examples/basic_usage.py) | Python API usage examples |
| [production_example.yaml](examples/production_example.yaml) | Production configuration template |

## 🎯 Use Cases

### 1. Website Monitoring & Recovery

Monitor your website and automatically recover from failures:

```yaml
monitoring:
  website:
    - url: "https://your-site.com"
      check_interval_s: 30

recovery_plans:
  website:
    actions:
      - description: "Check nginx status"
        command: "systemctl status nginx"
        safety_level: "safe"
      - description: "Restart nginx"
        command: "systemctl restart nginx"
        safety_level: "low"
        requires_approval: true
```

When your site goes down:
1. ✅ Autopilot detects the failure
2. 📧 Admins receive email alert with monitoring link
3. 🔍 Safety checks are performed
4. ✨ Recovery actions execute (safe ones auto-approved)
5. 👀 Admins can watch and control via dashboard

### 2. Database Monitoring

Monitor PostgreSQL and prevent outages:

```yaml
monitoring:
  postgresql:
    enabled: true
    connection_string: "${POSTGRES_URL}"
    check_interval_s: 60
```

### 3. Multi-Service Stack

Monitor your entire application stack:

- Frontend (nginx/Apache)
- Backend API (Node.js/Python)
- Database (PostgreSQL/MySQL)
- Cache (Redis)
- Message Queue (RabbitMQ)

## 🔐 Security Features

### Safety Levels

| Level | Description | Examples | Auto-Approved? |
|-------|-------------|----------|----------------|
| `safe` | Read-only operations | `ls`, `cat`, `status` | ✅ Yes |
| `low` | Minimal risk | Service restart | ⚠️ Configurable |
| `medium` | Moderate risk | Config changes | ⚠️ Usually requires approval |
| `high` | High risk | Database operations | ❌ Requires approval |
| `critical` | Destructive | System changes | ❌ Never auto-approved |

### Blocked Commands

The system automatically blocks dangerous operations:

- ❌ `rm -rf /`
- ❌ `DROP DATABASE`
- ❌ `mkfs`
- ❌ `dd if=/dev/zero`
- ❌ `shutdown`
- ❌ And more...

### Emergency Stop

Admins can halt all operations instantly via the monitoring dashboard.

## 📧 Email Notifications

### Features

- **Priority-based**: Only send important alerts
- **Rich HTML**: Beautiful, actionable emails
- **Rate Limited**: Prevent notification spam
- **Monitoring Links**: Direct access to live dashboard
- **Mobile Friendly**: Read on any device

### Sample Email

```
🔴 [PRODUCTION] CloudRecovery Alert: Website Down

Priority: HIGH
Service: nginx (website)
Status: DOWN
Time: 2024-01-15 14:30:00 UTC

Message:
The nginx web server is not responding to health checks.
Autopilot recovery has been initiated.

Auto-Recovery:
Attempted: Yes
Status: In Progress

🔍 Monitor Recovery:
[Open Monitoring Dashboard]

Click the link above to monitor the AI recovery process in real-time.
You can stop any dangerous operations from the monitoring dashboard.
```

## 🖥️ Monitoring Dashboard

### Features

- **Real-time Updates**: WebSocket-based live updates
- **Action Tracking**: See each recovery step as it executes
- **Approval Controls**: Approve or reject risky operations
- **Emergency Stop**: Big red button to halt everything
- **Activity Log**: Full audit trail of all actions
- **Multi-Admin**: Multiple admins can monitor simultaneously

### Dashboard Sections

1. **Service Information**: Service name, type, status
2. **Recovery Actions**: Live feed of executing actions
3. **Activity Log**: Real-time event stream
4. **Connected Admins**: Who else is watching
5. **Emergency Controls**: Stop button

## 🛠️ Architecture

### Components

```
┌─────────────────────────────────────────┐
│     CloudRecovery Daemon                │
│  ┌────────────────────────────────────┐ │
│  │  Monitoring Agent                  │ │
│  │  - Website checks                  │ │
│  │  - Database health                 │ │
│  │  - Host resources                  │ │
│  └────────────────────────────────────┘ │
│  ┌────────────────────────────────────┐ │
│  │  Recovery Engine                   │ │
│  │  - Safety monitor                  │ │
│  │  - Action executor                 │ │
│  │  - Rollback handler                │ │
│  └────────────────────────────────────┘ │
│  ┌────────────────────────────────────┐ │
│  │  Notification System               │ │
│  │  - Email sender                    │ │
│  │  - Session manager                 │ │
│  │  - WebSocket broadcaster           │ │
│  └────────────────────────────────────┘ │
└─────────────────────────────────────────┘
                    │
                    ↓
          ┌─────────────────┐
          │   Admin Email   │
          │  with Link to   │
          │   Dashboard     │
          └─────────────────┘
                    │
                    ↓
     ┌──────────────────────────────┐
     │  Web Monitoring Dashboard    │
     │  - Real-time action view     │
     │  - Approve/Reject controls   │
     │  - Emergency stop button     │
     └──────────────────────────────┘
```

## 🔄 Workflow

### Typical Recovery Flow

1. **Detection**: Service health check fails
2. **Alert**: Email sent to admins with monitoring link
3. **Analysis**: Safety monitor analyzes recovery plan
4. **Execution**:
   - Safe actions execute automatically
   - Risky actions wait for approval
5. **Monitoring**: Admins watch via dashboard
6. **Intervention**: Emergency stop if needed
7. **Completion**: Service recovered or escalated

## 📊 Configuration Options

### Monitoring Intervals

- **Critical Services**: 30 seconds
- **Important Services**: 60 seconds
- **Background Services**: 300 seconds (5 minutes)

### Session Duration

- **Default**: 24 hours
- **Production**: 48 hours (longer for troubleshooting)
- **Auto-extend**: When accessed

### Safety Thresholds

```yaml
autopilot:
  max_safety_level: "medium"      # Don't auto-execute above this
  require_approval_above: "low"   # Always ask approval above this
```

## 🧪 Testing

### Before Production

1. **Test Email**: Verify SMTP credentials work
2. **Test Recovery**: Run in staging environment
3. **Test Dashboard**: Create mock session
4. **Test Safety**: Verify blocked commands work
5. **Test Emergency Stop**: Ensure it halts operations

### Testing Commands

```bash
# Test email
python examples/basic_usage.py

# Test recovery (dry-run)
cloudrecovery test-recovery --plan website --dry-run

# Test dashboard
cloudrecovery test-dashboard
```

## 📝 Best Practices Summary

1. **Start Conservative**: Begin with `max_safety_level: "safe"`
2. **Test in Staging**: Never deploy untested to production
3. **Use Rate Limiting**: Prevent alert fatigue
4. **Require Approvals**: For anything risky
5. **Monitor the Monitor**: Ensure daemon is healthy
6. **Regular Drills**: Monthly recovery testing
7. **Keep Updated**: Update admin contact lists
8. **Review Logs**: Weekly audit trail review

## 🆘 Support

### Getting Help

- **Documentation**: Read the guides in this folder
- **Examples**: Check `examples/` directory
- **Issues**: GitHub Issues for bugs
- **Discussions**: GitHub Discussions for questions

### Troubleshooting

Common issues and solutions:

| Problem | Solution |
|---------|----------|
| Emails not sending | Check SMTP credentials, firewall, spam folder |
| Recovery not starting | Verify `autopilot.enabled: true` |
| Dashboard not loading | Check session expiration, verify token |
| Commands blocked | Review safety level settings |

See [AUTOPILOT_SETUP.md#troubleshooting](AUTOPILOT_SETUP.md#troubleshooting) for detailed troubleshooting.

## 📄 License

CloudRecovery is licensed under the Apache License 2.0.

## 🙏 Contributing

Contributions welcome! Please:

1. Read the documentation
2. Test your changes
3. Follow best practices
4. Submit pull request

## 🔗 Quick Links

- [Setup Guide](AUTOPILOT_SETUP.md)
- [Best Practices](BEST_PRACTICES.md)
- [Code Examples](examples/basic_usage.py)
- [Production Config](examples/production_example.yaml)
- [Main README](../README.md)

---

**Ready to get started?** → [Autopilot Setup Guide](AUTOPILOT_SETUP.md)
