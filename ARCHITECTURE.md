# AgentForge 아키텍처 문서

> 연구자(ResearcherAgent)가 소스코드를 탐색할 때 이 문서를 먼저 읽고, 필요한 파일만 선택적으로 읽어라.
> 모든 소스 파일 경로는 `c:\harness\agentforge\` 기준.

---

## 전체 데이터 흐름

```
사용자 Slack 메시지
    ↓
slack_interface.py — 세션 상태(clarifying/running/l4_waiting) 판별
    ↓
[clarifying] ClarifierAgent — 다중턴 요구사항 명확화
    ↓
[running] GraphBuilder.from_spec() — LangGraph 컴파일
    ↓
refine_requirements → build_dag → present_plan → [사용자 승인]
    ↓
dispatch_workers → WorkerAgent (병렬, Docker 격리)
    ↓
verify_ci → verify_semantic → merge_task
    ↓ (실패 시)
escalate → [L0 재시도 / L1 세션 교체 / L2 모델 업그레이드 / L3 중단 / L4 사용자 승인]
    ↓ (성공 시)
finalize → FINAL_REPORT.md → END
```

---

## 파일별 역할

### `agentforge/agentforge/main.py`
CLI 진입점. `agentforge start` 명령 처리. Slack 인터페이스 초기화, `memory/` 디렉토리 생성, AGENTS.md 자동 생성, 세션 복원(`_restore_sessions`).
- **수정 시 주의**: 새 에이전트 초기화나 디렉토리 생성은 여기서 추가.

---

### `agentforge/agentforge/agents/`

| 파일 | 역할 |
|------|------|
| `base.py` | 모든 에이전트 추상 기반 클래스. `__init__(model_tier: ModelTier)` |
| `clarifier.py` | 사용자 요구사항 다중턴 명확화. `ClarifierAgent.next_turn(history)` → JSON 응답 |
| `leader.py` | 핵심 오케스트레이터. `LeaderAgent.run(state)` → add_task/submit_plan/post_si_issue 도구 사용. 시스템 프롬프트에 워크플로우 4단계 및 SI채널 기준 포함 |
| `leader_tools.py` | 리더의 도구 함수 모음. `add_task`, `submit_plan`, `get_dag_status`, `post_si_issue` 등 |
| `worker.py` | 개별 태스크 실행 에이전트. `run_worker_task(instruction, workspace_root)` → `TaskReport`. Docker 격리 + git 커밋 |
| `sub_orchestrator.py` | 위임된 sub-DAG를 독립 처리하는 Sonnet 기반 서브 오케스트레이터 |

---

### `agentforge/agentforge/core/`

| 파일 | 역할 |
|------|------|
| `models.py` | 모든 데이터 모델(Pydantic). `TaskInstruction`, `TaskReport`, `TaskNode`, `AgentEntry`, `CIResult`, `SemanticResult`, `EscalationAction`, `ImprovementProposal`, `ReloadGuide`, `SandboxResult`. `MODEL_IDS` 딕셔너리 |
| `state.py` | LangGraph 상태 타입 `AgentForgeState`. `make_initial_state(session_id, user_request)`. 필드: `task_nodes`, `dag_index`, `workflow_spec`, `workspace_root`, `test_passed` 등 |
| `checkpoint.py` | LangGraph 체크포인터(AsyncSqliteSaver). `JsonPlusSerializer(allowed_msgpack_modules=True)` 설정. `init_checkpointer()`, `get_checkpointer()` |
| `registry.py` | 에이전트 풀 관리. `AgentRegistry`. 7일 성공률 기반 `get_best_agent(tier)`. `mark_busy/idle/failed/update_stats` |
| `context_store.py` | Slack 스레드 → 세션 매핑 SQLite 영속화. 상태: `clarifying/confirming/running/l4_waiting`. `ThreadContextStore` |
| `global_context.py` | 크로스세션 사용자 선호도·기술스택·패턴 JSON 영속화. `GlobalContextStore.get/update` |

---

### `agentforge/agentforge/graph/`

| 파일 | 역할 |
|------|------|
| `nodes.py` | LangGraph 노드 함수 15개. `refine_requirements_node`, `build_dag_node`, `present_plan_node`, `dispatch_workers_node`, `verify_ci_node`, `verify_semantic_node`, `merge_task_node`, `escalate_node`, `interrupt_l2_node`, `interrupt_l4_node`, `finalize_node` 등. `_detect_run_guide`, `_get_ready_task_ids` 헬퍼 포함 |
| `edges.py` | LangGraph 조건 엣지 라우팅 함수. `route_after_verify_ci`, `route_after_verify_semantic`, `route_after_escalate`, `route_after_finalize`, `route_after_interrupt_l4` 등 |

---

### `agentforge/agentforge/interfaces/`

| 파일 | 역할 |
|------|------|
| `base_interface.py` | 인터페이스 추상 기반 클래스(ABC). `start/stop/send_message/update_message/send_l4_prompt` |
| `slack_interface.py` | Slack Bolt 기반 봇. `SlackInterface`. `stream_graph_to_slack` (LangGraph 이벤트 스트리밍), `_handle_complaint`, `_restore_sessions`, `_post_or_update` (상태 메시지 업데이트), `self._status_ts` (세션별 상태 메시지 TS 관리). **ScribeAgent/ResearcherAgent 연동 포인트** |

---

### `agentforge/agentforge/verification/`

| 파일 | 역할 |
|------|------|
| `ci_layer.py` | CI 자동 검증. `CIVerifier.verify(instruction, report, workspace_root)` → `CIResult`. 파일 존재·테스트 통과·git 커밋 확인 |
| `semantic_layer.py` | Claude Opus 기반 수락 기준 의미 검증. `SemanticVerifier.verify(instruction, report, ci_result)` → `SemanticResult`. JSON 파싱 실패 시 REJECT |

---

### `agentforge/agentforge/observer/`

| 파일 | 역할 |
|------|------|
| `historian.py` | 태스크 이벤트를 `memory/journal/YYYY-MM-DD_session_{id}.md` 파일에 기록. `Historian.record_event/record_complaint`. **ScribeAgent가 파일 저널 백업용으로 내부 사용** |
| `retrospective.py` | ~~패턴 분석 (하드코딩된 모델 업그레이드 제안)~~ → **ResearcherAgent로 완전 교체 예정** |
| `self_improve.py` | 승인된 제안 적용. `SelfImproveWorkflow.apply(proposal)`. `_apply_config` (YAML 직접 수정), `_apply_code` (git worktree 기반 AF 세션 실행). **핵심 수정 파일** |
| `scribe.py` | *(신규)* 사관 에이전트. SI채널에 세션 스레드 생성·태스크 이벤트 기록·세션 요약. |
| `researcher.py` | *(신규)* 연구자 에이전트. 3계층 트리거 분석·개선 제안·승인 후 실행. RetrospectiveAgent 대체. |

---

### `agentforge/agentforge/workspace/`

| 파일 | 역할 |
|------|------|
| `manager.py` | 세션별 워크스페이스 생성. `WorkspaceManager(session_id)`. `self.root = AF_WORKSPACE_DIR / session_id`. `init()` → git init + src/tests/instructions 디렉토리 생성. `run_tests()` → Docker/local 테스트 실행 |

---

### `agentforge/agentforge/tools/`

| 파일 | 역할 |
|------|------|
| `dev_tools.py` | LangChain 도구로 Docker 격리 실행. `docker_python`, `docker_bash`, `docker_run_tests`. `ALL_DEV_TOOLS` 리스트 (worker가 사용) |

---

### `agentforge/agentforge/sandbox/`

| 파일 | 역할 |
|------|------|
| `docker_executor.py` | 메모리/CPU 제한 + 네트워크 격리 Docker 컨테이너 실행. `DockerExecutor.run_code/run_tests/run_command`. `AF_MOCK_MODE=true` 시 mock 반환 |

---

### `agentforge/workflows/`

| 파일 | 역할 |
|------|------|
| `builder.py` | `WorkflowSpec` → 컴파일된 LangGraph. `GraphBuilder.from_yaml/from_spec`. `_topological_sort` (DAG 위상정렬 + 사이클 검사) |
| `templates/feature_dev.yaml` | 기능개발 4단계 템플릿: `design`(Sonnet) → `implement`(Haiku) → `test`(Haiku) → `review`(Opus) |

---

## 핵심 환경변수

| 변수 | 기본값 | 용도 |
|------|--------|------|
| `SLACK_BOT_TOKEN` | — | Slack 봇 토큰 |
| `SLACK_APP_TOKEN` | — | Slack Socket Mode 토큰 |
| `SLACK_SIGNING_SECRET` | — | 요청 서명 검증 |
| `AF_SLACK_CHANNEL` | — | 기본 작업 채널 ID |
| `AF_SI_CHANNEL` | — | Self-Improve 채널 ID |
| `AF_LEADER_MODEL` | `claude-opus-4-7` | 리더 에이전트 모델 |
| `AF_WORKSPACE_DIR` | `workspace` (CWD 기준) | 런타임 세션 워크스페이스 루트 |
| `AF_LOCAL_MODEL` | `ollama/llama3` | LOCAL 티어 모델명 |
| `AF_MOCK_MODE` | `false` | true 시 외부 호출 모두 mock |
| `ANTHROPIC_API_KEY` | — | Claude API 키 |

---

## 자주 수정되는 파일 및 주의사항

| 파일 | 수정 시 주의 |
|------|-------------|
| `graph/nodes.py` | LangGraph 노드는 순수 함수여야 함. 부작용은 비동기 서비스(Slack, git)로 위임. |
| `graph/edges.py` | 조건 엣지 반환값은 `builder.py`의 `add_conditional_edges` 딕셔너리 키와 일치해야 함. |
| `core/models.py` | Pydantic 모델 변경 시 체크포인터 역직렬화 호환성 확인. |
| `core/state.py` | `AgentForgeState`에 필드 추가 시 `make_initial_state()`에도 기본값 추가. |
| `interfaces/slack_interface.py` | `self._status_ts[session_id]` 로 세션별 상태 메시지 관리. 새 채널 수신은 `_leader_channels` 집합에 등록. |
| `observer/self_improve.py` | `_apply_code()`는 `workspace_root`를 git worktree 경로로 설정해야 AF 소스에 반영됨. |

---

## 데이터 모델 관계

```
WorkflowSpec
  └─ TaskSpec[] → (빌드 시) → TaskInstruction[]
                                    ↓ (워커 실행 후)
                              TaskReport
                                    ↓ (검증)
                              CIResult + SemanticResult
                                    ↓ (실패 시)
                              EscalationAction
                                    ↓ (자가 개선 제안)
                              ImprovementProposal → ReloadGuide
```

---

## 에스컬레이션 레벨

| 레벨 | 동작 |
|------|------|
| L0 | 동일 에이전트 재지시 (retry) |
| L1 | 동일 티어 신규 에이전트 세션 교체 |
| L2 | 모델 티어 업그레이드 (Haiku → Sonnet → Opus) + interrupt |
| L3 | 태스크 중단 + DAG 부분 블록 |
| L4 | 사용자 승인 요청 interrupt |

---

## 연구자 탐색 가이드

분석 목적별 읽어야 할 파일:

| 목적 | 읽어야 할 파일 |
|------|--------------|
| 태스크 실패 원인 분석 | `graph/nodes.py` (escalate_node), `verification/ci_layer.py`, `verification/semantic_layer.py` |
| 에스컬레이션 패턴 | `graph/edges.py` (route_after_escalate), `graph/nodes.py` (escalate_node) |
| 워커 동작 | `agents/worker.py`, `tools/dev_tools.py` |
| 세션 상태 흐름 | `core/state.py`, `interfaces/slack_interface.py` (stream_graph_to_slack) |
| 개선 적용 | `observer/self_improve.py` (_apply_code), `workflows/builder.py` |
| 검증 로직 | `verification/ci_layer.py`, `verification/semantic_layer.py` |
