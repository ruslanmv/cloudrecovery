"""
CloudRecovery Autopilot - Basic Usage Examples

This file demonstrates basic usage patterns for the autopilot system.
"""

import asyncio
from datetime import datetime

from cloudrecovery.notifications import (
    EmailNotifier,
    NotificationConfig,
    NotificationEvent,
    NotificationPriority,
)
from cloudrecovery.autopilot import (
    RecoveryEngine,
    RecoveryPlan,
    RecoveryAction,
    SafetyMonitor,
    SessionManager,
)
from cloudrecovery.autopilot.safety_monitor import SafetyLevel


# =============================================================================
# Example 1: Send Email Notification
# =============================================================================

async def example_send_email():
    """Send a test email notification to admins."""

    # Configure email settings
    config = NotificationConfig(
        smtp_host="smtp.gmail.com",
        smtp_port=587,
        smtp_use_tls=True,
        smtp_username="your-email@gmail.com",
        smtp_password="your-app-password",
        smtp_from_email="cloudrecovery@example.com",
        smtp_from_name="CloudRecovery Autopilot",
        admin_emails=["admin@example.com"],
        enabled=True,
        min_priority=NotificationPriority.MEDIUM,
    )

    # Create email notifier
    notifier = EmailNotifier(config)

    # Create notification event
    event = NotificationEvent(
        event_id="test-001",
        priority=NotificationPriority.HIGH,
        service_name="nginx",
        service_type="website",
        status="down",
        title="Website Down - nginx Not Responding",
        message="The nginx web server is not responding to health checks. "
                "Autopilot recovery has been initiated.",
        details={
            "url": "https://example.com",
            "error": "Connection timeout after 10 seconds",
            "last_success": "2024-01-15 14:30:00 UTC",
        },
        recovery_attempted=True,
        recovery_session_url="https://cloudrecovery.example.com/monitor/abc123?token=xyz",
        agent_id="agent-prod-01",
        environment="production",
    )

    # Send notification
    result = await notifier.send_notification(event)

    if result.success:
        print(f"✓ Email sent successfully to {len(result.recipients)} recipients")
    else:
        print(f"✗ Failed to send email: {result.error}")


# =============================================================================
# Example 2: Basic Recovery Plan Execution
# =============================================================================

async def example_basic_recovery():
    """Execute a simple recovery plan with safety monitoring."""

    # Create safety monitor
    safety = SafetyMonitor(
        max_safety_level=SafetyLevel.MEDIUM,
        require_approval_above=SafetyLevel.LOW,
    )

    # Create recovery engine
    engine = RecoveryEngine(safety_monitor=safety)

    # Create recovery plan
    plan = RecoveryPlan(
        service_name="nginx",
        service_type="website",
        actions=[
            RecoveryAction(
                description="Check nginx service status",
                command="systemctl status nginx",
                safety_level=SafetyLevel.SAFE,
            ),
            RecoveryAction(
                description="Check nginx error logs",
                command="tail -n 50 /var/log/nginx/error.log",
                safety_level=SafetyLevel.SAFE,
            ),
            RecoveryAction(
                description="Test port 80 connectivity",
                command="netstat -tlnp | grep :80",
                safety_level=SafetyLevel.SAFE,
            ),
            RecoveryAction(
                description="Restart nginx service",
                command="systemctl restart nginx",
                safety_level=SafetyLevel.LOW,
                requires_approval=False,  # Auto-approve for this example
                rollback_command="systemctl start nginx",
            ),
        ],
    )

    # Execute recovery
    print(f"Starting recovery for {plan.service_name}...")
    results = await engine.execute_recovery(plan, auto_approve_safe=True)

    # Display results
    for result in results:
        status_icon = "✓" if result.status == "completed" else "✗"
        print(f"\n{status_icon} Action: {result.action_id}")
        print(f"  Status: {result.status}")
        if result.output:
            print(f"  Output: {result.output[:100]}...")
        if result.error:
            print(f"  Error: {result.error}")


