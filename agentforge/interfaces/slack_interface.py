from __future__ import annotations

import asyncio
import logging
import os
import uuid
from typing import Any, Optional

from agentforge.interfaces.base_interface import BaseInterface

logger = logging.getLogger(__name__)

_MOCK = os.getenv("AF_MOCK_MODE", "false").lower() == "true"

# Keywords that indicate user confirmation
_CONFIRM_KEYWORDS = {"네", "예", "yes", "y", "진행", "ㅇㅇ", "ok", "확인", "go", "그래"}
_CANCEL_KEYWORDS  = {"아니오", "아니", "no", "n", "취소", "cancel", "그만", "중단"}


class SlackInterface(BaseInterface):
    """
    Slack Bot interface using slack-bolt async mode.

    Flow:
      1. app_mention → ClarifierAgent asks focused questions in thread
      2. User replies in thread → more questions, or confirmation prompt (Block Kit buttons)
      3. User clicks [진행] → graph execution starts in background
      4. Graph streams updates back to the thread
      5. L4 interrupt → Block Kit [계속 진행] / [중단] buttons
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
        # thread_ts → clarification state (stage: "clarifying" | "confirming")
        self._pending_clarification: dict[str, dict] = {}
        # session_id → pending L4 state
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
        from agentforge.core.checkpoint import init_checkpointer

        await init_checkpointer()

        self._app = AsyncApp(
            token=self._bot_token,
            signing_secret=self._signing_secret,
        )
        self._register_handlers()
        self._handler = AsyncSocketModeHandler(self._app, self._app_token)

        await self._log_startup_info()
        await self._handler.start_async()

    async def _log_startup_info(self) -> None:
        client = self._app.client
        sep = "-" * 60

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

        try:
            channels_resp = await client.conversations_list(
                types="public_channel,private_channel",
                exclude_archived=True,
                limit=200,
            )
            joined = [ch for ch in channels_resp.get("channels", []) if ch.get("is_member")]
            if joined:
                logger.info("  참여 채널 (%d개):", len(joined))
                for ch in joined:
                    logger.info("    #%s  (id=%s, members=%s)",
                                ch.get("name", "?"), ch.get("id", "?"), ch.get("num_members", "?"))
            else:
                logger.warning("  [!] 참여 중인 채널 없음")
                logger.warning("      채널에서 /invite @%s 를 실행하세요", bot_name)
        except Exception as exc:
            if "missing_scope" in str(exc):
                logger.info("  참여 채널 조회 불가 (channels:read 스코프 없음)")
                logger.info("  채널 초대 방법: 해당 채널에서 /invite @%s 입력", bot_name)
            else:
                logger.warning("  conversations.list 실패: %s", exc)

        logger.info("  수신 이벤트 : app_mention, message (thread replies)")
        logger.info("  인터랙션   : clarify_confirm, clarify_cancel, l4_continue, l4_abort")
        logger.info(sep)

    async def stop(self) -> None:
        if _MOCK or self._handler is None:
            return
        await self._handler.close_async()

    async def send_message(self, channel: str, text: str, **kwargs: Any) -> Any:
        if _MOCK:
            logger.info("[mock] send_message channel=%s text=%.80s", channel, text)
            return {"ts": "0.0", "channel": channel}
        return await self._app.client.chat_postMessage(channel=channel, text=text, **kwargs)

    async def update_message(self, channel: str, ts: str, text: str, **kwargs: Any) -> Any:
        if _MOCK:
            logger.info("[mock] update_message channel=%s ts=%s text=%.80s", channel, ts, text)
            return {}
        return await self._app.client.chat_update(channel=channel, ts=ts, text=text, **kwargs)

    async def send_l4_prompt(self, channel: str, thread_ts: str, task_id: str, summary: str) -> Any:
        blocks = _build_l4_blocks(task_id, summary)
        if _MOCK:
            logger.info("[mock] send_l4_prompt task_id=%s", task_id)
            return {"ts": "0.0", "channel": channel}
        return await self._app.client.chat_postMessage(
            channel=channel,
            thread_ts=thread_ts,
            text=f"경고: 개입 필요: {task_id}",
            blocks=blocks,
        )

    def _on_task_done(self, task: "asyncio.Task") -> None:
        if task.cancelled():
            return
        exc = task.exception()
        if exc:
            logger.exception("Background task failed: %s", exc, exc_info=exc)

    # ------------------------------------------------------------------
    # Slack handlers
    # ------------------------------------------------------------------

    def _register_handlers(self) -> None:
        app = self._app

        @app.middleware
        async def log_all_events(payload: dict, next: Any) -> None:
            event_type = (
                payload.get("event", {}).get("type")
                or payload.get("type")
                or "unknown"
            )
            subtype = payload.get("event", {}).get("subtype", "")
            channel = (
                payload.get("event", {}).get("channel")
                or payload.get("channel_id", "")
            )
            logger.debug(
                "[Slack] 수신: type=%s%s channel=%s",
                event_type,
                f"/{subtype}" if subtype else "",
                channel or "(없음)",
            )
            await next()

        @app.event("app_mention")
        async def handle_mention(event: dict, say: Any) -> None:
            await self._on_mention(event, say)

        @app.event("message")
        async def handle_message(event: dict, client: Any) -> None:
            # Ignore bot messages and subtypes (edits, deletes, etc.)
            if event.get("subtype"):
                return
            # Only handle user messages inside active clarification threads
            thread_ts = event.get("thread_ts")
            if thread_ts and thread_ts in self._pending_clarification:
                user_text = event.get("text", "").strip()
                channel   = event.get("channel", "")
                if user_text:
                    task = asyncio.create_task(
                        self._handle_clarification_reply(client, channel, thread_ts, user_text)
                    )
                    task.add_done_callback(self._on_task_done)

        # Clarification confirm / cancel buttons
        @app.action("clarify_confirm")
        async def handle_clarify_confirm(body: dict, ack: Any, client: Any) -> None:
            await ack()
            await self._on_clarify_action(body, client, confirmed=True)

        @app.action("clarify_cancel")
        async def handle_clarify_cancel(body: dict, ack: Any, client: Any) -> None:
            await ack()
            await self._on_clarify_action(body, client, confirmed=False)

        # L4 escalation buttons
        @app.action("l4_continue")
        async def handle_l4_continue(body: dict, ack: Any) -> None:
            await ack()
            await self._on_l4_action(body, resume_value="continue")

        @app.action("l4_abort")
        async def handle_l4_abort(body: dict, ack: Any) -> None:
            await ack()
            await self._on_l4_action(body, resume_value="abort")

    # ------------------------------------------------------------------
    # Clarification flow
    # ------------------------------------------------------------------

    async def _on_mention(self, event: dict, say: Any) -> None:
        text: str     = event.get("text", "")
        user_id: str  = event.get("user", "")
        channel: str  = event.get("channel", "")
        thread_ts: str = event.get("thread_ts") or event.get("ts", "")

        # Strip bot mention prefix <@BOTID>
        request = text.split(">", 1)[-1].strip() if ">" in text else text.strip()

        logger.info("Mention from %s in %s: %.80s", user_id, channel, request)

        # If this mention is a reply inside an existing clarification thread, treat it
        # as a clarification reply (user mentioned bot while answering)
        if thread_ts in self._pending_clarification:
            if request:
                task = asyncio.create_task(
                    self._handle_clarification_reply(
                        self._app.client, channel, thread_ts, request
                    )
                )
                task.add_done_callback(self._on_task_done)
            return

        if not request:
            await say(text="요청 내용을 입력해 주세요.", thread_ts=thread_ts)
            return

        # Complaint detection (runs independently)
        if any(kw in request for kw in self.COMPLAINT_KEYWORDS):
            asyncio.create_task(
                self._handle_complaint(user_id, request, channel, thread_ts)
            )

        # Start clarification dialogue
        self._pending_clarification[thread_ts] = {
            "channel": channel,
            "thread_ts": thread_ts,
            "user_id": user_id,
            "request": request,
            "history": [{"role": "user", "content": request}],
            "stage": "clarifying",
        }

        task = asyncio.create_task(
            self._run_clarification_turn(self._app.client, channel, thread_ts)
        )
        task.add_done_callback(self._on_task_done)

    async def _handle_clarification_reply(
        self, client: Any, channel: str, thread_ts: str, user_text: str
    ) -> None:
        state = self._pending_clarification.get(thread_ts)
        if state is None:
            return

        stage = state["stage"]

        if stage == "confirming":
            # Already showed confirmation buttons — text reply is ignored here;
            # user should click the button. But handle plain text as fallback.
            lowered = user_text.lower().strip()
            if any(k in lowered for k in _CONFIRM_KEYWORDS):
                await self._execute_graph(client, state)
            elif any(k in lowered for k in _CANCEL_KEYWORDS):
                self._pending_clarification.pop(thread_ts, None)
                await client.chat_postMessage(
                    channel=channel, thread_ts=thread_ts,
                    text="취소됐습니다. 다시 요청하려면 봇을 멘션해 주세요."
                )
            return

        # stage == "clarifying": add user reply to history and continue
        state["history"].append({"role": "user", "content": user_text})
        await self._run_clarification_turn(client, channel, thread_ts)

    async def _run_clarification_turn(
        self, client: Any, channel: str, thread_ts: str
    ) -> None:
        state = self._pending_clarification.get(thread_ts)
        if state is None:
            return

        if _MOCK:
            # In mock mode, skip LLM and go straight to confirmation
            await self._send_confirmation_prompt(client, channel, thread_ts,
                                                  summary=f"요청: {state['request']}")
            return

        try:
            from agentforge.agents.clarifier import ClarifierAgent
            agent = ClarifierAgent()
            result = await agent.next_turn(state["history"])
        except Exception as exc:
            logger.exception("ClarifierAgent error: %s", exc)
            await client.chat_postMessage(
                channel=channel, thread_ts=thread_ts,
                text=f"요구사항 분석 중 오류가 발생했습니다: {exc}"
            )
            self._pending_clarification.pop(thread_ts, None)
            return

        if result.get("status") == "ready":
            summary = result.get("summary", state["request"])
            state["history"].append({"role": "assistant", "content": summary})
            await self._send_confirmation_prompt(client, channel, thread_ts, summary)
        else:
            question = result.get("message", "요구사항을 좀 더 설명해 주시겠어요?")
            state["history"].append({"role": "assistant", "content": question})
            await client.chat_postMessage(
                channel=channel, thread_ts=thread_ts, text=question
            )

    async def _send_confirmation_prompt(
        self, client: Any, channel: str, thread_ts: str, summary: str
    ) -> None:
        state = self._pending_clarification.get(thread_ts)
        if state is None:
            return
        state["stage"] = "confirming"
        state["summary"] = summary

        blocks = _build_confirmation_blocks(summary, thread_ts)
        await client.chat_postMessage(
            channel=channel,
            thread_ts=thread_ts,
            text="요구사항을 확인했습니다. 이대로 진행할까요?",
            blocks=blocks,
        )

    async def _on_clarify_action(
        self, body: dict, client: Any, confirmed: bool
    ) -> None:
        action = body.get("actions", [{}])[0]
        thread_ts = action.get("value", "")
        state = self._pending_clarification.get(thread_ts)
        if state is None:
            return

        channel = state["channel"]

        if not confirmed:
            self._pending_clarification.pop(thread_ts, None)
            await client.chat_postMessage(
                channel=channel, thread_ts=thread_ts,
                text="취소됐습니다. 다시 요청하려면 봇을 멘션해 주세요."
            )
            return

        await self._execute_graph(client, state)

    async def _execute_graph(self, client: Any, state: dict) -> None:
        thread_ts = state["thread_ts"]
        channel   = state["channel"]
        summary   = state.get("summary", state["request"])

        self._pending_clarification.pop(thread_ts, None)

        session_id = str(uuid.uuid4())
        await client.chat_postMessage(
            channel=channel,
            thread_ts=thread_ts,
            text=f"진행합니다. 세션 ID: `{session_id[:8]}`",
        )

        from agentforge.core.models import WorkflowSpec
        from agentforge.core.state import make_initial_state
        from workflows.builder import GraphBuilder

        # Use the clarified summary as the actual request fed to the graph
        state_obj = make_initial_state(session_id=session_id, user_request=summary)
        graph = GraphBuilder().from_spec(WorkflowSpec(name="pipeline", tasks=[]))

        task = asyncio.create_task(
            self.stream_graph_to_slack(graph, state_obj, channel, thread_ts, session_id)
        )
        task.add_done_callback(self._on_task_done)

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
                node_name  = event.get("name", "")

                if event_name == "on_chain_start" and node_name not in ("", "LangGraph"):
                    logger.info("[%s] node start: %s", session_id[:8], node_name)
                    await _post_or_update(f"`{node_name}` 실행 중...")

                elif event_name == "on_chain_end" and node_name == "interrupt_l4":
                    data    = event.get("data", {})
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
                    return

                elif event_name == "on_chain_end" and node_name == "finalize":
                    output = event.get("data", {}).get("output", {})
                    report = output.get("final_report", "완료")
                    await _post_or_update(f"완료\n\n{report}")

        except Exception as exc:
            logger.exception("Graph streaming error: %s", exc)
            await _post_or_update(f"오류 발생: {exc}")

    # ------------------------------------------------------------------
    # L4 escalation
    # ------------------------------------------------------------------

    async def _on_l4_action(self, body: dict, resume_value: str) -> None:
        action     = body.get("actions", [{}])[0]
        session_id = action.get("value", "")
        pending    = self._pending_l4.pop(session_id, None)
        if pending is None:
            return

        from langgraph.types import Command

        channel   = pending["channel"]
        thread_ts = pending["thread_ts"]
        graph     = pending["graph"]
        config    = pending["config"]

        label = "계속 진행" if resume_value == "continue" else "중단"
        await self.send_message(channel, f"사용자 선택: {label}", thread_ts=thread_ts)

        asyncio.create_task(
            self._resume_graph(graph, config, resume_value, channel, thread_ts)
        )

    async def _resume_graph(
        self, graph: Any, config: dict, resume_value: str,
        channel: str, thread_ts: str,
    ) -> None:
        from langgraph.types import Command

        async def _post(text: str) -> None:
            await self.send_message(channel, text, thread_ts=thread_ts)

        try:
            async for event in graph.astream_events(
                Command(resume=resume_value), config=config, version="v2"
            ):
                event_name = event.get("event", "")
                node_name  = event.get("name", "")
                if event_name == "on_chain_end" and node_name == "finalize":
                    output = event.get("data", {}).get("output", {})
                    report = output.get("final_report", "완료")
                    await _post(f"완료\n\n{report}")
        except Exception as exc:
            await _post(f"재개 중 오류: {exc}")

    # ------------------------------------------------------------------
    # Complaint handling
    # ------------------------------------------------------------------

    async def _handle_complaint(
        self, user_id: str, message: str, channel: str, thread_ts: str
    ) -> None:
        try:
            from agentforge.observer.historian import Historian
            from agentforge.observer.retrospective import RetrospectiveAgent
            from pathlib import Path

            historian = Historian()
            await historian.record_complaint(user_id, message)

            retro    = RetrospectiveAgent()
            proposal = await retro.analyze(Path("memory/journal"), trigger="complaint", context=message)
            if proposal:
                text = (
                    f"개선 제안 #{proposal.proposal_id}\n\n"
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

def _build_confirmation_blocks(summary: str, thread_ts: str) -> list[dict]:
    return [
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": (
                    "*요구사항 정리가 완료됐습니다.*\n\n"
                    f"{summary}\n\n"
                    "이대로 진행할까요?"
                ),
            },
        },
        {
            "type": "actions",
            "elements": [
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "진행"},
                    "style": "primary",
                    "action_id": "clarify_confirm",
                    "value": thread_ts,
                },
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "취소"},
                    "style": "danger",
                    "action_id": "clarify_cancel",
                    "value": thread_ts,
                },
            ],
        },
    ]


def _build_l4_blocks(task_id: str, summary: str) -> list[dict]:
    return [
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": (
                    f"*L4 에스컬레이션 - 개입 필요*\n\n"
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
