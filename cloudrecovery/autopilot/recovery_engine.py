"""Automated recovery engine with safety controls."""

import asyncio
import logging
from typing import List, Optional, Dict, Callable
from datetime import datetime
from enum import Enum
from pydantic import BaseModel, Field
import uuid

from cloudrecovery.autopilot.safety_monitor import SafetyMonitor, SafetyCheck, SafetyLevel
from cloudrecovery.notifications.models import NotificationEvent, NotificationPriority
from cloudrecovery.signals.models import Evidence

logger = logging.getLogger(__name__)


class RecoveryStatus(str, Enum):
    """Status of recovery operation."""
    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    WAITING_APPROVAL = "waiting_approval"
    APPROVED = "approved"
    REJECTED = "rejected"
    COMPLETED = "completed"
    FAILED = "failed"
    EMERGENCY_STOPPED = "emergency_stopped"


class RecoveryAction(BaseModel):
    """A single recovery action to execute."""

    action_id: str = Field(default_factory=lambda: str(uuid.uuid4()), description="Unique action ID")
    description: str = Field(description="Human-readable description")
    command: str = Field(description="Command to execute")
    safety_level: SafetyLevel = Field(description="Safety level of this action")
    requires_approval: bool = Field(default=False, description="Whether admin approval is required")
    timeout_seconds: int = Field(default=300, description="Timeout for execution")
    rollback_command: Optional[str] = Field(default=None, description="Command to rollback if this fails")

    class Config:
        use_enum_values = True


class RecoveryResult(BaseModel):
    """Result of a recovery action."""

    action_id: str = Field(description="ID of the action")
    status: RecoveryStatus = Field(description="Result status")
    started_at: Optional[datetime] = Field(default=None, description="Start timestamp")
    completed_at: Optional[datetime] = Field(default=None, description="Completion timestamp")
    output: Optional[str] = Field(default=None, description="Command output")
    error: Optional[str] = Field(default=None, description="Error message if failed")
    safety_check: Optional[SafetyCheck] = Field(default=None, description="Safety check result")
    admin_approved_by: Optional[str] = Field(default=None, description="Admin who approved")

    class Config:
        use_enum_values = True


class RecoveryPlan(BaseModel):
    """Complete recovery plan with multiple actions."""

    plan_id: str = Field(default_factory=lambda: str(uuid.uuid4()), description="Unique plan ID")
    service_name: str = Field(description="Service being recovered")
    service_type: str = Field(description="Type of service (website, postgresql, mcp)")
    created_at: datetime = Field(default_factory=datetime.utcnow, description="Plan creation time")
    actions: List[RecoveryAction] = Field(description="List of recovery actions")
    evidence: Optional[Evidence] = Field(default=None, description="Evidence that triggered recovery")

    class Config:
        use_enum_values = True


