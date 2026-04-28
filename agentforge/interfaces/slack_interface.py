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

        self._si_channel: str = os.getenv("AF_SI_CHANNEL", "")

        # In-memory cache: thread_ts → state dict (backed by ThreadContextStore)
        self._pending_clarification: dict[str, dict] = {}
        # session_id → pending L4 info (also persisted via thread_contexts stage=l4_waiting)
        self._pending_l4: dict[str, dict] = {}
        # session_id → Slack message ts for the live status message (updated in-place)
        self._status_ts: dict[str, str] = {}

        # ScribeAgent / ResearcherAgent — initialized lazily after Slack app is ready
        self._scribe: Any = None
        self._researcher: Any = None

        # task id → {channel, thread_ts, user_id} for error reporting in _on_task_done
        self._task_ctx: dict[int, dict] = {}

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
        from agentforge.core.global_context import init_global_context_store

        await init_checkpointer()
        await init_context_store()
        await init_global_context_store()

        self._app = AsyncApp(
            token=self._bot_token,
            signing_secret=self._signing_secret,
        )
        self._register_handlers()
        self._handler = AsyncSocketModeHandler(self._app, self._app_token)

        # ScribeAgent / ResearcherAgent 초기화 (Slack 앱 준비 후)
        from agentforge.observer.scribe import ScribeAgent
        from agentforge.observer.researcher import ResearcherAgent
        self._scribe = ScribeAgent(self._app.client, self._si_channel)
        self._researcher = ResearcherAgent(self._app.client, self._si_channel)
        if not self._si_channel:
            logger.warning("AF_SI_CHANNEL 미설정 — SI채널 기능 비활성화")

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

            elif stage in ("l4_waiting", "l2_waiting", "plan_waiting", "plan_modify_waiting") and session_id:
                task_id = state.get("task_id", "?")
                self._pending_l4[session_id] = {
                    "channel": channel,
                    "thread_ts": thread_ts,
                    "graph": _build_graph(),
                    "config": {"configurable": {"thread_id": session_id}},
                    "task_id": task_id,
                }
                logger.info("Restored interrupt state stage=%s session=%s", stage, session_id[:8])
                if stage == "l4_waiting":
                    note = "L4 에스컬레이션 버튼을 누르거나 메시지로 계속/중단을 알려주세요."
                elif stage == "l2_waiting":
                    note = "L2 모델 업그레이드 버튼을 누르거나 메시지로 의사를 알려주세요."
                elif stage == "plan_waiting":
                    note = "작업 계획서 승인 버튼을 누르거나 메시지로 의사를 알려주세요."
                else:
                    note = "수정 내용을 이 스레드에 답장해 주세요."
                await self._notify(channel, thread_ts,
                                   f"봇이 재시작됐습니다. 세션 `{session_id[:8]}`이 대기 중입니다. {note}")

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

    async def send_l4_prompt(
        self, channel: str, thread_ts: str, task_id: str, summary: str,
        session_id: str = "",
    ) -> Any:
        blocks = _build_l4_blocks(session_id, task_id, summary)
        if _MOCK:
            logger.info("[mock] send_l4_prompt task_id=%s session_id=%s", task_id, session_id[:8])
            return {"ts": "0.0"}
        return await self._app.client.chat_postMessage(
            channel=channel,
            thread_ts=thread_ts,
            text=f"경고: 개입 필요: {task_id}",
            blocks=blocks,
        )

    def _on_task_done(self, task: "asyncio.Task") -> None:
        ctx = self._task_ctx.pop(id(task), {})
        if task.cancelled():
            return
        exc = task.exception()
        if exc:
            import traceback as _tb
            tb_text = "".join(_tb.format_exception(type(exc), exc, exc.__traceback__))
            logger.exception("Background task failed: %s", exc, exc_info=exc)

            channel   = ctx.get("channel", "")
            thread_ts = ctx.get("thread_ts", "")
            user_id   = ctx.get("user_id", "")

            # 사용자 스레드에 에러 알림 (thread_ts가 있을 때만)
            if channel and thread_ts and self._app:
                import asyncio as _asyncio
                _asyncio.create_task(
                    self._app.client.chat_postMessage(
                        channel=channel,
                        thread_ts=thread_ts,
                        text="처리 중 오류가 발생했습니다. 잠시 후 다시 시도해주세요.",
                    )
                )

            # SI채널에 pre-session 에러 게시
            if self._scribe and self._si_channel:
                channel_name = channel or "unknown"
                import asyncio as _asyncio
                _asyncio.create_task(
                    self._scribe.record_pre_session_error(
                        channel_name=channel_name,
                        user_id=user_id or "unknown",
                        error_msg=str(exc),
                        traceback_text=tb_text,
                    )
                )

    def _create_task(
        self,
        coro: "Any",
        *,
        channel: str = "",
        thread_ts: str = "",
        user_id: str = "",
    ) -> "asyncio.Task":
        """asyncio.create_task + _on_task_done 등록 + 컨텍스트 저장 헬퍼."""
        import asyncio as _asyncio
        task = _asyncio.create_task(coro)
        self._task_ctx[id(task)] = {"channel": channel, "thread_ts": thread_ts, "user_id": user_id}
        task.add_done_callback(self._on_task_done)
        return task

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

            # SI채널 메시지 → 연구자에게 전달
            if self._si_channel and channel == self._si_channel:
                user_id = event.get("user", "")
                if self._researcher:
                    asyncio.create_task(
                        self._researcher.on_si_message(channel, thread_ts, user_id, text)
                    )
                return

            # 연구자가 만든 개선 채널 → 해당 채널을 새 리더 세션으로 라우팅
            if self._researcher and channel in self._researcher.improvement_channels:
                user_text = text.strip()
                if user_text:
                    _uid = event.get("user", "")
                    self._create_task(
                        self._handle_improvement_channel_message(client, channel, event.get("ts", ""), user_text),
                        channel=channel,
                        thread_ts=event.get("ts", ""),
                        user_id=_uid,
                    )
                return

            # Handle reply inside an active clarification thread (no mention required)
            if thread_ts and thread_ts in self._pending_clarification:
                user_text = text.strip()
                if user_text:
                    _uid = event.get("user", "")
                    self._create_task(
                        self._handle_clarification_reply(client, channel, thread_ts, user_text),
                        channel=channel,
                        thread_ts=thread_ts,
                        user_id=_uid,
                    )
                return

            # All other messages: observed but not acted upon

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

        @app.action("l2_upgrade")
        async def handle_l2_upgrade(body: dict, ack: Any) -> None:
            await ack()
            await self._on_l2_action(body, choice="upgrade")

        @app.action("l2_stop")
        async def handle_l2_stop(body: dict, ack: Any) -> None:
            await ack()
            await self._on_l2_action(body, choice="stop")

        @app.action("plan_approve")
        async def handle_plan_approve(body: dict, ack: Any) -> None:
            await ack()
            await self._on_plan_action(body, action="approved")

        @app.action("plan_modify")
        async def handle_plan_modify(body: dict, ack: Any) -> None:
            await ack()
            await self._on_plan_action(body, action="modify_request")

        # --- SI채널 / 연구자 제안 핸들러 ---

        @app.action("proposal_approve")
        async def handle_proposal_approve(body: dict, ack: Any) -> None:
            await ack()
            proposal_id = (body.get("actions") or [{}])[0].get("value", "")
            if self._researcher and proposal_id:
                asyncio.create_task(self._researcher.execute_improvement(proposal_id))

        @app.action("proposal_reject")
        async def handle_proposal_reject(body: dict, ack: Any, client: Any) -> None:
            await ack()
            proposal_id = (body.get("actions") or [{}])[0].get("value", "")
            if self._researcher and proposal_id:
                asyncio.create_task(self._researcher.handle_merge_cancel(proposal_id))

        @app.action("proposal_auto_approve")
        async def handle_proposal_auto_approve(body: dict, ack: Any) -> None:
            await ack()
            proposal_id = (body.get("actions") or [{}])[0].get("value", "")
            if self._researcher and proposal_id:
                async def _delayed():
                    await asyncio.sleep(86400)  # 24시간
                    await self._researcher.execute_improvement(proposal_id)
                asyncio.create_task(_delayed())

        @app.action("improvement_merge")
        async def handle_improvement_merge(body: dict, ack: Any) -> None:
            await ack()
            proposal_id = (body.get("actions") or [{}])[0].get("value", "")
            if self._researcher and proposal_id:
                asyncio.create_task(self._researcher.handle_merge_approve(proposal_id))

        @app.action("improvement_cancel")
        async def handle_improvement_cancel(body: dict, ack: Any) -> None:
            await ack()
            proposal_id = (body.get("actions") or [{}])[0].get("value", "")
            if self._researcher and proposal_id:
                asyncio.create_task(self._researcher.handle_merge_cancel(proposal_id))

        @app.action("improvement_diff")
        async def handle_improvement_diff(body: dict, ack: Any) -> None:
            await ack()
            proposal_id = (body.get("actions") or [{}])[0].get("value", "")
            if self._researcher and proposal_id:
                asyncio.create_task(self._researcher.handle_diff_view(proposal_id))

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
            logger.info(
                "[mention] thread=%s stage=%s session=%s → routing to %s handler",
                thread_ts[:12], stage,
                str(existing.get("session_id", ""))[:8] or "none",
                "running" if stage == "running" else
                "completed" if stage == "completed" else
                "clarifying" if stage in ("clarifying", "confirming") else
                stage,
            )
            if stage == "running":
                if request:
                    self._create_task(
                        self._dispatch_running_thread(
                            self._app.client, existing, request, channel, thread_ts
                        ),
                        channel=channel, thread_ts=thread_ts, user_id=user_id,
                    )
                return
            if stage == "l2_waiting":
                if request:
                    self._create_task(
                        self._dispatch_interrupt_thread(
                            self._app.client, existing, request, channel, thread_ts,
                            interrupt_type="l2",
                        ),
                        channel=channel, thread_ts=thread_ts, user_id=user_id,
                    )
                else:
                    await say(
                        text="버튼으로 업그레이드 승인/중단을 선택하거나, 메시지로 의사를 알려주세요.",
                        thread_ts=thread_ts,
                    )
                return
            if stage == "l4_waiting":
                if request:
                    self._create_task(
                        self._dispatch_interrupt_thread(
                            self._app.client, existing, request, channel, thread_ts,
                            interrupt_type="l4",
                        ),
                        channel=channel, thread_ts=thread_ts, user_id=user_id,
                    )
                else:
                    await say(
                        text="L4 에스컬레이션 버튼을 사용하거나 메시지로 계속/중단 의사를 알려주세요.",
                        thread_ts=thread_ts,
                    )
                return
            if stage == "plan_waiting":
                if request:
                    self._create_task(
                        self._dispatch_interrupt_thread(
                            self._app.client, existing, request, channel, thread_ts,
                            interrupt_type="plan",
                        ),
                        channel=channel, thread_ts=thread_ts, user_id=user_id,
                    )
                else:
                    await say(
                        text="계획서 버튼으로 승인/수정 요청을 하거나, 메시지로 의사를 알려주세요.",
                        thread_ts=thread_ts,
                    )
                return
            if stage == "plan_modify_waiting":
                session_id_val = existing.get("session_id", "")
                pending = self._pending_l4.get(session_id_val)
                if request.strip() and pending:
                    existing["stage"] = "running"
                    self._pending_clarification[thread_ts] = existing
                    await self._persist(thread_ts)
                    asyncio.create_task(
                        self._resume_graph(
                            pending["graph"], pending["config"],
                            {"action": "modify", "feedback": request.strip()},
                            channel, thread_ts, session_id_val,
                        )
                    )
                else:
                    await say(
                        text="수정 내용을 이 스레드에 답장해 주세요.",
                        thread_ts=thread_ts,
                    )
                return
            if stage == "completed":
                if request:
                    self._create_task(
                        self._dispatch_completed_thread(
                            self._app.client, existing, request, channel, thread_ts, user_id
                        ),
                        channel=channel, thread_ts=thread_ts, user_id=user_id,
                    )
                return
            if stage in ("clarifying", "confirming"):
                # @mention in a clarifying thread → LeaderAgent decides intent.
                # Plain replies (no mention) go through _handle_clarification_reply directly.
                if request:
                    self._create_task(
                        self._dispatch_clarifying_thread(
                            self._app.client, existing, request, channel, thread_ts, user_id
                        ),
                        channel=channel, thread_ts=thread_ts, user_id=user_id,
                    )
                return

        if not request:
            await say(text="요청 내용을 입력해 주세요.", thread_ts=thread_ts)
            return

        logger.info("[mention] new thread — starting clarification for user=%s", user_id)
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

        self._create_task(
            self._run_clarification_turn(self._app.client, channel, thread_ts),
            channel=channel, thread_ts=thread_ts, user_id=user_id,
        )

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

        if stage == "plan_modify_waiting":
            session_id_val = state.get("session_id", "")
            pending = self._pending_l4.get(session_id_val)
            if user_text.strip() and pending:
                state["stage"] = "running"
                self._pending_clarification[thread_ts] = state
                await self._persist(thread_ts)
                asyncio.create_task(
                    self._resume_graph(
                        pending["graph"], pending["config"],
                        {"action": "modify", "feedback": user_text.strip()},
                        channel, thread_ts, session_id_val,
                    )
                )
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
            exc_str = str(exc)
            if "529" in exc_str or "overloaded" in exc_str.lower():
                msg = (
                    "Anthropic API가 일시적으로 과부하 상태입니다. "
                    "잠시 후 메시지를 다시 보내주시면 재시도합니다."
                )
            elif "429" in exc_str or "rate_limit" in exc_str.lower():
                msg = (
                    "API 요청 한도에 도달했습니다. "
                    "잠시 후 메시지를 다시 보내주시면 재시도합니다."
                )
            else:
                msg = (
                    f"요구사항 분석 중 오류가 발생했습니다. "
                    f"메시지를 다시 보내주시면 재시도합니다. (`{exc_str[:120]}`)"
                )
            await client.chat_postMessage(
                channel=channel, thread_ts=thread_ts, text=msg,
            )
            # Keep thread in clarifying stage so user can retry without re-mentioning
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

        logger.info(
            "[execute_graph] thread=%s session=%s summary=%.120s",
            thread_ts[:12], session_id[:8], summary,
        )

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

        self._create_task(
            self.stream_graph_to_slack(graph, state_obj, channel, thread_ts, session_id),
            channel=channel, thread_ts=thread_ts,
        )

    # ------------------------------------------------------------------
    # Graph streaming
    # ------------------------------------------------------------------

    async def stream_graph_to_slack(
        self,
        graph: Any,
        initial_state,  # Optional[dict | Command] — None resumes from checkpoint
        channel: str,
        thread_ts: str,
        session_id: str,
    ) -> None:
        """
        Stream graph events to Slack thread.
        - Pass initial_state=None to resume from existing checkpoint.
        - Pass Command(resume=value) to resume from an interrupt node.
        """
        config = {"configurable": {"thread_id": session_id}}

        async def _post_or_update(text: str) -> None:
            ts = self._status_ts.get(session_id)
            if ts is None:
                resp = await self.send_message(channel, text, thread_ts=thread_ts)
                if resp:
                    self._status_ts[session_id] = resp.get("ts", "")
            else:
                await self.update_message(channel, ts, text)

        from agentforge.observer.historian import Historian
        historian = Historian()

        # ScribeAgent: 세션 스레드 시작
        if self._scribe:
            asyncio.create_task(
                self._scribe.start_session_thread(
                    session_id=session_id,
                    workflow_name="AF 세션",
                    task_count=0,
                )
            )

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

                elif event_name == "on_chain_end" and node_name == "dispatch_workers":
                    output = event.get("data", {}).get("output", {}) or {}
                    dag_index  = output.get("dag_index", {})
                    task_nodes = output.get("task_nodes", {})

                    total     = len(dag_index)
                    n_done    = sum(1 for s in dag_index.values() if "completed" in str(s).lower())
                    n_failed  = sum(1 for s in dag_index.values() if str(s).lower() in ("taskstatus.failed", "failed", "taskstatus.blocked", "blocked"))

                    for tid, status in dag_index.items():
                        status_s = str(status).lower()
                        if not any(x in status_s for x in ("completed", "failed", "blocked")):
                            continue  # pending/running: 아직 보고 대상 아님
                        node_obj = task_nodes.get(tid)
                        report = (node_obj.get("report") if isinstance(node_obj, dict)
                                  else getattr(node_obj, "report", None)) if node_obj else None
                        if not report:
                            continue

                        rsum = (report.get("summary", "") if isinstance(report, dict) else getattr(report, "summary", ""))
                        rdur = (report.get("duration_seconds", 0) if isinstance(report, dict) else getattr(report, "duration_seconds", 0))
                        rtok = (report.get("tokens_used", 0) if isinstance(report, dict) else getattr(report, "tokens_used", 0))
                        deliverables = (report.get("deliverables", []) if isinstance(report, dict) else getattr(report, "deliverables", []))

                        is_completed = "completed" in status_s
                        icon = "✅" if is_completed else "❌"

                        # 이 태스크 완료로 unblock 될 다음 태스크
                        newly_ready = []
                        if is_completed:
                            for dep_tid, dep_node in task_nodes.items():
                                deps = (dep_node.get("instruction", {}).get("depends_on", []) if isinstance(dep_node, dict)
                                        else getattr(dep_node.instruction, "depends_on", []))
                                dep_status = str(dag_index.get(dep_tid, "")).lower()
                                if tid in deps and "pending" in dep_status:
                                    newly_ready.append(dep_tid)

                        lines = [
                            f"{icon} **{tid}** ({n_done}/{total} 완료) — {rdur:.0f}초, {rtok:,}토큰",
                            f"*요약*: {str(rsum)[:200]}",
                        ]
                        if deliverables:
                            lines.append("*생성 파일*:\n" + "\n".join(f"  • `{f}`" for f in deliverables[:10]))
                        if newly_ready:
                            lines.append(f"*다음 실행 예정*: {', '.join(f'`{t}`' for t in newly_ready)}")
                        elif n_done + n_failed >= total:
                            lines.append("*모든 작업 처리 완료 — 검증 단계로 이동합니다*")

                        await self.send_message(channel, "\n".join(lines), thread_ts=thread_ts)

                        asyncio.create_task(historian.record_event(
                            session_id=session_id,
                            node="dispatch_workers",
                            task_id=tid,
                            result="success" if is_completed else "failed",
                            elapsed_s=float(rdur) if rdur else 0.0,
                            tokens=int(rtok) if rtok else 0,
                        ))
                        # ScribeAgent: 태스크 완료 기록
                        if self._scribe:
                            node_obj2 = task_nodes.get(tid)
                            inst = (node_obj2.get("instruction") if isinstance(node_obj2, dict)
                                    else getattr(node_obj2, "instruction", None)) if node_obj2 else None
                            title = getattr(inst, "title", tid) if inst else tid
                            criteria = getattr(inst, "acceptance_criteria", []) if inst else []
                            asyncio.create_task(self._scribe.record_task_event(
                                session_id=session_id,
                                task_id=tid,
                                task_title=title,
                                task_index=n_done,
                                total_tasks=total,
                                event_type="merge" if is_completed else "error",
                                payload={
                                    "elapsed_s": float(rdur) if rdur else 0.0,
                                    "tokens": int(rtok) if rtok else 0,
                                    "acceptance_criteria": criteria,
                                },
                            ))

                elif event_name == "on_chain_end" and node_name == "escalate":
                    output = event.get("data", {}).get("output", {}) or {}
                    level   = output.get("current_escalation_level", 0)
                    history = output.get("escalation_history", [])
                    last    = history[-1] if history else {}
                    task_id = last.get("task_id", "?")
                    if level == 1:
                        msg = f"🔄 재시도 1회차: `{task_id}` — 동일 모델로 재시도합니다..."
                    elif level == 2:
                        msg = f"🔄 재시도 2회차: `{task_id}` — 마지막 자동 재시도입니다. 실패 시 승인 요청합니다..."
                    elif level == 3:
                        msg = f"⬆️ 모델 업그레이드: `{task_id}` — 상위 에이전트로 재시도합니다..."
                    elif level >= 4:
                        msg = f"🚨 최대 재시도 초과: `{task_id}` — 수동 개입이 필요합니다"
                    else:
                        msg = f"⚠️ 에스컬레이션 L{level}: `{task_id}`"
                    await self.send_message(channel, msg, thread_ts=thread_ts)
                    asyncio.create_task(historian.record_event(
                        session_id=session_id,
                        node=f"escalate_L{level}",
                        task_id=task_id,
                        result="escalate",
                        elapsed_s=0.0,
                        tokens=0,
                    ))

                elif event_name == "on_chain_end" and node_name == "verify_ci":
                    output = event.get("data", {}).get("output", {}) or {}
                    ci_passed = output.get("ci_passed", True)
                    task_id   = output.get("current_task_id", "")
                    if task_id:
                        icon = "✅ CI 통과" if ci_passed else "❌ CI 실패"
                        logger.info("[%s] %s: %s", session_id[:8], icon, task_id)

                elif event_name == "on_chain_end" and node_name == "verify_semantic":
                    output  = event.get("data", {}).get("output", {}) or {}
                    verdict = (output.get("semantic_result") or {}).get("verdict", "")
                    if verdict:
                        icon = "✅ 검증 통과" if verdict == "ACCEPT" else "❌ 검증 거부"
                        logger.info("[%s] %s", session_id[:8], icon)

                elif event_name == "on_chain_end" and node_name == "finalize":
                    output = event.get("data", {}).get("output", {})
                    final_report = output.get("final_report", "완료")
                    # Update the live status message to show completion, then post the full
                    # report as a separate new message so it's easy to find in the thread.
                    await _post_or_update("✅ 모든 작업 완료 — 최종 보고서를 아래에서 확인하세요.")
                    self._status_ts.pop(session_id, None)  # next call posts fresh message
                    await self.send_message(channel, final_report, thread_ts=thread_ts)
                    asyncio.create_task(historian.record_event(
                        session_id=session_id,
                        node="finalize",
                        task_id="-",
                        result="success",
                        elapsed_s=0.0,
                        tokens=0,
                    ))
                    # ScribeAgent: 세션 종료 요약
                    if self._scribe:
                        asyncio.create_task(self._scribe.end_session(
                            session_id=session_id,
                            total_tasks=0, succeeded=0, failed=0,
                            total_tokens=0, elapsed_minutes=0.0,
                        ))
                    # ResearcherAgent: 5분 후 세션 회고 트리거
                    if self._researcher:
                        asyncio.create_task(self._researcher.on_session_end(session_id))

        except Exception as exc:
            logger.exception("Graph streaming error: %s", exc)
            error_msg = str(exc)
            await _post_or_update(f"오류 발생: {exc}")
        else:
            # Stream ended cleanly without a final_report — check if paused at interrupt
            if not final_report:
                try:
                    snap = await graph.aget_state(config)
                    next_nodes = list(snap.next) if snap and snap.next else []

                    if "interrupt_l2" in next_nodes:
                        gs = snap.values or {}
                        task_id_l2 = gs.get("current_task_id", "unknown")
                        task_nodes_snap = gs.get("task_nodes", {})
                        node_snap = task_nodes_snap.get(task_id_l2)
                        if node_snap:
                            tier = getattr(node_snap.instruction, "model_tier", None)
                            current_tier = str(tier) if tier else "haiku"
                        else:
                            current_tier = "haiku"
                        from agentforge.graph.nodes import _upgrade_tier
                        from agentforge.core.models import ModelTier
                        try:
                            tier_enum = ModelTier(current_tier)
                        except ValueError:
                            tier_enum = ModelTier.HAIKU
                        next_tier = str(_upgrade_tier(tier_enum))

                        thread_state = self._pending_clarification.get(thread_ts, {})
                        thread_state.update({"stage": "l2_waiting", "task_id": task_id_l2, "session_id": session_id})
                        self._pending_clarification[thread_ts] = thread_state
                        await self._persist(thread_ts)

                        self._pending_l4[session_id] = {
                            "channel": channel, "thread_ts": thread_ts,
                            "graph": graph, "config": config,
                            "task_id": task_id_l2, "interrupt_type": "l2",
                        }
                        await self._app.client.chat_postMessage(
                            channel=channel, thread_ts=thread_ts,
                            blocks=_build_l2_blocks(session_id, task_id_l2, current_tier, next_tier),
                        )
                        return

                    elif "interrupt_l4" in next_nodes:
                        gs = snap.values or {}
                        task_id_l4 = gs.get("current_task_id", "?")
                        final_rpt  = gs.get("final_report", "수동 개입 필요")
                        state_obj = self._pending_clarification.get(thread_ts, {})
                        state_obj.update({"stage": "l4_waiting", "task_id": task_id_l4, "session_id": session_id})
                        self._pending_clarification[thread_ts] = state_obj
                        await self._persist(thread_ts)
                        self._pending_l4[session_id] = {
                            "channel": channel, "thread_ts": thread_ts,
                            "graph": graph, "config": config, "task_id": task_id_l4,
                        }
                        await self.send_l4_prompt(channel, thread_ts, task_id_l4, str(final_rpt), session_id=session_id)
                        return

                    elif "present_plan" in next_nodes:
                        gs = snap.values or {}
                        ws_root = gs.get("workspace_root", "")
                        plan_md = ""
                        if ws_root:
                            from pathlib import Path as _Path
                            plan_path = _Path(ws_root) / "PLAN.md"
                            if plan_path.exists():
                                plan_md = plan_path.read_text(encoding="utf-8")
                        thread_state = self._pending_clarification.get(thread_ts, {})
                        thread_state.update({"stage": "plan_waiting", "session_id": session_id})
                        self._pending_clarification[thread_ts] = thread_state
                        await self._persist(thread_ts)
                        self._pending_l4[session_id] = {
                            "channel": channel, "thread_ts": thread_ts,
                            "graph": graph, "config": config, "interrupt_type": "plan",
                        }
                        await self._app.client.chat_postMessage(
                            channel=channel, thread_ts=thread_ts,
                            text="작업 계획서가 준비됐습니다. 검토 후 승인해 주세요.",
                            blocks=_build_plan_blocks(session_id, plan_md),
                        )
                        return
                except Exception as _snap_exc:
                    logger.warning("Failed to check graph interrupt state: %s", _snap_exc)
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
            # Don't overwrite interrupt-waiting stages that were set by early returns above.
            _interrupt_stages = {"l2_waiting", "l4_waiting", "plan_waiting", "plan_modify_waiting"}
            if state.get("stage") not in _interrupt_stages:
                state["stage"] = "completed"
                self._status_ts.pop(session_id, None)  # clear live-status slot on completion
            state["result"] = final_report or (f"오류: {error_msg}" if error_msg else "")
            # "summary" retains the clarified requirements — do not overwrite
            self._pending_clarification[thread_ts] = state
            await self._persist(thread_ts)

            # Update cross-session global context
            try:
                from agentforge.core.global_context import get_global_context_store
                await get_global_context_store().record_session(
                    session_id=session_id,
                    success=bool(final_report and not error_msg),
                    summary=state.get("summary", ""),
                    result=state["result"],
                )
            except Exception:
                pass

    # ------------------------------------------------------------------
    # L4 escalation
    # ------------------------------------------------------------------

    async def _on_l2_action(self, body: dict, choice: str) -> None:
        action     = body.get("actions", [{}])[0]
        session_id = action.get("value", "")
        pending    = self._pending_l4.pop(session_id, None)
        if pending is None:
            return

        channel   = pending["channel"]
        thread_ts = pending["thread_ts"]
        graph     = pending["graph"]
        config    = pending["config"]

        if choice == "upgrade":
            label = "✅ 업그레이드 승인 — 더 높은 수준의 에이전트로 재시도합니다"
        else:
            label = "❌ 작업 중단"
        await self.send_message(channel, label, thread_ts=thread_ts)

        asyncio.create_task(
            self._resume_graph(graph, config, {"choice": choice}, channel, thread_ts, session_id)
        )

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

    async def _on_plan_action(self, body: dict, action: str) -> None:
        action_data = body.get("actions", [{}])[0]
        session_id  = action_data.get("value", "")
        pending     = self._pending_l4.get(session_id)
        if pending is None:
            return

        channel   = pending["channel"]
        thread_ts = pending["thread_ts"]
        graph     = pending["graph"]
        config    = pending["config"]

        if action == "approved":
            self._pending_l4.pop(session_id, None)
            thread_state = self._pending_clarification.get(thread_ts, {})
            thread_state["stage"] = "running"
            self._pending_clarification[thread_ts] = thread_state
            await self._persist(thread_ts)
            await self.send_message(channel, "✅ 계획 승인 — 작업을 시작합니다...", thread_ts=thread_ts)
            asyncio.create_task(
                self._resume_graph(graph, config, "approved", channel, thread_ts, session_id)
            )
        else:
            # modify_request — ask user to reply with modification details
            thread_state = self._pending_clarification.get(thread_ts, {})
            thread_state.update({"stage": "plan_modify_waiting", "session_id": session_id})
            self._pending_clarification[thread_ts] = thread_state
            await self._persist(thread_ts)
            await self.send_message(
                channel,
                "✏️ 수정 내용을 이 스레드에 답장해 주세요.",
                thread_ts=thread_ts,
            )

    async def _resume_graph(
        self, graph: Any, config: dict, resume_value,
        channel: str, thread_ts: str, session_id: str,
    ) -> None:
        from langgraph.types import Command
        await self.stream_graph_to_slack(
            graph, Command(resume=resume_value), channel, thread_ts, session_id
        )

    # ------------------------------------------------------------------
    # Session resume
    # ------------------------------------------------------------------

    async def _resume_from_checkpoint(
        self, client: Any, state: dict, channel: str, thread_ts: str,
        override_session_id: str = "",
    ) -> None:
        """Resume an existing session: keep COMPLETED tasks, reset FAILED/BLOCKED to PENDING."""
        session_id = override_session_id or state.get("session_id", "")
        if not session_id:
            await client.chat_postMessage(
                channel=channel, thread_ts=thread_ts,
                text="재개할 세션을 찾을 수 없습니다. '새 작업 시작'을 요청해 주세요.",
            )
            return

        from agentforge.core.models import EscalationLevel, TaskStatus

        graph = _build_graph()
        config = {"configurable": {"thread_id": session_id}}
        snap = await graph.aget_state(config)

        if snap is None or not snap.values:
            await client.chat_postMessage(
                channel=channel, thread_ts=thread_ts,
                text=f"세션 `{session_id[:8]}`의 체크포인트를 찾을 수 없습니다. 처음부터 재시작합니다...",
            )
            state["result"] = None
            await self._execute_graph(client, state)
            return

        chk_values = snap.values
        dag_index  = dict(chk_values.get("dag_index",  {}))
        task_nodes = dict(chk_values.get("task_nodes", {}))

        if not dag_index:
            await client.chat_postMessage(
                channel=channel, thread_ts=thread_ts,
                text="DAG 진행 상태가 없습니다. 처음부터 재시작합니다...",
            )
            state["result"] = None
            await self._execute_graph(client, state)
            return

        # Reset failed/blocked tasks to PENDING
        terminal = {TaskStatus.FAILED, TaskStatus.BLOCKED}
        reset_ids: list[str] = []
        for tid, status in list(dag_index.items()):
            if status in terminal:
                dag_index[tid] = TaskStatus.PENDING
                node = task_nodes.get(tid)
                if node is not None:
                    try:
                        node = node.model_copy(update={
                            "status": TaskStatus.PENDING,
                            "escalation_level": EscalationLevel.L0,
                            "report": None,
                            "assigned_agent_id": None,
                        })
                        task_nodes[tid] = node
                    except Exception:
                        pass
                reset_ids.append(tid)

        completed_count = sum(1 for s in dag_index.values() if s == TaskStatus.COMPLETED)
        total = len(dag_index)

        await client.chat_postMessage(
            channel=channel, thread_ts=thread_ts,
            text=(
                f"세션 `{session_id[:8]}` 재개 중...\n"
                f"완료 작업 {completed_count}/{total}개 유지"
                + (f", 재시도 예정: {', '.join(f'`{t}`' for t in reset_ids)}" if reset_ids else "")
            ),
        )

        # Inject modified state continuing from after build_dag
        resume_values = {
            **{k: v for k, v in chk_values.items()
               if k not in ("dag_index", "task_nodes", "current_escalation_level",
                            "failing_task_id", "semantic_result", "ci_result", "ci_passed",
                            "current_task_id", "delegated_task_ids")},
            "dag_index": dag_index,
            "task_nodes": task_nodes,
            "current_escalation_level": 0,
            "failing_task_id": None,
            "semantic_result": None,
            "ci_passed": True,
            "current_task_id": None,
            "delegated_task_ids": [],
        }
        await graph.aupdate_state(config, resume_values, as_node="build_dag")

        state["stage"] = "running"
        state["session_id"] = session_id
        self._pending_clarification[thread_ts] = state
        await self._persist(thread_ts)

        self._create_task(
            self.stream_graph_to_slack(graph, None, channel, thread_ts, session_id),
            channel=channel, thread_ts=thread_ts,
        )

    # ------------------------------------------------------------------
    # Completed / running thread dispatchers (tool calling)
    # ------------------------------------------------------------------

    async def _dispatch_interrupt_thread(
        self,
        client: Any,
        state: dict,
        user_message: str,
        channel: str,
        thread_ts: str,
        interrupt_type: str,  # "l4", "l2", "plan"
    ) -> None:
        """Route @mention in an interrupt-waiting state through LeaderAgent with interrupt tools."""
        from agentforge.agents.leader import LeaderAgent

        session_id = state.get("session_id", "")
        task_id    = state.get("task_id", "?")
        pending    = self._pending_l4.get(session_id) if session_id else None

        logger.info(
            "[dispatch_interrupt] thread=%s stage=%s type=%s session=%s user_text=%.80s",
            thread_ts[:12], state.get("stage"), interrupt_type,
            session_id[:8] if session_id else "none", user_message,
        )

        # Build context string describing the current interrupt situation
        if interrupt_type == "l4":
            interrupt_ctx = (
                f"현재 태스크 `{task_id}`가 최대 재시도를 초과하여 L4 에스컬레이션 대기 중입니다.\n"
                "사용 가능한 액션:\n"
                "- continue_task: 태스크를 계속 진행 (조건 추가 가능)\n"
                "- abort_task: 태스크 중단\n"
                "- answer_question / 조회 도구: 상황 파악 후 응답"
            )
        elif interrupt_type == "l2":
            interrupt_ctx = (
                f"현재 태스크 `{task_id}`가 2회 재시도 후 실패하여 L2 모델 업그레이드 대기 중입니다.\n"
                "사용 가능한 액션:\n"
                "- upgrade_model: 더 높은 모델로 업그레이드하여 재시도\n"
                "- stop_task: 태스크 중단\n"
                "- answer_question / 조회 도구: 상황 파악 후 응답"
            )
        else:  # plan
            interrupt_ctx = (
                "현재 작업 계획서 승인 대기 중입니다.\n"
                "사용 가능한 액션:\n"
                "- approve_plan: 계획 승인 후 작업 시작\n"
                "- request_plan_modification: 수정 요청\n"
                "- answer_question / 조회 도구: 계획 내용 설명"
            )

        thread_text = await self._fetch_thread_messages(client, channel, thread_ts)
        global_ctx  = await self._load_global_context_str()
        enriched_state = {
            **state,
            "thread_messages": thread_text,
            "_global_context": global_ctx,
        }

        async def post(text: str) -> None:
            await client.chat_postMessage(channel=channel, thread_ts=thread_ts, text=text)

        # L4 callbacks
        async def on_l4_continue(conditions: str) -> None:
            if not pending:
                await post("세션 정보를 찾을 수 없습니다. 버튼을 눌러주세요.")
                return
            label = f"계속 진행합니다" + (f" (조건: {conditions})" if conditions else "")
            await post(label)
            self._pending_l4.pop(session_id, None)
            state["stage"] = "running"
            self._pending_clarification[thread_ts] = state
            await self._persist(thread_ts)
            asyncio.create_task(self._resume_graph(
                pending["graph"], pending["config"],
                "continue", channel, thread_ts, session_id,
            ))

        async def on_l4_abort() -> None:
            if not pending:
                await post("세션 정보를 찾을 수 없습니다.")
                return
            await post("태스크를 중단합니다.")
            self._pending_l4.pop(session_id, None)
            state["stage"] = "running"
            self._pending_clarification[thread_ts] = state
            await self._persist(thread_ts)
            asyncio.create_task(self._resume_graph(
                pending["graph"], pending["config"],
                "abort", channel, thread_ts, session_id,
            ))

        # L2 callbacks
        async def on_l2_upgrade() -> None:
            if not pending:
                await post("세션 정보를 찾을 수 없습니다.")
                return
            await post("✅ 업그레이드 승인 — 더 높은 수준의 에이전트로 재시도합니다")
            self._pending_l4.pop(session_id, None)
            state["stage"] = "running"
            self._pending_clarification[thread_ts] = state
            await self._persist(thread_ts)
            asyncio.create_task(self._resume_graph(
                pending["graph"], pending["config"],
                {"choice": "upgrade"}, channel, thread_ts, session_id,
            ))

        async def on_l2_stop() -> None:
            if not pending:
                await post("세션 정보를 찾을 수 없습니다.")
                return
            await post("❌ 작업 중단")
            self._pending_l4.pop(session_id, None)
            state["stage"] = "running"
            self._pending_clarification[thread_ts] = state
            await self._persist(thread_ts)
            asyncio.create_task(self._resume_graph(
                pending["graph"], pending["config"],
                {"choice": "stop"}, channel, thread_ts, session_id,
            ))

        # Plan callbacks
        async def on_plan_approve() -> None:
            if not pending:
                await post("세션 정보를 찾을 수 없습니다.")
                return
            await post("✅ 계획 승인 — 작업을 시작합니다...")
            self._pending_l4.pop(session_id, None)
            state["stage"] = "running"
            self._pending_clarification[thread_ts] = state
            await self._persist(thread_ts)
            asyncio.create_task(self._resume_graph(
                pending["graph"], pending["config"],
                "approved", channel, thread_ts, session_id,
            ))

        async def on_plan_modify(feedback: str) -> None:
            if not pending:
                await post("세션 정보를 찾을 수 없습니다.")
                return
            await post(f"✏️ 수정 요청 접수: {feedback[:100]}")
            self._pending_l4.pop(session_id, None)
            state["stage"] = "running"
            self._pending_clarification[thread_ts] = state
            await self._persist(thread_ts)
            asyncio.create_task(self._resume_graph(
                pending["graph"], pending["config"],
                {"action": "modify", "feedback": feedback},
                channel, thread_ts, session_id,
            ))

        agent = LeaderAgent()
        await agent.dispatch_user_message(
            user_message=user_message,
            thread_state=enriched_state,
            allow_actions=True,
            post_fn=post,
            on_l4_continue=on_l4_continue if interrupt_type == "l4" else None,
            on_l4_abort=on_l4_abort if interrupt_type == "l4" else None,
            on_l2_upgrade=on_l2_upgrade if interrupt_type == "l2" else None,
            on_l2_stop=on_l2_stop if interrupt_type == "l2" else None,
            on_plan_approve=on_plan_approve if interrupt_type == "plan" else None,
            on_plan_modify=on_plan_modify if interrupt_type == "plan" else None,
            interrupt_context=interrupt_ctx,
        )

    async def _dispatch_completed_thread(
        self,
        client: Any,
        state: dict,
        user_message: str,
        channel: str,
        thread_ts: str,
        user_id: str,
    ) -> None:
        """Delegate to LeaderAgent tool calling (allow_actions=True)."""
        from agentforge.agents.leader import LeaderAgent

        logger.info(
            "[dispatch_completed] thread=%s session=%s user_text=%.80s",
            thread_ts[:12],
            str(state.get("session_id", ""))[:8] or "none",
            user_message,
        )
        # Inject Slack thread history and global context so the LLM has full context
        thread_text  = await self._fetch_thread_messages(client, channel, thread_ts)
        global_ctx   = await self._load_global_context_str()
        enriched_state = {**state, "thread_messages": thread_text, "_global_context": global_ctx}

        async def post(text: str) -> None:
            await client.chat_postMessage(channel=channel, thread_ts=thread_ts, text=text)

        async def on_retry(requirements: str = "") -> None:
            """Restart with confirmed requirements — fall back to clarification if none confirmed."""
            confirmed_summary = requirements.strip() or state.get("summary", "")
            if not confirmed_summary:
                await client.chat_postMessage(
                    channel=channel, thread_ts=thread_ts,
                    text="아직 요구사항이 확정되지 않았습니다. 명확화를 다시 진행합니다...",
                )
                state["stage"] = "clarifying"
                await self._persist(thread_ts)
                await self._run_clarification_turn(client, channel, thread_ts)
                return
            state["summary"] = confirmed_summary
            await client.chat_postMessage(
                channel=channel, thread_ts=thread_ts,
                text="이전 요구사항으로 작업을 재시도합니다...",
            )
            state["result"] = None
            await self._execute_graph(client, state)

        async def on_start(request: str) -> None:
            original_request = state.get("summary") or state.get("request", "")
            new_request = request.strip() or original_request
            await self._delete_thread(thread_ts)
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

        async def on_resume(session_id: str = "") -> None:
            await self._resume_from_checkpoint(
                client, state, channel, thread_ts,
                override_session_id=session_id,
            )

        agent = LeaderAgent()
        await agent.dispatch_user_message(
            user_message=user_message,
            thread_state=enriched_state,
            allow_actions=True,
            post_fn=post,
            on_start_new_task=on_start,
            on_retry_session=on_retry,
            on_resume_session=on_resume,
            on_delete_thread=lambda: self._delete_thread(thread_ts),
        )

    async def _dispatch_running_thread(
        self,
        client: Any,
        state: dict,
        user_message: str,
        channel: str,
        thread_ts: str,
    ) -> None:
        """Delegate to LeaderAgent tool calling.

        Read-only by default, but if the user explicitly requests a resume/restart
        (e.g. after an auto-resume failure), allow action tools so they can trigger it.
        """
        from agentforge.agents.leader import LeaderAgent

        session_id = state.get("session_id", "")
        logger.info(
            "[dispatch_running] thread=%s session=%s user_text=%.80s",
            thread_ts[:12], session_id[:8] or "none", user_message,
        )
        global_ctx = await self._load_global_context_str()
        thread_text = await self._fetch_thread_messages(client, channel, thread_ts)
        enriched_state = {**state, "_global_context": global_ctx, "thread_messages": thread_text}

        async def post(text: str) -> None:
            await client.chat_postMessage(channel=channel, thread_ts=thread_ts, text=text)

        async def on_resume(sid: str = "") -> None:
            await self._resume_from_checkpoint(client, state, channel, thread_ts,
                                               override_session_id=sid)

        async def on_retry(requirements: str = "") -> None:
            confirmed = requirements.strip() or state.get("summary", "")
            if not confirmed:
                state["stage"] = "clarifying"
                await self._persist(thread_ts)
                await self._run_clarification_turn(client, channel, thread_ts)
                return
            state["summary"] = confirmed
            state["result"] = None
            await self._execute_graph(client, state)

        # Allow actions only when the user is explicitly asking to resume/restart.
        # This handles the case where auto-resume failed and the session is stuck "running".
        _resume_keywords = {"재개", "이어서", "계속", "재시작", "다시", "resume", "retry"}
        allow_actions = any(kw in user_message for kw in _resume_keywords)

        agent = LeaderAgent()
        await agent.dispatch_user_message(
            user_message=user_message,
            thread_state=enriched_state,
            allow_actions=allow_actions,
            post_fn=post,
            on_resume_session=on_resume if allow_actions else None,
            on_retry_session=on_retry if allow_actions else None,
            on_start_new_task=None,
            on_delete_thread=None,
        )

    async def _dispatch_clarifying_thread(
        self,
        client: Any,
        state: dict,
        user_message: str,
        channel: str,
        thread_ts: str,
        user_id: str,
    ) -> None:
        """
        Handle @mention in a clarifying/confirming thread via LeaderAgent tool calling.

        The LLM sees the full Slack thread history and chooses:
          - retry_current_session  : skip clarification, use confirmed requirements
          - start_new_task         : clear state, restart with new requirements
          - continue_clarification : pass message to ClarifierAgent and proceed normally
          - answer_question        : answer a question without changing state
          - QUERY_TOOLS            : look up session progress, logs, etc.
        """
        from agentforge.agents.leader import LeaderAgent

        logger.info(
            "[dispatch_clarifying] thread=%s stage=%s session=%s user_text=%.80s",
            thread_ts[:12],
            state.get("stage", "?"),
            str(state.get("session_id", ""))[:8] or "none",
            user_message,
        )
        thread_text = await self._fetch_thread_messages(client, channel, thread_ts)
        global_ctx  = await self._load_global_context_str()
        enriched_state = {**state, "thread_messages": thread_text, "_global_context": global_ctx}

        async def post(text: str) -> None:
            await client.chat_postMessage(channel=channel, thread_ts=thread_ts, text=text)

        async def on_retry(requirements: str = "") -> None:
            confirmed_summary = requirements.strip() or state.get("summary", "")
            if not confirmed_summary:
                await client.chat_postMessage(
                    channel=channel, thread_ts=thread_ts,
                    text="아직 요구사항이 확정되지 않았습니다. 명확화를 다시 진행합니다...",
                )
                await self._run_clarification_turn(client, channel, thread_ts)
                return
            state["summary"] = confirmed_summary
            await client.chat_postMessage(
                channel=channel, thread_ts=thread_ts,
                text="요구사항을 확인했습니다. 명확화 단계를 건너뛰고 바로 작업을 시작합니다...",
            )
            state["result"] = None
            await self._execute_graph(client, state)

        async def on_start(request: str) -> None:
            new_request = request.strip() or state.get("request", "")
            await self._delete_thread(thread_ts)
            fresh_state: dict = {
                "channel": channel, "thread_ts": thread_ts, "user_id": user_id,
                "request": new_request,
                "history": [{"role": "user", "content": new_request}],
                "stage": "clarifying", "summary": None, "session_id": None, "task_id": None,
            }
            self._pending_clarification[thread_ts] = fresh_state
            await self._persist(thread_ts)
            await self._run_clarification_turn(client, channel, thread_ts)

        async def on_continue(message: str) -> None:
            """Route back to ClarifierAgent with the clarification message."""
            await self._handle_clarification_reply(client, channel, thread_ts, message)

        async def on_resume(session_id: str = "") -> None:
            await self._resume_from_checkpoint(
                client, state, channel, thread_ts,
                override_session_id=session_id,
            )

        agent = LeaderAgent()
        await agent.dispatch_user_message(
            user_message=user_message,
            thread_state=enriched_state,
            allow_actions=True,
            post_fn=post,
            on_start_new_task=on_start,
            on_retry_session=on_retry,
            on_resume_session=on_resume,
            on_continue_clarification=on_continue,
            on_delete_thread=lambda: self._delete_thread(thread_ts),
        )

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

    async def _handle_improvement_channel_message(
        self, client: Any, channel: str, ts: str, text: str
    ) -> None:
        """연구자가 만든 개선 채널 메시지를 새 리더 세션으로 처리한다."""
        try:
            from agentforge.core.state import make_initial_state
            from agentforge.core.checkpoint import get_checkpointer
            from workflows.builder import GraphBuilder

            session_id = f"improve-{ts[:8]}"
            state = make_initial_state(session_id=session_id, user_request=text)
            graph = GraphBuilder().from_spec(
                __import__("agentforge.core.models", fromlist=["WorkflowSpec"]).WorkflowSpec(
                    name=f"improvement_{session_id}"
                )
            )
            asyncio.create_task(
                self.stream_graph_to_slack(graph, state, channel, ts, session_id)
            )
        except Exception as exc:
            logger.warning("[improvement channel] handler error: %s", exc)

    async def _handle_complaint(
        self, user_id: str, message: str, channel: str, thread_ts: str
    ) -> None:
        try:
            from agentforge.observer.historian import Historian
            historian = Historian()
            await historian.record_complaint(user_id, message)

            # SI채널에 QA 이슈로도 게시 (연구자가 분석)
            if self._si_channel and self._researcher:
                await self._researcher.on_si_message(
                    self._si_channel, None, user_id,
                    f"[리더 QA] {message}"
                )
        except Exception as exc:
            logger.warning("Complaint handling error: %s", exc)

    # ------------------------------------------------------------------
    # Persistence helpers
    # ------------------------------------------------------------------

    async def _load_global_context_str(self) -> str:
        try:
            from agentforge.core.global_context import get_global_context_store
            return await get_global_context_store().get_formatted()
        except Exception:
            return ""

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


