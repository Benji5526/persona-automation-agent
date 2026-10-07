# SaaS Roadmap (보존, 구현 안 함)

Personal Edition이 안정된 뒤 다른 사용자에게 제공할 가능성이 있을 때만 이 로드맵을 연다. 구조는 [../architecture/saas.md](../architecture/saas.md), 원문은 PRD·TECH_DESIGN이다.

| 단계 | 내용 | 근거 |
|---|---|---|
| S0 | Personal 안정화: 첫 실제 게시 4주, 주 7개 (TECH 44.4 G3) | PRD 3.6 |
| S1 | 소유 모델 확장: `organization`·`membership`, RLS를 조직 기준으로 | TECH 36.2, 10.21 |
| S2 | 고객별 한도·플랜·과금 | TECH 15.18, 39장 |
| S3 | 워커 풀·GPU 풀, 공정 선점, 자동 시작·종료 | TECH 36.4, `GpuProvider` |
| S4 | 고객 온보딩, 관리자 화면, 운영 감시(Incident·SLO) | TECH 37·37-A |
| S5 | Multi-Persona 자율 운영, 실험·최적화 | PRD 4.4, 8.8, 8.9, TECH 34·35 |

각 단계는 요청이 있을 때 별도 설계로 시작한다.