# =============================================================================
# Example 3: Recovery with Monitoring Session
# =============================================================================

async def example_recovery_with_monitoring():
    """Execute recovery and create monitoring session for admins."""

    # Create session manager
    session_manager = SessionManager(
        base_url="https://cloudrecovery.example.com",
        default_session_duration_hours=24,
    )

    # Create safety monitor
    safety = SafetyMonitor(max_safety_level=SafetyLevel.MEDIUM)

    # Create recovery engine
    engine = RecoveryEngine(safety_monitor=safety)

    # Create recovery plan
    plan = RecoveryPlan(
        service_name="postgresql",
        service_type="postgresql",
        actions=[
            RecoveryAction(
                description="Check PostgreSQL status",
                command="systemctl status postgresql",
                safety_level=SafetyLevel.SAFE,
            ),
            RecoveryAction(
                description="Check PostgreSQL logs",
                command="tail -n 100 /var/log/postgresql/postgresql-*.log",
                safety_level=SafetyLevel.SAFE,
            ),
            RecoveryAction(
                description="Restart PostgreSQL",
                command="systemctl restart postgresql",
                safety_level=SafetyLevel.MEDIUM,
                requires_approval=True,
            ),
        ],
    )

    # Create monitoring session
    session = session_manager.create_session(plan, priority="high")

    print(f"Recovery session created!")
    print(f"Monitor at: {session.get_session_url(session_manager.base_url)}")
    print(f"Session ID: {session.session_id}")
    print(f"Expires: {session.expires_at}")

    # Execute recovery
    results = await engine.execute_recovery(plan, auto_approve_safe=True)

    print(f"\nRecovery completed with {len(results)} actions")


# =============================================================================
# Example 4: Safety Checks
# =============================================================================

def example_safety_checks():
    """Demonstrate safety checking for commands."""

    # Create safety monitor
    safety = SafetyMonitor(max_safety_level=SafetyLevel.MEDIUM)

    # Test various commands
    commands = [
        "systemctl status nginx",
        "cat /var/log/nginx/error.log",
        "systemctl restart nginx",
        "rm -rf /tmp/cache",
        "DROP TABLE users",
        "sudo reboot",
    ]

    print("Safety Check Results:")
    print("=" * 60)

    for cmd in commands:
        check = safety.check_command(cmd)

        status = "✓ PASS" if check.passed else "✗ BLOCK"
        print(f"\n{status} | {cmd}")
        print(f"  Level: {check.level}")
        print(f"  Reason: {check.reason}")

        # Log the check
        safety.log_operation(
            command=cmd,
            safety_check=check,
            executed=False,
        )


# =============================================================================
# Example 5: Emergency Stop
# =============================================================================

async def example_emergency_stop():
    """Demonstrate emergency stop functionality."""

    # Create safety monitor
    safety = SafetyMonitor(max_safety_level=SafetyLevel.HIGH)

    # Create recovery engine
    engine = RecoveryEngine(safety_monitor=safety)

    # Create a risky recovery plan
    plan = RecoveryPlan(
        service_name="database",
        service_type="postgresql",
        actions=[
            RecoveryAction(
                description="Check database status",
                command="systemctl status postgresql",
                safety_level=SafetyLevel.SAFE,
            ),
            RecoveryAction(
                description="Backup database",
                command="pg_dump mydb > /backup/mydb.sql",
                safety_level=SafetyLevel.MEDIUM,
            ),
            RecoveryAction(
                description="Restart database",
                command="systemctl restart postgresql",
                safety_level=SafetyLevel.HIGH,
                requires_approval=True,
            ),
        ],
    )

    # Start recovery in background
    print("Starting recovery...")

    # Simulate admin triggering emergency stop
    print("\n⚠️ Admin triggered EMERGENCY STOP!")
    safety.activate_emergency_stop()

    # Try to execute recovery (will be blocked)
    results = await engine.execute_recovery(plan, auto_approve_safe=True)

    # All actions should be emergency stopped
    for result in results:
        print(f"\nAction: {result.action_id}")
        print(f"Status: {result.status}")
        if result.status == "emergency_stopped":
            print("✓ Correctly blocked by emergency stop")