def _build_l2_blocks(session_id: str, task_id: str, current_tier: str, next_tier: str) -> list[dict]:
    return [
        {
            "type": "section",
            "text": {"type": "mrkdwn", "text": (
                f"⚠️ *{task_id}* 작업이 2회 재시도 후에도 실패했습니다.\n"
                f"현재 모델: `{current_tier}` → 업그레이드: `{next_tier}`\n\n"
                "더 높은 수준의 에이전트를 사용할까요? (추가 비용 발생)"
            )},
        },
        {
            "type": "actions",
            "elements": [
                {"type": "button", "text": {"type": "plain_text", "text": "✅ 업그레이드 승인"},
                 "style": "primary", "action_id": "l2_upgrade", "value": session_id},
                {"type": "button", "text": {"type": "plain_text", "text": "❌ 작업 중단"},
                 "style": "danger", "action_id": "l2_stop", "value": session_id},
            ],
        },
    ]


def _build_plan_blocks(session_id: str, plan_md: str) -> list[dict]:
    plan_text = plan_md[:2900] if plan_md else "(계획서를 불러올 수 없습니다)"
    return [
        {
            "type": "section",
            "text": {"type": "mrkdwn", "text": plan_text},
        },
        {
            "type": "actions",
            "elements": [
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "✅ 계획 승인"},
                    "style": "primary",
                    "action_id": "plan_approve",
                    "value": session_id,
                },
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "✏️ 수정 요청"},
                    "action_id": "plan_modify",
                    "value": session_id,
                },
            ],
        },
    ]


def _build_l4_blocks(session_id: str, task_id: str, summary: str) -> list[dict]:
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
                    "value": session_id,
                },
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "중단"},
                    "style": "danger",
                    "action_id": "l4_abort",
                    "value": session_id,
                },
            ],
        },
    ]
