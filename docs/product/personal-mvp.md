# Personal MVP

범위의 정본은 **[../MVP_SCOPE_LOCK.md](../MVP_SCOPE_LOCK.md)** 이다 (링크가 깨지지 않도록 파일은 옮기지 않았다). 완료 체크는 [../MVP_CHECKLIST.md](../MVP_CHECKLIST.md), 구조는 [../architecture/personal.md](../architecture/personal.md), GPU 선택은 [../architecture/execution-targets.md](../architecture/execution-targets.md).

## 요약

```text
Login → Persona → Content Job → (활성 Execution Target: Local/Cloud GPU) → Python → ComfyUI → Asset
  → 사람 승인 → SNS → Analytics → GPT Decision → 새 Content Job
```

- 사용자 1명(OWNER). SaaS 기능은 구현하지 않는다.
- 수동 [생성]과 자동 생성은 같은 경로다 (Content Job → n8n → 활성 워커).
- 팬 상호작용, 실험·최적화, Multi-Persona 자율 운영은 MVP v1.0 밖이다.
