# 카드사용내역분석 에이전트

## 목표
한달치 카드 csv/excel file 입력 - 평균소비/주 사용처/과소비 top3/절약제안이 담긴 .docx리포트 1건 생성. 모든 분석은 로컬에서만. 외부API에는 PII•가맹점•금액 전송금지.

## 적용할 specs(specs/폴더는 없으면 즉시 생성)
system_prompt.md
retry_policy.md
guardrails.md
memory_policy.md
architecture.md

## 입출력
- 입력 : harness-book-samples/card-insight안의 엑셀파일
- 출력 : output/card-report/{YYYYMMDD}_report.docx

## 핵심원칙(절대규칙)
- PII마스킹 : 이름•카드번호•전화번호 자동가림
- 외부전송금지 : 가맹점명•금액•날짜는 LLM호출시에도 익명화 토큰으로 치환
- 단정금지 : "OO 끊어!" --> "OO 카테고리 지출이 전월대비 X% 증가. 검토권장"

## 운영한도
- 한 요청당 토큰 40000 , 시간 60초

## 동작모드 라우팅(필수)
- 자연어요청은 자기지식으로 답하지 말고 무조건:
(1) agents/card-insight/main.py {요청}
(2) 결과파일경로 + 핵심3줄(총 사용액•과소비top3•절약제안요약) 표시
(3) 면책문구를 답변끝에 그대로 표시
예외: "코드수정"•"파일/스펙 작성•수정지시"•"구조설명" -- 이때만 라우팅 건너뜀
