# RTO/RPO Evidence — Lab 23

**Sinh viên:** Nguyễn Như Tài — MSSV 2A202602976

Quy tắc duy nhất: mỗi con số ở đây phải trỏ được về **một dòng log thật**
(`đường/dẫn.jsonl:số_dòng`). `pytest tests/test_rto_evidence.py` sẽ mở từng file ra kiểm tra.

Môi trường: bare mode trong WSL2 Ubuntu, `chaos/kill_region.py --mode netblock --mock`
(SIGSTOP), `WARMUP_SECONDS=6`, `EDGE_TTL_SECONDS=5`, loadgen 2 req/s, timeout 3s.
Mọi số "+giây" tính từ `ts` của sự kiện `action:kill` tương ứng.

## 1. Drill 1 — không có DR (baseline)

| Chỉ số | Giá trị | Cách đo | Evidence |
|---|---|---|---|
| t_outage | `2026-10-09T15:25:00.6Z` | chaos kill | `chaos/chaos-events.jsonl:1` |
| Request fail đầu tiên | +0.0s (ReadTimeout sau 2011.9ms) | dòng `ok:false` đầu tiên sau t_outage | `reports/drill-1-nodr.jsonl:17` |
| Request thành công sau đó | không có (0/16 request sau kill thành công) | không có dòng `ok:true` nào sau t_outage, tới dòng cuối | `reports/drill-1-nodr.jsonl:32` |
| RTO | `NO_RECOVERY` (16/32 request fail) | `tools/measure_rto.py --loadgen reports/drill-1-nodr.jsonl` | `reports/drill-1-nodr.jsonl:32` |

## 2. Drill 2 — có DR

| Mốc | +giây từ t_outage | Cách đo | Evidence |
|---|---|---|---|
| t_outage (mốc 0) | 0 (`2026-10-09T15:25:53.8Z`) | `action:kill` | `chaos/chaos-events.jsonl:3` |
| User thấy lỗi đầu tiên | +0.5s | dòng `ok:false` đầu (ReadTimeout) | `reports/drill-2-withdr.jsonl:26` |
| Health check phát hiện | +14.9s | `to:UNHEALTHY, region:a`, `consecutive_fails:3` | `reports/health-events.jsonl:2` |
| Snapshot restore xong | +17.6s | `step:2_restore_snapshot` | `reports/failover-events.jsonl:2` |
| Region phụ ready | +24.0s | `step:4_wait_ready`, `waited_s:6.36` | `reports/failover-events.jsonl:4` |
| DNS cutover | +24.0s | `step:5_dns_cutover` | `reports/failover-events.jsonl:5` |
| **RTO đo được** | **+28.7s** | dòng `ok:true` đầu sau lỗi, `served_by:b` | `reports/drill-2-withdr.jsonl:40` |

| Chỉ số | Đo được | Mục tiêu (slide §1) | Verdict |
|---|---|---|---|
| RTO — Inference API | 28.7s | 300s (5 phút) | **PASS** (dư 271.3s) |
| RPO — Vector DB | 4.0s / 2 doc | 300s (5 phút) | **PASS** |

RPO lấy từ `rpo_seconds` và `docs_lost` ở `reports/failover-events.jsonl:2`. Snapshot đã restore
là chu kỳ replicate thứ 2: `reports/replication.jsonl:2`. Drill 2 có 14 request fail, không có
warning nào từ `tools/measure_rto.py` (`valid:true`).

## 3. RTO của tôi gồm những gì (bắt buộc — đây là phần chấm điểm hiểu bài)

| Thành phần | Giây | Nó đến từ đâu | Giảm được bằng cách nào |
|---|---|---|---|
| Health-check detect floor | 14.9s | `interval_s × threshold` = 5 × 3 = 15s, trong `reports/health-events.jsonl:2` (t0 → UNHEALTHY) | Giảm interval xuống 2s (floor 6s) — đổi lại nhiều probe hơn và nguy cơ flapping cao hơn; giữ threshold=3 |
| Snapshot restore | 2.7s | Health detect → `3_scale_pool` (`reports/failover-events.jsonl:3`): runbook đọc alert + probe xác nhận lại A (timeout 2s) + copy snapshot (~0.01s) | Bỏ probe xác nhận lại khi đã có alert từ health checker; hoặc pre-stage snapshot sẵn ở region B |
| GPU pool warm-up | 6.4s | `waited_s` ở `4_wait_ready` (`reports/failover-events.jsonl:4`) = `WARMUP_SECONDS` | Giữ region B ở `pool_state=full` (warm standby nóng) — tốn tiền GPU idle |
| DNS/LB TTL cache | 4.7s | t_recovered − t_cutover = 28.7 − 24.0 (`reports/drill-2-withdr.jsonl:40`) | Hạ `EDGE_TTL_SECONDS` từ 5s xuống 1s; hoặc edge tự health-check upstream thay vì chỉ đọc file |
| **Tổng** | **28.7s** | 14.9 + 2.7 + 6.4 + 4.7 | |

## 4. Cấu hình health check

| Tham số | Giá trị | Evidence |
|---|---|---|
| `interval_s` | 5.0s | `reports/health-events.jsonl:2` |
| `threshold` | 3 lần fail liên tiếp | `reports/health-events.jsonl:2` |
| Detect floor | 15.0s — chiếm ~52% RTO | `reports/health-events.jsonl:2` |
| Timeout mỗi probe | 2.0s (`reason: timeout_2.0s`) | `reports/health-events.jsonl:2` |
