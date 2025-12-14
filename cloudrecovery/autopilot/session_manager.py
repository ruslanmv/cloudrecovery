"""Session management for admin monitoring of autopilot operations."""

import logging
import secrets
from typing import Dict, List, Optional, Set
from datetime import datetime, timedelta
from pydantic import BaseModel, Field
import uuid

from cloudrecovery.autopilot.recovery_engine import RecoveryPlan, RecoveryResult

logger = logging.getLogger(__name__)


class MonitoringSession(BaseModel):
    """Admin monitoring session for a recovery operation."""

    session_id: str = Field(default_factory=lambda: str(uuid.uuid4()), description="Unique session ID")
    session_token: str = Field(default_factory=lambda: secrets.token_urlsafe(32), description="Secret session token")
    plan_id: str = Field(description="ID of recovery plan being monitored")

    created_at: datetime = Field(default_factory=datetime.utcnow, description="Session creation time")
    expires_at: datetime = Field(description="Session expiration time")
    last_accessed: datetime = Field(default_factory=datetime.utcnow, description="Last access time")

    # Session metadata
    service_name: str = Field(description="Service being recovered")
    service_type: str = Field(description="Type of service")
    priority: str = Field(default="medium", description="Recovery priority")

    # Connected admins
    connected_admins: Set[str] = Field(default_factory=set, description="Set of connected admin IDs")

    # Emergency stop tracking
    emergency_stopped: bool = Field(default=False, description="Whether emergency stop was triggered")
    stopped_by: Optional[str] = Field(default=None, description="Admin who stopped recovery")
    stop_reason: Optional[str] = Field(default=None, description="Reason for stop")

    class Config:
        use_enum_values = True

    def is_expired(self) -> bool:
        """Check if session is expired.

        Returns:
            True if session is expired
        """
        return datetime.utcnow() > self.expires_at

    def get_session_url(self, base_url: str) -> str:
        """Get the monitoring URL for this session.

        Args:
            base_url: Base URL of the server (e.g., https://recovery.example.com)

        Returns:
            Full monitoring URL with token
        """
        return f"{base_url}/monitor/{self.session_id}?token={self.session_token}"


