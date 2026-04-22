# AgentForge

에이전트로 구성된 AI 개발 팀 오케스트레이션 시스템.  
자연어 요구사항을 받아 작업을 분해하고, Anthropic 모델로 구성된 에이전트 팀이 자율적으로 실행·검증·에스컬레이션한다.

---

## 목차

1. [시스템 요구사항](#시스템-요구사항)
2. [초기 세팅](#초기-세팅)
3. [Slack 앱 설정](#slack-앱-설정)
4. [환경변수 설정](#환경변수-설정)
5. [실행](#실행)
6. [사용법](#사용법)
7. [Workspace & 형상관리](#workspace--형상관리)
8. [Docker 샌드박스 (선택)](#docker-샌드박스-선택)
9. [워크플로우 커스터마이징](#워크플로우-커스터마이징)
10. [에스컬레이션 프로토콜](#에스컬레이션-프로토콜)
11. [ObserverAgent — 자기 개선 루프](#observeragent--자기-개선-루프)
12. [세션 관리](#세션-관리)
13. [운영 모니터링](#운영-모니터링)
14. [트러블슈팅](#트러블슈팅)
15. [디렉토리 구조](#디렉토리-구조)

---

## 시스템 요구사항

| 항목 | 요구사항 |
|------|----------|
| Python | 3.11 이상 |
| uv | 최신 버전 |
| Git | 2.x 이상 (결과물 형상관리) |
| Docker | 20.10 이상 (선택 — 테스트 샌드박스) |
| Slack Workspace | 관리자 권한 필요 |
| Anthropic API | [console.anthropic.com](https://console.anthropic.com) 에서 키 발급 |

> **Anthropic 크레딧 주의**: claude.ai 결제와 API 크레딧은 별개 시스템이다.  
> API 키는 반드시 **console.anthropic.com** 의 계정에 크레딧이 있어야 한다.

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

### 2. 의존성 설치

```bash
cd agentforge
uv sync          # 의존성 설치
uv sync --dev    # 개발 의존성 포함 (pytest 등)
```

### 3. 환경변수 파일 생성

```bash
cp .env.example .env
# .env 파일을 열어 필수 값 입력 (아래 환경변수 설정 참조)
```

### 4. 초기 실행 확인

```bash
uv run agentforge --help
```

```
 Commands:
   start    Start the AgentForge Slack bot.
   status   List all sessions with step count and latest checkpoint.
   session  Show detailed checkpoint history for a session.
   kill     Delete a session and all its checkpoints from the database.
   resume   Resume a paused (L4) session from its checkpoint.
```

첫 실행 시 `AGENTS.md`, `memory/`, `workspace/` 디렉토리가 자동 생성된다.

---

## Slack 앱 설정

AgentForge는 Slack Socket Mode로 동작한다.

### 1. Slack App 생성

1. [api.slack.com/apps](https://api.slack.com/apps) → **Create New App** → **From scratch**
2. App Name: `AgentForge`, Workspace 선택 후 생성

### 2. 권한(OAuth Scopes) 설정

**OAuth & Permissions** → **Bot Token Scopes** 에 추가:

| Scope | 필수 여부 | 용도 |
|-------|-----------|------|
| `app_mentions:read` | **필수** | @AgentForge 멘션 수신 |
| `chat:write` | **필수** | 메시지 전송 |
| `chat:write.customize` | **필수** | 메시지 수정 |
| `channels:history` | **필수** | 채널 메시지 읽기 (스레드 답글 수신) |
| `groups:history` | **필수** | 프라이빗 채널 스레드 답글 수신 |
| `im:history` | 권장 | DM 메시지 읽기 |
| `channels:read` | 권장 | 시작 시 참여 채널 목록 진단 출력 |
| `groups:read` | 권장 | 프라이빗 채널 진단 출력 |

> **스코프 변경 후에는 반드시 앱을 재설치해야 한다.**  
> **Install App** → **Reinstall to Workspace**

### 3. 이벤트 구독 설정

**Event Subscriptions** → **Enable Events** → On  
**Subscribe to bot events** 에 추가:

| 이벤트 | 필수 여부 | 용도 |
|--------|-----------|------|
| `app_mention` | **필수** | @AgentForge 멘션 수신 |
| `message.channels` | **필수** | 요구사항 대화 스레드 답글 수신 |
| `message.groups` | 권장 | 프라이빗 채널 스레드 답글 수신 |

> **주의**: `message.channels` 없으면 요구사항 대화 중 사용자 답변을 수신하지 못한다.

### 4. Interactivity 설정

**Interactivity & Shortcuts** → **Interactivity** → On  
(Socket Mode 사용 시 Request URL 불필요 — 자동 처리)

### 5. Socket Mode 활성화

**Socket Mode** → **Enable Socket Mode** → On  
**App-Level Token** 생성:
- Token Name: `agentforge-socket`
- Scope: `connections:write`
- 생성된 토큰 복사 → `.env`의 `SLACK_APP_TOKEN`

### 6. 앱 설치 및 토큰 복사

**Install App** → **Install to Workspace**
- **Bot User OAuth Token** (`xoxb-...`) → `SLACK_BOT_TOKEN`
- **Signing Secret** (Basic Information 탭) → `SLACK_SIGNING_SECRET`

### 7. 채널에 봇 초대 (필수)

앱 설치만으로는 채널에 봇이 추가되지 않는다.  
사용할 채널에서 실행:

```
/invite @agentforge
```

또는 **채널 이름 클릭 → 통합 → 앱 추가 → AgentForge** 선택.

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
  수신 이벤트 : app_mention, message (thread replies)
------------------------------------------------------------
```

상세 이벤트 로그 확인:
```bash
AF_LOG_LEVEL=DEBUG uv run agentforge start
# [Slack] 수신: type=app_mention channel=C0XXXXX
# [Slack] 수신: type=message channel=C0XXXXX
```

---

## 환경변수 설정

```env
# ============================================================
# Anthropic API (console.anthropic.com 에서 발급)
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
# Workspace (결과물 저장 디렉토리)
# ============================================================
AF_WORKSPACE_DIR=workspace     # 세션별 결과물 저장 루트

# ============================================================
# Docker 샌드박스 (선택 — 테스트 실행용)
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

**최소 필수 설정:**

```env
ANTHROPIC_API_KEY=sk-ant-...
SLACK_BOT_TOKEN=xoxb-...
SLACK_APP_TOKEN=xapp-...
SLACK_SIGNING_SECRET=...
```

---

## 실행

### 프로덕션 — Slack Bot 시작

```bash
uv run agentforge start
```

### 개발 / 테스트 — Mock 모드

API 키 없이 전체 그래프 흐름과 파일 생성을 검증한다.

```bash
uv run agentforge start --mock
```

Mock 모드에서는 실제 API 호출 없이 `workspace/{session_id}/` 에 샘플 파일이 생성되고 git 커밋까지 수행된다.

### 테스트 실행

```bash
uv run pytest tests/unit/ -v
uv run pytest tests/integration/ -v
uv run pytest tests/ -v
```

---

## 사용법

### Slack 요청 흐름

AgentForge는 요청을 즉시 처리하지 않고 **요구사항 대화를 먼저 진행**한다.

```
사용자: @AgentForge JWT 인증 시스템 구현해줘

AgentForge: 백엔드 언어/프레임워크는 어떻게 하시겠어요?

사용자: FastAPI + Python

AgentForge: 토큰 만료 시간이나 리프레시 토큰 정책이 있으신가요?

사용자: 액세스 1시간, 리프레시 7일

AgentForge: [요구사항 정리가 완료됐습니다]
  - FastAPI + Python JWT 인증 시스템
  - 액세스 토큰 1시간, 리프레시 7일
  - 로그인/로그아웃/토큰 갱신 엔드포인트
  이대로 진행할까요?  [진행]  [취소]

사용자: [진행] 클릭

AgentForge: 진행합니다. 세션 ID: `3f2a1b8c`
            `refine_requirements` 실행 중...
            `dispatch_workers` 실행 중...
            ...
            작업 완료
            작업 디렉토리: workspace/3f2a1b8c-...
```

**대화 규칙:**
- 봇이 질문하면 스레드에 답변 (멘션 없이 일반 텍스트도 인식)
- 2~5번 문답 후 충분한 요구사항이 파악되면 자동으로 확인 단계로 전환
- [진행] 버튼 클릭 후에만 실제 작업이 시작되고 세션이 생성됨

### 실행 결과 확인

```bash
# 세션 목록
uv run agentforge status

# 결과물 확인
ls workspace/<session-id>/
git -C workspace/<session-id>/ log --oneline
```

### L4 에스컬레이션 — 사용자 개입

자동 해결 한계 도달 시 Slack에 버튼이 표시된다:

```
[계속 진행]   [중단]
```

### 중단된 세션 재개

```bash
uv run agentforge status
uv run agentforge resume <session-id>
uv run agentforge resume <session-id> --choice abort
```

---

## Workspace & 형상관리

### 구조

세션마다 독립된 git 저장소가 생성된다:

```
workspace/
└── {session-id}/
    ├── .git/           ← 자동 초기화
    ├── README.md       ← 초기 커밋 (자동)
    ├── src/            ← Worker가 생성한 소스 코드
    └── tests/          ← Worker가 생성한 테스트 코드
```

### Worker 동작 방식

Worker 에이전트는 태스크 수행 후 **파일 내용을 포함한** JSON을 반환한다:

```json
{
  "status": "completed",
  "files": [
    {"path": "src/auth.py", "content": "...전체 파일 내용..."},
    {"path": "tests/test_auth.py", "content": "..."}
  ],
  "evidence": {"tests_passed": 5, "tests_failed": 0},
  "summary": "JWT 인증 모듈 구현 완료"
}
```

파일은 `workspace/{session-id}/` 에 저장되고, 태스크 완료마다 자동 커밋된다:

```
feat(auth_impl): JWT 인증 모듈 구현 완료
feat(test_suite): 인증 테스트 5개 작성 완료
```

### git 작업

```bash
# 커밋 이력 확인
git -C workspace/<session-id>/ log --oneline

# 변경 내용 확인
git -C workspace/<session-id>/ diff HEAD~1

# 특정 파일 내용
cat workspace/<session-id>/src/auth.py
```

### 테스트 실행 (CI 단계)

CI 검증 시 `workspace/{session-id}/tests/` 에 테스트가 있으면 자동 실행된다:

1. **Docker 우선**: `python:3.11-slim` 컨테이너에서 `pytest` 실행 (네트워크 차단)
2. **Docker 없으면 로컬**: `python -m pytest` 로 직접 실행

테스트 결과는 CI 검증 결과에 반영되며, 실패 시 에스컬레이션 트리거가 된다.

---

## Docker 샌드박스 (선택)

Docker는 테스트 실행의 격리 환경으로 사용된다. **없어도 AgentForge가 동작**하며, Docker가 없으면 로컬에서 테스트를 실행한다.

### 설치 확인

```bash
docker --version
docker run --rm hello-world
```

### 커스텀 이미지 (특정 패키지 필요 시)

```dockerfile
# sandbox/Dockerfile
FROM python:3.11-slim
RUN pip install pytest numpy pandas requests
WORKDIR /workspace
```

```bash
docker build -t agentforge-sandbox ./sandbox/
```

`.env` 에 설정:
```env
AF_SANDBOX_IMAGE=agentforge-sandbox
```

### 동작 확인

```bash
docker run --rm --network=none --memory=512m --cpus=1 \
  -v $(pwd)/workspace/<session-id>:/workspace \
  python:3.11-slim \
  sh -c "pip install pytest -q && pytest /workspace/tests/ -v"
```

---

## 워크플로우 커스터마이징

### 기본 제공 템플릿

| 파일 | 용도 |
|------|------|
| `workflows/templates/feature_dev.yaml` | 신규 기능 개발 |
| `workflows/templates/bug_fix.yaml` | 버그 수정 |

### 커스텀 워크플로우 작성

```yaml
name: api_integration
tasks:
  - id: spec_analysis
    title: "API 스펙 분석"
    model_tier: sonnet        # opus / sonnet / haiku
    timeout_minutes: 20
    acceptance_criteria:
      - "엔드포인트 목록이 정리되었다"

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

---

## 에스컬레이션 프로토콜

| 레벨 | 트리거 | 동작 | 사용자 개입 |
|------|--------|------|-------------|
| L0 | 1차 실패 | 동일 에이전트로 재시도 | 불필요 |
| L1 | 2차 실패 | 새 에이전트 스폰 후 재시도 | 불필요 |
| L2 | 3차 실패 | 모델 티어 업그레이드 (Haiku→Sonnet→Opus) | 불필요 |
| L3 | 4차 실패 | 해당 태스크 BLOCKED, 나머지 계속 | 불필요 |
| L4 | 5차 실패 | Slack 버튼으로 사용자에게 전달 | **필요** |

---

## ObserverAgent — 자기 개선 루프

### 일지 확인

```
memory/
├── journal/          # 세션별 실행 기록 (자동)
├── retrospectives/   # 주기적 회고 요약
└── proposals/
    ├── pending/      # 검토 대기 중 제안서
    └── applied/      # 적용된 제안서
```

### 개선 제안 트리거

| 조건 | 방식 |
|------|------|
| 특정 태스크 실패율 > 30% | 자동 분석 |
| Slack에서 불만 표현 ("느려", "왜", "버그" 등) | 즉시 분석 |
| 10개 세션 완료마다 | 정기 분석 |

---

## 세션 관리

```bash
# 전체 세션 목록 (체크포인트 수, 마지막 노드)
uv run agentforge status

# 특정 세션 체크포인트 이력 상세 조회 (prefix 입력 가능)
uv run agentforge session 3f2a1b

# 세션 삭제 (확인 프롬프트)
uv run agentforge kill 3f2a1b

# 확인 없이 삭제
uv run agentforge kill 3f2a1b --yes

# L4 대기 중인 세션 재개
uv run agentforge resume 3f2a1b
uv run agentforge resume 3f2a1b --choice abort
```

> 세션 삭제는 DB의 체크포인트만 제거한다.  
> `workspace/{session-id}/` 의 결과물 파일은 별도로 삭제해야 한다.

---

## 운영 모니터링

### LangSmith 트레이싱

`.env`에 LangSmith 설정이 있으면 모든 에이전트 실행이 자동 트레이싱된다.  
[smith.langchain.com](https://smith.langchain.com) 에서 확인:
- 노드별 입/출력 및 실행 시간
- 토큰 사용량 및 비용 추정
- 에러 트레이스

### 로그 레벨

```env
AF_LOG_LEVEL=DEBUG   # DEBUG / INFO / WARNING / ERROR
```

### 체크포인트 DB

```bash
uv run agentforge status
sqlite3 agentforge.db \
  "SELECT thread_id, checkpoint_id FROM checkpoints ORDER BY rowid DESC LIMIT 10;"
```

---

## 트러블슈팅

### API 크레딧 오류 (`credit balance is too low`)

claude.ai 결제와 API 크레딧은 별개 시스템이다.  
[console.anthropic.com](https://console.anthropic.com) → Billing → Add credits 에서 충전.  
API 키가 속한 계정과 크레딧이 충전된 계정이 일치하는지 확인한다.

### API 키 없이 기능 확인

```bash
uv run agentforge start --mock
uv run pytest tests/ -v
```

Mock 모드에서는 실제 API 없이 `workspace/` 에 파일이 생성되고 git 커밋까지 수행된다.

### Docker 없이 실행

Docker가 없어도 AgentForge는 정상 동작한다.  
테스트 실행이 로컬 `python -m pytest` 로 fallback되며, Docker 관련 오류는 발생하지 않는다.

### Slack 봇이 응답하지 않는다

1. Event Subscriptions에 `app_mention` + `message.channels` 모두 등록됐는지 확인
2. Socket Mode 토큰이 `xapp-` 로 시작하는지 확인
3. 채널에 봇이 초대됐는지 확인 (`/invite @agentforge`)
4. 상세 로그: `AF_LOG_LEVEL=DEBUG uv run agentforge start`

### 요구사항 대화 중 답변이 무시된다

`channels:history` 또는 `groups:history` 스코프가 없거나  
Event Subscriptions에 `message.channels`가 없을 수 있다.  
Slack App 설정 확인 후 앱 재설치.

### 세션이 중간에 끊겼다

```bash
uv run agentforge status
uv run agentforge resume <session-id>
```

### `AsyncSqliteSaver` 관련 오류

```bash
uv sync   # aiosqlite 패키지 설치 확인
```

---

## 디렉토리 구조

```
agentforge/
├── AGENTS.md                    # 에이전트 공유 행동 규범 (자동 생성)
├── pyproject.toml
├── agentforge.db                # 세션 체크포인트 (SQLite)
├── .env                         # 환경변수 (git 제외 권장)
│
├── workspace/                   # 세션별 결과물 (자동 생성)
│   └── {session-id}/
│       ├── .git/                # 태스크마다 자동 커밋
│       ├── src/                 # 생성된 소스 코드
│       └── tests/               # 생성된 테스트 코드
│
├── workflows/
│   ├── templates/
│   │   ├── feature_dev.yaml
│   │   └── bug_fix.yaml
│   └── builder.py
│
├── memory/                      # 운영 기억 (자동 생성)
│   ├── journal/
│   ├── retrospectives/
│   └── proposals/
│
├── agentforge/
│   ├── main.py                  # CLI (start/status/session/kill/resume)
│   ├── core/                    # 스키마, 상태, 체크포인트, 레지스트리
│   ├── agents/                  # Leader, Worker, SubOrchestrator, Clarifier
│   ├── graph/                   # LangGraph 노드 및 조건부 엣지
│   ├── workspace/               # WorkspaceManager (git, 파일 I/O, 테스트 실행)
│   ├── sandbox/                 # Docker 격리 실행
│   ├── interfaces/              # Slack Bot (astream_events + Block Kit)
│   ├── observer/                # Historian, Retrospective, SelfImprove
│   └── verification/            # CI 레이어, 시맨틱 검증 레이어
│
└── tests/
    ├── unit/
    └── integration/
```
