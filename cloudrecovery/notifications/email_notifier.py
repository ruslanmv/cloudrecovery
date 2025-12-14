"""Email notification system for CloudRecovery."""

import asyncio
import logging
from typing import List, Optional, Dict
from datetime import datetime, timedelta
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
import aiosmtplib
import uuid

from cloudrecovery.notifications.models import (
    NotificationConfig,
    NotificationEvent,
    NotificationPriority,
    NotificationChannel,
    NotificationStatus,
)

logger = logging.getLogger(__name__)


class EmailNotifier:
    """Handles email notifications for CloudRecovery events."""

    def __init__(self, config: NotificationConfig):
        """Initialize email notifier.

        Args:
            config: Notification configuration
        """
        self.config = config
        self._last_sent: Dict[str, datetime] = {}  # Track rate limiting

    async def send_notification(self, event: NotificationEvent) -> NotificationStatus:
        """Send notification for an event.

        Args:
            event: Notification event to send

        Returns:
            NotificationStatus with send result
        """
        notification_id = str(uuid.uuid4())

        # Check if notifications are enabled
        if not self.config.enabled:
            logger.debug("Notifications disabled, skipping")
            return NotificationStatus(
                notification_id=notification_id,
                event_id=event.event_id,
                channel=NotificationChannel.EMAIL,
                recipients=[],
                success=False,
                error="Notifications disabled",
            )

        # Check priority threshold
        priority_levels = {
            NotificationPriority.LOW: 0,
            NotificationPriority.MEDIUM: 1,
            NotificationPriority.HIGH: 2,
            NotificationPriority.CRITICAL: 3,
        }

        if priority_levels[event.priority] < priority_levels[self.config.min_priority]:
            logger.debug(f"Event priority {event.priority} below threshold {self.config.min_priority}")
            return NotificationStatus(
                notification_id=notification_id,
                event_id=event.event_id,
                channel=NotificationChannel.EMAIL,
                recipients=[],
                success=False,
                error=f"Priority below threshold ({self.config.min_priority})",
            )

        # Check rate limiting
        if not self._should_send(event):
            logger.info(f"Rate limit hit for event type: {event.service_name}/{event.status}")
            return NotificationStatus(
                notification_id=notification_id,
                event_id=event.event_id,
                channel=NotificationChannel.EMAIL,
                recipients=[],
                success=False,
                error="Rate limited",
            )

        # Check if we have admin emails configured
        if not self.config.admin_emails:
            logger.warning("No admin emails configured, cannot send notification")
            return NotificationStatus(
                notification_id=notification_id,
                event_id=event.event_id,
                channel=NotificationChannel.EMAIL,
                recipients=[],
                success=False,
                error="No admin emails configured",
            )

        try:
            # Send email
            await self._send_email(event, self.config.admin_emails)

            # Update rate limiting tracker
            rate_limit_key = f"{event.service_name}:{event.status}"
            self._last_sent[rate_limit_key] = datetime.utcnow()

            logger.info(f"Notification sent for event {event.event_id} to {len(self.config.admin_emails)} recipients")

            return NotificationStatus(
                notification_id=notification_id,
                event_id=event.event_id,
                channel=NotificationChannel.EMAIL,
                recipients=self.config.admin_emails,
                success=True,
            )

        except Exception as e:
            logger.error(f"Failed to send notification: {e}", exc_info=True)
            return NotificationStatus(
                notification_id=notification_id,
                event_id=event.event_id,
                channel=NotificationChannel.EMAIL,
                recipients=self.config.admin_emails,
                success=False,
                error=str(e),
            )

    def _should_send(self, event: NotificationEvent) -> bool:
        """Check if we should send notification based on rate limiting.

        Args:
            event: Event to check

        Returns:
            True if notification should be sent
        """
        rate_limit_key = f"{event.service_name}:{event.status}"
        last_sent = self._last_sent.get(rate_limit_key)

        if last_sent is None:
            return True

        time_since_last = (datetime.utcnow() - last_sent).total_seconds()
        return time_since_last >= self.config.rate_limit_seconds

    async def _send_email(self, event: NotificationEvent, recipients: List[str]) -> None:
        """Send email notification.

        Args:
            event: Event to notify about
            recipients: List of recipient email addresses

        Raises:
            Exception: If email sending fails
        """
        # Build email subject
        subject = self._build_subject(event)

        # Build email body
        html_body = self._build_html_body(event)
        text_body = self._build_text_body(event)

        # Create message
        message = MIMEMultipart("alternative")
        message["Subject"] = subject
        message["From"] = f"{self.config.smtp_from_name} <{self.config.smtp_from_email}>"
        message["To"] = ", ".join(recipients)
        message["X-Priority"] = self._get_email_priority(event.priority)

        # Attach parts
        part1 = MIMEText(text_body, "plain")
        part2 = MIMEText(html_body, "html")
        message.attach(part1)
        message.attach(part2)

        # Send email
        smtp_kwargs = {
            "hostname": self.config.smtp_host,
            "port": self.config.smtp_port,
            "use_tls": self.config.smtp_use_tls,
        }

        if self.config.smtp_username and self.config.smtp_password:
            smtp_kwargs["username"] = self.config.smtp_username
            smtp_kwargs["password"] = self.config.smtp_password

        await aiosmtplib.send(
            message,
            **smtp_kwargs,
        )

    def _build_subject(self, event: NotificationEvent) -> str:
        """Build email subject line.

        Args:
            event: Notification event

        Returns:
            Email subject string
        """
        priority_emoji = {
            NotificationPriority.LOW: "ℹ️",
            NotificationPriority.MEDIUM: "⚠️",
            NotificationPriority.HIGH: "🔴",
            NotificationPriority.CRITICAL: "🚨",
        }

        emoji = priority_emoji.get(event.priority, "")
        env = f"[{event.environment.upper()}]" if event.environment else ""

        return f"{emoji} {env} CloudRecovery Alert: {event.title}"

    def _build_text_body(self, event: NotificationEvent) -> str:
        """Build plain text email body.

        Args:
            event: Notification event

        Returns:
            Plain text email body
        """
        lines = [
            "CloudRecovery Alert",
            "=" * 60,
            "",
            f"Priority: {event.priority.upper()}",
            f"Service: {event.service_name} ({event.service_type})",
            f"Status: {event.status.upper()}",
            f"Time: {event.timestamp.strftime('%Y-%m-%d %H:%M:%S UTC')}",
            "",
            "Message:",
            "-" * 60,
            event.message,
            "",
        ]

        if event.recovery_attempted:
            lines.extend([
                "Auto-Recovery:",
                "-" * 60,
                f"Attempted: Yes",
                f"Success: {event.recovery_success if event.recovery_success is not None else 'In Progress'}",
                "",
            ])

        if event.recovery_session_url:
            lines.extend([
                "Monitor Recovery:",
                "-" * 60,
                f"Session URL: {event.recovery_session_url}",
                "",
                "Click the link above to monitor the AI recovery process in real-time.",
                "You can stop any dangerous operations from the monitoring dashboard.",
                "",
            ])

        if event.details:
            lines.extend([
                "Additional Details:",
                "-" * 60,
            ])
            for key, value in event.details.items():
                lines.append(f"{key}: {value}")
            lines.append("")

        lines.extend([
            "=" * 60,
            "This is an automated notification from CloudRecovery Daemon.",
            f"Agent ID: {event.agent_id or 'N/A'}",
            f"Event ID: {event.event_id}",
        ])

        return "\n".join(lines)

    def _build_html_body(self, event: NotificationEvent) -> str:
        """Build HTML email body.

        Args:
            event: Notification event

        Returns:
            HTML email body
        """
        # Priority color coding
        priority_colors = {
            NotificationPriority.LOW: "#17a2b8",      # info blue
            NotificationPriority.MEDIUM: "#ffc107",    # warning yellow
            NotificationPriority.HIGH: "#fd7e14",      # orange
            NotificationPriority.CRITICAL: "#dc3545",  # danger red
        }

        color = priority_colors.get(event.priority, "#6c757d")

        html = f"""
<!DOCTYPE html>
<html>
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>CloudRecovery Alert</title>
</head>
<body style="font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, 'Helvetica Neue', Arial, sans-serif; line-height: 1.6; color: #333; max-width: 600px; margin: 0 auto; padding: 20px;">

    <div style="background: linear-gradient(135deg, {color} 0%, {color}dd 100%); color: white; padding: 30px; border-radius: 8px 8px 0 0; text-align: center;">
        <h1 style="margin: 0; font-size: 24px;">CloudRecovery Alert</h1>
        <p style="margin: 10px 0 0 0; font-size: 14px; opacity: 0.9;">{event.environment.upper() if event.environment else 'SYSTEM'} Environment</p>
    </div>

    <div style="background: #f8f9fa; padding: 30px; border-radius: 0 0 8px 8px;">

        <div style="background: white; padding: 20px; border-radius: 6px; margin-bottom: 20px; border-left: 4px solid {color};">
            <h2 style="margin: 0 0 15px 0; color: {color}; font-size: 20px;">{event.title}</h2>

            <table style="width: 100%; border-collapse: collapse; margin-bottom: 15px;">
                <tr>
                    <td style="padding: 8px 0; color: #6c757d; width: 120px;"><strong>Priority:</strong></td>
                    <td style="padding: 8px 0;"><span style="background: {color}; color: white; padding: 4px 12px; border-radius: 12px; font-size: 12px; font-weight: bold;">{event.priority.upper()}</span></td>
                </tr>
                <tr>
                    <td style="padding: 8px 0; color: #6c757d;"><strong>Service:</strong></td>
                    <td style="padding: 8px 0;">{event.service_name} <span style="color: #6c757d;">({event.service_type})</span></td>
                </tr>
                <tr>
                    <td style="padding: 8px 0; color: #6c757d;"><strong>Status:</strong></td>
                    <td style="padding: 8px 0; text-transform: uppercase; font-weight: bold; color: {color};">{event.status}</td>
                </tr>
                <tr>
                    <td style="padding: 8px 0; color: #6c757d;"><strong>Time:</strong></td>
                    <td style="padding: 8px 0;">{event.timestamp.strftime('%Y-%m-%d %H:%M:%S UTC')}</td>
                </tr>
            </table>

            <div style="background: #f8f9fa; padding: 15px; border-radius: 4px; margin-top: 15px;">
                <p style="margin: 0; white-space: pre-wrap;">{event.message}</p>
            </div>
        </div>
"""

        if event.recovery_attempted:
            recovery_status_color = "#28a745" if event.recovery_success else "#ffc107"
            recovery_text = "Success" if event.recovery_success else "In Progress"

            html += f"""
        <div style="background: white; padding: 20px; border-radius: 6px; margin-bottom: 20px;">
            <h3 style="margin: 0 0 15px 0; color: #495057; font-size: 16px;">🤖 Auto-Recovery Status</h3>
            <table style="width: 100%; border-collapse: collapse;">
                <tr>
                    <td style="padding: 8px 0; color: #6c757d; width: 120px;"><strong>Attempted:</strong></td>
                    <td style="padding: 8px 0;">Yes</td>
                </tr>
                <tr>
                    <td style="padding: 8px 0; color: #6c757d;"><strong>Status:</strong></td>
                    <td style="padding: 8px 0;"><span style="background: {recovery_status_color}; color: white; padding: 4px 12px; border-radius: 12px; font-size: 12px; font-weight: bold;">{recovery_text}</span></td>
                </tr>
            </table>
        </div>
"""

        if event.recovery_session_url:
            html += f"""
        <div style="background: #e7f3ff; border: 2px solid #0066cc; padding: 20px; border-radius: 6px; margin-bottom: 20px; text-align: center;">
            <h3 style="margin: 0 0 10px 0; color: #0066cc; font-size: 16px;">🔍 Monitor AI Recovery</h3>
            <p style="margin: 0 0 15px 0; color: #495057;">View real-time recovery progress and stop dangerous operations</p>
            <a href="{event.recovery_session_url}" style="display: inline-block; background: #0066cc; color: white; padding: 12px 30px; text-decoration: none; border-radius: 6px; font-weight: bold;">Open Monitoring Dashboard</a>
        </div>
"""

        if event.details:
            html += """
        <div style="background: white; padding: 20px; border-radius: 6px; margin-bottom: 20px;">
            <h3 style="margin: 0 0 15px 0; color: #495057; font-size: 16px;">Additional Details</h3>
            <table style="width: 100%; border-collapse: collapse;">
"""
            for key, value in event.details.items():
                html += f"""
                <tr>
                    <td style="padding: 8px 0; color: #6c757d; width: 150px;"><strong>{key}:</strong></td>
                    <td style="padding: 8px 0; word-break: break-all;">{value}</td>
                </tr>
"""
            html += """
            </table>
        </div>
"""

        html += f"""
        <div style="text-align: center; padding: 20px 0; color: #6c757d; font-size: 12px;">
            <p style="margin: 5px 0;">This is an automated notification from CloudRecovery Daemon</p>
            <p style="margin: 5px 0;">Agent ID: {event.agent_id or 'N/A'} | Event ID: {event.event_id}</p>
        </div>

    </div>

</body>
</html>
"""

        return html

    def _get_email_priority(self, priority: NotificationPriority) -> str:
        """Get email priority header value.

        Args:
            priority: Notification priority

        Returns:
            Email priority header value
        """
        if priority == NotificationPriority.CRITICAL:
            return "1"  # Highest
        elif priority == NotificationPriority.HIGH:
            return "2"  # High
        else:
            return "3"  # Normal
