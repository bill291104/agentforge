from __future__ import annotations

import os
from datetime import UTC, datetime
from enum import Enum
from typing import Any, Literal, Optional

from pydantic import BaseModel, Field


class ModelTier(str, Enum):
    OPUS = "opus"
    SONNET = "sonnet"
    HAIKU = "haiku"
    LOCAL = "local"


MODEL_IDS: dict[ModelTier, str] = {
    ModelTier.OPUS: "claude-opus-4-7",
    ModelTier.SONNET: "claude-sonnet-4-6",
    ModelTier.HAIKU: "claude-haiku-4-5-20251001",
    ModelTier.LOCAL: os.getenv("AF_LOCAL_MODEL", "ollama/llama3"),
}


class TaskStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    BLOCKED = "blocked"
    TIMEOUT = "timeout"


class EscalationLevel(int, Enum):
    L0 = 0  # 재지시 (동일 에이전트)
    L1 = 1  # 세션 교체 (동일 등급)
    L2 = 2  # 모델 업그레이드
    L3 = 3  # 태스크 중단 + DAG 부분 블록
    L4 = 4  # 사용자 에스컬레이션


# ---------------------------------------------------------------------------
# Workflow spec (YAML 파싱용)
# ---------------------------------------------------------------------------

class TaskSpec(BaseModel):
    id: str
    title: str = ""
    description: str = ""
    model_tier: ModelTier = ModelTier.HAIKU
    timeout_minutes: int = 30
    retry_limit: int = 2
    priority: int = 5
    depends_on: list[str] = []
    acceptance_criteria: list[str] = []
    deliverable_format: dict[str, Any] = {}


class WorkflowSpec(BaseModel):
    name: str
    version: str = "1.0"
    tasks: list[TaskSpec] = []

    @classmethod
    def from_yaml(cls, path: str) -> "WorkflowSpec":
        import yaml
        with open(path, encoding="utf-8") as f:
            data = yaml.safe_load(f)
        return cls.model_validate(data)


# ---------------------------------------------------------------------------
# Task instruction / report
# ---------------------------------------------------------------------------

class TaskInstruction(BaseModel):
    task_id: str
    title: str
    description: str
    inputs: list[str] = []
    acceptance_criteria: list[str]
    deliverable_format: dict[str, Any] = {}
    model_tier: ModelTier
    timeout_minutes: int
    retry_limit: int = 2
    priority: int = 5
    depends_on: list[str] = []


class TaskReport(BaseModel):
    task_id: str
    status: TaskStatus
    deliverables: list[str] = []            # relative paths of written/committed files
    evidence: dict[str, Any] = {}
    summary: str
    tokens_used: int = 0
    duration_seconds: float = 0.0
    escalation_history: list[EscalationLevel] = []


# ---------------------------------------------------------------------------
# Agent pool
# ---------------------------------------------------------------------------

class AgentEntry(BaseModel):
    agent_id: str
    model_tier: ModelTier
    model_name: str
    status: Literal["idle", "busy", "failed"] = "idle"
    success_rate_7d: float = 1.0
    avg_completion_minutes: float = 30.0
    current_task_id: Optional[str] = None
    total_tasks_completed: int = 0


# ---------------------------------------------------------------------------
# DAG node
# ---------------------------------------------------------------------------

class TaskNode(BaseModel):
    instruction: TaskInstruction
    status: TaskStatus = TaskStatus.PENDING
    assigned_agent_id: Optional[str] = None
    report: Optional[TaskReport] = None
    escalation_level: EscalationLevel = EscalationLevel.L0
    attempt_count: int = 0
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    started_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None


# ---------------------------------------------------------------------------
# Verification results
# ---------------------------------------------------------------------------

class CIResult(BaseModel):
    passed: bool
    failed_criteria: list[str] = []
    auto_verified: list[str] = []
    details: dict[str, Any] = {}


class SemanticResult(BaseModel):
    verdict: Literal["ACCEPT", "REJECT"]
    criteria_results: dict[str, Literal["PASS", "FAIL"]] = {}
    rejection_reason: Optional[str] = None
    suggested_fix: Optional[str] = None


# ---------------------------------------------------------------------------
# Escalation action
# ---------------------------------------------------------------------------

class EscalationAction(BaseModel):
    action: Literal["retry", "spawn", "upgrade", "stop", "report_user"]
    new_agent_id: Optional[str] = None
    new_model_tier: Optional[ModelTier] = None
    rejection_notice: Optional[dict[str, Any]] = None
    user_report: Optional[str] = None


# ---------------------------------------------------------------------------
# Improvement proposal (ObserverAgent)
# ---------------------------------------------------------------------------

class ImprovementProposal(BaseModel):
    proposal_id: str
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    trigger: Literal["auto", "complaint", "threshold"]
    problem: str
    evidence: list[str] = []
    root_cause: str
    change_type: Literal["config", "code"]
    target_files: list[str] = []
    diff_preview: str = ""
    impact: str = ""
    restart_required: bool = False
    status: Literal["pending", "accepted", "rejected", "applied"] = "pending"


class ReloadGuide(BaseModel):
    proposal_id: str
    changed_files: list[str]
    restart_required: bool
    instructions: str
    claude_code_command: Optional[str] = None


# ---------------------------------------------------------------------------
# Sandbox result
# ---------------------------------------------------------------------------

class SandboxResult(BaseModel):
    success: bool
    stdout: str = ""
    stderr: str = ""
    exit_code: int = 0
    duration_seconds: float = 0.0
