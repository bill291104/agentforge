from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any, Callable, Coroutine, Optional

import anthropic

from agentforge.core.models import MODEL_IDS, ModelTier, TaskStatus

logger = logging.getLogger(__name__)

SONNET = MODEL_IDS[ModelTier.SONNET]

# ---------------------------------------------------------------------------
# Tool schemas
# ---------------------------------------------------------------------------

QUERY_TOOLS: list[dict] = [
    {
        "name": "get_session_progress",
        "description": (
            "현재 세션(또는 지정 세션)의 DAG 진행 상태를 조회합니다. "
            "태스크별 상태(pending/running/completed/failed/blocked), 시도 횟수, 요약을 반환합니다."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "session_id": {
                    "type": "string",
                    "description": "조회할 세션 ID. 생략 시 현재 스레드의 세션을 사용합니다.",
                }
            },
        },
    },
    {
        "name": "read_execution_log",
        "description": (
            "세션 실행 일지 파일을 읽어 반환합니다. "
            "노드 실행 순서, 에스컬레이션 기록, 소요 시간 등을 확인할 수 있습니다."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "session_id": {
                    "type": "string",
                    "description": "조회할 세션 ID. 생략 시 현재 스레드의 세션을 사용합니다.",
                },
                "tail_lines": {
                    "type": "integer",
                    "description": "반환할 최근 줄 수. 기본값 100.",
                },
            },
        },
    },
    {
        "name": "list_workspace_files",
        "description": "워크스페이스의 파일 목록을 반환합니다. 생성된 소스 파일, 테스트 파일 등을 확인할 수 있습니다.",
        "input_schema": {
            "type": "object",
            "properties": {
                "session_id": {
                    "type": "string",
                    "description": "조회할 세션 ID. 생략 시 현재 스레드의 세션을 사용합니다.",
                }
            },
        },
    },
    {
        "name": "read_workspace_file",
        "description": "워크스페이스 내 특정 파일의 내용을 반환합니다.",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "워크스페이스 루트 기준 상대 경로 (예: src/app.py)",
                },
                "session_id": {
                    "type": "string",
                    "description": "조회할 세션 ID. 생략 시 현재 스레드의 세션을 사용합니다.",
                },
            },
            "required": ["path"],
        },
    },
    {
        "name": "get_git_history",
        "description": "워크스페이스의 git 커밋 로그를 반환합니다. 어떤 파일이 커밋됐는지 확인할 수 있습니다.",
        "input_schema": {
            "type": "object",
            "properties": {
                "n": {
                    "type": "integer",
                    "description": "반환할 커밋 수. 기본값 10.",
                },
                "session_id": {
                    "type": "string",
                    "description": "조회할 세션 ID. 생략 시 현재 스레드의 세션을 사용합니다.",
                },
            },
        },
    },
    {
        "name": "list_all_sessions",
        "description": "AgentForge DB에 저장된 모든 세션 목록을 반환합니다. 세션 ID, 체크포인트 수를 포함합니다.",
        "input_schema": {
            "type": "object",
            "properties": {
                "limit": {
                    "type": "integer",
                    "description": "반환할 최대 세션 수. 기본값 20.",
                }
            },
        },
    },
    {
        "name": "read_local_file",
        "description": (
            "로컬 파일 시스템의 파일을 읽습니다. 절대 경로를 사용하세요. "
            "사용자가 특정 경로의 파일을 읽어달라고 할 때 사용합니다. "
            "예: C:/projects/README.md, /home/user/config.yaml"
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "읽을 파일의 절대 경로 (예: C:/aether-j/README.md)",
                },
                "encoding": {
                    "type": "string",
                    "description": "파일 인코딩 (기본: utf-8). 한글 파일에는 utf-8 또는 cp949.",
                },
                "max_lines": {
                    "type": "integer",
                    "description": "반환할 최대 줄 수 (기본: 300). 큰 파일은 이 값을 늘리세요.",
                },
            },
            "required": ["path"],
        },
    },
    {
        "name": "list_local_directory",
        "description": (
            "로컬 디렉토리의 파일/폴더 목록을 반환합니다. 절대 경로를 사용하세요. "
            "프로젝트 구조 파악, 파일 존재 확인 등에 사용합니다. "
            "예: C:/aether-j, /home/user/projects"
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "조회할 디렉토리의 절대 경로",
                },
                "recursive": {
                    "type": "boolean",
                    "description": "하위 디렉토리 포함 여부 (기본: false). 대형 프로젝트는 false 권장.",
                },
                "include_hidden": {
                    "type": "boolean",
                    "description": "숨김 파일(.으로 시작) 포함 여부 (기본: false)",
                },
            },
            "required": ["path"],
        },
    },
]

