# AgentForge

에이전트로 구성된 AI 개발 팀 오케스트레이션 시스템.  
자연어 요구사항을 받아 작업을 분해하고, Anthropic 모델로 구성된 에이전트 팀이 자율적으로 실행·검증·에스컬레이션한다.

---

## 목차

1. [시스템 요구사항](#시스템-요구사항)
2. [초기 세팅](#초기-세팅)
3. [Slack 앱 설정](#slack-앱-설정)
4. [Docker 샌드박스 설정](#docker-샌드박스-설정)
5. [환경변수 설정](#환경변수-설정)
6. [실행](#실행)
7. [사용법](#사용법)
8. [워크플로우 커스터마이징](#워크플로우-커스터마이징)
9. [에스컬레이션 프로토콜](#에스컬레이션-프로토콜)
10. [ObserverAgent — 자기 개선 루프](#observeragent--자기-개선-루프)
11. [운영 모니터링](#운영-모니터링)
12. [트러블슈팅](#트러블슈팅)

---

## 시스템 요구사항

| 항목 | 요구사항 |
|------|----------|
| Python | 3.11 이상 |
| uv | 최신 버전 |
| Docker | 20.10 이상 (샌드박스 실행용) |
| Slack Workspace | 관리자 권한 필요 |
| Anthropic API | 키 발급 필요 |

---

## 초기 세팅

### 1. uv 설치

```powershell
# Windows PowerShell
powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
```

```bash
# macOS / Linux
curl -LsSf https://astral.sh/uv/install.sh | sh
```

### 2. 저장소 클론 및 의존성 설치

```bash
cd c:\harness\agentforge   # 또는 프로젝트 디렉토리

uv sync                    # 의존성 설치 (lock 파일 기준)
uv sync --dev              # 개발 의존성 포함 (pytest 등)
```

### 3. 환경변수 파일 생성

```bash
cp .env.example .env
```

`.env` 파일을 열어 필수 값을 채운다 (아래 [환경변수 설정](#환경변수-설정) 참조).

### 4. 초기 실행 확인

```bash
uv run agentforge --help
```

```
 Usage: agentforge [OPTIONS] COMMAND [ARGS]...

 AgentForge - Agent-based development team orchestrator

 Commands:
   start   Start the AgentForge Slack bot.
   status  Show active sessions from the checkpoint database.
   resume  Resume a paused (L4) session from its checkpoint.
```

첫 실행 시 `AGENTS.md`와 `memory/` 디렉토리가 자동 생성된다.

---

## Slack 앱 설정

AgentForge는 Slack Socket Mode로 동작한다.

### 1. Slack App 생성

1. [api.slack.com/apps](https://api.slack.com/apps) -> **Create New App** -> **From scratch**
2. App Name: `AgentForge`, Workspace 선택 후 생성

### 2. 권한(OAuth Scopes) 설정

**OAuth & Permissions** -> **Bot Token Scopes** 에 추가:

| Scope | 필수 여부 | 용도 |
|-------|-----------|------|
| `app_mentions:read` | **필수** | @AgentForge 멘션 수신 |
| `chat:write` | **필수** | 메시지 전송 |
| `chat:write.customize` | **필수** | 메시지 수정 |
| `channels:history` | **필수** | 채널 메시지 읽기 |
| `im:history` | 권장 | DM 메시지 읽기 |
| `channels:read` | 권장 | 시작 시 참여 채널 목록 진단 출력 |
| `groups:read` | 권장 | 프라이빗 채널 진단 출력 |

> **스코프 변경 후에는 반드시 앱을 재설치해야 한다.**  
> **Install App** -> **Reinstall to Workspace**

### 3. 이벤트 구독 설정

**Event Subscriptions** -> **Enable Events** -> On  
**Subscribe to bot events** 에 추가:

| 이벤트 | 용도 |
|--------|------|
| `app_mention` | **필수** — 채널에서 @AgentForge 멘션 수신 |
| `message.channels` | 권장 — 일반 메시지 수신 (채널 진단용) |

> **주의**: 이벤트 구독이 없으면 멘션을 해도 봇이 응답하지 않는다.

### 4. Interactivity 설정

**Interactivity & Shortcuts** -> **Interactivity** -> On  
(Socket Mode 사용 시 Request URL 불필요 — 자동 처리)

### 5. Socket Mode 활성화

**Socket Mode** -> **Enable Socket Mode** -> On  
**App-Level Token** 생성:
- Token Name: `agentforge-socket`
- Scope: `connections:write`
- 생성된 토큰 복사 -> `.env`의 `SLACK_APP_TOKEN`

### 6. 앱 설치 및 토큰 복사

**Install App** -> **Install to Workspace**  
- **Bot User OAuth Token** (`xoxb-...`) -> `SLACK_BOT_TOKEN`
- **Signing Secret** (Basic Information 탭) -> `SLACK_SIGNING_SECRET`

### 7. 채널에 봇 초대 (필수)

앱 설치만으로는 채널에 봇이 추가되지 않는다.  
사용할 채널에서 다음 중 하나를 실행한다:

```
/invite @agentforge
```

또는 **채널 이름 클릭 -> 통합 -> 앱 추가 -> AgentForge** 선택.

> 초대 후 `uv run agentforge start` 시 로그에 참여 채널 목록이 표시된다.  
> 목록이 비어 있으면 초대가 되지 않은 것이다.

### 시작 시 진단 로그 예시

정상 설정 시:
```
------------------------------------------------------------
AgentForge Slack Bot 시작됨
  봇 이름  : @agentforge
  봇 ID    : B0XXXXXXX  (User ID: U0XXXXXXX)
  워크스페이스: My Workspace
  멘션 형식 : <@U0XXXXXXX>
  참여 채널 (1개):
    #dev-team  (id=C0XXXXX, members=5)
  수신 이벤트 : app_mention
------------------------------------------------------------
```

`channels:read` 스코프 없을 때 (봇 동작엔 무관):
```
  참여 채널 조회 불가 (channels:read 스코프 없음)
  -> Slack App 설정 > OAuth > Bot Scopes 에 channels:read 추가 후 재설치
  채널 초대 방법: 해당 채널에서 /invite @agentforge 입력
```

모든 수신 이벤트를 보려면 `AF_LOG_LEVEL=DEBUG` 로 시작한다:
```bash
AF_LOG_LEVEL=DEBUG uv run agentforge start
# [Slack] 수신: type=app_mention channel=C0XXXXX
# [Slack] 수신: type=message/bot_message channel=C0XXXXX
```

---

## Docker 샌드박스 설정

Worker 에이전트가 코드를 실행할 때 Docker 컨테이너를 사용한다.  
네트워크가 차단된 격리 환경에서 실행된다 (`--network=none`).

### 1. Docker 설치 확인

```bash
docker --version
docker run --rm hello-world
```

### 2. 샌드박스 이미지 준비

기본 이미지는 `python:3.11-slim`이다.  
프로젝트에서 특정 패키지가 필요한 경우 커스텀 이미지를 빌드한다:

```dockerfile
# sandbox/Dockerfile
FROM python:3.11-slim
RUN pip install pytest numpy pandas requests
WORKDIR /workspace
```

```bash
docker build -t agentforge-sandbox ./sandbox/
```

커스텀 이미지 사용 시 `.env` 설정:

```env
AF_SANDBOX_IMAGE=agentforge-sandbox
```

### 3. 샌드박스 동작 확인

```bash
docker run --rm --network=none --memory=512m --cpus=1 \
  python:3.11-slim python -c "print('sandbox ok')"
```

---

## 환경변수 설정

`.env` 파일에 모든 설정을 관리한다.

```env
# ============================================================
# Anthropic API
# ============================================================
ANTHROPIC_API_KEY=sk-ant-...

# ============================================================
# LangSmith 트레이싱 (선택 사항)
# ============================================================
LANGCHAIN_TRACING_V2=true
LANGCHAIN_API_KEY=ls__...
LANGCHAIN_PROJECT=agentforge

# ============================================================
# Slack
# ============================================================
SLACK_BOT_TOKEN=xoxb-...
SLACK_APP_TOKEN=xapp-...
SLACK_SIGNING_SECRET=...

# ============================================================
# 모델 설정 (기본값 사용 권장)
# ============================================================
AF_LEADER_MODEL=claude-opus-4-7
AF_ORCHESTRATOR_MODEL=claude-sonnet-4-6
AF_WORKER_MODEL=claude-haiku-4-5-20251001

# ============================================================
# 컨텍스트 임계값
# ============================================================
AF_CONTEXT_COMPRESS_PCT=0.70   # 70% 초과 시 완료 태스크 요약 압축
AF_CONTEXT_SPAWN_PCT=0.90      # 90% 초과 시 SubOrchestrator 위임

# ============================================================
# Docker 샌드박스
# ============================================================
AF_SANDBOX_IMAGE=python:3.11-slim
AF_SANDBOX_MEMORY=512m
AF_SANDBOX_CPUS=1

# ============================================================
# 데이터베이스 (체크포인트)
# ============================================================
AF_DB_PATH=agentforge.db

# ============================================================
# 개발 / 테스트
# ============================================================
AF_MOCK_MODE=false     # true 로 설정하면 모든 API 호출을 건너뜀
AF_LOG_LEVEL=INFO
```

**최소 필수 설정** (Slack 없이 CLI + Python API만 사용):

```env
ANTHROPIC_API_KEY=sk-ant-...
```

---

## 실행

### 프로덕션 -- Slack Bot 시작

```bash
uv run agentforge start
```

### 개발 / 테스트 -- Mock 모드

API 키 없이 전체 그래프 흐름을 검증한다.

```bash
AF_MOCK_MODE=true uv run agentforge start --mock
```

### 백그라운드 실행 (Linux / macOS)

```bash
nohup uv run agentforge start > logs/agentforge.log 2>&1 &
echo $! > agentforge.pid
```

종료:

```bash
kill $(cat agentforge.pid)
```

### 테스트 실행

```bash
uv run pytest tests/unit/ -v          # 단위 테스트 (83개)
uv run pytest tests/integration/ -v   # 통합 테스트 (16개)
uv run pytest tests/ -v               # 전체 (99개)
```

---

## 사용법

### Slack에서 요청하기

AgentForge가 설치된 채널에 봇을 초대한 후 멘션으로 요청한다:

```
/invite @AgentForge
```

```
@AgentForge JWT 인증 시스템을 구현해줘.
사용자 로그인/로그아웃, 토큰 갱신, 미들웨어까지 포함해야 해.
```

AgentForge가 자동으로:
1. 요구사항 분석 후 태스크 DAG 구성
2. 각 태스크를 적절한 에이전트에 배분 (Opus/Sonnet/Haiku)
3. 실행 중 진행 상황을 스레드에 업데이트
4. CI 검증 (파일 존재 여부, 테스트 결과) + 시맨틱 검증 (acceptance_criteria 항목별 PASS/FAIL)
5. 완료 보고 전달

### 실행 중 상태 확인

```bash
uv run agentforge status
```

### L4 에스컬레이션 -- 사용자 개입

5회 연속 실패 시 Slack에 버튼이 표시된다:

```
[계속 진행]   [중단]
```

- **계속 진행**: 에이전트가 다른 방향으로 재시도
- **중단**: 세션 종료, 현재까지 완료된 결과 보고

### 중단된 세션 재개

```bash
uv run agentforge status              # session-id 확인
uv run agentforge resume <session-id>
uv run agentforge resume <session-id> --choice abort  # 중단으로 처리
```

---

## 워크플로우 커스터마이징

### 기본 제공 템플릿

| 파일 | 용도 |
|------|------|
| `workflows/templates/feature_dev.yaml` | 신규 기능 개발 (설계->구현->테스트->리뷰) |
| `workflows/templates/bug_fix.yaml` | 버그 수정 (진단->수정->회귀 테스트) |

### 커스텀 워크플로우 작성

`workflows/templates/` 아래에 YAML 파일을 추가한다:

```yaml
name: api_integration
version: "1.0"
tasks:
  - id: spec_analysis
    title: "API 스펙 분석"
    model_tier: sonnet        # opus / sonnet / haiku
    timeout_minutes: 20
    acceptance_criteria:
      - "엔드포인트 목록이 정리되었다"
      - "인증 방식이 파악되었다"

  - id: client_impl
    title: "클라이언트 구현"
    model_tier: haiku
    timeout_minutes: 45
    depends_on: [spec_analysis]
    acceptance_criteria:
      - "클라이언트 코드가 생성되었다"
      - "기본 연결 테스트 통과"
```

**모델 티어 선택 가이드:**

| 티어 | 모델 | 적합한 태스크 |
|------|------|---------------|
| `opus` | claude-opus-4-7 | 아키텍처 설계, 코드 리뷰, 시맨틱 검증 |
| `sonnet` | claude-sonnet-4-6 | 중간 복잡도 구현, 서브오케스트레이터 |
| `haiku` | claude-haiku-4-5 | 단순 구현, 텍스트 변환, 반복 작업 |

### Python API로 직접 실행

```python
import asyncio
from agentforge.core.models import WorkflowSpec, TaskSpec
from agentforge.core.state import make_initial_state
from workflows.builder import GraphBuilder

async def run():
    spec = WorkflowSpec(
        name="my_workflow",
        tasks=[
            TaskSpec(id="t1", acceptance_criteria=["파일 생성됨"]),
            TaskSpec(id="t2", depends_on=["t1"], acceptance_criteria=["테스트 통과"]),
        ],
    )
    state = make_initial_state(session_id="my-session", user_request="내 요청")
    state["workflow_spec"] = spec

    graph = GraphBuilder().from_spec(spec, with_checkpointer=False)
    result = await graph.ainvoke(
        state,
        config={"configurable": {"thread_id": "my-session"}},
    )
    print(result["final_report"])

asyncio.run(run())
```

---

## 에스컬레이션 프로토콜

태스크 실패 시 자동으로 에스컬레이션 레벨이 상승한다.

| 레벨 | 트리거 | 동작 | 사용자 개입 |
|------|--------|------|-------------|
| L0 | 1차 실패 | 동일 에이전트로 재시도 | 불필요 |
| L1 | 2차 실패 | 새 에이전트 스폰 후 재시도 | 불필요 |
| L2 | 3차 실패 | 모델 티어 업그레이드 (Haiku->Sonnet->Opus) | 불필요 |
| L3 | 4차 실패 | 해당 태스크 의존 항목 BLOCKED, 나머지 계속 진행 | 불필요 |
| L4 | 5차 실패 | Slack 버튼으로 사용자에게 전달 | **필요** |

L3까지는 완전 자율 동작하며, L4에서만 사람이 개입한다.

---

## ObserverAgent -- 자기 개선 루프

AgentForge는 실행 기록을 분석해 스스로 개선 제안을 생성하고,  
수락 시 자신의 설정/코드를 수정한다.

### 일지 확인

모든 세션 실행 기록은 `memory/journal/` 에 자동 저장된다:

```
memory/
├── journal/
│   ├── 2026-04-21_session_3f2a1b.md   # 세션별 실행 기록
│   └── 2026-04-21_complaints.md        # 불만/피드백 기록
├── retrospectives/                     # 주기적 회고 요약
└── proposals/
    ├── pending/                        # 검토 대기 중 제안서
    └── applied/                        # 수락 및 적용된 제안서
```

### 개선 제안 트리거 조건

| 조건 | 방식 |
|------|------|
| 특정 태스크 실패율 > 30% | 자동 분석 |
| Slack에서 불만 표현 ("느려", "왜", "버그", "문제" 등) | 즉시 분석 |
| 10개 세션 완료마다 | 정기 분석 |

### 제안서 수동 적용 (Slack 없이)

```bash
# 대기 중인 제안서 확인
ls memory/proposals/pending/
cat memory/proposals/pending/proposal_007.md

# Config 변경 (YAML 등) -- 재시작 불필요
# 제안서 내용에 따라 workflows/templates/*.yaml 을 직접 수정

# 코드 변경 -- git 브랜치 병합 후 재시작
git merge self-improve/proposal-007
uv run agentforge start
```

---

## 운영 모니터링

### LangSmith 트레이싱

`.env`에 LangSmith 설정이 있으면 모든 에이전트 실행이 자동 트레이싱된다.  
[smith.langchain.com](https://smith.langchain.com) 에서 확인:
- 노드별 입/출력 및 실행 시간
- 토큰 사용량 및 비용 추정
- 에러 트레이스

### 로그 레벨 조정

```env
AF_LOG_LEVEL=DEBUG   # DEBUG / INFO / WARNING / ERROR
```

### 체크포인트 데이터베이스

모든 세션 상태는 `agentforge.db` (SQLite)에 저장된다.

```bash
uv run agentforge status   # 저장된 세션 목록

# DB 직접 조회
sqlite3 agentforge.db \
  "SELECT thread_id, checkpoint_id FROM checkpoints ORDER BY rowid DESC LIMIT 10;"
```

---

## 트러블슈팅

### API 키 없이 기능을 확인하고 싶다

```bash
AF_MOCK_MODE=true uv run agentforge start --mock
AF_MOCK_MODE=true uv run pytest tests/ -v
```

### Docker를 사용할 수 없는 환경

`AF_MOCK_MODE=true` 설정 시 Docker 실행도 자동으로 모킹된다.  
실제 코드 실행이 필요한 경우 `agentforge/sandbox/docker_executor.py`의  
`run_command()` 메서드를 로컬 subprocess로 교체할 수 있다.

### Slack 봇이 응답하지 않는다

1. Socket Mode 토큰이 `xapp-` 로 시작하는지 확인
2. Bot Token이 `xoxb-` 로 시작하는지 확인
3. 채널에 봇이 초대되었는지 확인 (`/invite @AgentForge`)
4. 상세 로그 확인: `AF_LOG_LEVEL=DEBUG uv run agentforge start`

### `langgraph.checkpoint.sqlite` 모듈 오류

```bash
uv add langgraph-checkpoint-sqlite
```

### 세션이 중간에 끊겼다

```bash
uv run agentforge status
uv run agentforge resume <session-id>
```

### 테스트가 실패한다

```bash
AF_MOCK_MODE=true uv run pytest tests/ -v
```

모든 테스트는 mock 모드로 실행된다. API 키 없이도 99개 전부 통과해야 정상이다.

---

## 디렉토리 구조

```
agentforge/
├── AGENTS.md                    # 에이전트 공유 행동 규범 (자동 생성)
├── pyproject.toml
├── .env                         # 환경변수 (git 제외 권장)
│
├── workflows/
│   ├── templates/
│   │   ├── feature_dev.yaml     # 기능 개발 템플릿
│   │   └── bug_fix.yaml         # 버그 수정 템플릿
│   └── builder.py               # WorkflowSpec -> LangGraph 변환
│
├── memory/                      # 운영 기억 (자동 생성)
│   ├── journal/                 # 세션별 실행 일지
│   ├── retrospectives/          # 회고 요약
│   └── proposals/               # 개선 제안서
│
├── agentforge/
│   ├── main.py                  # CLI 진입점
│   ├── core/                    # 스키마, 상태, 체크포인트, 레지스트리
│   ├── agents/                  # Leader, Worker, SubOrchestrator
│   ├── graph/                   # LangGraph 노드 및 조건부 엣지
│   ├── sandbox/                 # Docker 격리 실행
│   ├── tools/                   # @tool 데코레이터 (Worker 도구)
│   ├── interfaces/              # Slack Bot (astream_events + Block Kit)
│   ├── observer/                # Historian, Retrospective, SelfImprove
│   └── verification/            # CI 레이어, 시맨틱 검증 레이어
│
└── tests/
    ├── unit/                    # 83개 단위 테스트
    └── integration/             # 16개 통합 테스트
```
