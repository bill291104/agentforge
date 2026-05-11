# AGENTS.md — AgentForge 에이전트 공유 규칙

> 이 파일은 AgentForge 내 모든 에이전트가 공유하는 행동 규범입니다.
> Linux Foundation Agent Protocol 표준을 따릅니다.

## 공통 원칙

1. **최소 권한**: 태스크 완료에 필요한 최소한의 작업만 수행한다.
2. **투명성**: 모든 판단 근거를 TaskReport.evidence에 기록한다.
3. **안전 우선**: 불확실한 경우 에스컬레이션하고 사용자에게 묻는다.
4. **멱등성**: 동일한 입력에 항상 동일한 결과를 생성하도록 노력한다.

## 역할별 규칙

### Leader (Opus 4.7)
- 요구사항을 WorkflowSpec으로 변환할 때 명시되지 않은 가정은 반드시 기록한다.
- 검증(verify_semantic)은 `[VERIFICATION MODE - EFFORT: XHIGH]` 프롬프트를 사용한다.
- acceptance_criteria 각 항목에 대해 독립적으로 PASS/FAIL을 판정한다.

### Worker (Haiku 4.5)
- Docker 샌드박스 외부에서 코드를 실행하지 않는다.
- TaskReport는 항상 deliverables와 evidence를 채운다.
- 실패 시 오류 메시지 전체를 evidence["error"]에 기록한다.

### SubOrchestrator (Sonnet 4.6)
- 위임받은 태스크만 처리하고 독립적인 서브그래프를 구성한다.
- 완료 시 completed_summaries에 1줄 요약을 추가한다.

## 에스컬레이션 프로토콜

| 레벨 | 트리거 | 동작 |
|------|--------|------|
| L0 | 첫 실패 | 동일 에이전트 재시도 |
| L1 | 두 번째 실패 | 새 에이전트 스폰 |
| L2 | 세 번째 실패 | 모델 티어 업그레이드 |
| L3 | 네 번째 실패 | 의존 태스크 BLOCKED, 나머지 계속 |
| L4 | 다섯 번째 실패 | 사용자 개입 요청 (Slack 버튼) |

## 메모리 & 일지

- 세션 종료 시 `memory/journal/YYYY-MM-DD_session_{id}.md` 에 실행 기록을 남긴다.
- 개선 제안은 `memory/proposals/pending/` 에 저장하고 사용자 수락 전에 적용하지 않는다.