INTERRUPT_TOOLS: list[dict] = [
    {
        "name": "continue_task",
        "description": (
            "에스컬레이션된 태스크를 계속 진행합니다. "
            "조건이나 수정 사항이 있으면 conditions에 명시하세요 "
            "(예: 'TypeScript strict 제외', '기존 파일 검증만 하면 됨'). "
            "L4 에스컬레이션 대기 상황에서 사용합니다."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "conditions": {
                    "type": "string",
                    "description": "계속 진행 시 적용할 조건 또는 수정 사항. 조건이 없으면 생략하세요.",
                }
            },
        },
    },
    {
        "name": "abort_task",
        "description": "에스컬레이션된 태스크를 중단합니다. L4 에스컬레이션 대기 상황에서 사용합니다.",
        "input_schema": {
            "type": "object",
            "properties": {
                "reason": {"type": "string", "description": "중단 이유 (로그용)"},
            },
        },
    },
    {
        "name": "upgrade_model",
        "description": "태스크를 더 높은 등급의 모델로 업그레이드하여 재시도합니다. L2 에스컬레이션 대기 상황에서 사용합니다.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "stop_task",
        "description": "태스크를 중단합니다. L2 에스컬레이션 대기 상황에서 사용합니다.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "approve_plan",
        "description": "제시된 작업 계획서를 승인하고 작업을 시작합니다. plan_waiting 상황에서 사용합니다.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "request_plan_modification",
        "description": "작업 계획서 수정을 요청합니다. plan_waiting 상황에서 사용합니다.",
        "input_schema": {
            "type": "object",
            "properties": {
                "feedback": {
                    "type": "string",
                    "description": "수정 요청 내용 (예: 'FastAPI 대신 Flask 사용', '태스크 3과 4를 합쳐서')",
                }
            },
            "required": ["feedback"],
        },
    },
]

ACTION_TOOLS: list[dict] = [
    {
        "name": "resume_session",
        "description": (
            "기존 작업 세션을 이어서 진행합니다. 완료(COMPLETED)된 작업은 그대로 유지하고 "
            "실패(FAILED)하거나 차단(BLOCKED)된 작업만 재시도합니다. "
            "사용자가 '재개', '이어서', '계속', '이어해', '다시 시작' 등을 요청하고 "
            "이 스레드에 이미 진행된 작업 세션이 있을 때 사용합니다. "
            "완전히 처음부터 다시 시작하려면 retry_current_session을 사용하세요."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "session_id": {
                    "type": "string",
                    "description": "재개할 세션 ID. 생략 시 현재 스레드의 세션을 사용합니다.",
                }
            },
        },
    },
    {
        "name": "retry_current_session",
        "description": (
            "이전 요구사항 그대로 작업을 처음부터 재시작합니다. "
            "완료된 작업을 포함해 모든 작업을 처음부터 다시 실행합니다. "
            "사용자가 '처음부터 다시', '전체 재시작', '다시 해봐' 등을 요청할 때 사용합니다. "
            "완료된 작업을 유지하고 실패한 작업만 재시도하려면 resume_session을 사용하세요. "
            "스레드 대화 기록에서 요구사항을 파악할 수 있으면 requirements 필드에 넣어주세요."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "requirements": {
                    "type": "string",
                    "description": (
                        "스레드 대화에서 파악한 확정 요구사항 요약. "
                        "저장된 요구사항이 없거나 오류 메시지로 덮어써진 경우 여기에 올바른 내용을 입력하세요."
                    ),
                }
            },
        },
    },
    {
        "name": "start_new_task",
        "description": (
            "수정된 요구사항으로 완전히 새로운 작업을 시작합니다. "
            "사용자가 기술 스택 변경, 새로운 기능 추가, 전혀 다른 요청을 할 때 사용합니다. "
            "명확화(clarification) 단계부터 다시 시작하므로 요구사항이 변경될 때만 사용하세요."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "request": {
                    "type": "string",
                    "description": "새 작업 요청 내용. 비어 있으면 이전 요구사항을 그대로 사용합니다.",
                }
            },
        },
    },
    {
        "name": "update_global_context",
        "description": (
            "모든 세션에서 공유되는 글로벌 컨텍스트를 업데이트합니다. "
            "사용자가 선호도(기술 스택, 언어 등)를 밝히거나, 프로젝트 관련 중요 사항이 결정될 때 사용합니다. "
            "answer_question이나 다른 도구와 함께 사용할 수 있습니다."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "key": {
                    "type": "string",
                    "description": "업데이트할 키 (예: user_preferences, project_notes, known_patterns)",
                },
                "value": {
                    "description": "저장할 값 (문자열, 객체, 배열 모두 가능)",
                },
            },
            "required": ["key", "value"],
        },
    },
    {
        "name": "continue_clarification",
        "description": (
            "사용자가 요구사항을 명확화하는 대화를 계속합니다. "
            "사용자의 메시지가 질문에 대한 답변이거나 추가 요구사항 설명일 때 사용합니다. "
            "재시도/재시작/재개 요청에는 retry_current_session을, 새 기능 추가에는 start_new_task를 사용하세요."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "message": {
                    "type": "string",
                    "description": "사용자의 명확화 답변을 그대로 전달합니다.",
                }
            },
            "required": ["message"],
        },
    },
    {
        "name": "answer_question",
        "description": (
            "사용자 질문에 답합니다. "
            "실패 원인 분석, 결과 설명, 개선 방안 제안, 진행 상황 문의 등에 사용합니다. "
            "필요한 정보를 조회 도구로 먼저 수집한 후 이 도구로 종합 답변을 제공하세요."
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
    {
        "name": "post_si_issue",
        "description": (
            "AF 자체 동작 문제를 SI채널(#af-self-improve)에 QA 이슈로 게시합니다. "
            "태스크 내용 문제가 아닌 AF 시스템 동작 문제일 때만 사용하세요. "
            "예: semantic verifier 반복 REJECT, escalate 3회 이상, timeout 반복."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "symptom": {
                    "type": "string",
                    "description": "관찰된 증상 (예: verify_semantic이 REJECT를 3회 반복)",
                },
                "suspected_cause": {
                    "type": "string",
                    "description": "의심되는 원인 (예: semantic_layer.py의 수락 기준이 너무 엄격함)",
                },
                "reproduction": {
                    "type": "string",
                    "description": "재현 조건 (예: acceptance_criteria에 '100% 테스트 통과' 포함 시)",
                },
            },
            "required": ["symptom", "suspected_cause"],
        },
    },
]

