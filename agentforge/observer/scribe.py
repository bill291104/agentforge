from __future__ import annotations

import logging
import os
from datetime import UTC, datetime
from typing import Any, Optional

logger = logging.getLogger(__name__)

_MOCK = os.getenv("AF_MOCK_MODE", "false").lower() == "true"


class ScribeAgent:
    """
    사관(史官) — SI채널에 세션 스레드를 생성하고 태스크 이벤트를 서술형으로 기록한다.

    Slack API 직접 호출 (chat.write.customize 권한 필요).
    Historian 파일 저널은 병존 유지 (디버깅용 백업).
    """

    _USERNAME = "사관 🗒️"
    _ICON = ":scroll:"

    def __init__(self, slack_client: Any, si_channel: str) -> None:
        self._client = slack_client
        self._si_channel = si_channel
        # session_id → thread_ts
        self._threads: dict[str, str] = {}
        # session_id → {task_id → message_ts}
        self._task_msgs: dict[str, dict[str, str]] = {}

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def start_session_thread(
        self,
        session_id: str,
        workflow_name: str,
        task_count: int,
        model_summary: str = "",
    ) -> Optional[str]:
        """SI채널에 세션 스레드 헤더를 게시하고 thread_ts를 반환한다."""
        if _MOCK or not self._si_channel:
            logger.info("[scribe][mock] start_session_thread session=%s", session_id)
            return None

        ts_str = datetime.now(UTC).strftime("%Y-%m-%d %H:%M UTC")
        text = (
            f"🗂️ *세션 `{session_id[:8]}`* | {workflow_name}\n"
            f"시작: {ts_str} | 태스크: {task_count}개"
            + (f" | {model_summary}" if model_summary else "")
        )
        try:
            resp = await self._client.chat_postMessage(
                channel=self._si_channel,
                text=text,
                username=self._USERNAME,
                icon_emoji=self._ICON,
            )
            ts = resp["ts"]
            self._threads[session_id] = ts
            self._task_msgs[session_id] = {}
            logger.info("[scribe] session thread created session=%s ts=%s", session_id, ts)
            return ts
        except Exception as exc:
            logger.warning("[scribe] start_session_thread failed: %s", exc)
            return None

    async def record_task_event(
        self,
        session_id: str,
        task_id: str,
        task_title: str,
        task_index: int,
        total_tasks: int,
        event_type: str,
        payload: dict[str, Any],
    ) -> None:
        """
        태스크 라이프사이클 이벤트를 SI채널 스레드에 기록한다.

        event_type: "dispatch" | "ci_result" | "semantic_result" | "merge" | "escalate" | "error"
        payload 공통 키:
            - model_tier: str
            - elapsed_s: float
            - tokens: int
            - files_created: list[str]
            - files_modified: list[str]
            - commit_sha: str
            - commit_msg: str
            - error_trace: str
            - ci_passed: bool
            - ci_failed_criteria: list[str]
            - ci_stdout: str
            - semantic_verdict: "ACCEPT" | "REJECT"
            - semantic_reason: str
            - escalation_level: int
            - escalation_reason: str
        """
        if _MOCK or not self._si_channel:
            logger.info("[scribe][mock] record_task_event session=%s task=%s event=%s",
                        session_id, task_id, event_type)
            return

        thread_ts = self._threads.get(session_id)
        if not thread_ts:
            logger.warning("[scribe] no thread for session=%s", session_id)
            return

        existing_ts = self._task_msgs.get(session_id, {}).get(task_id)
        text = self._format_task_event(
            task_id, task_title, task_index, total_tasks, event_type, payload
        )

        try:
            if existing_ts and event_type != "dispatch":
                # 기존 태스크 메시지에 이벤트 내용 추가 (update)
                await self._client.chat_update(
                    channel=self._si_channel,
                    ts=existing_ts,
                    text=text,
                )
            else:
                # dispatch 이벤트 또는 첫 메시지: 새 스레드 메시지 생성
                resp = await self._client.chat_postMessage(
                    channel=self._si_channel,
                    thread_ts=thread_ts,
                    text=text,
                    username=self._USERNAME,
                    icon_emoji=self._ICON,
                )
                if session_id not in self._task_msgs:
                    self._task_msgs[session_id] = {}
                self._task_msgs[session_id][task_id] = resp["ts"]
        except Exception as exc:
            logger.warning("[scribe] record_task_event failed task=%s event=%s: %s",
                           task_id, event_type, exc)

    async def end_session(
        self,
        session_id: str,
        total_tasks: int,
        succeeded: int,
        failed: int,
        total_tokens: int,
        elapsed_minutes: float,
        retry_summary: str = "",
        is_auto_improvement: bool = False,
    ) -> None:
        """세션 요약 메시지를 스레드에 추가한다."""
        if _MOCK or not self._si_channel:
            logger.info("[scribe][mock] end_session session=%s", session_id)
            return

        thread_ts = self._threads.get(session_id)
        if not thread_ts:
            return

        tag = " `[auto-improvement]`" if is_auto_improvement else ""
        icon = "✅" if failed == 0 else "⚠️"
        text = (
            f"{icon} *세션 완료*{tag} | {succeeded}/{total_tasks} 성공 | "
            f"총 {elapsed_minutes:.0f}분 | {total_tokens:,} 토큰"
        )
        if retry_summary:
            text += f"\n재시도: {retry_summary}"

        try:
            await self._client.chat_postMessage(
                channel=self._si_channel,
                thread_ts=thread_ts,
                text=text,
                username=self._USERNAME,
                icon_emoji=self._ICON,
            )
        except Exception as exc:
            logger.warning("[scribe] end_session failed session=%s: %s", session_id, exc)
        finally:
            self._threads.pop(session_id, None)
            self._task_msgs.pop(session_id, None)

    # ------------------------------------------------------------------
    # Formatting
    # ------------------------------------------------------------------

    def _format_task_event(
        self,
        task_id: str,
        task_title: str,
        task_index: int,
        total_tasks: int,
        event_type: str,
        p: dict[str, Any],
    ) -> str:
        ts_str = datetime.now(UTC).strftime("%H:%M")
        header = f"🔧 *[{task_index}/{total_tasks}] {task_title}* — `{task_id}`"
        model = p.get("model_tier", "")
        if model:
            header += f" | {model}"

        lines = [header]

        if event_type == "dispatch":
            lines.append(f"시작: {ts_str}")
            criteria = p.get("acceptance_criteria", [])
            if criteria:
                lines.append("📋 *수락 기준*")
                for c in criteria[:5]:
                    lines.append(f"  • {c}")
                if len(criteria) > 5:
                    lines.append(f"  … 외 {len(criteria) - 5}개")

        elif event_type == "ci_result":
            ci_passed = p.get("ci_passed", False)
            icon = "✅" if ci_passed else "❌"
            lines.append(f"🧪 CI: {icon}")
            failed_criteria = p.get("ci_failed_criteria", [])
            if failed_criteria:
                lines.append(f"실패 기준: {', '.join(failed_criteria)}")
            # 파일 변경 내역
            created = p.get("files_created", [])
            modified = p.get("files_modified", [])
            deleted = p.get("files_deleted", [])
            if created:
                lines.append("→ 생성: " + ", ".join(created))
            if modified:
                lines.append("→ 수정: " + ", ".join(modified))
            if deleted:
                lines.append("→ 삭제: " + ", ".join(deleted))
            # 커밋
            sha = p.get("commit_sha", "")
            msg = p.get("commit_msg", "")
            if sha:
                lines.append(f"→ 커밋 `{sha[:7]}` {msg}")
            # 에러 스택트레이스
            trace = p.get("error_trace", "")
            if trace and not ci_passed:
                lines.append("```")
                lines.append(trace[:800])
                lines.append("```")

        elif event_type == "semantic_result":
            verdict = p.get("semantic_verdict", "")
            icon = "✅" if verdict == "ACCEPT" else "❌"
            lines.append(f"🎯 의미 검증: {icon} {verdict}")
            reason = p.get("semantic_reason", "")
            if reason:
                lines.append(f"  근거: {reason[:200]}")

        elif event_type == "merge":
            sha = p.get("commit_sha", "")
            msg = p.get("commit_msg", "")
            elapsed = p.get("elapsed_s", 0)
            tokens = p.get("tokens", 0)
            lines.append(
                f"→ main 병합 `{sha[:7] if sha else '-'}` {msg}"
            )
            if elapsed or tokens:
                lines.append(f"⏱️ {elapsed/60:.0f}분 · {tokens:,} 토큰")

        elif event_type == "escalate":
            level = p.get("escalation_level", 0)
            reason = p.get("escalation_reason", "")
            attempt = p.get("attempt_count", 0)
            level_labels = {0: "L0 재지시", 1: "L1 세션 교체", 2: "L2 모델 업그레이드",
                            3: "L3 태스크 중단", 4: "L4 사용자 승인 요청"}
            label = level_labels.get(level, f"L{level}")
            lines.append(f"⚠️ 에스컬레이션 → {label} (시도 {attempt}회)")
            if reason:
                lines.append(f"  원인: {reason[:200]}")

        elif event_type == "error":
            trace = p.get("error_trace", "")
            lines.append(f"❌ 오류 발생 ({ts_str})")
            if trace:
                lines.append("```")
                lines.append(trace[:800])
                lines.append("```")

        return "\n".join(lines)
