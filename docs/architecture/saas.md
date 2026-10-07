# SaaS Edition — 보존된 확장 설계

> **지금은 구현하지 않는다.** 이 문서는 PRD 1~8과 TECH_DESIGN 9~55의 SaaS 지향 내용을 **삭제하지 않고 보존**하며, Personal Edition에서 어디로 확장하는지를 가리킨다. 구현하려면 사용자가 명시적으로 요청하고 이 문서와 [TECH_DESIGN 56장](../TECH_DESIGN.md)을 먼저 갱신한다.

## 확장 구조 (미래)

```text
User → Organization → Tenant → Control Plane → Job Queue → Worker Pool → GPU Pool → ComfyUI Workers
```

## 확장 지점 (Personal 설계에 이미 남아 있는 것)

| 지점 | 지금 (Personal) | SaaS에서 |
|---|---|---|
| 소유 | `personas.user_id`, RLS `Persona → 소유자` | `organization_id`·`tenant_id`·`membership` 열 추가, 정책 확장 (TECH 36.2의 `persona_members`) |
| 역할 | `users.role` = `operator`/`admin` (OWNER ≡ admin) | 조직 역할 (OWNER·EDITOR·VIEWER) |
| 가입 | 허용 목록 1개 (24.3) | 초대·가입·플랜 |
| 한도 | `app_settings.limits` 전역 | 고객별 Quota, `limits_override`, `persona_platform_settings` (36.4·39) |
| GPU | `app_settings.active_worker` 하나 | 고객별 GPU 할당, 워커 풀·공정 선점 (36.4 V2) |
| 워커 | `worker_status` 등록부 + `target`·`provider` | 워커 풀 관리, 자동 시작·종료 (`GpuProvider.start/stop/idle_shutdown`) |
| 프로필 | `app_settings.app_mode = 'personal'` | `saas` 값과 `capabilities`로 화면 켜기 |

## 섹션 프로필 지도 (본문은 수정하지 않는다)

| 프로필 | 의미 | PRD / TECH_DESIGN |
|---|---|---|
| **ACTIVE** | Personal에서 지금 구현 | PRD 1~7 (MVP·V1), TECH 9~14, 15(핵심: RLS·비밀값·네트워크), 16~27, 28·29(V1 게시·성과), 41~52, 53·54(AI Decision, 권한 수준 1~2), 56 |
| **LATER** | Personal에서 V2 이후 | TECH 30~33(AI Decision·팬·자율 루프·권한 핵심), 31·55(팬 상호작용), 32(자율 루프) |
| **DEFERRED** | Personal에서 보류, SaaS·Long-term | PRD 4.4 Multi-Persona Operator, 8.8 Phase 7, 8.9 Phase 8; TECH 34·35(실험·최적화), 36(Multi-Persona), 37-A·38·39 중 Incident·SLO·추이 테이블·오프사이트 백업(Object Lock·주간 복원 검증)·금액 한도·비용 추정, 33.5의 정책 버전 관리 |

- DEFERRED는 문서에 남아 있고 `tests`·마이그레이션에는 아직 없다 (테이블도 만들지 않았다). 1인 운영에는 Dashboard·Error Center·간단한 알림·하루 DB 백업이면 충분하다 (TECH 37.1, 38.13 MVP).
- **만들지 않은 SaaS 테이블**: `organizations`, `memberships`, `subscriptions`, `persona_members`, `persona_platform_settings`, 고객별 한도 테이블.

자세한 로드맵: [../product/saas-roadmap.md](../product/saas-roadmap.md).