class SessionManager:
    """Manages monitoring sessions for recovery operations."""

    def __init__(
        self,
        base_url: str = "http://localhost:8787",
        default_session_duration_hours: int = 24,
    ):
        """Initialize session manager.

        Args:
            base_url: Base URL for generating session links
            default_session_duration_hours: Default session duration in hours
        """
        self.base_url = base_url
        self.default_session_duration_hours = default_session_duration_hours

        # Active sessions
        self.sessions: Dict[str, MonitoringSession] = {}

        # Session by plan ID (for quick lookup)
        self.plan_sessions: Dict[str, str] = {}  # plan_id -> session_id

        # Session by token (for authentication)
        self.token_sessions: Dict[str, str] = {}  # token -> session_id

    def create_session(
        self,
        plan: RecoveryPlan,
        priority: str = "medium",
        duration_hours: Optional[int] = None,
    ) -> MonitoringSession:
        """Create a new monitoring session for a recovery plan.

        Args:
            plan: Recovery plan to monitor
            priority: Recovery priority
            duration_hours: Session duration in hours (None = use default)

        Returns:
            MonitoringSession with unique session URL
        """
        duration = duration_hours or self.default_session_duration_hours

        session = MonitoringSession(
            plan_id=plan.plan_id,
            expires_at=datetime.utcnow() + timedelta(hours=duration),
            service_name=plan.service_name,
            service_type=plan.service_type,
            priority=priority,
        )

        # Store session
        self.sessions[session.session_id] = session
        self.plan_sessions[plan.plan_id] = session.session_id
        self.token_sessions[session.session_token] = session.session_id

        logger.info(
            f"Created monitoring session {session.session_id} for plan {plan.plan_id} "
            f"(expires in {duration}h)"
        )

        return session

    def get_session(self, session_id: str) -> Optional[MonitoringSession]:
        """Get session by ID.

        Args:
            session_id: Session ID

        Returns:
            MonitoringSession or None if not found
        """
        session = self.sessions.get(session_id)

        if session and session.is_expired():
            logger.info(f"Session {session_id} has expired, removing")
            self._remove_session(session)
            return None

        return session

    def get_session_by_token(self, token: str) -> Optional[MonitoringSession]:
        """Get session by authentication token.

        Args:
            token: Session token

        Returns:
            MonitoringSession or None if not found/invalid
        """
        session_id = self.token_sessions.get(token)
        if not session_id:
            return None

        return self.get_session(session_id)

    def get_session_by_plan(self, plan_id: str) -> Optional[MonitoringSession]:
        """Get session by recovery plan ID.

        Args:
            plan_id: Recovery plan ID

        Returns:
            MonitoringSession or None if not found
        """
        session_id = self.plan_sessions.get(plan_id)
        if not session_id:
            return None

        return self.get_session(session_id)

    def authenticate_session(self, session_id: str, token: str) -> bool:
        """Authenticate a session access attempt.

        Args:
            session_id: Session ID
            token: Session token

        Returns:
            True if authentication succeeds
        """
        session = self.get_session(session_id)

        if not session:
            logger.warning(f"Authentication failed: session {session_id} not found")
            return False

        if session.session_token != token:
            logger.warning(f"Authentication failed: invalid token for session {session_id}")
            return False

        # Update last accessed time
        session.last_accessed = datetime.utcnow()

        return True

    def connect_admin(self, session_id: str, admin_id: str) -> bool:
        """Register an admin as connected to a session.

        Args:
            session_id: Session ID
            admin_id: Admin identifier

        Returns:
            True if successful
        """
        session = self.get_session(session_id)

        if not session:
            return False

        session.connected_admins.add(admin_id)
        session.last_accessed = datetime.utcnow()

        logger.info(f"Admin {admin_id} connected to session {session_id}")

        return True

    def disconnect_admin(self, session_id: str, admin_id: str) -> bool:
        """Unregister an admin from a session.

        Args:
            session_id: Session ID
            admin_id: Admin identifier

        Returns:
            True if successful
        """
        session = self.get_session(session_id)

        if not session:
            return False

        session.connected_admins.discard(admin_id)

        logger.info(f"Admin {admin_id} disconnected from session {session_id}")

        return True

    def emergency_stop_session(
        self,
        session_id: str,
        admin_id: str,
        reason: str = "Admin emergency stop",
    ) -> bool:
        """Trigger emergency stop for a session.

        Args:
            session_id: Session ID
            admin_id: Admin who triggered stop
            reason: Reason for emergency stop

        Returns:
            True if successful
        """
        session = self.get_session(session_id)

        if not session:
            return False

        session.emergency_stopped = True
        session.stopped_by = admin_id
        session.stop_reason = reason

        logger.critical(
            f"EMERGENCY STOP triggered for session {session_id} by admin {admin_id}: {reason}"
        )

        return True

    def extend_session(self, session_id: str, additional_hours: int = 24) -> bool:
        """Extend session expiration time.

        Args:
            session_id: Session ID
            additional_hours: Hours to add to expiration

        Returns:
            True if successful
        """
        session = self.get_session(session_id)

        if not session:
            return False

        session.expires_at += timedelta(hours=additional_hours)

        logger.info(f"Extended session {session_id} by {additional_hours}h")

        return True

    def close_session(self, session_id: str) -> bool:
        """Close and remove a session.

        Args:
            session_id: Session ID

        Returns:
            True if successful
        """
        session = self.sessions.get(session_id)

        if not session:
            return False

        self._remove_session(session)

        logger.info(f"Closed session {session_id}")

        return True

    def _remove_session(self, session: MonitoringSession) -> None:
        """Remove session from all tracking structures.

        Args:
            session: Session to remove
        """
        # Remove from sessions dict
        self.sessions.pop(session.session_id, None)

        # Remove from plan lookup
        self.plan_sessions.pop(session.plan_id, None)

        # Remove from token lookup
        self.token_sessions.pop(session.session_token, None)

    def cleanup_expired_sessions(self) -> int:
        """Remove expired sessions.

        Returns:
            Number of sessions removed
        """
        expired = [
            session_id
            for session_id, session in self.sessions.items()
            if session.is_expired()
        ]

        for session_id in expired:
            session = self.sessions[session_id]
            self._remove_session(session)

        if expired:
            logger.info(f"Cleaned up {len(expired)} expired sessions")

        return len(expired)

    def get_all_sessions(self) -> List[MonitoringSession]:
        """Get all active sessions.

        Returns:
            List of active sessions
        """
        # Cleanup expired sessions first
        self.cleanup_expired_sessions()

        return list(self.sessions.values())

    def get_session_stats(self) -> Dict:
        """Get session statistics.

        Returns:
            Dictionary with session statistics
        """
        self.cleanup_expired_sessions()

        return {
            "total_sessions": len(self.sessions),
            "emergency_stopped": sum(1 for s in self.sessions.values() if s.emergency_stopped),
            "connected_admins": sum(len(s.connected_admins) for s in self.sessions.values()),
            "sessions_by_service_type": {
                service_type: sum(1 for s in self.sessions.values() if s.service_type == service_type)
                for service_type in set(s.service_type for s in self.sessions.values())
            },
        }
