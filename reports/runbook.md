# Runbook 1 trang — Region chính down

**Sinh viên:** Nguyễn Như Tài — MSSV 2A202602976

Runbook phải chạy được lúc 3h sáng bởi người KHÔNG viết nó. Mỗi bước: lệnh copy-paste
được + cách biết bước đó xong.

**Điều kiện kích hoạt:** health checker ghi `"to": "UNHEALTHY", "region": "a"` vào
`reports/health-events.jsonl` (3 lần `/readyz` fail liên tiếp, interval 5s), hoặc có alert
về tỉ lệ lỗi ở edge. **Chạy tất cả lệnh từ thư mục gốc repo**, trong WSL/Linux.

**Cách nhanh (bán tự động, khuyến nghị):** `python3 dr/runbook.py --primary a --target b --backend fs`
tự chạy cả 7 bước bên dưới, hỏi `y/N` trước khi failover, và ghi log vào
`reports/runbook-run.jsonl` + `reports/failover-events.jsonl`. Chỉ dùng `--auto` trong drill/CI.
Các bước thủ công dưới đây là đường dự phòng khi script lỗi.

| # | Bước | Lệnh | Biết là xong khi | Ai làm |
|---|---|---|---|---|
| 1 | Xác nhận outage | `python3 chaos/kill_region.py status` (chạy 3 lần, cách nhau 5s) và `tail -n 3 reports/health-events.jsonl` | 3 lần liên tiếp `a.ready=false`, `b.alive=true`; health log có `to:UNHEALTHY, region:a`. Nếu `a.ready=true` lại → **dừng**, không phải outage | On-call SRE |
| 2 | Mở incident + bấm giờ RTO | `echo "{\"ts\": $(date +%s.%N), \"event\": \"incident_open\", \"sev\": \"SEV1\"}" >> reports/runbook-run.jsonl` rồi báo kênh #incident: "SEV1 region-a down, bắt đầu failover sang b" | Có dòng `incident_open` trong `reports/runbook-run.jsonl`; kênh incident đã có người nhận Incident Commander | On-call SRE (mở) · Incident Commander (nhận) |
| 3 | Restore state ở region phụ | `python3 state/snapshot.py lag --backend fs` rồi `python3 state/snapshot.py get --region b --backend fs` | `lag.rpo_seconds` < 300 (nếu lớn hơn → báo IC trước khi tiếp tục); `get` in ra `embed_model_version` **giống** region-a; `curl -s localhost:8002/v1/state` có `count>0`, `weights:true` | On-call SRE |
| 4 | Scale pool warm→full | `printf full > state/region-b/pool_state` rồi chờ: `until curl -sf localhost:8002/readyz >/dev/null; do sleep 1; done; echo READY` | `curl -s localhost:8002/readyz` trả HTTP 200, `"ready": true` (thường ~6s warm-up). Quá 60s chưa ready → **ABORT, không cutover**, escalate | On-call SRE |
| 5 | DNS/LB cutover | `printf b > edge/active_region` (**chỉ sau khi bước 4 ready**) | `curl -s localhost:8080/edge/state` cho `active_region=b` (chờ tối đa `ttl_seconds`=5s cho cache hết hạn) | On-call SRE, IC xác nhận |
| 6 | Verify golden signals | `for i in $(seq 10); do curl -s -o /dev/null -w "%{http_code} %{time_total}\n" localhost:8080/v1/infer; done` | 10/10 trả `200`, p95 < 500ms, error rate < 1%; body có `"region":"b"` | On-call SRE |
| 7 | Đo RTO + postmortem | `python3 tools/measure_rto.py --loadgen reports/drill-2-withdr.jsonl --target-rto 300` | `valid:true`, `rto_verdict` != null (PASS nếu ≤ 300s), có `rpo_at_restore_s` + `docs_lost`; điền `reports/postmortem.md` trong 48h | Incident Commander |

**Rollback / abort trong lúc failover:**
- Bước 3 lỗi (không có snapshot, `embed_model_version` lệch) hoặc bước 4 quá 60s chưa ready
  → **không** đổi `edge/active_region`, giữ traffic ở a, escalate lên Platform lead.
  Cutover vào region chưa ready = 503 từ cả hai phía.
- Bước 6 lỗi > 5% sau cutover và region-a đã ready trở lại → IC được phép trả DNS về a ngay
  (`printf a > edge/active_region`).

**Failback (trả traffic về region A):** không tự động — §4 Anti-Patterns: full-auto không
có circuit breaker → 2 region flap qua lại.
- **Điều kiện:** region-a `/readyz` = 200 liên tục ≥ 30 phút; root cause đã được xác định và
  sửa; data ghi vào region-b trong lúc outage đã được snapshot ngược về a
  (`python3 state/snapshot.py put --region b --backend fs` → `get --region a`), `docs_lost` = 0;
  ngoài giờ cao điểm.
- **Thực hiện:** chạy lại runbook theo chiều ngược: `python3 dr/runbook.py --primary b --target a --backend fs`.
- **Ai quyết định:** Incident Commander, có sự đồng ý của Engineering Manager trực.
  On-call không tự failback một mình.
- **Cooldown:** sau một lần failover, không được failover tiếp trong 30 phút trừ khi IC phê duyệt.

**Restore region-a sau drill (chỉ môi trường lab):** `python3 chaos/kill_region.py restore --region a --backend bare`
(nếu đã dùng `--mode stop` thì chạy lại `bash scripts/up_bare.sh`).
