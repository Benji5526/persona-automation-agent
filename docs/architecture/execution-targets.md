# Execution Target — Local ↔ Cloud GPU

> 정본: [TECH_DESIGN 56장](../TECH_DESIGN.md) (56.2 모델, 56.3 pull, 56.4 DB, 56.5 능력 검사·배포, 56.6 보안). 이 문서는 읽기 쉬운 요약이다.

## 용어

| 용어 | 뜻 |
|---|---|
| **Execution Target** | 일을 실행하는 장소. `local`(내 PC) 또는 `cloud`(렌탈 GPU Pod) |
| **워커** | Target에서 도는 Python 브릿지 한 대 (`worker_status`의 한 행, 예 `python:local-1`) |
| **활성 워커** | `app_settings.active_worker`. 이 워커만 generation Job을 선점한다 (`null` = 제한 없음) |
| **GPU Provider** | 누가 GPU를 빌려주나 (`local`, `runpod`). 메타데이터 + 생명주기 어댑터 |

## 규칙

1. **GPU 모델을 코드·설정·문서 본문에 고정하지 않는다.** 모델명·VRAM은 워커가 `/system_stats`로 보고한다.
2. **Python 실행 인터페이스는 Target과 무관하다.** `POST /v1/jobs`, 선점·결과 등록 방식은 Local이든 Cloud든 같다.
3. **Frontend는 GPU 인프라를 모른다.** Lovable → Python → GPU가 아니라 Lovable → Supabase(RPC) → n8n/워커다. 주소·키·Pod ID는 DB·Frontend에 두지 않는다.
4. **ComfyUI는 어디서도 인터넷에 노출하지 않는다** (`127.0.0.1`).
5. 수동 [생성]과 자동 생성은 **같은 경로**를 쓴다 (Content Job → n8n → 활성 워커).

## 디스패치: pull

```text
브릿지 (5초마다): 놀고 있고 ComfyUI가 살아 있으면
   claim_next_automation_job('generation', WORKER_ID)
   → DB가 active_worker == WORKER_ID 일 때만 Job을 준다
```
- Cloud Pod는 **밖으로 나가는 연결만** 있으면 된다 (Supabase). 터널·Access·브릿지 토큰 노출이 필요 없다.
- push(`POST /v1/jobs`, WF-003)는 호환용으로 남는다. 같은 DB 선점을 거치므로 중복 실행이 없다.

## 전환

```text
설정: Execution Target ◉ Local ○ Cloud   →   app_settings.active_worker = 그 워커 id
```
- Job에 Target을 고정하지 않는다. 대기 중인 Job은 활성 워커가 바뀌면 그 워커가 가져간다.
- 활성 워커가 꺼져 있으면 Job은 `pending`으로 기다리고 화면이 "꺼져 있어요 · 대기 N건"을 보여준다.

## 능력 검사

Registry의 Workflow에 `requirements.min_vram_gb`(선택). 워커 VRAM이 모자라면 ComfyUI 호출 전에 `WORKFLOW_UNSUPPORTED_ON_TARGET`(재시도 없음). 이때 활성 워커를 바꾸고 [실패한 단계만 다시 실행]한다. 모델·LoRA는 Target마다 따로 설치하고(`worker_status.models`), 전환할 때 신원 LoRA가 없으면 경고한다.

## 설정 예 (워커 `.env`, 비밀이므로 DB에 두지 않는다)

```env
# Local (이 PC)
EXECUTION_TARGET=local
WORKER_ID=python:local-1
PULL_JOBS=true
COMFY_URL=http://127.0.0.1:8188

# Cloud (Runpod Pod 안)
EXECUTION_TARGET=cloud
WORKER_ID=python:runpod-1
PULL_JOBS=true
COMFY_URL=http://127.0.0.1:8188
SUPABASE_SECRET_KEY=<클라우드 전용 키, Pod 종료 후 폐기>
```

## 위험

- **렌탈 GPU에 `service_role` 키를 둔다**: 클라우드 전용 키를 따로 만들고 Pod 종료 즉시 폐기·교체한다.
- **켜 둔 Pod의 비용**: 지금은 수동 시작·종료. `idle_shutdown`은 인터페이스만 둔다.
- **LoRA·모델 불일치**: 가장 흔한 전환 실패. 경고만 하고 자동 동기화는 하지 않는다.
- **VRAM 차이**: 이 PC는 RTX 3070 8GB다. 큰 모델(예: 약 16GB의 `flux1-dev-fp8`)은 Cloud에서 돌린다.
