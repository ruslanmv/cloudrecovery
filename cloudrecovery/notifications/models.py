"""Notification models for CloudRecovery."""

from enum import Enum
from typing import List, Optional
from datetime import datetime
from pydantic import BaseModel, EmailStr, Field


class NotificationPriority(str, Enum):
    """Notification priority levels."""
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


class NotificationChannel(str, Enum):
    """Notification delivery channels."""
    EMAIL = "email"
    WEBHOOK = "webhook"
    SLACK = "slack"


class NotificationConfig(BaseModel):
    """Configuration for notification system."""

    # Email settings
    smtp_host: str = Field(default="localhost", description="SMTP server hostname")
    smtp_port: int = Field(default=587, description="SMTP server port")
    smtp_username: Optional[str] = Field(default=None, description="SMTP username")
    smtp_password: Optional[str] = Field(default=None, description="SMTP password")
    smtp_use_tls: bool = Field(default=True, description="Use TLS for SMTP")
    smtp_from_email: EmailStr = Field(default="noreply@cloudrecovery.local", description="From email address")
    smtp_from_name: str = Field(default="CloudRecovery Daemon", description="From name")

    # Admin contacts
    admin_emails: List[EmailStr] = Field(default_factory=list, description="List of admin email addresses")

    # Notification settings
    enabled: bool = Field(default=True, description="Enable/disable notifications")
    min_priority: NotificationPriority = Field(default=NotificationPriority.MEDIUM, description="Minimum priority to send")
    rate_limit_seconds: int = Field(default=300, description="Minimum seconds between duplicate notifications")

    # Webhook settings (optional)
    webhook_url: Optional[str] = Field(default=None, description="Webhook URL for notifications")
    webhook_enabled: bool = Field(default=False, description="Enable webhook notifications")

    class Config:
        use_enum_values = True


class NotificationEvent(BaseModel):
    """Event that triggers a notification."""

    event_id: str = Field(description="Unique event identifier")
    timestamp: datetime = Field(default_factory=datetime.utcnow, description="Event timestamp")
    priority: NotificationPriority = Field(description="Event priority")

    # Event details
    service_name: str = Field(description="Name of affected service (website, postgresql, mcp)")
    service_type: str = Field(description="Type of service")
    status: str = Field(description="Service status (down, degraded, recovered)")

    # Event context
    title: str = Field(description="Notification title")
    message: str = Field(description="Notification message")
    details: Optional[dict] = Field(default=None, description="Additional event details")

    # Recovery information
    recovery_attempted: bool = Field(default=False, description="Whether auto-recovery was attempted")
    recovery_success: Optional[bool] = Field(default=None, description="Recovery outcome")
    recovery_session_url: Optional[str] = Field(default=None, description="URL to monitor recovery session")

    # Agent information
    agent_id: Optional[str] = Field(default=None, description="ID of agent that detected the event")
    environment: Optional[str] = Field(default=None, description="Environment (prod, staging, dev)")

    class Config:
        use_enum_values = True


class NotificationTemplate(BaseModel):
    """Email template for notifications."""

    subject: str = Field(description="Email subject template")
    html_body: str = Field(description="HTML email body template")
    text_body: str = Field(description="Plain text email body template")


class NotificationStatus(BaseModel):
    """Status of a sent notification."""

    notification_id: str = Field(description="Unique notification ID")
    event_id: str = Field(description="Related event ID")
    timestamp: datetime = Field(default_factory=datetime.utcnow, description="Send timestamp")
    channel: NotificationChannel = Field(description="Delivery channel used")
    recipients: List[str] = Field(description="List of recipients")
    success: bool = Field(description="Whether notification was sent successfully")
    error: Optional[str] = Field(default=None, description="Error message if failed")

    class Config:
        use_enum_values = True