# =============================================================================
# Example 6: Multi-Service Recovery
# =============================================================================

async def example_multi_service_recovery():
    """Execute recovery plans for multiple services."""

    # Create safety monitor and engine
    safety = SafetyMonitor(max_safety_level=SafetyLevel.MEDIUM)
    engine = RecoveryEngine(safety_monitor=safety)

    # Create plans for multiple services
    services = ["nginx", "postgresql", "redis"]

    for service in services:
        plan = RecoveryPlan(
            service_name=service,
            service_type="service",
            actions=[
                RecoveryAction(
                    description=f"Check {service} status",
                    command=f"systemctl status {service}",
                    safety_level=SafetyLevel.SAFE,
                ),
                RecoveryAction(
                    description=f"Restart {service}",
                    command=f"systemctl restart {service}",
                    safety_level=SafetyLevel.LOW,
                ),
            ],
        )

        print(f"\n{'='*60}")
        print(f"Recovering {service}...")
        print('='*60)

        results = await engine.execute_recovery(plan, auto_approve_safe=True)

        for result in results:
            status = "✓" if result.status == "completed" else "✗"
            print(f"{status} {result.action_id}: {result.status}")


# =============================================================================
# Example 7: Custom Recovery Plan from Evidence
# =============================================================================

async def example_custom_recovery():
    """Create and execute a custom recovery plan based on evidence."""

    from cloudrecovery.signals.models import Evidence

    # Create safety monitor and engine
    safety = SafetyMonitor(max_safety_level=SafetyLevel.MEDIUM)
    engine = RecoveryEngine(safety_monitor=safety)

    # Create evidence of failure
    evidence = Evidence(
        source="synthetics",
        kind="site_check",
        severity="critical",
        message="Website health check failed: Connection timeout",
        payload={
            "url": "https://example.com",
            "status_code": None,
            "latency_ms": None,
            "error": "Connection timeout after 10 seconds",
        },
    )

    # Create recovery plan from evidence
    plan = engine.create_plan_from_evidence(evidence)

    if plan:
        print(f"Created recovery plan with {len(plan.actions)} actions")
        print(f"Service: {plan.service_name} ({plan.service_type})")

        # Execute the plan
        results = await engine.execute_recovery(plan, auto_approve_safe=True)

        print(f"\nRecovery results:")
        for result in results:
            print(f"  - {result.status}")
    else:
        print("No recovery plan could be created for this evidence")


# =============================================================================
# Main - Run Examples
# =============================================================================

if __name__ == "__main__":
    print("CloudRecovery Autopilot - Usage Examples")
    print("=" * 60)

    # Run sync example
    print("\n\n# Example 4: Safety Checks")
    print("-" * 60)
    example_safety_checks()

    # Run async examples
    print("\n\n# Example 2: Basic Recovery")
    print("-" * 60)
    asyncio.run(example_basic_recovery())

    # Uncomment to run other examples:

    # print("\n\n# Example 1: Send Email")
    # asyncio.run(example_send_email())

    # print("\n\n# Example 3: Recovery with Monitoring")
    # asyncio.run(example_recovery_with_monitoring())

    # print("\n\n# Example 5: Emergency Stop")
    # asyncio.run(example_emergency_stop())

    # print("\n\n# Example 6: Multi-Service Recovery")
    # asyncio.run(example_multi_service_recovery())

    # print("\n\n# Example 7: Custom Recovery from Evidence")
    # asyncio.run(example_custom_recovery())

    print("\n\n" + "=" * 60)
    print("Examples completed!")
