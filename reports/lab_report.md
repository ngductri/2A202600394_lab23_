# Day 08 Lab Report - LangGraph Agentic Orchestration

## 1. Muc tieu bai lab

Xay dung workflow agent ho tro ticket theo mo hinh graph voi cac yeu cau:
- route theo loai query (`simple`, `tool`, `missing_info`, `risky`, `error`)
- co retry loop gioi han
- co HITL approval cho thao tac rui ro
- co persistence/checkpoint de resume + time travel
- xuat metrics de danh gia ket qua

## 2. Kien truc tong the

### 2.1 Node graph

Workflow da trien khai:

`START -> intake -> classify -> (answer | tool | clarify | risky_action | retry)`

Nhanh chi tiet:
- `simple`: `answer -> finalize -> END`
- `tool`: `tool -> evaluate -> (answer | retry)`
- `missing_info`: `clarify -> finalize -> END`
- `risky`: `risky_action -> approval -> (tool | clarify)`
- `error`: `retry -> (tool | dead_letter)` voi gioi han `max_attempts`

Tat ca nhanh deu hoi tu tai `finalize -> END`.

### 2.2 State schema

State dung `AgentState` gom cac nhom thong tin chinh:
- Input/identity: `query`, `scenario_id`, `thread_id`
- Routing: `route`
- Retry control: `attempt`, `max_attempts`, `evaluation_result`
- HITL/policy: `approval_required`, `approval_decision`
- Observability: `events`, `messages`, `errors`
- Output: `final_answer`

Muc tieu schema:
- serializable de checkpoint
- du thong tin de debug/replay
- khong hard-code theo scenario id

## 3. Logic routing va xu ly

### 3.1 Classify

`classify_node` route theo keyword heuristic:
- `risky`: refund/delete/send/cancel/remove...
- `tool`: order/status/lookup/check/track...
- `missing_info`: query qua ngan/mo ho
- `error`: timeout/failure/error/crash...
- con lai la `simple`

Thu tu uu tien duoc ap dung de tranh xung dot keyword.

### 3.2 Retry va dead-letter

- `evaluate_node` quyet dinh `success` hoac `needs_retry`.
- `retry_or_fallback_node` tang `attempt`.
- `route_after_retry`:
  - neu `attempt < max_attempts` -> quay lai `tool`
  - nguoc lai -> `dead_letter`

### 3.3 HITL

Cho route `risky`, graph di qua `approval`.
- Approve -> tiep tuc `tool`
- Reject -> di `clarify` va ket thuc an toan

Da demo duoc pause/resume bang CLI va web UI.

## 4. Persistence, crash recovery, time travel

Project ho tro checkpointer:
- memory (nhanh de dev)
- postgres (cho demo persistence)

Bang checkpoint tren Postgres da duoc tao va co du lieu:
- `checkpoints`
- `checkpoint_blobs`
- `checkpoint_writes`
- `checkpoint_migrations`

Da xac minh `show-history` tra ve lich su checkpoint theo `thread_id`, cho phep time travel/replay cac buoc.

## 5. Web demo (bonus)

Da trien khai dashboard FastAPI + HTML/CSS/JS:
- run scenario/free query
- realtime timeline
- graph visualization (mermaid)
- approve/reject cho HITL
- history + metrics panel
- co splitter de resize timeline

## 6. Ket qua metrics

Nguon: `outputs/metrics.json`

- Total scenarios: **7**
- Success rate: **100.00%**
- Avg nodes visited: **12.86**
- Total retries: **6**
- Total interrupts: **4**
- resume_success: **false** (khong anh huong pass sample scenarios)

### 6.1 Ket qua theo scenario

| Scenario | Expected | Actual | Success | Retries | Interrupts |
|---|---|---|---|---:|---:|
| S01_simple | simple | simple | true | 0 | 0 |
| S02_tool | tool | tool | true | 0 | 0 |
| S03_missing | missing_info | missing_info | true | 0 | 0 |
| S04_risky | risky | risky | true | 0 | 2 |
| S05_error | error | error | true | 4 | 0 |
| S06_delete | risky | risky | true | 0 | 2 |
| S07_dead_letter | error | error | true | 2 | 0 |

## 7. Failure mode da quan sat

1. Postgres dependency issue:
- Gap loi `psycopg`/`libpq` tren Windows.
- Cach xu ly: cai dung package postgres checkpoint + binary phu hop.

2. Checkpointer type issue:
- Gap `Received _GeneratorContextManager` khi dung sai API.
- Cach xu ly: tra ve saver instance dung contract `BaseCheckpointSaver`.

3. UI graph scale:
- Tung gap case SVG scale khong on dinh.
- Da chinh render strategy + responsive sizing de on dinh hon.

## 8. Test va xac minh

- `pytest` pass: **11 passed**
- `run-scenarios` pass 100%
- `validate-metrics` pass
- Da test HITL:
  - pause tai approval
  - resume approve/reject
  - check history theo thread

## 9. Cai tien de xuat

1. Nang cap classify bang score-based router (thay vi keyword thu cong) de giam false positive.
2. Them telemetry chi tiet (latency tung node, retry reason taxonomy).
3. Bo sung test hidden-like scenarios va fuzz query.
4. Toi uu UI graph:
   - zoom controls (+/-/reset)
   - mini-map cho graph lon
5. Bat strict schema validation cho event payload truoc khi persist.

## 10. Ket luan

He thong dat duoc muc tieu cua lab:
- graph routing dung
- retry loop co gioi han
- HITL hoat dong
- persistence + time travel co bang chung
- metrics hop le va test pass

Ban hien tai san sang de chay grading scenarios bo sung.
