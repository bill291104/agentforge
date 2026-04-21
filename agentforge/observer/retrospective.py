from __future__ import annotations

import logging
import os
import re
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

_MOCK = os.getenv("AF_MOCK_MODE", "false").lower() == "true"
_PROPOSAL_DIR = Path(os.getenv("AF_PROPOSAL_DIR", "memory/proposals"))


class RetrospectiveAgent:
    """
    Analyzes journal files to find patterns (repeated failures, slow tasks) and
    generates improvement proposals.

    Trigger conditions:
    - Automatic: every 10 sessions (or daily midnight)
    - Immediate: when user expresses a complaint
    - Threshold: task failure rate > 30%
    """

    TRIGGERS = {
        "session_count": 10,
        "failure_rate_threshold": 0.30,
        "complaint_keywords": ["느려", "왜", "실패", "이상", "버그", "문제"],
    }

    def __init__(self, proposal_dir: Optional[Path] = None) -> None:
        self._proposal_dir = proposal_dir or _PROPOSAL_DIR
        (self._proposal_dir / "pending").mkdir(parents=True, exist_ok=True)
        (self._proposal_dir / "applied").mkdir(parents=True, exist_ok=True)

    async def analyze(
        self,
        journal_dir: Path,
        trigger: str = "auto",
        context: str = "",
    ) -> Optional["ImprovementProposal"]:  # noqa: F821
        """Read journals, find patterns, and optionally create a proposal."""
        if _MOCK:
            return self._mock_proposal(trigger, context)

        journals = sorted(journal_dir.glob("*.md"))[-20:] if journal_dir.exists() else []
        if not journals:
            return None

        patterns = self._find_patterns(journals)
        if not patterns:
            return None

        return await self._draft_proposal(patterns, trigger, context)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _find_patterns(self, journal_paths: list[Path]) -> list[dict]:
        """Scan journals for repeated failure patterns."""
        failure_counts: dict[str, int] = {}
        total_counts: dict[str, int] = {}

        for path in journal_paths:
            try:
                text = path.read_text(encoding="utf-8")
            except OSError:
                continue
            for line in text.splitlines():
                # Table rows: | HH:MM | node | task_id | result | ...
                match = re.match(r"\|\s*\S+\s*\|\s*(\S+)\s*\|\s*(\S+)\s*\|\s*(\S+)\s*\|", line)
                if not match:
                    continue
                node, task_id, result = match.group(1), match.group(2), match.group(3)
                key = f"{node}:{task_id}"
                total_counts[key] = total_counts.get(key, 0) + 1
                if "❌" in result or "fail" in result.lower():
                    failure_counts[key] = failure_counts.get(key, 0) + 1

        patterns = []
        for key, total in total_counts.items():
            failures = failure_counts.get(key, 0)
            if total >= 3 and failures / total >= self.TRIGGERS["failure_rate_threshold"]:
                node, task_id = key.split(":", 1)
                patterns.append({
                    "node": node,
                    "task_id": task_id,
                    "failure_rate": failures / total,
                    "total": total,
                    "failures": failures,
                })
        return sorted(patterns, key=lambda p: p["failure_rate"], reverse=True)

    async def _draft_proposal(
        self, patterns: list[dict], trigger: str, context: str
    ) -> "ImprovementProposal":  # noqa: F821
        from agentforge.core.models import ImprovementProposal

        top = patterns[0]
        proposal_id = str(uuid.uuid4())[:8]
        problem = (
            f"`{top['task_id']}` 태스크의 실패율: "
            f"{top['failure_rate']*100:.0f}% ({top['failures']}/{top['total']} 실행)"
        )
        suggested_change = (
            f"{top['node']} 노드에서 `{top['task_id']}`의 "
            f"모델 티어를 Haiku → Sonnet으로 업그레이드 권장"
        )
        if context:
            problem += f"\n사용자 불만 컨텍스트: {context[:200]}"

        evidence_strs = [
            f"{p['task_id']}: {p['failures']}/{p['total']} 실패"
            for p in patterns[:3]
        ]
        proposal = ImprovementProposal(
            proposal_id=proposal_id,
            trigger=trigger if trigger in ("auto", "complaint", "threshold") else "auto",
            problem=problem,
            evidence=evidence_strs,
            root_cause=f"`{top['task_id']}` 복잡도가 현재 모델 티어 처리 범위 초과",
            change_type="config",
            target_files=["workflows/templates/feature_dev.yaml"],
            impact="L0 에스컬레이션 감소 예상",
        )
        await self._save_proposal(proposal)
        return proposal

    def _mock_proposal(self, trigger: str, context: str) -> Optional["ImprovementProposal"]:  # noqa: F821
        if not context:
            return None
        from agentforge.core.models import ImprovementProposal

        return ImprovementProposal(
            proposal_id="mock-001",
            trigger="complaint",
            problem=f"[mock] trigger={trigger} context={context[:60]}",
            evidence=[],
            root_cause="[mock]",
            change_type="config",
            impact="[mock]",
        )

    async def _save_proposal(self, proposal: "ImprovementProposal") -> None:  # noqa: F821
        path = self._proposal_dir / "pending" / f"proposal_{proposal.proposal_id}.md"
        content = (
            f"# 개선 제안서 #{proposal.proposal_id}\n"
            f"생성일: {datetime.now(UTC).strftime('%Y-%m-%d %H:%M')}\n\n"
            f"## 문제\n{proposal.problem}\n\n"
            f"## 근본 원인\n{proposal.root_cause}\n\n"
            f"## 제안된 변경\n{proposal.diff_preview or '(상세 diff 없음)'}\n\n"
            f"## 영향도\n{proposal.impact}\n\n"
            f"재시작 필요: {'예' if proposal.restart_required else '아니오'}\n"
        )
        try:
            import aiofiles
            async with aiofiles.open(path, "w", encoding="utf-8") as f:
                await f.write(content)
        except Exception as exc:
            logger.warning("Failed to save proposal: %s", exc)