LEADER_TOOLS: list[dict] = QUERY_TOOLS + ACTION_TOOLS

_FILE_TOOLS = {"read_local_file", "list_local_directory"}


def _resolve_max_turns(tools: list[dict], allow_actions: bool) -> int:
    """
    작업 복잡도에 따라 최대 턴 수를 결정한다.

    우선순위:
    1. 환경변수 AF_LEADER_MAX_TURNS — 사용자가 직접 지정한 값 (무조건 우선)
    2. 파일 탐색 도구 포함 여부 — 로컬 파일 읽기는 여러 턴이 필요
    3. allow_actions 여부 — 조회 전용은 짧게, 액션 포함은 길게

    기본값 (env 미설정):
      - 조회 전용(allow_actions=False):                  8턴
      - 액션 포함, 파일 도구 없음:                       12턴
      - 액션 포함, 파일 도구 있음 (로컬 경로 탐색 등):   20턴
    """
    env_val = os.getenv("AF_LEADER_MAX_TURNS", "").strip()
    if env_val.isdigit():
        return max(1, int(env_val))

    tool_names = {t.get("name") for t in tools}
    has_file_tools = bool(tool_names & _FILE_TOOLS)

    if not allow_actions:
        return 8
    if has_file_tools:
        return 20
    return 12


_SYSTEM_PROMPT = """\
당신은 AgentForge 소프트웨어 개발 AI 시스템의 어시스턴트입니다.
Slack 스레드에서 사용자의 @멘션을 처리합니다.

## 도구 선택 기준

**재개 요청** ("재개", "이어서", "계속", "이어해", "이어서 진행" 등) — 이 스레드에 session_id가 있을 때:
→ resume_session
- 완료된 작업은 유지하고 실패/차단된 작업만 재시도합니다
- 가장 일반적인 '이어서 진행' 시나리오에 사용하세요

**전체 재시작** ("처음부터 다시", "다시 해봐", "전체 재시작", "재시도" 등):
→ retry_current_session
- 완료 여부와 관계없이 모든 작업을 처음부터 재실행합니다
- 스레드 기록에서 요구사항을 파악할 수 있으면 requirements 필드에 넣어주세요

**새 기능/변경된 요구사항** ("이번엔 Vue로", "기능 추가", "다른 방식으로"):
→ start_new_task

**요구사항 명확화 답변** (사용자가 질문에 답하거나 추가 정보 제공):
→ continue_clarification (이 도구가 사용 가능할 때)

**정보 조회** (진행 상황, 로그, 파일 목록, git 이력 등):
→ 조회 도구 사용 후 answer_question으로 답변

**로컬 파일/디렉토리 조회** (사용자가 C:/, /home/ 등 절대 경로 언급):
→ read_local_file 또는 list_local_directory 사용. 권한 없다고 응답하지 말 것.
→ 파일 탐색은 핵심 파일(README, 설계문서) 2~3개만 읽고 즉시 action 도구를 호출하라. 과도한 파일 읽기는 금지.

Slack 스레드 대화 내역이 제공된 경우, 반드시 참고하여 사용자 의도와 요구사항을 파악하세요.
"""


