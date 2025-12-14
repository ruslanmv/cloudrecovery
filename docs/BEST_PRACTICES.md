# CloudRecovery Autopilot - Best Practices

This document outlines best practices for running CloudRecovery Autopilot safely and effectively in production environments.

## Table of Contents

1. [Safety First](#safety-first)
2. [Monitoring Configuration](#monitoring-configuration)
3. [Email Notifications](#email-notifications)
4. [Recovery Plans](#recovery-plans)
5. [Admin Monitoring](#admin-monitoring)
6. [Security](#security)
7. [Testing](#testing)
8. [Deployment](#deployment)

## Safety First

### Start Conservative

**DO:**
- Begin with `max_safety_level: "safe"` (read-only)
- Require approval for all actions initially
- Test extensively in staging before production
- Monitor carefully for the first week

**DON'T:**
- Set `max_safety_level: "critical"` immediately
- Auto-approve high-risk operations
- Deploy directly to production
- Disable safety checks

### Gradual Rollout

```yaml
# Week 1: Read-only monitoring
autopilot:
  enabled: true
  auto_approve_safe: false  # Manual approval even for safe ops
  max_safety_level: "safe"

# Week 2: Enable low-risk auto-recovery
autopilot:
  enabled: true
  auto_approve_safe: true
  max_safety_level: "low"

# Week 3+: Enable medium-risk (if confident)
autopilot:
  enabled: true
  auto_approve_safe: true
  max_safety_level: "medium"
```

### Use Blocked Commands

Always maintain a blocklist of dangerous commands:

```yaml
autopilot:
  blocked_commands:
    # Destructive file operations
    - "rm -rf /"
    - "mkfs"
    - "dd if=/dev/zero"

    # System modifications
    - "fdisk"
    - "parted"
    - "cryptsetup luksFormat"

    # Database dangers
    - "DROP DATABASE"
    - "TRUNCATE TABLE"

    # Network risks
    - "iptables -F"
    - "ufw disable"

    # Access risks
    - "passwd root"
    - "chmod 777 /etc"
```

## Monitoring Configuration

### Health Check Intervals

Choose appropriate intervals based on criticality:

```yaml
monitoring:
  # Critical production website: frequent checks
  website:
    - url: "https://production.example.com"
      check_interval_s: 30  # Every 30 seconds

  # Internal API: moderate checks
  website:
    - url: "https://internal-api.example.com/health"
      check_interval_s: 60  # Every minute

  # Background services: less frequent
  mcp:
    check_interval_s: 300  # Every 5 minutes
```

**Guidelines:**
- Customer-facing services: 30-60 seconds
- Internal services: 1-5 minutes
- Batch jobs: 5-15 minutes
- Development environments: 10-30 minutes

### Resource Thresholds

Set realistic thresholds with buffer zones:

```yaml
monitoring:
  host:
    thresholds:
      # CPU (leave headroom)
      cpu_critical: 90  # Not 95, to allow spike handling
      cpu_warning: 70

      # Memory (account for caches)
      memory_critical: 85  # Not 95, Linux uses RAM for cache
      memory_warning: 70

      # Disk (early warning)
      disk_critical: 85  # Give time to clean up
      disk_warning: 70
```

### Timeout Values

Set appropriate timeouts:

```yaml
monitoring:
  website:
    - url: "https://api.example.com"
      timeout_s: 5  # Fast API should respond quickly

  postgresql:
    connection_timeout_s: 10  # Database may be slower
```

## Email Notifications

### Admin Email Lists

Structure your admin contacts strategically:

```yaml
notifications:
  admin_emails:
    # Primary on-call
    - "oncall@example.com"

    # Engineering leads
    - "lead-sre@example.com"
    - "lead-devops@example.com"

    # Escalation
    - "cto@example.com"  # For critical issues only
```

**Best Practices:**
- Use distribution lists/groups for teams
- Include at least 2 contacts
- Test email delivery to all addresses
- Keep list updated during on-call rotations

### Priority Levels

Set appropriate priority thresholds:

```yaml
notifications:
  # Only send medium+ priority emails
  min_priority: "medium"

  # Rate limiting to prevent spam
  rate_limit_seconds: 300  # 5 minutes between duplicate alerts
```

**Priority Guidelines:**
- `LOW`: Informational, daily summary
- `MEDIUM`: Warning conditions, degraded performance
- `HIGH`: Service outages, failed recoveries
- `CRITICAL`: Multi-service failures, security incidents

### Rate Limiting

Prevent notification fatigue:

```yaml
notifications:
  # Don't send duplicate alerts too frequently
  rate_limit_seconds: 300  # 5 minutes

  # But allow escalation
  escalation:
    after_minutes: 15  # Escalate if not resolved
    to: "manager@example.com"
```

## Recovery Plans

### Structure Recovery Actions

Order actions from safe to risky:

```yaml
recovery_plans:
  website:
    actions:
      # 1. Information gathering (SAFE)
      - description: "Check service status"
        command: "systemctl status nginx"
        safety_level: "safe"

      - description: "Check error logs"
        command: "tail -n 100 /var/log/nginx/error.log"
        safety_level: "safe"

      # 2. Diagnostics (SAFE)
      - description: "Test port connectivity"
        command: "netstat -tlnp | grep :80"
        safety_level: "safe"

      # 3. Low-risk fixes (LOW)
      - description: "Reload configuration"
        command: "systemctl reload nginx"
        safety_level: "low"
        requires_approval: true

      # 4. Higher-risk fixes (MEDIUM)
      - description: "Restart service"
        command: "systemctl restart nginx"
        safety_level: "medium"
        requires_approval: true
        rollback_command: "systemctl start nginx"
```

### Provide Rollback Commands

Always include rollback where possible:

```yaml
- description: "Update database schema"
  command: "psql -f /opt/migrations/v2_upgrade.sql"
  safety_level: "high"
  requires_approval: true
  rollback_command: "psql -f /opt/migrations/v2_rollback.sql"
```

### Use Gates

Prevent risky actions if conditions aren't met:

```yaml
- description: "Restart database"
  command: "systemctl restart postgresql"
  safety_level: "high"
  requires_approval: true
  gates:
    - type: "synthetic_check"
      url: "https://backup-db.example.com"
      must_be: "healthy"
```

## Admin Monitoring

### Dashboard Access

**DO:**
- Use unique tokens per session
- Set reasonable expiration times (24 hours default)
- Include session URLs in all alert emails
- Monitor who accesses dashboards

**DON'T:**
- Share dashboard URLs publicly
- Use predictable session IDs
- Keep sessions active indefinitely
- Disable authentication

### Emergency Stop Protocol

Train admins on when to use emergency stop:

**Use Emergency Stop When:**
- Commands appear to be deleting data
- System resources (CPU/memory) spike abnormally
- Multiple services failing simultaneously
- Unfamiliar or suspicious commands
- Database operations without WHERE clauses

**Emergency Stop Process:**
1. Click "Emergency Stop" button
2. Provide clear reason
3. All pending operations are halted
4. Investigate the issue
5. Manually resolve or create new recovery plan

### Multi-Admin Coordination

When multiple admins are monitoring:

```markdown
**Protocol:**
1. First admin to join "owns" the session
2. Communicate via chat/Slack before approving
3. Only one admin approves/rejects actions
4. Document decisions in the dashboard
5. Use emergency stop if disagreement occurs
```

## Security

### Authentication & Authorization

```yaml
security:
  # Require TLS for all communications
  require_tls: true

  # Verify SSL certificates
  verify_ssl: true

  # Short-lived tokens
  token_expiration_hours: 24

  # Rotate tokens regularly
  token_rotation_days: 30
```

### Secrets Management

**DO:**
- Use environment variables for secrets
- Rotate credentials quarterly
- Use App Passwords (Gmail) or API keys (SendGrid)
- Encrypt sensitive configuration files

**DON'T:**
- Commit `.env` files to Git
- Share SMTP passwords in Slack/email
- Use personal email accounts
- Store plaintext passwords

### Audit Logging

Enable comprehensive logging:

```yaml
logging:
  level: "INFO"
  file: "/var/log/cloudrecovery/autopilot.log"

  # Audit all operations
  audit:
    enabled: true
    log_commands: true
    log_approvals: true
    log_emergency_stops: true
```

Review logs regularly:

```bash
# Daily review
sudo grep "EMERGENCY_STOP\|CRITICAL\|failed" /var/log/cloudrecovery/autopilot.log

# Weekly summary
sudo journalctl -u cloudrecovery-autopilot --since "1 week ago" | grep "recovery"
```

### Least Privilege

Run the daemon with minimal permissions:

```bash
# Create dedicated user
sudo useradd -r -s /bin/false cloudrecovery

# Grant only necessary sudo privileges
# /etc/sudoers.d/cloudrecovery:
cloudrecovery ALL=(ALL) NOPASSWD: /bin/systemctl status *
cloudrecovery ALL=(ALL) NOPASSWD: /bin/systemctl restart nginx
cloudrecovery ALL=(ALL) NOPASSWD: /bin/systemctl restart postgresql
```

## Testing

### Pre-Production Testing

Before deploying to production:

```bash
# 1. Test email notifications
python -m cloudrecovery.notifications.test_email

# 2. Test recovery engine with dry-run
cloudrecovery test-recovery --plan website --dry-run

# 3. Test monitoring dashboard
cloudrecovery test-dashboard --create-mock-session

# 4. Simulate service failure
cloudrecovery simulate-failure --service nginx --duration 60s
```

### Staging Environment

Maintain a staging environment that mirrors production:

```yaml
# staging.yaml
agent:
  env: "staging"

monitoring:
  # Use staging URLs
  website:
    - url: "https://staging.example.com"

notifications:
  # Send to test email
  admin_emails:
    - "dev-team@example.com"
```

### Regular Drills

Schedule recovery drills:

```markdown
**Monthly Recovery Drill:**
1. Randomly select a service
2. Simulate failure (stop service)
3. Verify autopilot detects it
4. Verify email notification sent
5. Verify recovery executes
6. Verify admins can access dashboard
7. Test emergency stop
8. Document findings
```

## Deployment

### Deployment Checklist

Before deploying:

- [ ] Configuration reviewed and tested
- [ ] SMTP credentials verified (test email sent)
- [ ] Admin emails confirmed
- [ ] Safety levels set conservatively
- [ ] Blocked commands list reviewed
- [ ] Recovery plans tested in staging
- [ ] Monitoring intervals appropriate
- [ ] Resource thresholds validated
- [ ] Logs configured and writable
- [ ] Systemd service file created
- [ ] User permissions configured
- [ ] Firewall rules updated
- [ ] Dashboard access tested
- [ ] Emergency stop tested
- [ ] Runbook created for admins

### Production Deployment

```bash
# 1. Install package
pip install cloudrecovery

# 2. Create configuration
sudo mkdir -p /etc/cloudrecovery
sudo cp config/autopilot_config.yaml /etc/cloudrecovery/

# 3. Set up environment
sudo cp .env /opt/cloudrecovery/
sudo chmod 600 /opt/cloudrecovery/.env

# 4. Create systemd service
sudo cp cloudrecovery-autopilot.service /etc/systemd/system/
sudo systemctl daemon-reload

# 5. Enable and start
sudo systemctl enable cloudrecovery-autopilot
sudo systemctl start cloudrecovery-autopilot

# 6. Verify running
sudo systemctl status cloudrecovery-autopilot
```

### Post-Deployment

```bash
# Monitor for first 24 hours
sudo journalctl -u cloudrecovery-autopilot -f

# Check for errors
sudo grep ERROR /var/log/cloudrecovery/autopilot.log

# Verify monitoring
curl -I https://your-website.example.com  # Should be monitored

# Test recovery (optional, in staging)
sudo systemctl stop nginx  # Should trigger recovery
```

### Rollback Plan

If issues occur:

```bash
# 1. Stop the service
sudo systemctl stop cloudrecovery-autopilot

# 2. Disable autopilot
sudo nano /etc/cloudrecovery/autopilot_config.yaml
# Set: autopilot.enabled: false

# 3. Review logs
sudo journalctl -u cloudrecovery-autopilot --since "1 hour ago" > /tmp/autopilot.log

# 4. Report issue
# Submit logs to support

# 5. Revert to previous version (if upgraded)
pip install cloudrecovery==0.1.0
```

## Monitoring the Monitor

### Health Checks for Autopilot

Monitor the autopilot daemon itself:

```yaml
# Add to your existing monitoring
- name: "CloudRecovery Autopilot Health"
  check: "systemctl is-active cloudrecovery-autopilot"
  interval: 60s
  alert_if: "inactive"
```

### Metrics to Track

Key metrics:
- Recovery attempts per day
- Recovery success rate
- Average time to recovery
- Emergency stop frequency
- Approval delays
- Email delivery failures

### Regular Reviews

**Weekly:**
- Review recovery logs
- Check email delivery
- Verify admin list accuracy

**Monthly:**
- Analyze recovery patterns
- Update recovery plans
- Test emergency procedures
- Review blocked commands

**Quarterly:**
- Security audit
- Rotate credentials
- Update documentation
- Train new admins

## Common Pitfalls

### 1. Over-Automation

**Problem:** Auto-approving too many operations

**Solution:**
- Start with manual approvals
- Gradually increase automation
- Always require approval for destructive operations

### 2. Alert Fatigue

**Problem:** Too many low-priority emails

**Solution:**
- Set `min_priority: "medium"` or higher
- Use rate limiting
- Consolidate alerts

### 3. Insufficient Testing

**Problem:** First learning in production

**Solution:**
- Maintain staging environment
- Test all recovery plans
- Regular drills

### 4. Poor Security

**Problem:** Weak authentication, exposed credentials

**Solution:**
- Use environment variables
- Enable TLS
- Rotate tokens
- Audit logs

### 5. Missing Rollbacks

**Problem:** No way to undo failed recovery

**Solution:**
- Always define rollback commands
- Test rollbacks in staging
- Document manual rollback procedures

## Summary

**Key Takeaways:**

1. **Safety First**: Start conservative, test extensively
2. **Monitor Everything**: Including the monitoring system
3. **Clear Communication**: Keep admin lists updated
4. **Security Always**: Never compromise on security
5. **Test Regularly**: Drills and staging are essential
6. **Document Everything**: Recovery plans, procedures, decisions
7. **Review and Improve**: Learn from each recovery

## Additional Resources

- [Setup Guide](AUTOPILOT_SETUP.md)
- [Usage Examples](examples/)
- [API Reference](API_REFERENCE.md)
- [Troubleshooting](TROUBLESHOOTING.md)
