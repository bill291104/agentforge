from __future__ import annotations

import asyncio
import json
import logging
import os
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Optional
from uuid import uuid4

logger = logging.getLogger(__name__)

_MOCK = os.getenv("AF_MOCK_MODE", "false").lower() == "true"
_AF_SOURCE = Path(os.getenv("AF_SOURCE_DIR", str(Path(__file__).parent.parent.parent)))
_PATTERNS_FILE = Path(os.getenv("AF_RESEARCHER_PATTERNS", "memory/researcher/patterns.json"))
_ARCHITECTURE_MD = _AF_SOURCE / "ARCHITECTURE.md"


class ResearcherAgent:
    """
    연구자(研究者) — SI채널을 중심으로 AF 개선을 연구하고 제안하는 에이전트.

    3계층 트리거:
      1. 즉각: SI채널 신규 게시물 (on_si_message)
      2. 세션 종료 후 5분: on_session_end
      3. 일별: on_daily_cron

    분석 도구: read_file, search_code, git_log, git_diff, git_show, read_si_thread
    """

    _USERNAME = "연구자 🔬"
    _ICON = ":microscope:"

    def __init__(self, slack_client: Any, si_channel: str) -> None:
        self._client = slack_client
        self._si_channel = si_channel
        self._patterns_file = _PATTERNS_FILE
        self._patterns_file.parent.mkdir(parents=True, exist_ok=True)
        # proposal_id → {"channel": ..., "branch": ..., "worktree": ...}
        self._active_improvements: dict[str, dict] = {}
        # improvement 채널 동적 등록 (slack_interface가 읽음)
        self.improvement_channels: set[str] = set()

    # ------------------------------------------------------------------
    # Trigger handlers
    # ------------------------------------------------------------------

    async def on_si_message(
        self,
        channel: str,
        thread_ts: Optional[str],
        user: str,
        text: str,
    ) -> None:
        """SI채널에 사용자 또는 리더가 메시지를 게시했을 때 호출된다."""
        if _MOCK:
            logger.info("[researcher][mock] on_si_message user=%s text=%.60s", user, text)
            return

        logger.info("[researcher] on_si_message user=%s text=%.80s", user, text)
        analysis = await self._analyze(
            trigger="complaint",
            context=text,
            thread_ts=thread_ts,
        )
        if analysis:
            await self.propose_improvement(analysis, reply_thread_ts=thread_ts)

    async def on_session_end(self, session_id: str) -> None:
        """세션 종료 후 5분 뒤 호출된다 (asyncio.create_task로 지연 실행)."""
        if _MOCK:
            logger.info("[researcher][mock] on_session_end session=%s", session_id)
            return

        await asyncio.sleep(300)  # 5분 대기 (사관이 세션 요약을 완성할 시간)
        logger.info("[researcher] on_session_end session=%s", session_id)

        # 해당 세션 스레드 내용 읽기
        thread_content = await self._read_si_thread_by_session(session_id)
        if not thread_content:
            return

        # auto-improvement 세션은 분석 제외
        if "[auto-improvement]" in thread_content:
            logger.info("[researcher] skipping auto-improvement session=%s", session_id)
            return

        analysis = await self._analyze(
            trigger="session_end",
            context=f"세션 {session_id[:8]} 완료.\n\n{thread_content[:3000]}",
        )
        if analysis:
            await self.propose_improvement(analysis)

    async def on_daily_cron(self) -> None:
        """매일 오전 9시 cron에 의해 호출된다."""
        if _MOCK:
            logger.info("[researcher][mock] on_daily_cron")
            return

        logger.info("[researcher] on_daily_cron")
        patterns = self._load_patterns()
        summary = json.dumps(patterns, ensure_ascii=False, indent=2)[:2000]

        analysis = await self._analyze(
            trigger="daily",
            context=f"누적 패턴 데이터:\n{summary}",
        )
        if analysis:
            await self._post(analysis.get("summary", "일별 분석 완료. 제안 없음."))

    # ------------------------------------------------------------------
    # Proposal lifecycle
    # ------------------------------------------------------------------

    async def propose_improvement(
        self,
        analysis: dict[str, Any],
        reply_thread_ts: Optional[str] = None,
    ) -> str:
        """SI채널에 개선 제안을 게시하고 proposal_id를 반환한다."""
        if _MOCK:
            logger.info("[researcher][mock] propose_improvement analysis=%.80s", str(analysis))
            return "mock-proposal"

        proposal_id = f"R-{uuid4().hex[:6].upper()}"
        proposal = {
            "proposal_id": proposal_id,
            "created_at": datetime.now(UTC).isoformat(),
            "trigger": analysis.get("trigger", "auto"),
            "problem": analysis.get("problem", ""),
            "root_cause": analysis.get("root_cause", ""),
            "suggestion": analysis.get("suggestion", ""),
            "target_files": analysis.get("target_files", []),
            "evidence": analysis.get("evidence", []),
            "status": "pending",
        }

        pending_dir = Path("memory/proposals/pending")
        pending_dir.mkdir(parents=True, exist_ok=True)
        (pending_dir / f"proposal_{proposal_id}.md").write_text(
            _render_proposal_md(proposal), encoding="utf-8"
        )

        blocks = _build_proposal_blocks(proposal)
        try:
            await self._client.chat_postMessage(
                channel=self._si_channel,
                thread_ts=reply_thread_ts,
                blocks=blocks,
                text=f"🔬 개선 제안 #{proposal_id}",
                username=self._USERNAME,
                icon_emoji=self._ICON,
            )
            logger.info("[researcher] proposal posted proposal_id=%s", proposal_id)
        except Exception as exc:
            logger.warning("[researcher] propose_improvement post failed: %s", exc)

        return proposal_id

    async def execute_improvement(self, proposal_id: str) -> None:
        """승인된 제안을 실행한다: git worktree 생성 → 채널 생성 → 리더 지시."""
        if _MOCK:
            logger.info("[researcher][mock] execute_improvement proposal_id=%s", proposal_id)
            return

        logger.info("[researcher] execute_improvement proposal_id=%s", proposal_id)
        pending = Path("memory/proposals/pending") / f"proposal_{proposal_id}.md"
        applied = Path("memory/proposals/applied")
        applied.mkdir(parents=True, exist_ok=True)

        # --- git worktree 생성 ---
        branch = f"self-improve/{proposal_id}"
        ws_dir = Path(os.getenv("AF_WORKSPACE_DIR", "workspace"))
        worktree_path = ws_dir / f"improve-{proposal_id}"

        try:
            subprocess.run(
                ["git", "worktree", "add", str(worktree_path.absolute()), "-b", branch],
                cwd=str(_AF_SOURCE),
                check=True,
                capture_output=True,
                text=True,
            )
            logger.info("[researcher] worktree created path=%s branch=%s",
                        worktree_path, branch)
        except subprocess.CalledProcessError as exc:
            logger.error("[researcher] git worktree failed: %s", exc.stderr)
            await self._post(
                f"❌ 개선 프로젝트 #{proposal_id} 실패: git worktree 생성 오류\n```{exc.stderr[:300]}```"
            )
            return

        # --- Slack 채널 생성 ---
        channel_name = f"af-improve-{proposal_id.lower()}"
        try:
            resp = await self._client.conversations_create(name=channel_name, is_private=False)
            improve_channel_id = resp["channel"]["id"]
            # 봇 자신을 채널에 참여시킴
            await self._client.conversations_join(channel=improve_channel_id)
            # 동적 채널 등록 (SlackInterface가 이 채널의 메시지를 리더 세션으로 라우팅)
            self.improvement_channels.add(improve_channel_id)
            logger.info("[researcher] improvement channel created id=%s", improve_channel_id)
        except Exception as exc:
            logger.error("[researcher] channel create failed: %s", exc)
            await self._post(f"❌ 개선 프로젝트 #{proposal_id} 실패: 채널 생성 오류")
            return

        # --- 개선 지시서 게시 ---
        proposal_md = pending.read_text(encoding="utf-8") if pending.exists() else ""
        instruction = (
            f"## AF 자가 개선 프로젝트 #{proposal_id}\n\n"
            f"**작업 공간**: `{worktree_path.absolute()}`\n"
            f"**브랜치**: `{branch}`\n\n"
            f"이 채널의 메시지는 AF 개선 지시입니다. 아래 제안서를 분석하고 "
            f"`workspace_root={worktree_path.absolute()}` 에서 AF 소스코드를 수정하라.\n\n"
            f"{proposal_md[:2000]}"
        )
        try:
            await self._client.chat_postMessage(
                channel=improve_channel_id,
                text=instruction,
                username=self._USERNAME,
                icon_emoji=self._ICON,
            )
        except Exception as exc:
            logger.warning("[researcher] instruction post failed: %s", exc)

        # --- 제안 상태 업데이트 ---
        if pending.exists():
            pending.rename(applied / f"proposal_{proposal_id}.md")

        self._active_improvements[proposal_id] = {
            "channel": improve_channel_id,
            "branch": branch,
            "worktree": str(worktree_path),
        }

        # --- SI채널 공지 ---
        try:
            channel_link = f"<#{improve_channel_id}>"
            await self._client.chat_postMessage(
                channel=self._si_channel,
                text=(
                    f"🚀 *개선 프로젝트 #{proposal_id} 시작*\n"
                    f"채널: {channel_link} | 브랜치: `{branch}`\n"
                    f"워크트리: `{worktree_path}`"
                ),
                username=self._USERNAME,
                icon_emoji=self._ICON,
            )
        except Exception as exc:
            logger.warning("[researcher] SI channel notify failed: %s", exc)

    async def handle_merge_approve(self, proposal_id: str) -> None:
        """개선 완료 후 main 병합 승인을 처리한다."""
        if _MOCK:
            logger.info("[researcher][mock] handle_merge_approve proposal_id=%s", proposal_id)
            return

        info = self._active_improvements.get(proposal_id)
        if not info:
            logger.warning("[researcher] no active improvement for proposal_id=%s", proposal_id)
            return

        branch = info["branch"]
        worktree = info["worktree"]

        try:
            subprocess.run(
                ["git", "merge", "--no-ff", branch, "-m", f"Merge {branch}"],
                cwd=str(_AF_SOURCE),
                check=True,
                capture_output=True,
                text=True,
            )
            subprocess.run(
                ["git", "worktree", "remove", worktree, "--force"],
                cwd=str(_AF_SOURCE),
                check=True,
                capture_output=True,
                text=True,
            )
            subprocess.run(
                ["git", "branch", "-d", branch],
                cwd=str(_AF_SOURCE),
                capture_output=True,
                text=True,
            )
            self._active_improvements.pop(proposal_id, None)

            await self._client.chat_postMessage(
                channel=self._si_channel,
                text=(
                    f"✅ *개선 #{proposal_id} main 병합 완료*\n"
                    f"브랜치 `{branch}` → main\n"
                    f"⚠️ 변경사항 적용을 위해 AF를 재시작하세요: `uv run agentforge start`"
                ),
                username=self._USERNAME,
                icon_emoji=self._ICON,
            )
        except subprocess.CalledProcessError as exc:
            logger.error("[researcher] merge failed: %s", exc.stderr)
            await self._post(f"❌ 병합 실패 #{proposal_id}\n```{exc.stderr[:400]}```")

    async def handle_merge_cancel(self, proposal_id: str) -> None:
        """개선 취소: 브랜치와 워크트리를 삭제한다."""
        if _MOCK:
            return

        info = self._active_improvements.pop(proposal_id, None)
        if not info:
            return

        try:
            subprocess.run(
                ["git", "worktree", "remove", info["worktree"], "--force"],
                cwd=str(_AF_SOURCE), capture_output=True, text=True,
            )
            subprocess.run(
                ["git", "branch", "-D", info["branch"]],
                cwd=str(_AF_SOURCE), capture_output=True, text=True,
            )
            await self._post(f"❌ 개선 #{proposal_id} 취소됨. 브랜치 삭제 완료.")
        except Exception as exc:
            logger.warning("[researcher] cancel cleanup failed: %s", exc)

    async def handle_diff_view(self, proposal_id: str) -> None:
        """개선 브랜치의 diff를 조회해 SI채널에 요약 게시한다."""
        if _MOCK:
            return

        info = self._active_improvements.get(proposal_id)
        if not info:
            return

        try:
            result = subprocess.run(
                ["git", "diff", "main", info["branch"], "--stat"],
                cwd=str(_AF_SOURCE), capture_output=True, text=True,
            )
            diff_stat = result.stdout[:800] or "(변경 없음)"
            await self._post(
                f"🔍 개선 #{proposal_id} diff 요약\n```{diff_stat}```\n"
                f"상세: `git diff main {info['branch']}`"
            )
        except Exception as exc:
            logger.warning("[researcher] diff_view failed: %s", exc)

    # ------------------------------------------------------------------
    # Pattern storage
    # ------------------------------------------------------------------

    def update_patterns(self, session_id: str, escalations: int, failures: int,
                        tokens: int, elapsed_s: float) -> None:
        """세션 통계를 누적 패턴 파일에 기록한다."""
        patterns = self._load_patterns()
        sessions = patterns.setdefault("sessions", [])
        sessions.append({
            "session_id": session_id[:8],
            "date": datetime.now(UTC).strftime("%Y-%m-%d"),
            "escalations": escalations,
            "failures": failures,
            "tokens": tokens,
            "elapsed_s": elapsed_s,
        })
        # 최근 100개 세션만 유지
        patterns["sessions"] = sessions[-100:]
        patterns["updated_at"] = datetime.now(UTC).isoformat()
        self._save_patterns(patterns)

    def _load_patterns(self) -> dict:
        if self._patterns_file.exists():
            try:
                return json.loads(self._patterns_file.read_text(encoding="utf-8"))
            except Exception:
                pass
        return {"sessions": [], "proposals": []}

    def _save_patterns(self, patterns: dict) -> None:
        self._patterns_file.write_text(
            json.dumps(patterns, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    # ------------------------------------------------------------------
    # Analysis (Claude API call)
    # ------------------------------------------------------------------

    async def _analyze(
        self,
        trigger: str,
        context: str,
        thread_ts: Optional[str] = None,
    ) -> Optional[dict[str, Any]]:
        """Claude API로 AF 개선 분석을 수행한다. 제안이 없으면 None 반환."""
        import anthropic
        from agentforge.core.models import MODEL_IDS, ModelTier

        arch_text = ""
        if _ARCHITECTURE_MD.exists():
            arch_text = _ARCHITECTURE_MD.read_text(encoding="utf-8")[:4000]

        patterns = self._load_patterns()
        pattern_summary = json.dumps(patterns.get("sessions", [])[-20:],
                                     ensure_ascii=False)[:1000]

        system_prompt = f"""\
당신은 AgentForge(AF) 시스템 연구자입니다.
AF 소스코드 구조를 숙지하고 있으며, 관찰된 문제로부터 구체적인 코드 개선을 제안합니다.

## AF 아키텍처 요약
{arch_text}

## 누적 패턴 데이터 (최근 20 세션)
{pattern_summary}

분석 결과를 반드시 아래 JSON으로만 응답하라. 제안이 없으면 null 반환:
{{
  "trigger": "complaint|session_end|daily",
  "problem": "문제 설명",
  "root_cause": "근본 원인",
  "suggestion": "구체적 개선 제안 (어떤 파일의 어떤 부분을 어떻게)",
  "target_files": ["파일 경로"],
  "evidence": ["근거 목록"],
  "summary": "SI채널 표시용 한줄 요약"
}}
"""
        user_prompt = f"트리거: {trigger}\n\n맥락:\n{context[:3000]}"

        try:
            client = anthropic.AsyncAnthropic()
            resp = await client.messages.create(
                model=MODEL_IDS[ModelTier.OPUS],
                max_tokens=2048,
                system=system_prompt,
                messages=[{"role": "user", "content": user_prompt}],
            )
            raw = resp.content[0].text.strip()
            if raw.lower() == "null" or not raw:
                return None
            # JSON 블록 추출
            if "```" in raw:
                for part in raw.split("```")[1::2]:
                    candidate = part.lstrip("json").strip()
                    try:
                        return json.loads(candidate)
                    except Exception:
                        continue
            return json.loads(raw)
        except Exception as exc:
            logger.warning("[researcher] _analyze failed: %s", exc)
            return None

    async def _read_si_thread_by_session(self, session_id: str) -> str:
        """SI채널에서 session_id를 포함하는 스레드 내용을 읽어온다."""
        if not self._si_channel:
            return ""
        try:
            # 최근 메시지에서 해당 세션 스레드 찾기
            history = await self._client.conversations_history(
                channel=self._si_channel, limit=50
            )
            for msg in history.get("messages", []):
                if session_id[:8] in msg.get("text", ""):
                    thread_ts = msg["ts"]
                    replies = await self._client.conversations_replies(
                        channel=self._si_channel, ts=thread_ts
                    )
                    texts = [r.get("text", "") for r in replies.get("messages", [])]
                    return "\n---\n".join(texts)
        except Exception as exc:
            logger.warning("[researcher] _read_si_thread failed: %s", exc)
        return ""

    async def _post(self, text: str, thread_ts: Optional[str] = None) -> None:
        """SI채널에 메시지를 게시한다."""
        if _MOCK or not self._si_channel:
            logger.info("[researcher][mock] post: %s", text[:80])
            return
        try:
            await self._client.chat_postMessage(
                channel=self._si_channel,
                thread_ts=thread_ts,
                text=text,
                username=self._USERNAME,
                icon_emoji=self._ICON,
            )
        except Exception as exc:
            logger.warning("[researcher] _post failed: %s", exc)


# ------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------

def _render_proposal_md(p: dict) -> str:
    lines = [
        f"# 개선 제안 #{p['proposal_id']}",
        f"생성: {p['created_at']} | 트리거: {p['trigger']}",
        "",
        f"## 문제\n{p['problem']}",
        f"## 근본 원인\n{p['root_cause']}",
        f"## 제안\n{p['suggestion']}",
        f"## 대상 파일\n" + "\n".join(f"- {f}" for f in p.get("target_files", [])),
        f"## 근거\n" + "\n".join(f"- {e}" for e in p.get("evidence", [])),
        f"\n상태: {p['status']}",
    ]
    return "\n".join(lines)


def _build_proposal_blocks(p: dict) -> list[dict]:
    proposal_id = p["proposal_id"]
    return [
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": (
                    f"🔬 *개선 제안 #{proposal_id}*\n"
                    f"*문제*: {p['problem'][:200]}\n"
                    f"*근본 원인*: {p['root_cause'][:200]}\n"
                    f"*제안*: {p['suggestion'][:300]}\n"
                    f"*대상 파일*: {', '.join(p.get('target_files', []))}"
                ),
            },
        },
        {
            "type": "actions",
            "elements": [
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "✅ 승인"},
                    "style": "primary",
                    "action_id": "proposal_approve",
                    "value": proposal_id,
                },
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "❌ 거부"},
                    "style": "danger",
                    "action_id": "proposal_reject",
                    "value": proposal_id,
                },
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "⏰ 24h 후 자동 승인"},
                    "action_id": "proposal_auto_approve",
                    "value": proposal_id,
                },
            ],
        },
    ]
