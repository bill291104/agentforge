from __future__ import annotations

import asyncio
import logging
import os
import uuid
from typing import Any, Optional

from agentforge.interfaces.base_interface import BaseInterface

logger = logging.getLogger(__name__)

_MOCK = os.getenv("AF_MOCK_MODE", "false").lower() == "true"


class SlackInterface(BaseInterface):
    """
    Slack Bot interface using slack-bolt async mode.

    Responsibilities:
    - Receive @AgentForge <request> → trigger graph execution
    - Stream astream_events() → real-time thread updates
    - L4 interrupt_before → Block Kit buttons [계속 진행] / [중단]
    - Complaint keyword detection → historian.record_complaint()
    """

    COMPLAINT_KEYWORDS = ["느려", "왜", "실패", "이상", "버그", "문제"]

    def __init__(
        self,
        bot_token: Optional[str] = None,
        app_token: Optional[str] = None,
        signing_secret: Optional[str] = None,
    ) -> None:
        self._bot_token = bot_token or os.getenv("SLACK_BOT_TOKEN", "")
        self._app_token = app_token or os.getenv("SLACK_APP_TOKEN", "")
        self._signing_secret = signing_secret or os.getenv("SLACK_SIGNING_SECRET", "")
        self._app: Any = None
        self._handler: Any = None
        # session_id → (channel, thread_ts) for L4 resume
        self._pending_l4: dict[str, dict[str, str]] = {}

    # ------------------------------------------------------------------
    # BaseInterface
    # ------------------------------------------------------------------

    async def start(self) -> None:
        if _MOCK:
            logger.info("[mock] SlackInterface.start() — no-op in mock mode")
            return
        from slack_bolt.async_app import AsyncApp
        from slack_bolt.adapter.socket_mode.async_handler import AsyncSocketModeHandler

        self._app = AsyncApp(
            token=self._bot_token,
            signing_secret=self._signing_secret,
        )
        self._register_handlers()
        self._handler = AsyncSocketModeHandler(self._app, self._app_token)

        # Print bot identity and channel membership before opening the socket
        await self._log_startup_info()

        await self._handler.start_async()

    async def _log_startup_info(self) -> None:
        """Fetch and log bot identity + joined channels via Slack API."""
        client = self._app.client
        sep = "-" * 60

        # 1. Bot identity (auth.test)
        try:
            auth = await client.auth_test()
            bot_id   = auth.get("bot_id", "?")
            bot_name = auth.get("user", "?")
            team     = auth.get("team", "?")
            user_id  = auth.get("user_id", "?")
            logger.info(sep)
            logger.info("AgentForge Slack Bot 시작됨")
            logger.info("  봇 이름  : @%s", bot_name)
            logger.info("  봇 ID    : %s  (User ID: %s)", bot_id, user_id)
            logger.info("  워크스페이스: %s", team)
            logger.info("  멘션 형식 : <@%s>", user_id)
        except Exception as exc:
            logger.error("auth.test 실패 — 토큰을 확인하세요: %s", exc)
            return

        # 2. Joined channels (conversations.list filtered to member=true)
        try:
            channels_resp = await client.conversations_list(
                types="public_channel,private_channel",
                exclude_archived=True,
                limit=200,
            )
            joined = [
                ch for ch in channels_resp.get("channels", [])
                if ch.get("is_member")
            ]
            if joined:
                logger.info("  참여 채널 (%d개):", len(joined))
                for ch in joined:
                    name    = ch.get("name", "?")
                    ch_id   = ch.get("id", "?")
                    members = ch.get("num_members", "?")
                    logger.info("    #%s  (id=%s, members=%s)", name, ch_id, members)
            else:
                logger.warning(
                    "  [!] 참여 중인 채널 없음 — 채널에 /invite @%s 를 실행하세요", bot_name
                )
        except Exception as exc:
            logger.warning("conversations.list 실패: %s", exc)

        # 3. Subscribed events summary
        logger.info("  수신 이벤트 : app_mention")
        logger.info("  인터랙션   : l4_continue, l4_abort (Block Kit 버튼)")
        logger.info(sep)

    async def stop(self) -> None:
        if _MOCK or self._handler is None:
            return
        await self._handler.close_async()

    async def send_message(self, channel: str, text: str, **kwargs: Any) -> Any:
        if _MOCK:
            logger.info("[mock] send_message channel=%s text=%.80s", channel, text)
            return {"ts": "0.0", "channel": channel}
        return await self._app.client.chat_postMessage(
            channel=channel, text=text, **kwargs
        )

    async def update_message(self, channel: str, ts: str, text: str, **kwargs: Any) -> Any:
        if _MOCK:
            logger.info("[mock] update_message channel=%s ts=%s text=%.80s", channel, ts, text)
            return {}
        return await self._app.client.chat_update(
            channel=channel, ts=ts, text=text, **kwargs
        )

    async def send_l4_prompt(
        self, channel: str, thread_ts: str, task_id: str, summary: str
    ) -> Any:
        """Send Block Kit interactive message for L4 human-in-the-loop."""
        blocks = _build_l4_blocks(task_id, summary)
        if _MOCK:
            logger.info("[mock] send_l4_prompt task_id=%s", task_id)
            return {"ts": "0.0", "channel": channel}
        return await self._app.client.chat_postMessage(
            channel=channel,
            thread_ts=thread_ts,
            text=f"⚠️ 개입 필요: {task_id}",
            blocks=blocks,
        )

    def _on_task_done(self, task: "asyncio.Task") -> None:
        """Log exceptions from background asyncio tasks so they don't vanish silently."""
        if task.cancelled():
            return
        exc = task.exception()
        if exc:
            logger.exception("Background task failed: %s", exc, exc_info=exc)

    # ------------------------------------------------------------------
    # Graph streaming
    # ------------------------------------------------------------------

    async def stream_graph_to_slack(
        self,
        graph: Any,
        initial_state: dict,
        channel: str,
        thread_ts: str,
        session_id: str,
    ) -> None:
        """
        Run the AgentForge graph and stream progress updates to a Slack thread.
        Handles L4 interrupt by sending Block Kit buttons and waiting for response.
        """
        config = {"configurable": {"thread_id": session_id}}
        status_ts: Optional[str] = None

        async def _post_or_update(text: str) -> None:
            nonlocal status_ts
            if status_ts is None:
                resp = await self.send_message(channel, text, thread_ts=thread_ts)
                status_ts = resp.get("ts")
            else:
                await self.update_message(channel, status_ts, text)

        logger.info("Graph streaming started: session=%s channel=%s", session_id, channel)
        try:
            async for event in graph.astream_events(initial_state, config=config, version="v2"):
                event_name = event.get("event", "")
                node_name = event.get("name", "")

                if event_name == "on_chain_start" and node_name not in ("", "LangGraph"):
                    logger.info("[%s] node start: %s", session_id[:8], node_name)
                    await _post_or_update(f"`{node_name}` 실행 중...")

                elif event_name == "on_chain_end" and node_name == "interrupt_l4":
                    # L4 interrupt: graph paused, send buttons
                    data = event.get("data", {})
                    task_id = data.get("output", {}).get("current_task_id", "?")
                    summary = data.get("output", {}).get("final_report", "수동 개입 필요")
                    self._pending_l4[session_id] = {
                        "channel": channel,
                        "thread_ts": thread_ts,
                        "graph": graph,
                        "config": config,
                        "task_id": task_id,
                    }
                    await self.send_l4_prompt(channel, thread_ts, task_id, str(summary))
                    return  # graph paused — will resume via button handler

                elif event_name == "on_chain_end" and node_name == "finalize":
                    output = event.get("data", {}).get("output", {})
                    report = output.get("final_report", "완료")
                    await _post_or_update(f"✅ 완료\n\n{report}")

        except Exception as exc:
            logger.exception("Graph streaming error: %s", exc)
            await _post_or_update(f"❌ 오류 발생: {exc}")

    # ------------------------------------------------------------------
    # Slack event/action handlers
    # ------------------------------------------------------------------

    def _register_handlers(self) -> None:
        app = self._app

        @app.event("app_mention")
        async def handle_mention(event: dict, say: Any) -> None:
            await self._on_mention(event, say)

        @app.action("l4_continue")
        async def handle_l4_continue(body: dict, ack: Any) -> None:
            await ack()
            await self._on_l4_action(body, resume_value="continue")

        @app.action("l4_abort")
        async def handle_l4_abort(body: dict, ack: Any) -> None:
            await ack()
            await self._on_l4_action(body, resume_value="abort")

    async def _on_mention(self, event: dict, say: Any) -> None:
        text: str = event.get("text", "")
        user_id: str = event.get("user", "")
        channel: str = event.get("channel", "")
        thread_ts: str = event.get("thread_ts") or event.get("ts", "")

        logger.info("Mention received from %s in %s: %.80s", user_id, channel, text)

        # Strip bot mention prefix <@BOTID>
        request = text.split(">", 1)[-1].strip() if ">" in text else text.strip()
        if not request:
            await say(text="요청 내용을 입력해 주세요.", thread_ts=thread_ts)
            return

        # Immediate acknowledgment — user sees a response right away
        await say(text=f"요청을 받았습니다. 처리를 시작합니다...\n세션 ID: `{str(uuid.uuid4())[:8]}`", thread_ts=thread_ts)

        # Complaint detection
        if any(kw in request for kw in self.COMPLAINT_KEYWORDS):
            asyncio.create_task(self._handle_complaint(user_id, request, channel, thread_ts))

        # Build graph and initial state
        from agentforge.core.models import WorkflowSpec
        from agentforge.core.state import make_initial_state
        from workflows.builder import GraphBuilder

        session_id = str(uuid.uuid4())
        state = make_initial_state(session_id=session_id, user_request=request)
        # GraphBuilder.from_spec already starts at refine_requirements;
        # pass an empty spec so it uses the full pipeline without a pre-built DAG.
        graph = GraphBuilder().from_spec(WorkflowSpec(name="pipeline", tasks=[]))

        task = asyncio.create_task(
            self.stream_graph_to_slack(graph, state, channel, thread_ts, session_id)
        )
        # Log unhandled exceptions from the background task
        task.add_done_callback(self._on_task_done)

    async def _on_l4_action(self, body: dict, resume_value: str) -> None:
        """Handle Block Kit button press for L4 escalation."""
        action = body.get("actions", [{}])[0]
        session_id = action.get("value", "")
        pending = self._pending_l4.pop(session_id, None)
        if pending is None:
            return

        from langgraph.types import Command

        channel = pending["channel"]
        thread_ts = pending["thread_ts"]
        graph = pending["graph"]
        config = pending["config"]

        label = "계속 진행" if resume_value == "continue" else "중단"
        await self.send_message(channel, f"👤 사용자 선택: {label}", thread_ts=thread_ts)

        asyncio.create_task(
            self._resume_graph(graph, config, resume_value, channel, thread_ts)
        )

    async def _resume_graph(
        self,
        graph: Any,
        config: dict,
        resume_value: str,
        channel: str,
        thread_ts: str,
    ) -> None:
        from langgraph.types import Command

        status_ts: Optional[str] = None

        async def _post(text: str) -> None:
            nonlocal status_ts
            resp = await self.send_message(channel, text, thread_ts=thread_ts)
            status_ts = resp.get("ts")

        try:
            async for event in graph.astream_events(
                Command(resume=resume_value), config=config, version="v2"
            ):
                event_name = event.get("event", "")
                node_name = event.get("name", "")
                if event_name == "on_chain_end" and node_name == "finalize":
                    output = event.get("data", {}).get("output", {})
                    report = output.get("final_report", "완료")
                    await _post(f"✅ 완료\n\n{report}")
        except Exception as exc:
            await _post(f"❌ 재개 중 오류: {exc}")

    async def _handle_complaint(
        self, user_id: str, message: str, channel: str, thread_ts: str
    ) -> None:
        """Forward complaints to the Historian and trigger retrospective analysis."""
        try:
            from agentforge.observer.historian import Historian
            from agentforge.observer.retrospective import RetrospectiveAgent
            from pathlib import Path

            historian = Historian()
            await historian.record_complaint(user_id, message)

            retro = RetrospectiveAgent()
            journal_dir = Path("memory/journal")
            proposal = await retro.analyze(journal_dir, trigger="complaint", context=message)
            if proposal:
                text = (
                    f"💡 개선 제안 #{proposal.proposal_id}\n\n"
                    f"*문제*: {proposal.problem}\n"
                    f"*제안*: {proposal.suggested_change}\n\n"
                    f"[수락] 또는 [거부] 버튼을 통해 응답해 주세요."
                )
                await self.send_message(channel, text, thread_ts=thread_ts)
        except Exception as exc:
            logger.warning("Complaint handling error: %s", exc)


# ------------------------------------------------------------------
# Block Kit helpers
# ------------------------------------------------------------------

def _build_l4_blocks(task_id: str, summary: str) -> list[dict]:
    return [
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": (
                    f"⚠️ *L4 에스컬레이션 — 개입 필요*\n\n"
                    f"태스크 `{task_id}`가 자동 해결 범위를 초과했습니다.\n\n"
                    f"*요약*: {summary[:300]}"
                ),
            },
        },
        {
            "type": "actions",
            "elements": [
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "계속 진행"},
                    "style": "primary",
                    "action_id": "l4_continue",
                    "value": task_id,
                },
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "중단"},
                    "style": "danger",
                    "action_id": "l4_abort",
                    "value": task_id,
                },
            ],
        },
    ]
