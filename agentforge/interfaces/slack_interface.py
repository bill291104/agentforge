from __future__ import annotations

import asyncio
import logging
import os
import uuid
from typing import Any, Optional

from agentforge.interfaces.base_interface import BaseInterface

logger = logging.getLogger(__name__)

_MOCK = os.getenv("AF_MOCK_MODE", "false").lower() == "true"

_CONFIRM_KEYWORDS = {"네", "예", "yes", "y", "진행", "ㅇㅇ", "ok", "확인", "go", "그래"}
_CANCEL_KEYWORDS  = {"아니오", "아니", "no", "n", "취소", "cancel", "그만", "중단"}

# Tools the LLM can call when responding to a mention in a completed thread
_COMPLETED_THREAD_TOOLS = [
    {
        "name": "start_new_task",
        "description": (
            "이전 작업을 재시작하거나 수정된 요구사항으로 새 작업을 시작합니다. "
            "사용자가 다시 시도하거나, 수정/개선을 원하거나, 새 기능을 추가하고 싶을 때 사용합니다."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "request": {
                    "type": "string",
                    "description": "새 작업 요청 내용. 비어 있으면 이전 요구사항을 그대로 사용합니다.",
                }
            },
            "required": [],
        },
    },
    {
        "name": "answer_question",
        "description": (
            "완료(또는 실패)된 세션에 대한 질문에 답합니다. "
            "실패 원인 분석, 결과 설명, 개선 방안 제안, 진행 상황 문의 등에 사용합니다."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "answer": {
                    "type": "string",
                    "description": "사용자 질문에 대한 답변 (마크다운 허용).",
                }
            },
            "required": ["answer"],
        },
    },
]