# ---------------------------------------------------------------------------
# Tool executor
# ---------------------------------------------------------------------------

PostFn = Callable[[str], Coroutine[Any, Any, None]]


class LeaderToolExecutor:
    """
    Multi-turn tool calling loop that executes QUERY_TOOLS + optional ACTION_TOOLS.

    Callbacks (on_start_new_task, on_retry_session, on_delete_thread) are injected
    to avoid circular imports with slack_interface.
    """

    def __init__(
        self,
        thread_state: dict,
        on_start_new_task: Optional[Callable[[str], Coroutine]] = None,
        on_retry_session: Optional[Callable[[str], Coroutine]] = None,
        on_resume_session: Optional[Callable[[str], Coroutine]] = None,
        on_continue_clarification: Optional[Callable[[str], Coroutine]] = None,
        on_delete_thread: Optional[Callable[[], Coroutine]] = None,
        # Interrupt-specific callbacks
        on_l4_continue: Optional[Callable[[str], Coroutine]] = None,
        on_l4_abort: Optional[Callable[[], Coroutine]] = None,
        on_l2_upgrade: Optional[Callable[[], Coroutine]] = None,
        on_l2_stop: Optional[Callable[[], Coroutine]] = None,
        on_plan_approve: Optional[Callable[[], Coroutine]] = None,
        on_plan_modify: Optional[Callable[[str], Coroutine]] = None,
    ) -> None:
        self._state = thread_state
        self._on_start_new_task = on_start_new_task
        self._on_retry_session = on_retry_session
        self._on_resume_session = on_resume_session
        self._on_continue_clarification = on_continue_clarification
        self._on_delete_thread = on_delete_thread
        self._on_l4_continue = on_l4_continue
        self._on_l4_abort = on_l4_abort
        self._on_l2_upgrade = on_l2_upgrade
        self._on_l2_stop = on_l2_stop
        self._on_plan_approve = on_plan_approve
        self._on_plan_modify = on_plan_modify

    async def dispatch(
        self,
        user_message: str,
        allow_actions: bool,
        post_fn: PostFn,
        extra_tools: Optional[list[dict]] = None,
        interrupt_context: str = "",
    ) -> None:
        tools = LEADER_TOOLS if allow_actions else QUERY_TOOLS
        if extra_tools:
            tools = list(tools) + extra_tools
        context = self._build_context(interrupt_context=interrupt_context)
        messages: list[dict] = [
            {"role": "user", "content": f"{context}\n\n사용자: {user_message}"}
        ]

        client = anthropic.AsyncAnthropic()
        max_turns = _resolve_max_turns(tools, allow_actions)
        logger.info("[leader_tools] max_turns=%d allow_actions=%s tools=%d",
                    max_turns, allow_actions, len(tools))

        for turn in range(max_turns):
            try:
                response = await client.messages.create(
                    model=SONNET,
                    max_tokens=2048,
                    system=[{
                        "type": "text",
                        "text": _SYSTEM_PROMPT,
                        "cache_control": {"type": "ephemeral"},
                    }],
                    tools=tools,
                    messages=messages,
                )
            except Exception as exc:
                logger.exception("LeaderToolExecutor API error: %s", exc)
                await post_fn(f"요청 처리 중 오류가 발생했습니다: {exc}")
                return

            tool_blocks = [b for b in response.content if getattr(b, "type", None) == "tool_use"]
            text_blocks  = [b for b in response.content if getattr(b, "type", None) == "text"]

            logger.info(
                "[leader_tools] turn=%d stop_reason=%s tools=%s",
                turn, response.stop_reason,
                [b.name for b in tool_blocks],
            )

            if not tool_blocks:
                text = " ".join(getattr(b, "text", "") for b in text_blocks).strip()
                if text:
                    await post_fn(text)
                return

            messages.append({"role": "assistant", "content": response.content})
            tool_results: list[dict] = []

            for block in tool_blocks:
                name  = block.name
                args  = block.input or {}

                # Terminal action tools — execute and return immediately
                if name == "resume_session":
                    session_id = args.get("session_id", "").strip() or self._state.get("session_id", "")
                    logger.info(
                        "[leader_tools] → resume_session session_id=%.36s",
                        session_id or "(현재 스레드)",
                    )
                    if self._on_resume_session:
                        await self._on_resume_session(session_id)
                    return

                if name == "retry_current_session":
                    requirements = args.get("requirements", "").strip()
                    logger.info(
                        "[leader_tools] → retry_current_session requirements=%.120s",
                        requirements or "(스레드에서 파악)",
                    )
                    if self._on_retry_session:
                        await self._on_retry_session(requirements)
                    return

                if name == "continue_clarification":
                    message = args.get("message", "").strip()
                    logger.info(
                        "[leader_tools] → continue_clarification message=%.120s", message
                    )
                    if message and self._on_continue_clarification:
                        await self._on_continue_clarification(message)
                    return

                if name == "start_new_task":
                    request = args.get("request", "").strip() or self._state.get("request", "")
                    logger.info("[leader_tools] → start_new_task request=%.120s", request)
                    if self._on_delete_thread:
                        await self._on_delete_thread()
                    if self._on_start_new_task:
                        await self._on_start_new_task(request)
                    return

                if name == "answer_question":
                    answer = args.get("answer", "답변을 생성할 수 없습니다.")
                    logger.info("[leader_tools] → answer_question len=%d", len(answer))
                    await post_fn(answer)
                    return

                if name == "post_si_issue":
                    symptom = args.get("symptom", "")
                    cause   = args.get("suspected_cause", "")
                    repro   = args.get("reproduction", "")
                    session_short = session_id[:8] if session_id else "?"
                    import os
                    si_channel = os.getenv("AF_SI_CHANNEL", "")
                    if si_channel:
                        try:
                            # Slack API 직접 호출은 slack_interface 계층에서 해야 하지만
                            # leader_tools는 Slack client에 접근 불가 → post_fn으로 사용자 스레드에 알리고,
                            # slack_interface._handle_complaint가 SI채널에 라우팅함
                            qa_text = (
                                f"🔍 *[리더] QA 이슈*\n"
                                f"세션: `{session_short}`\n"
                                f"증상: {symptom}\n"
                                f"의심 원인: {cause}"
                                + (f"\n재현 조건: {repro}" if repro else "")
                            )
                            logger.info("[leader_tools] → post_si_issue symptom=%.80s", symptom)
                            await post_fn(f"SI채널에 QA 이슈를 게시했습니다.\n{qa_text}")
                        except Exception as exc:
                            logger.warning("[leader_tools] post_si_issue failed: %s", exc)
                    else:
                        await post_fn("⚠️ AF_SI_CHANNEL이 설정되지 않아 SI채널에 게시할 수 없습니다.")
                    return

                # Interrupt action tools
                if name == "continue_task":
                    conditions = args.get("conditions", "").strip()
                    logger.info("[leader_tools] → continue_task conditions=%.120s", conditions)
                    if self._on_l4_continue:
                        await self._on_l4_continue(conditions)
                    return

                if name == "abort_task":
                    reason = args.get("reason", "")
                    logger.info("[leader_tools] → abort_task reason=%.120s", reason)
                    if self._on_l4_abort:
                        await self._on_l4_abort()
                    return

                if name == "upgrade_model":
                    logger.info("[leader_tools] → upgrade_model")
                    if self._on_l2_upgrade:
                        await self._on_l2_upgrade()
                    return

                if name == "stop_task":
                    logger.info("[leader_tools] → stop_task")
                    if self._on_l2_stop:
                        await self._on_l2_stop()
                    return

                if name == "approve_plan":
                    logger.info("[leader_tools] → approve_plan")
                    if self._on_plan_approve:
                        await self._on_plan_approve()
                    return

                if name == "request_plan_modification":
                    feedback = args.get("feedback", "").strip()
                    logger.info("[leader_tools] → request_plan_modification feedback=%.120s", feedback)
                    if feedback and self._on_plan_modify:
                        await self._on_plan_modify(feedback)
                    return

                if name == "update_global_context":
                    key   = args.get("key", "")
                    value = args.get("value")
                    if key:
                        try:
                            from agentforge.core.global_context import get_global_context_store
                            await get_global_context_store().update({key: value})
                            result_str = f"글로벌 컨텍스트 업데이트 완료: {key}"
                        except Exception as exc:
                            result_str = f"업데이트 실패: {exc}"
                    else:
                        result_str = "key가 비어 있습니다."
                    tool_results.append({
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": result_str,
                    })
                    continue

                # Query tools — execute and collect result
                result = await self._execute(name, args)
                logger.info(
                    "[leader_tools] tool=%s result_len=%d result_preview=%.120s",
                    name, len(result), result,
                )
                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": result,
                })

            messages.append({"role": "user", "content": tool_results})

        # max_turns exhausted — post whatever text the model last returned
        text = " ".join(getattr(b, "text", "") for b in text_blocks).strip()
        await post_fn(text or "처리 제한에 도달했습니다. 다시 시도해 주세요.")

    def _build_context(self, interrupt_context: str = "") -> str:
        original_request  = self._state.get("request", "")
        clarified_summary = self._state.get("summary", "")
        result            = self._state.get("result", "")
        session_id        = self._state.get("session_id", "")
        stage             = self._state.get("stage", "")
        thread_messages   = self._state.get("thread_messages", "")
        global_ctx        = self._state.get("_global_context", "")
        logger.info(
            "[leader_tools] context stage=%s session=%s "
            "has_summary=%s has_result=%s has_thread_msgs=%s",
            stage,
            str(session_id)[:8] or "none",
            bool(clarified_summary),
            bool(result),
            bool(thread_messages),
        )

        parts = ["## 현재 스레드 상태", f"- 단계: {stage}"]
        if interrupt_context:
            parts.append(f"\n## ⚠️ 현재 대기 중인 상황\n{interrupt_context}")
        if session_id:
            parts.append(f"- 세션 ID: {session_id}")
        if original_request:
            parts.append(f"- 원래 요청: {original_request}")
        if clarified_summary:
            parts.append(f"\n## 확정된 요구사항 (사용자가 확인한 내용)\n{clarified_summary}")
        if result:
            parts.append(f"\n## 마지막 작업 결과\n{result}")
        if thread_messages:
            parts.append(f"\n## Slack 스레드 대화 내역\n{thread_messages}")
        if global_ctx:
            parts.append(f"\n## 글로벌 컨텍스트 (모든 세션 공유)\n{global_ctx}")

        return "\n".join(parts)

    # ------------------------------------------------------------------
    # Query tool implementations
    # ------------------------------------------------------------------

    async def _execute(self, name: str, args: dict) -> str:
        try:
            if name == "get_session_progress":
                return await self._get_session_progress(args)
            if name == "read_execution_log":
                return await self._read_execution_log(args)
            if name == "list_workspace_files":
                return await self._list_workspace_files(args)
            if name == "read_workspace_file":
                return await self._read_workspace_file(args)
            if name == "get_git_history":
                return await self._get_git_history(args)
            if name == "list_all_sessions":
                return await self._list_all_sessions(args)
            if name == "read_local_file":
                return await self._read_local_file(args)
            if name == "list_local_directory":
                return await self._list_local_directory(args)
            return f"알 수 없는 도구: {name}"
        except Exception as exc:
            logger.warning("Tool %s error: %s", name, exc)
            return f"오류: {exc}"

    def _resolve_session_id(self, args: dict) -> Optional[str]:
        return args.get("session_id") or self._state.get("session_id")

    async def _get_session_progress(self, args: dict) -> str:
        session_id = self._resolve_session_id(args)
        if not session_id:
            return "세션 ID를 확인할 수 없습니다."

        from agentforge.core.checkpoint import init_checkpointer
        cp = await init_checkpointer()
        config = {"configurable": {"thread_id": session_id}}
        # aget() returns the raw Checkpoint dict (not CheckpointTuple); channel_values is a top-level key
        checkpoint = await cp.aget(config)
        if checkpoint is None:
            return f"세션 {session_id[:8]}의 체크포인트를 찾을 수 없습니다."

        state = checkpoint.get("channel_values", {})
        dag_index  = state.get("dag_index", {})
        task_nodes = state.get("task_nodes", {})

        if not dag_index:
            return f"세션 {session_id[:8]}: DAG 인덱스 없음 (아직 시작되지 않았거나 refine 단계)"

        rows = []
        for tid, status in dag_index.items():
            node = task_nodes.get(tid)
            title    = ""
            attempts = 0
            summary  = ""
            if node:
                if hasattr(node, "instruction"):
                    title = node.instruction.title
                    attempts = node.attempt_count
                elif isinstance(node, dict):
                    title    = node.get("instruction", {}).get("title", "")
                    attempts = node.get("attempt_count", 0)
                report = node.report if hasattr(node, "report") else node.get("report")
                if report:
                    summary = report.summary if hasattr(report, "summary") else (report.get("summary") or "")
            rows.append(f"- [{status}] {tid}: {title} (시도:{attempts}) {summary[:80]}")

        total     = len(dag_index)
        completed = sum(1 for s in dag_index.values()
                        if s == TaskStatus.COMPLETED or str(s) == "completed")
        failed    = sum(1 for s in dag_index.values()
                        if s == TaskStatus.FAILED or str(s) == "failed")

        return (
            f"세션 {session_id[:8]} 진행 상태 — 완료: {completed}/{total}  실패: {failed}\n"
            + "\n".join(rows)
        )

    async def _read_execution_log(self, args: dict) -> str:
        session_id = self._resolve_session_id(args)
        tail_lines = int(args.get("tail_lines", 100))

        journal_dir = Path("memory/journal")
        if not journal_dir.exists():
            return "일지 디렉토리가 존재하지 않습니다."

        # Find files matching session prefix
        pattern = f"*{session_id[:8]}*" if session_id else "*.md"
        files = sorted(journal_dir.glob(pattern), key=lambda p: p.stat().st_mtime, reverse=True)

        if not files:
            # Fallback: most recent log file
            all_files = sorted(journal_dir.glob("*.md"), key=lambda p: p.stat().st_mtime, reverse=True)
            if not all_files:
                return "일지 파일을 찾을 수 없습니다."
            files = all_files[:1]

        target = files[0]
        try:
            import aiofiles
            async with aiofiles.open(target, encoding="utf-8") as f:
                lines = await f.readlines()
            tail = lines[-tail_lines:] if len(lines) > tail_lines else lines
            return f"파일: {target.name}\n\n" + "".join(tail)
        except Exception as exc:
            return f"파일 읽기 오류: {exc}"

    def _get_workspace_manager(self, session_id: Optional[str]):
        if not session_id:
            return None
        from agentforge.workspace.manager import WorkspaceManager
        ws = WorkspaceManager(session_id)
        if not ws.root.exists():
            return None
        return ws

    async def _list_workspace_files(self, args: dict) -> str:
        session_id = self._resolve_session_id(args)
        ws = self._get_workspace_manager(session_id)
        if ws is None:
            return "워크스페이스를 찾을 수 없습니다." if session_id else "세션 ID를 확인할 수 없습니다."

        files = [
            str(p.relative_to(ws.root))
            for p in sorted(ws.root.rglob("*"))
            if p.is_file() and ".git" not in p.parts
        ]
        if not files:
            return "워크스페이스에 파일이 없습니다."
        return f"워크스페이스 ({ws.root}):\n" + "\n".join(f"  {f}" for f in files)

    async def _read_workspace_file(self, args: dict) -> str:
        rel_path   = args.get("path", "")
        session_id = self._resolve_session_id(args)
        ws = self._get_workspace_manager(session_id)
        if ws is None:
            return "워크스페이스를 찾을 수 없습니다."

        target = (ws.root / rel_path).resolve()
        if not str(target).startswith(str(ws.root.resolve())):
            return "보안 오류: 워크스페이스 외부 경로는 읽을 수 없습니다."
        if not target.exists():
            return f"파일을 찾을 수 없습니다: {rel_path}"
        try:
            content = target.read_text(encoding="utf-8")
            if len(content) > 8000:
                content = content[:8000] + "\n... (잘림)"
            return f"파일: {rel_path}\n\n```\n{content}\n```"
        except Exception as exc:
            return f"파일 읽기 오류: {exc}"

    async def _get_git_history(self, args: dict) -> str:
        n          = int(args.get("n", 10))
        session_id = self._resolve_session_id(args)
        ws = self._get_workspace_manager(session_id)
        if ws is None:
            return "워크스페이스를 찾을 수 없습니다."

        log = ws.git_log(n=n)
        return log if log else "커밋 기록이 없습니다."

    async def _list_all_sessions(self, args: dict) -> str:
        limit = int(args.get("limit", 20))
        db_path = os.getenv("AF_DB_PATH", "agentforge.db")
        if not Path(db_path).exists():
            return "데이터베이스가 존재하지 않습니다."
        try:
            import aiosqlite
            async with aiosqlite.connect(db_path) as db:
                async with db.execute(
                    """
                    SELECT thread_id, COUNT(*) AS steps, MAX(rowid) AS last_rowid
                    FROM checkpoints
                    GROUP BY thread_id
                    ORDER BY last_rowid DESC
                    LIMIT ?
                    """,
                    (limit,),
                ) as cur:
                    rows = await cur.fetchall()
        except Exception as exc:
            return f"DB 조회 오류: {exc}"

        if not rows:
            return "저장된 세션이 없습니다."

        lines = [f"최근 세션 (최대 {limit}개):"]
        for thread_id, steps, _ in rows:
            lines.append(f"  - {thread_id} (체크포인트: {steps}개)")
        return "\n".join(lines)

    async def _read_local_file(self, args: dict) -> str:
        import aiofiles
        path_str = args.get("path", "").strip()
        encoding = args.get("encoding", "utf-8")
        max_lines = int(args.get("max_lines", 300))

        if not path_str:
            return "path 파라미터가 필요합니다."

        path = Path(path_str)
        if not path.exists():
            return f"파일을 찾을 수 없습니다: {path_str}"
        if not path.is_file():
            return f"경로가 파일이 아닙니다: {path_str}"

        try:
            async with aiofiles.open(path, encoding=encoding, errors="replace") as f:
                content = await f.read()
        except PermissionError:
            return f"파일 읽기 권한이 없습니다: {path_str}"
        except Exception as exc:
            return f"파일 읽기 오류: {exc}"

        lines = content.splitlines()
        total = len(lines)
        if total > max_lines:
            lines = lines[:max_lines]
            footer = f"\n... (총 {total}줄 중 {max_lines}줄 표시. max_lines를 늘리면 더 볼 수 있습니다)"
        else:
            footer = ""

        return "\n".join(lines) + footer

    async def _list_local_directory(self, args: dict) -> str:
        import os as _os
        from datetime import datetime as _dt

        path_str = args.get("path", "").strip()
        recursive = bool(args.get("recursive", False))
        include_hidden = bool(args.get("include_hidden", False))
        max_items = 500

        if not path_str:
            return "path 파라미터가 필요합니다."

        path = Path(path_str)
        if not path.exists():
            return f"경로를 찾을 수 없습니다: {path_str}"
        if not path.is_dir():
            return f"경로가 디렉토리가 아닙니다: {path_str}"

        try:
            entries: list[tuple[str, str, int, str]] = []  # (type, rel_path, size, mtime)

            if recursive:
                for root, dirs, files in _os.walk(path):
                    if not include_hidden:
                        dirs[:] = [d for d in dirs if not d.startswith(".")]
                    root_path = Path(root)
                    for name in sorted(dirs):
                        if not include_hidden and name.startswith("."):
                            continue
                        rel = str((root_path / name).relative_to(path))
                        entries.append(("D", rel, 0, ""))
                    for name in sorted(files):
                        if not include_hidden and name.startswith("."):
                            continue
                        fp = root_path / name
                        try:
                            stat = fp.stat()
                            size = stat.st_size
                            mtime = _dt.fromtimestamp(stat.st_mtime).strftime("%Y-%m-%d %H:%M")
                        except OSError:
                            size, mtime = 0, ""
                        rel = str(fp.relative_to(path))
                        entries.append(("F", rel, size, mtime))
                    if len(entries) >= max_items:
                        break
            else:
                for item in sorted(path.iterdir(), key=lambda p: (p.is_file(), p.name)):
                    if not include_hidden and item.name.startswith("."):
                        continue
                    if item.is_dir():
                        entries.append(("D", item.name, 0, ""))
                    else:
                        try:
                            stat = item.stat()
                            size = stat.st_size
                            mtime = _dt.fromtimestamp(stat.st_mtime).strftime("%Y-%m-%d %H:%M")
                        except OSError:
                            size, mtime = 0, ""
                        entries.append(("F", item.name, size, mtime))

        except PermissionError:
            return f"디렉토리 읽기 권한이 없습니다: {path_str}"
        except Exception as exc:
            return f"디렉토리 조회 오류: {exc}"

        if not entries:
            return f"{path_str}: 비어있는 디렉토리"

        truncated = ""
        if len(entries) >= max_items:
            truncated = f"\n... (최대 {max_items}개 표시됨)"
            entries = entries[:max_items]

        lines = [f"{path_str} ({len(entries)}개 항목):"]
        for typ, name, size, mtime in entries:
            if typ == "D":
                lines.append(f"  [D] {name}/")
            else:
                size_str = f"{size:,}B" if size < 1024 else f"{size // 1024:,}KB"
                lines.append(f"  [F] {name}  {size_str}  {mtime}")

        return "\n".join(lines) + truncated