class RecoveryEngine:
    """Manages automated recovery with safety controls."""

    def __init__(
        self,
        safety_monitor: SafetyMonitor,
        executor: Optional[Callable] = None,
        notification_callback: Optional[Callable] = None,
    ):
        """Initialize recovery engine.

        Args:
            safety_monitor: Safety monitor for checking operations
            executor: Function to execute commands (default: asyncio.create_subprocess_shell)
            notification_callback: Callback for sending notifications
        """
        self.safety_monitor = safety_monitor
        self.executor = executor or self._default_executor
        self.notification_callback = notification_callback

        # Track active recovery operations
        self.active_recoveries: Dict[str, RecoveryPlan] = {}
        self.recovery_results: Dict[str, List[RecoveryResult]] = {}

        # Approval queue
        self.pending_approvals: Dict[str, RecoveryAction] = {}

    async def execute_recovery(
        self,
        plan: RecoveryPlan,
        auto_approve_safe: bool = True,
    ) -> List[RecoveryResult]:
        """Execute a recovery plan.

        Args:
            plan: Recovery plan to execute
            auto_approve_safe: Automatically approve safe operations

        Returns:
            List of recovery results
        """
        logger.info(f"Starting recovery plan {plan.plan_id} for {plan.service_name}")

        self.active_recoveries[plan.plan_id] = plan
        results = []

        try:
            for action in plan.actions:
                # Check emergency stop
                if self.safety_monitor.is_emergency_stopped():
                    logger.critical(f"Emergency stop active - aborting recovery {plan.plan_id}")
                    result = RecoveryResult(
                        action_id=action.action_id,
                        status=RecoveryStatus.EMERGENCY_STOPPED,
                        error="Emergency stop activated",
                    )
                    results.append(result)
                    break

                # Execute action
                result = await self._execute_action(action, plan, auto_approve_safe)
                results.append(result)

                # Store results
                if plan.plan_id not in self.recovery_results:
                    self.recovery_results[plan.plan_id] = []
                self.recovery_results[plan.plan_id].append(result)

                # If action failed, stop execution
                if result.status == RecoveryStatus.FAILED:
                    logger.error(f"Action {action.action_id} failed, stopping recovery")
                    break

                # If action is waiting for approval, stop and wait
                if result.status == RecoveryStatus.WAITING_APPROVAL:
                    logger.info(f"Action {action.action_id} waiting for approval")
                    self.pending_approvals[action.action_id] = action
                    break

        finally:
            # Remove from active recoveries if completed
            if plan.plan_id in self.active_recoveries:
                if all(r.status in [RecoveryStatus.COMPLETED, RecoveryStatus.FAILED, RecoveryStatus.EMERGENCY_STOPPED] for r in results):
                    del self.active_recoveries[plan.plan_id]

        return results

    async def _execute_action(
        self,
        action: RecoveryAction,
        plan: RecoveryPlan,
        auto_approve_safe: bool,
    ) -> RecoveryResult:
        """Execute a single recovery action.

        Args:
            action: Action to execute
            plan: Recovery plan context
            auto_approve_safe: Auto-approve safe operations

        Returns:
            Recovery result
        """
        logger.info(f"Executing action {action.action_id}: {action.description}")

        result = RecoveryResult(
            action_id=action.action_id,
            status=RecoveryStatus.IN_PROGRESS,
            started_at=datetime.utcnow(),
        )

        # Safety check
        safety_check = self.safety_monitor.check_command(
            action.command,
            context={
                "service": plan.service_name,
                "service_type": plan.service_type,
            },
        )

        result.safety_check = safety_check

        # If safety check failed, don't execute
        if not safety_check.passed:
            logger.warning(f"Safety check failed for action {action.action_id}: {safety_check.reason}")
            result.status = RecoveryStatus.FAILED
            result.error = f"Safety check failed: {safety_check.reason}"
            result.completed_at = datetime.utcnow()

            # Log to safety monitor
            self.safety_monitor.log_operation(
                command=action.command,
                safety_check=safety_check,
                executed=False,
            )

            return result

        # Check if approval is required
        requires_approval = (
            action.requires_approval
            or self.safety_monitor.check_approval_required(safety_check.level)
        )

        if requires_approval and not auto_approve_safe:
            logger.info(f"Action {action.action_id} requires approval (level: {safety_check.level})")
            result.status = RecoveryStatus.WAITING_APPROVAL
            return result

        # Execute command
        try:
            output, error = await self.executor(action.command, action.timeout_seconds)

            if error:
                logger.error(f"Action {action.action_id} failed: {error}")
                result.status = RecoveryStatus.FAILED
                result.error = error

                # Attempt rollback if available
                if action.rollback_command:
                    logger.info(f"Attempting rollback for action {action.action_id}")
                    try:
                        await self.executor(action.rollback_command, action.timeout_seconds)
                    except Exception as e:
                        logger.error(f"Rollback failed: {e}")
            else:
                logger.info(f"Action {action.action_id} completed successfully")
                result.status = RecoveryStatus.COMPLETED
                result.output = output

            result.completed_at = datetime.utcnow()

            # Log to safety monitor
            self.safety_monitor.log_operation(
                command=action.command,
                safety_check=safety_check,
                executed=True,
                result=output if not error else error,
                admin_approved=not auto_approve_safe,
            )

        except asyncio.TimeoutError:
            logger.error(f"Action {action.action_id} timed out after {action.timeout_seconds}s")
            result.status = RecoveryStatus.FAILED
            result.error = f"Timeout after {action.timeout_seconds} seconds"
            result.completed_at = datetime.utcnow()

        except Exception as e:
            logger.error(f"Action {action.action_id} raised exception: {e}", exc_info=True)
            result.status = RecoveryStatus.FAILED
            result.error = str(e)
            result.completed_at = datetime.utcnow()

        return result

    async def approve_action(self, action_id: str, admin_id: str) -> RecoveryResult:
        """Approve a pending action.

        Args:
            action_id: ID of action to approve
            admin_id: ID of admin approving

        Returns:
            Recovery result after execution
        """
        if action_id not in self.pending_approvals:
            raise ValueError(f"No pending approval for action {action_id}")

        action = self.pending_approvals.pop(action_id)

        logger.info(f"Action {action_id} approved by admin {admin_id}")

        # Find the plan this action belongs to
        plan = None
        for recovery_plan in self.active_recoveries.values():
            if any(a.action_id == action_id for a in recovery_plan.actions):
                plan = recovery_plan
                break

        if not plan:
            raise ValueError(f"No active recovery plan found for action {action_id}")

        # Execute the approved action
        result = await self._execute_action(action, plan, auto_approve_safe=False)
        result.admin_approved_by = admin_id

        return result

    async def reject_action(self, action_id: str, admin_id: str) -> None:
        """Reject a pending action.

        Args:
            action_id: ID of action to reject
            admin_id: ID of admin rejecting
        """
        if action_id not in self.pending_approvals:
            raise ValueError(f"No pending approval for action {action_id}")

        action = self.pending_approvals.pop(action_id)

        logger.warning(f"Action {action_id} rejected by admin {admin_id}")

        # Record the rejection
        result = RecoveryResult(
            action_id=action_id,
            status=RecoveryStatus.REJECTED,
            error=f"Rejected by admin {admin_id}",
            completed_at=datetime.utcnow(),
        )

        # Find plan and store result
        for plan_id, recovery_plan in self.active_recoveries.items():
            if any(a.action_id == action_id for a in recovery_plan.actions):
                if plan_id not in self.recovery_results:
                    self.recovery_results[plan_id] = []
                self.recovery_results[plan_id].append(result)
                break

    async def _default_executor(self, command: str, timeout: int) -> tuple[str, str]:
        """Default command executor using asyncio.

        Args:
            command: Command to execute
            timeout: Timeout in seconds

        Returns:
            Tuple of (stdout, stderr)
        """
        proc = await asyncio.create_subprocess_shell(
            command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )

        try:
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(),
                timeout=timeout,
            )

            return stdout.decode(), stderr.decode()

        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            raise

    def get_active_recoveries(self) -> Dict[str, RecoveryPlan]:
        """Get currently active recovery operations.

        Returns:
            Dictionary of plan_id -> RecoveryPlan
        """
        return self.active_recoveries.copy()

    def get_recovery_results(self, plan_id: str) -> List[RecoveryResult]:
        """Get results for a recovery plan.

        Args:
            plan_id: ID of recovery plan

        Returns:
            List of recovery results
        """
        return self.recovery_results.get(plan_id, [])

    def get_pending_approvals(self) -> Dict[str, RecoveryAction]:
        """Get actions waiting for approval.

        Returns:
            Dictionary of action_id -> RecoveryAction
        """
        return self.pending_approvals.copy()

    def create_plan_from_evidence(self, evidence: Evidence) -> Optional[RecoveryPlan]:
        """Create a recovery plan from evidence.

        Args:
            evidence: Evidence of service failure

        Returns:
            Recovery plan or None if no plan can be created
        """
        # Determine service type from evidence
        service_type = "unknown"
        service_name = "unknown"

        if evidence.kind == "site_check":
            service_type = "website"
            service_name = evidence.payload.get("url", "website")
        elif "postgresql" in str(evidence.message).lower() or "postgres" in str(evidence.message).lower():
            service_type = "postgresql"
            service_name = "postgresql"
        elif "mcp" in str(evidence.message).lower():
            service_type = "mcp"
            service_name = "mcp-server"

        # Create recovery actions based on service type
        actions = self._create_recovery_actions(service_type, service_name, evidence)

        if not actions:
            logger.warning(f"No recovery actions available for {service_type}")
            return None

        return RecoveryPlan(
            service_name=service_name,
            service_type=service_type,
            actions=actions,
            evidence=evidence,
        )

    def _create_recovery_actions(
        self,
        service_type: str,
        service_name: str,
        evidence: Evidence,
    ) -> List[RecoveryAction]:
        """Create recovery actions for a service type.

        Args:
            service_type: Type of service
            service_name: Name of service
            evidence: Evidence that triggered recovery

        Returns:
            List of recovery actions
        """
        actions = []

        if service_type == "website":
            # Website recovery actions
            actions.extend([
                RecoveryAction(
                    description=f"Check if {service_name} web server process is running",
                    command=f"systemctl status nginx || systemctl status apache2 || systemctl status httpd",
                    safety_level=SafetyLevel.SAFE,
                ),
                RecoveryAction(
                    description="Check web server error logs",
                    command="tail -n 50 /var/log/nginx/error.log /var/log/apache2/error.log /var/log/httpd/error_log 2>/dev/null",
                    safety_level=SafetyLevel.SAFE,
                ),
                RecoveryAction(
                    description="Restart web server service",
                    command="systemctl restart nginx || systemctl restart apache2 || systemctl restart httpd",
                    safety_level=SafetyLevel.LOW,
                    requires_approval=True,
                ),
            ])

        elif service_type == "postgresql":
            # PostgreSQL recovery actions
            actions.extend([
                RecoveryAction(
                    description="Check PostgreSQL service status",
                    command="systemctl status postgresql",
                    safety_level=SafetyLevel.SAFE,
                ),
                RecoveryAction(
                    description="Check PostgreSQL logs for errors",
                    command="tail -n 50 /var/log/postgresql/postgresql-*.log 2>/dev/null || journalctl -u postgresql -n 50",
                    safety_level=SafetyLevel.SAFE,
                ),
                RecoveryAction(
                    description="Check disk space",
                    command="df -h /var/lib/postgresql",
                    safety_level=SafetyLevel.SAFE,
                ),
                RecoveryAction(
                    description="Restart PostgreSQL service",
                    command="systemctl restart postgresql",
                    safety_level=SafetyLevel.MEDIUM,
                    requires_approval=True,
                ),
            ])

        elif service_type == "mcp":
            # MCP server recovery actions
            actions.extend([
                RecoveryAction(
                    description="Check MCP server process",
                    command="ps aux | grep mcp | grep -v grep",
                    safety_level=SafetyLevel.SAFE,
                ),
                RecoveryAction(
                    description="Check MCP server logs",
                    command="tail -n 50 /var/log/mcp/server.log 2>/dev/null || journalctl -u mcp-server -n 50",
                    safety_level=SafetyLevel.SAFE,
                ),
                RecoveryAction(
                    description="Restart MCP server",
                    command="systemctl restart mcp-server || pkill -f mcp && /usr/local/bin/start-mcp-server.sh",
                    safety_level=SafetyLevel.LOW,
                    requires_approval=True,
                ),
            ])

        return actions