class SlackInterface(BaseInterface):
    """
    Slack Bot interface using slack-bolt async mode.

    Flow:
      1. app_mention → ClarifierAgent asks focused questions in thread
      2. User replies in thread → more questions, or confirmation prompt (Block Kit buttons)
      3. User clicks [진행] → graph execution starts in background
      4. Graph streams updates back to the thread
      5. L4 interrupt → Block Kit [계속 진행] / [중단] buttons

    Persistence:
      Thread context (clarifying/confirming/running/l4_waiting) is stored in SQLite
      so the bot can resume across restarts.

    Message visibility:
      The bot receives ALL messages in joined channels (message.channels event).
      It only responds when @-mentioned. Existing clarification threads are the
      exception — replies without mention are accepted inside active threads.

    Slack app settings required:
      Event Subscriptions → Subscribe to bot events:
        - app_mention
        - message.channels   (public channels)
        - message.groups     (private channels, if needed)
    """

    COMPLAINT_KEYWORDS = ["느려", "왜", "실패", "이상", "버그", "문제"]

    def __init__(
        self,
        bot_token: Optional[str] = None,
        app_token: Optional[str] = None,
        signing_secret: Optional[str] = None,
    ) -> None:
        self._bot_token     = bot_token or os.getenv("SLACK_BOT_TOKEN", "")
        self._app_token     = app_token or os.getenv("SLACK_APP_TOKEN", "")
        self._signing_secret = signing_secret or os.getenv("SLACK_SIGNING_SECRET", "")
        self._app: Any = None
        self._handler: Any = None
        self._bot_user_id: str = ""

        # In-memory cache: thread_ts → state dict (backed by ThreadContextStore)
        self._pending_clarification: dict[str, dict] = {}
        # session_id → pending L4 info (also persisted via thread_contexts stage=l4_waiting)
        self._pending_l4: dict[str, dict] = {}

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
        from agentforge.core.context_store import init_context_store

        await init_checkpointer()
        await init_context_store()

        self._app = AsyncApp(
            token=self._bot_token,
            signing_secret=self._signing_secret,
        )
        self._register_handlers()
        self._handler = AsyncSocketModeHandler(self._app, self._app_token)

        await self._log_startup_info()

        # Schedule session restoration as background task.
        # It runs once the event loop yields inside handler.start_async().
        asyncio.create_task(self._restore_sessions())

        await self._handler.start_async()

    async def _log_startup_info(self) -> None:
        client = self._app.client
        sep = "-" * 60
        try:
            auth = await client.auth_test()
            self._bot_user_id = auth.get("user_id", "")
            bot_name = auth.get("user", "?")
            team     = auth.get("team", "?")
            logger.info(sep)
            logger.info("AgentForge Slack Bot 시작됨")
            logger.info("  봇 이름  : @%s", bot_name)
            logger.info("  봇 ID    : %s", self._bot_user_id)
            logger.info("  워크스페이스: %s", team)
            logger.info("  멘션 형식 : <@%s>", self._bot_user_id)
        except Exception as exc:
            logger.error("auth.test 실패: %s", exc)
            return

        try:
            resp = await client.conversations_list(
                types="public_channel,private_channel",
                exclude_archived=True,
                limit=200,
            )
            joined = [ch for ch in resp.get("channels", []) if ch.get("is_member")]
            if joined:
                logger.info("  참여 채널 (%d개):", len(joined))
                for ch in joined:
                    logger.info("    #%s  (id=%s)", ch.get("name", "?"), ch.get("id", "?"))
            else:
                logger.warning("  [!] 참여 중인 채널 없음 — /invite @%s 를 실행하세요", auth.get("user", "?"))
        except Exception as exc:
            if "missing_scope" not in str(exc):
                logger.warning("  conversations.list 실패: %s", exc)

        logger.info("  수신 이벤트: app_mention, message.channels (멘션 시에만 응답)")
        logger.info("  컨텍스트 영속화: SQLite thread_contexts 테이블")
        logger.info(sep)

    async def _restore_sessions(self) -> None:
        """Called once on startup — resume threads that were active before restart."""
        # Small delay so the WebSocket connection is fully established
        await asyncio.sleep(2)

        from agentforge.core.context_store import get_context_store
        from agentforge.core.checkpoint import get_checkpointer

        store = get_context_store()
        self._pending_clarification = await store.load_all()

        if not self._pending_clarification:
            return

        logger.info("Restoring %d thread context(s)...", len(self._pending_clarification))
        cp = get_checkpointer()

        for thread_ts, state in list(self._pending_clarification.items()):
            stage      = state.get("stage", "")
            channel    = state.get("channel", "")
            session_id = state.get("session_id")

            if stage in ("clarifying", "confirming", "completed"):
                # Self-contained — resumes naturally when user sends next message.
                logger.info("Thread %s restored (stage=%s)", thread_ts[:12], stage)

            elif stage == "running" and session_id:
                config = {"configurable": {"thread_id": session_id}}
                cp_tuple = await cp.aget(config)
                if cp_tuple is None:
                    logger.warning("No checkpoint for session %s — clearing thread", session_id[:8])
                    await store.delete(thread_ts)
                    del self._pending_clarification[thread_ts]
                    await self._notify(channel, thread_ts,
                                       "봇이 재시작됐지만 작업 체크포인트를 찾을 수 없습니다. "
                                       "다시 멘션하여 새 작업을 시작해 주세요.")
                else:
                    logger.info("Resuming session %s for thread %s", session_id[:8], thread_ts[:12])
                    await self._notify(channel, thread_ts,
                                       f"봇이 재시작됐습니다. 세션 `{session_id[:8]}`을 이어서 진행합니다...")
                    graph = _build_graph()
                    asyncio.create_task(
                        self.stream_graph_to_slack(graph, None, channel, thread_ts, session_id)
                    )

            elif stage == "l4_waiting" and session_id:
                task_id = state.get("task_id", "?")
                self._pending_l4[session_id] = {
                    "channel": channel,
                    "thread_ts": thread_ts,
                    "graph": _build_graph(),
                    "config": {"configurable": {"thread_id": session_id}},
                    "task_id": task_id,
                }
                logger.info("Restored L4 state for session %s", session_id[:8])
                await self._notify(channel, thread_ts,
                                   f"봇이 재시작됐습니다. "
                                   f"세션 `{session_id[:8]}`의 L4 에스컬레이션 버튼을 눌러 계속하세요.")

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
            return {"ts": "0.0"}
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

        # Responds only when bot is @-mentioned
        @app.event("app_mention")
        async def handle_mention(event: dict, say: Any) -> None:
            await self._on_mention(event, say)

        # Receives ALL channel messages (requires message.channels event subscription)
        # Handles:
        #  - clarification replies in active threads (no mention needed)
        #  - passive observation of all other messages (for logging/historian)
        @app.event("message")
        async def handle_message(event: dict, client: Any) -> None:
            # Ignore subtypes: edits, deletes, thread_broadcast, etc.
            if event.get("subtype"):
                return
            # Ignore all bot messages (including our own)
            if event.get("bot_id") or event.get("app_id"):
                return

            text      = event.get("text", "")
            thread_ts = event.get("thread_ts")
            channel   = event.get("channel", "")

            # If the message mentions the bot, app_mention already handles it — skip
            if self._bot_user_id and f"<@{self._bot_user_id}>" in text:
                return

            # Handle reply inside an active clarification thread (no mention required)
            if thread_ts and thread_ts in self._pending_clarification:
                user_text = text.strip()
                if user_text:
                    task = asyncio.create_task(
                        self._handle_clarification_reply(client, channel, thread_ts, user_text)
                    )
                    task.add_done_callback(self._on_task_done)
                return

            # All other messages: observed but not acted upon
            # (Historian / complaint detection could be wired here in the future)

        # Clarification confirm / cancel
        @app.action("clarify_confirm")
        async def handle_clarify_confirm(body: dict, ack: Any, client: Any) -> None:
            await ack()
            await self._on_clarify_action(body, client, confirmed=True)

        @app.action("clarify_cancel")
        async def handle_clarify_cancel(body: dict, ack: Any, client: Any) -> None:
            await ack()
            await self._on_clarify_action(body, client, confirmed=False)

        # L4 escalation
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
        text: str      = event.get("text", "")
        user_id: str   = event.get("user", "")
        channel: str   = event.get("channel", "")
        thread_ts: str = event.get("thread_ts") or event.get("ts", "")

        # Strip bot mention prefix <@BOTID>
        request = text.split(">", 1)[-1].strip() if ">" in text else text.strip()

        logger.info("Mention from %s in %s: %.80s", user_id, channel, request)

        existing = self._pending_clarification.get(thread_ts)

        # Mention inside an existing thread — route by stage
        if existing:
            stage = existing.get("stage", "")
            if stage == "running":
                session_id = existing.get("session_id", "")
                await say(
                    text=f"세션 `{session_id[:8]}`이 실행 중입니다. 완료되면 알림을 드립니다.",
                    thread_ts=thread_ts,
                )
                return
            if stage == "l4_waiting":
                await say(
                    text="L4 에스컬레이션 버튼을 사용하여 작업을 계속하거나 중단해 주세요.",
                    thread_ts=thread_ts,
                )
                return
            if stage == "completed":
                # Let the LLM decide the action via tool calling
                if request:
                    task = asyncio.create_task(
                        self._dispatch_completed_thread(
                            self._app.client, existing, request, channel, thread_ts, user_id
                        )
                    )
                    task.add_done_callback(self._on_task_done)
                return
            elif stage in ("clarifying", "confirming"):
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

        # Complaint detection
        if any(kw in request for kw in self.COMPLAINT_KEYWORDS):
            asyncio.create_task(
                self._handle_complaint(user_id, request, channel, thread_ts)
            )

        # Start new clarification dialogue
        state: dict = {
            "channel": channel,
            "thread_ts": thread_ts,
            "user_id": user_id,
            "request": request,
            "history": [{"role": "user", "content": request}],
            "stage": "clarifying",
            "summary": None,
            "session_id": None,
            "task_id": None,
        }
        self._pending_clarification[thread_ts] = state
        await self._persist(thread_ts)

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
            lowered = user_text.lower().strip()
            if any(k in lowered for k in _CONFIRM_KEYWORDS):
                await self._execute_graph(client, state)
            elif any(k in lowered for k in _CANCEL_KEYWORDS):
                await self._delete_thread(thread_ts)
                await client.chat_postMessage(
                    channel=channel, thread_ts=thread_ts,
                    text="취소됐습니다. 다시 요청하려면 봇을 멘션해 주세요."
                )
            # else: wait for button click
            return

        # stage == "clarifying"
        state["history"].append({"role": "user", "content": user_text})
        await self._persist(thread_ts)
        await self._run_clarification_turn(client, channel, thread_ts)

    async def _run_clarification_turn(
        self, client: Any, channel: str, thread_ts: str
    ) -> None:
        state = self._pending_clarification.get(thread_ts)
        if state is None:
            return

        if _MOCK:
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
                text=f"요구사항 분석 중 오류: {exc}"
            )
            await self._delete_thread(thread_ts)
            return

        if result.get("status") == "ready":
            summary = result.get("summary", state["request"])
            state["history"].append({"role": "assistant", "content": summary})
            await self._persist(thread_ts)
            await self._send_confirmation_prompt(client, channel, thread_ts, summary)
        else:
            question = result.get("message", "요구사항을 좀 더 설명해 주시겠어요?")
            state["history"].append({"role": "assistant", "content": question})
            await self._persist(thread_ts)
            await client.chat_postMessage(
                channel=channel, thread_ts=thread_ts, text=question
            )

    async def _send_confirmation_prompt(
        self, client: Any, channel: str, thread_ts: str, summary: str
    ) -> None:
        state = self._pending_clarification.get(thread_ts)
        if state is None:
            return
        state["stage"]   = "confirming"
        state["summary"] = summary
        await self._persist(thread_ts)

        await client.chat_postMessage(
            channel=channel,
            thread_ts=thread_ts,
            text="요구사항을 확인했습니다. 이대로 진행할까요?",
            blocks=_build_confirmation_blocks(summary, thread_ts),
        )

    async def _on_clarify_action(
        self, body: dict, client: Any, confirmed: bool
    ) -> None:
        action    = body.get("actions", [{}])[0]
        thread_ts = action.get("value", "")
        state     = self._pending_clarification.get(thread_ts)
        if state is None:
            return

        channel = state["channel"]

        if not confirmed:
            await self._delete_thread(thread_ts)
            await client.chat_postMessage(
                channel=channel, thread_ts=thread_ts,
                text="취소됐습니다. 다시 요청하려면 봇을 멘션해 주세요."
            )
            return

        await self._execute_graph(client, state)

    async def _execute_graph(self, client: Any, state: dict) -> None:
        thread_ts  = state["thread_ts"]
        channel    = state["channel"]
        summary    = state.get("summary") or state["request"]
        session_id = str(uuid.uuid4())

        # Transition to "running" stage and persist
        state["stage"]      = "running"
        state["session_id"] = session_id
        await self._persist(thread_ts)

        await client.chat_postMessage(
            channel=channel, thread_ts=thread_ts,
            text=f"진행합니다. 세션 ID: `{session_id[:8]}`",
        )

        from agentforge.core.models import WorkflowSpec
        from agentforge.core.state import make_initial_state

        state_obj = make_initial_state(session_id=session_id, user_request=summary)
        graph     = _build_graph()

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
        initial_state: Optional[dict],
        channel: str,
        thread_ts: str,
        session_id: str,
    ) -> None:
        """
        Stream graph events to Slack thread.
        Pass initial_state=None to resume from an existing checkpoint (after restart).
        """
        config     = {"configurable": {"thread_id": session_id}}
        status_ts: Optional[str] = None

        async def _post_or_update(text: str) -> None:
            nonlocal status_ts
            if status_ts is None:
                resp = await self.send_message(channel, text, thread_ts=thread_ts)
                status_ts = resp.get("ts")
            else:
                await self.update_message(channel, status_ts, text)

        logger.info("Graph streaming started: session=%s", session_id[:8])
        final_report = ""
        error_msg    = ""
        try:
            async for event in graph.astream_events(
                initial_state, config=config, version="v2"
            ):
                event_name = event.get("event", "")
                node_name  = event.get("name", "")

                if event_name == "on_chain_start" and node_name not in ("", "LangGraph"):
                    logger.info("[%s] node start: %s", session_id[:8], node_name)
                    await _post_or_update(f"`{node_name}` 실행 중...")

                elif event_name == "on_chain_end" and node_name == "interrupt_l4":
                    data    = event.get("data", {})
                    task_id = data.get("output", {}).get("current_task_id", "?")
                    summary = data.get("output", {}).get("final_report", "수동 개입 필요")

                    # Persist L4 stage
                    state = self._pending_clarification.get(thread_ts, {})
                    state.update({"stage": "l4_waiting", "task_id": task_id})
                    self._pending_clarification[thread_ts] = state
                    await self._persist(thread_ts)

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
                    final_report = output.get("final_report", "완료")
                    await _post_or_update(f"완료\n\n{final_report}")

        except Exception as exc:
            logger.exception("Graph streaming error: %s", exc)
            error_msg = str(exc)
            await _post_or_update(f"오류 발생: {exc}")
        finally:
            # Keep thread context as "completed" so follow-up questions work.
            # The summary field holds the final report for context.
            state = self._pending_clarification.get(thread_ts) or {
                "thread_ts": thread_ts,
                "channel": channel,
                "user_id": "",
                "request": "",
                "history": [],
                "session_id": session_id,
                "task_id": None,
            }
            state["stage"]   = "completed"
            state["summary"] = final_report or (f"오류: {error_msg}" if error_msg else "")
            self._pending_clarification[thread_ts] = state
            await self._persist(thread_ts)

    # ------------------------------------------------------------------
    # L4 escalation
    # ------------------------------------------------------------------

    async def _on_l4_action(self, body: dict, resume_value: str) -> None:
        action     = body.get("actions", [{}])[0]
        session_id = action.get("value", "")
        pending    = self._pending_l4.pop(session_id, None)
        if pending is None:
            return

        channel   = pending["channel"]
        thread_ts = pending["thread_ts"]
        graph     = pending["graph"]
        config    = pending["config"]

        label = "계속 진행" if resume_value == "continue" else "중단"
        await self.send_message(channel, f"사용자 선택: {label}", thread_ts=thread_ts)

        asyncio.create_task(
            self._resume_graph(graph, config, resume_value, channel, thread_ts, session_id)
        )

    async def _resume_graph(
        self, graph: Any, config: dict, resume_value: str,
        channel: str, thread_ts: str, session_id: str,
    ) -> None:
        from langgraph.types import Command

        async def _post(text: str) -> None:
            await self.send_message(channel, text, thread_ts=thread_ts)

        final_report = ""
        error_msg    = ""
        try:
            async for event in graph.astream_events(
                Command(resume=resume_value), config=config, version="v2"
            ):
                event_name = event.get("event", "")
                node_name  = event.get("name", "")
                if event_name == "on_chain_end" and node_name == "finalize":
                    output = event.get("data", {}).get("output", {})
                    final_report = output.get("final_report", "완료")
                    await _post(f"완료\n\n{final_report}")
        except Exception as exc:
            error_msg = str(exc)
            await _post(f"재개 중 오류: {exc}")
        finally:
            state = self._pending_clarification.get(thread_ts) or {
                "thread_ts": thread_ts,
                "channel": channel,
                "user_id": "",
                "request": "",
                "history": [],
                "session_id": session_id,
                "task_id": None,
            }
            state["stage"]   = "completed"
            state["summary"] = final_report or (f"오류: {error_msg}" if error_msg else "")
            self._pending_clarification[thread_ts] = state
            await self._persist(thread_ts)

    # ------------------------------------------------------------------
    # Completed-thread dispatcher (tool calling)
    # ------------------------------------------------------------------

    async def _dispatch_completed_thread(
        self,
        client: Any,
        state: dict,
        user_message: str,
        channel: str,
        thread_ts: str,
        user_id: str,
    ) -> None:
        """
        Use tool calling so the LLM can choose between:
          - start_new_task  : restart or modify the task
          - answer_question : explain results, analyse failure, etc.
        """
        original_request = state.get("request", "")
        final_report     = state.get("summary", "")
        session_id       = state.get("session_id", "")

        thread_text = await self._fetch_thread_messages(client, channel, thread_ts)

        context = (
            f"## 원래 요청\n{original_request}\n\n"
            f"## 최종 작업 보고서 (세션 {session_id[:8] if session_id else '?'})\n"
            f"{final_report}\n\n"
            f"## 스레드 대화 내역\n{thread_text}\n\n"
            f"## 사용자 메시지\n{user_message}"
        )

        try:
            import anthropic
            from agentforge.core.models import MODEL_IDS, ModelTier

            ai = anthropic.AsyncAnthropic()
            response = await ai.messages.create(
                model=MODEL_IDS[ModelTier.SONNET],
                max_tokens=1024,
                tools=_COMPLETED_THREAD_TOOLS,
                system=(
                    "당신은 AgentForge 소프트웨어 개발 AI 시스템의 어시스턴트입니다.\n"
                    "제공된 작업 보고서와 스레드 대화 내역을 보고, 사용자 메시지의 의도에 맞는 도구를 선택하세요.\n"
                    "- 작업 재시작/재시도/수정/개선 요청 → start_new_task\n"
                    "- 결과 설명, 실패 원인 분석, 상태 확인 등 질문 → answer_question\n"
                    "반드시 도구를 호출하세요. 텍스트 응답만 하지 마세요."
                ),
                messages=[{"role": "user", "content": context}],
            )
        except Exception as exc:
            logger.exception("Dispatch completed thread error: %s", exc)
            await client.chat_postMessage(
                channel=channel, thread_ts=thread_ts,
                text=f"요청 처리 중 오류가 발생했습니다: {exc}"
            )
            return

        # Process tool call
        tool_use_block = next(
            (b for b in response.content if getattr(b, "type", None) == "tool_use"),
            None,
        )

        if tool_use_block is None:
            # Model returned text without calling a tool — show it as-is
            text = " ".join(
                getattr(b, "text", "") for b in response.content
                if getattr(b, "type", None) == "text"
            ).strip()
            await client.chat_postMessage(channel=channel, thread_ts=thread_ts, text=text or "처리할 수 없습니다.")
            return

        tool_name  = tool_use_block.name
        tool_input = tool_use_block.input or {}
        logger.info("Completed-thread tool selected: %s (thread=%s)", tool_name, thread_ts[:12])

        if tool_name == "start_new_task":
            new_request = tool_input.get("request", "").strip() or original_request
            await self._delete_thread(thread_ts)
            # Synthesize a fresh mention event by directly starting clarification
            fresh_state: dict = {
                "channel": channel,
                "thread_ts": thread_ts,
                "user_id": user_id,
                "request": new_request,
                "history": [{"role": "user", "content": new_request}],
                "stage": "clarifying",
                "summary": None,
                "session_id": None,
                "task_id": None,
            }
            self._pending_clarification[thread_ts] = fresh_state
            await self._persist(thread_ts)
            await self._run_clarification_turn(client, channel, thread_ts)

        elif tool_name == "answer_question":
            answer = tool_input.get("answer", "답변을 생성할 수 없습니다.")
            await client.chat_postMessage(channel=channel, thread_ts=thread_ts, text=answer)

    async def _fetch_thread_messages(
        self, client: Any, channel: str, thread_ts: str, limit: int = 30
    ) -> str:
        """Fetch and format messages from a Slack thread for LLM context."""
        try:
            resp = await client.conversations_replies(
                channel=channel,
                ts=thread_ts,
                limit=limit,
            )
            lines = []
            for msg in resp.get("messages", []):
                text = msg.get("text", "").strip()
                if not text:
                    continue
                bot_id = msg.get("bot_id", "")
                user   = msg.get("user", "")
                prefix = "[봇]" if bot_id else f"[사용자 {user[:6]}]"
                lines.append(f"{prefix}: {text[:800]}")
            return "\n".join(lines) if lines else "(메시지 없음)"
        except Exception as exc:
            logger.warning("Failed to fetch thread messages: %s", exc)
            return "(스레드 메시지를 불러올 수 없음)"

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
            proposal = await retro.analyze(
                Path("memory/journal"), trigger="complaint", context=message
            )
            if proposal:
                text = (
                    f"개선 제안 #{proposal.proposal_id}\n\n"
                    f"*문제*: {proposal.problem}\n"
                    f"*제안*: {proposal.suggested_change}\n\n"
                    "수락/거부 버튼을 통해 응답해 주세요."
                )
                await self.send_message(channel, text, thread_ts=thread_ts)
        except Exception as exc:
            logger.warning("Complaint handling error: %s", exc)

    # ------------------------------------------------------------------
    # Persistence helpers
    # ------------------------------------------------------------------

    async def _persist(self, thread_ts: str) -> None:
        """Persist the current in-memory state for thread_ts to SQLite."""
        state = self._pending_clarification.get(thread_ts)
        if state is None:
            return
        try:
            from agentforge.core.context_store import get_context_store
            await get_context_store().save(thread_ts, state)
        except Exception as exc:
            logger.warning("Failed to persist thread context %s: %s", thread_ts[:12], exc)

    async def _delete_thread(self, thread_ts: str) -> None:
        """Remove thread state from in-memory cache and SQLite."""
        self._pending_clarification.pop(thread_ts, None)
        try:
            from agentforge.core.context_store import get_context_store
            await get_context_store().delete(thread_ts)
        except Exception as exc:
            logger.warning("Failed to delete thread context %s: %s", thread_ts[:12], exc)

    async def _notify(self, channel: str, thread_ts: str, text: str) -> None:
        """Post a message to a Slack thread (used during startup restore)."""
        try:
            await self._app.client.chat_postMessage(
                channel=channel, thread_ts=thread_ts, text=text
            )
        except Exception as exc:
            logger.warning("Failed to notify thread %s: %s", thread_ts[:12], exc)


# ------------------------------------------------------------------
# Graph factory helper
# ------------------------------------------------------------------

def _build_graph() -> Any:
    """Build a fresh compiled graph (uses existing checkpoint for resumption)."""
    from agentforge.core.models import WorkflowSpec
    from workflows.builder import GraphBuilder
    return GraphBuilder().from_spec(WorkflowSpec(name="pipeline", tasks=[]))


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
