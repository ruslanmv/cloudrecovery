"""Safety monitoring system for autopilot operations."""

import logging
import re
from typing import List, Dict, Optional, Set, Callable
from datetime import datetime
from enum import Enum
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)


class SafetyLevel(str, Enum):
    """Safety levels for operations."""
    SAFE = "safe"              # Read-only, no risk
    LOW = "low"                # Minimal risk (restart service)
    MEDIUM = "medium"          # Moderate risk (modify config)
    HIGH = "high"              # High risk (database operations)
    CRITICAL = "critical"      # Critical risk (destructive operations)


class SafetyCheck(BaseModel):
    """Result of a safety check."""

    check_name: str = Field(description="Name of the safety check")
    passed: bool = Field(description="Whether the check passed")
    level: SafetyLevel = Field(description="Safety level assessed")
    reason: Optional[str] = Field(default=None, description="Reason for failure")
    details: Optional[dict] = Field(default=None, description="Additional details")

    class Config:
        use_enum_values = True


class SafetyViolation(Exception):
    """Raised when a safety check fails."""

    def __init__(self, check: SafetyCheck):
        self.check = check
        super().__init__(f"Safety violation: {check.check_name} - {check.reason}")


class SafetyMonitor:
    """Monitors operations for safety violations."""

    # Dangerous command patterns
    DESTRUCTIVE_PATTERNS = [
        r'\brm\s+-rf\s+/',
        r'\bmkfs\b',
        r'\bdd\s+if=',
        r'\b:\s*>\s*/dev/sd',
        r'\bformat\s+[a-z]:\b',
        r'\bfdisk\b.*\bdelete\b',
        r'\bparted\b.*\brm\b',
        r'\bcryptsetup\s+luksFormat\b',
    ]

    # Dangerous system modifications
    SYSTEM_RISK_PATTERNS = [
        r'\brm\b.*\b(passwd|shadow|sudoers)\b',
        r'\bchmod\s+777\s+/etc',
        r'\bchown\s+.*\s+/etc',
        r'\buseradd\b.*\broot\b',
        r'\bpasswd\s+root\b',
        r'\bsudo\s+su\b',
        r'\biptables\s+-F\b',
        r'\bsystemctl\s+(stop|disable).*sshd\b',
    ]

    # Database danger patterns
    DATABASE_RISK_PATTERNS = [
        r'\bDROP\s+DATABASE\b',
        r'\bDROP\s+TABLE\b.*\bCASCADE\b',
        r'\bTRUNCATE\s+TABLE\b',
        r'\bDELETE\s+FROM\b.*\bWHERE\s+1\s*=\s*1\b',
        r'\bDELETE\s+FROM\b(?!.*WHERE)',
        r'\bUPDATE\b.*\bSET\b(?!.*WHERE)',
        r'\bALTER\s+TABLE\b.*\bDROP\s+COLUMN\b',
    ]

    # Shutdown/reboot patterns
    AVAILABILITY_RISK_PATTERNS = [
        r'\bshutdown\b',
        r'\breboot\b',
        r'\bhalt\b',
        r'\bpoweroff\b',
        r'\binit\s+[06]\b',
        r'\bsystemctl\s+(reboot|poweroff|halt)\b',
    ]

    def __init__(
        self,
        max_safety_level: SafetyLevel = SafetyLevel.MEDIUM,
        require_approval_above: SafetyLevel = SafetyLevel.LOW,
        blocked_commands: Optional[List[str]] = None,
        allowed_commands: Optional[List[str]] = None,
    ):
        """Initialize safety monitor.

        Args:
            max_safety_level: Maximum allowed safety level without approval
            require_approval_above: Level above which approval is required
            blocked_commands: Additional commands to block
            allowed_commands: Commands explicitly allowed (overrides blocks)
        """
        self.max_safety_level = max_safety_level
        self.require_approval_above = require_approval_above
        self.blocked_commands: Set[str] = set(blocked_commands or [])
        self.allowed_commands: Set[str] = set(allowed_commands or [])

        # Track operations for audit
        self.operation_history: List[Dict] = []

        # Emergency stop flag
        self._emergency_stop = False

    def check_command(self, command: str, context: Optional[Dict] = None) -> SafetyCheck:
        """Check if a command is safe to execute.

        Args:
            command: Command to check
            context: Additional context (service, environment, etc.)

        Returns:
            SafetyCheck result
        """
        context = context or {}

        # Check emergency stop
        if self._emergency_stop:
            return SafetyCheck(
                check_name="emergency_stop",
                passed=False,
                level=SafetyLevel.CRITICAL,
                reason="Emergency stop activated - all operations blocked",
            )

        # Check allowed list first (overrides everything)
        if self._is_allowed(command):
            return SafetyCheck(
                check_name="allowed_list",
                passed=True,
                level=SafetyLevel.SAFE,
                reason="Command in allowed list",
            )

        # Check blocked list
        if self._is_blocked(command):
            return SafetyCheck(
                check_name="blocked_list",
                passed=False,
                level=SafetyLevel.CRITICAL,
                reason="Command in blocked list",
            )

        # Check for destructive operations
        for pattern in self.DESTRUCTIVE_PATTERNS:
            if re.search(pattern, command, re.IGNORECASE):
                return SafetyCheck(
                    check_name="destructive_operation",
                    passed=False,
                    level=SafetyLevel.CRITICAL,
                    reason=f"Destructive operation detected: {pattern}",
                    details={"pattern": pattern},
                )

        # Check for system risks
        for pattern in self.SYSTEM_RISK_PATTERNS:
            if re.search(pattern, command, re.IGNORECASE):
                return SafetyCheck(
                    check_name="system_risk",
                    passed=False,
                    level=SafetyLevel.CRITICAL,
                    reason=f"System modification risk detected: {pattern}",
                    details={"pattern": pattern},
                )

        # Check for database risks
        for pattern in self.DATABASE_RISK_PATTERNS:
            if re.search(pattern, command, re.IGNORECASE):
                return SafetyCheck(
                    check_name="database_risk",
                    passed=False,
                    level=SafetyLevel.HIGH,
                    reason=f"Dangerous database operation detected: {pattern}",
                    details={"pattern": pattern},
                )

        # Check for availability risks
        for pattern in self.AVAILABILITY_RISK_PATTERNS:
            if re.search(pattern, command, re.IGNORECASE):
                return SafetyCheck(
                    check_name="availability_risk",
                    passed=False,
                    level=SafetyLevel.HIGH,
                    reason=f"System availability risk detected: {pattern}",
                    details={"pattern": pattern},
                )

        # Assess general safety level
        safety_level = self._assess_command_level(command, context)

        # Check if level exceeds maximum
        level_order = {
            SafetyLevel.SAFE: 0,
            SafetyLevel.LOW: 1,
            SafetyLevel.MEDIUM: 2,
            SafetyLevel.HIGH: 3,
            SafetyLevel.CRITICAL: 4,
        }

        if level_order[safety_level] > level_order[self.max_safety_level]:
            return SafetyCheck(
                check_name="safety_level",
                passed=False,
                level=safety_level,
                reason=f"Operation level ({safety_level}) exceeds maximum allowed ({self.max_safety_level})",
            )

        return SafetyCheck(
            check_name="safety_level",
            passed=True,
            level=safety_level,
            reason="Command assessed as safe",
        )

    def check_approval_required(self, safety_level: SafetyLevel) -> bool:
        """Check if approval is required for a safety level.

        Args:
            safety_level: Safety level to check

        Returns:
            True if approval is required
        """
        level_order = {
            SafetyLevel.SAFE: 0,
            SafetyLevel.LOW: 1,
            SafetyLevel.MEDIUM: 2,
            SafetyLevel.HIGH: 3,
            SafetyLevel.CRITICAL: 4,
        }

        return level_order[safety_level] > level_order[self.require_approval_above]

    def _assess_command_level(self, command: str, context: Dict) -> SafetyLevel:
        """Assess the safety level of a command.

        Args:
            command: Command to assess
            context: Command context

        Returns:
            Assessed safety level
        """
        cmd_lower = command.lower()

        # Read-only operations
        readonly_keywords = ['cat', 'ls', 'grep', 'find', 'head', 'tail', 'less', 'more', 'echo', 'pwd', 'whoami']
        if any(cmd_lower.startswith(kw) for kw in readonly_keywords):
            return SafetyLevel.SAFE

        # SELECT queries
        if re.search(r'^\s*SELECT\b', command, re.IGNORECASE) and 'UPDATE' not in cmd_lower and 'DELETE' not in cmd_lower:
            return SafetyLevel.SAFE

        # Service restarts
        if re.search(r'\bsystemctl\s+(restart|reload)\b', command, re.IGNORECASE):
            return SafetyLevel.LOW

        # Configuration modifications
        if re.search(r'\b(sed|awk|perl)\b.*\b-i\b', command) or 'vim' in cmd_lower or 'nano' in cmd_lower:
            return SafetyLevel.MEDIUM

        # Database modifications with WHERE clauses
        if re.search(r'\b(UPDATE|DELETE)\b.*\bWHERE\b', command, re.IGNORECASE):
            return SafetyLevel.MEDIUM

        # Package management
        if re.search(r'\b(apt|yum|dnf)\s+(install|update|upgrade)\b', command, re.IGNORECASE):
            return SafetyLevel.MEDIUM

        # Default to MEDIUM for unknown commands
        return SafetyLevel.MEDIUM

    def _is_blocked(self, command: str) -> bool:
        """Check if command is in blocked list.

        Args:
            command: Command to check

        Returns:
            True if blocked
        """
        cmd_parts = command.split()
        if not cmd_parts:
            return False

        cmd_base = cmd_parts[0]
        return cmd_base in self.blocked_commands or command in self.blocked_commands

    def _is_allowed(self, command: str) -> bool:
        """Check if command is in allowed list.

        Args:
            command: Command to check

        Returns:
            True if allowed
        """
        cmd_parts = command.split()
        if not cmd_parts:
            return False

        cmd_base = cmd_parts[0]
        return cmd_base in self.allowed_commands or command in self.allowed_commands

    def activate_emergency_stop(self) -> None:
        """Activate emergency stop - blocks all operations."""
        logger.critical("EMERGENCY STOP ACTIVATED")
        self._emergency_stop = True

    def deactivate_emergency_stop(self) -> None:
        """Deactivate emergency stop."""
        logger.warning("Emergency stop deactivated")
        self._emergency_stop = False

    def is_emergency_stopped(self) -> bool:
        """Check if emergency stop is active.

        Returns:
            True if emergency stop is active
        """
        return self._emergency_stop

    def log_operation(
        self,
        command: str,
        safety_check: SafetyCheck,
        executed: bool,
        result: Optional[str] = None,
        admin_approved: bool = False,
    ) -> None:
        """Log an operation for audit trail.

        Args:
            command: Command that was checked/executed
            safety_check: Safety check result
            executed: Whether command was executed
            result: Execution result if executed
            admin_approved: Whether admin approved the operation
        """
        entry = {
            "timestamp": datetime.utcnow().isoformat(),
            "command": command,
            "safety_check": safety_check.dict(),
            "executed": executed,
            "result": result,
            "admin_approved": admin_approved,
        }

        self.operation_history.append(entry)

        # Log to logger as well
        if executed:
            logger.info(f"Operation executed: {command[:100]}... (approved={admin_approved})")
        else:
            logger.warning(f"Operation blocked: {command[:100]}... (reason={safety_check.reason})")

    def get_operation_history(self, limit: int = 100) -> List[Dict]:
        """Get recent operation history.

        Args:
            limit: Maximum number of entries to return

        Returns:
            List of operation log entries
        """
        return self.operation_history[-limit:]

    def clear_history(self) -> None:
        """Clear operation history."""
        self.operation_history.clear()
