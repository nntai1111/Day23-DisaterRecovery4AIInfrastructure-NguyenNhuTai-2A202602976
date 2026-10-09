# Postmortem — DR Drill Lab 23

**Sinh viên:** Nguyễn Như Tài — MSSV 2A202602976

Theo đúng template §4 "Sau Failover: Blameless Postmortem". Blameless: câu hỏi là
"hệ thống/process nào cho phép chuyện này", không phải "ai làm sai".

**Tóm tắt:** Ngày 2026-10-09 lúc 15:25:53Z, region-a bị netblock (SIGSTOP) giữa lúc đang
phục vụ traffic thật (2 req/s). Health checker phát hiện sau 14.9s, runbook tự động
(`--auto`) restore snapshot sang region-b, chờ warm-up rồi mới cutover DNS. Request đầu
tiên thành công từ region-b lúc +28.7s. 14 request bị lỗi trong thời gian outage.

## 0. Baseline trước khi có DR (Bước 1 + Drill 1)

| Câu hỏi | Trả lời |
|---|---|
| Region A chết thì thành phần nào phát hiện? | Không có thành phần nào. Edge chỉ đọc file `edge/active_region`, không tự health-check upstream. |
| Region B có data/weights không? | Không: `count:0`, `weights:false`, `pool_state:warm`. |
| Đổi `active_region` sang b ngay lúc đó thì sao? | User nhận `region_not_ready` (503) — process B sống nhưng không serve được inference. |

Drill 1 chứng minh điều đó: kill lúc `chaos/chaos-events.jsonl:1`, request fail đầu tiên ở
`reports/drill-1-nodr.jsonl:17`, và 16/32 request fail tới hết drill (`reports/drill-1-nodr.jsonl:32`)
→ `NO_RECOVERY`.

## 1. Timeline Drill 2 (mọi dòng phải có evidence path:line)

| ISO time (UTC) | +s | Sự kiện | Evidence |
|---|---:|---|---|
| 2026-10-09T15:25:51.7Z | −2.0 | health checker ghi region-b `UNHEALTHY` (B là warm standby rỗng — đúng kỳ vọng) | `reports/health-events.jsonl:1` |
| 2026-10-09T15:25:53.8Z | 0.0 | outage bắt đầu (`action:kill`, netblock, region-b còn alive) | `chaos/chaos-events.jsonl:3` |
| 2026-10-09T15:25:54.2Z | 0.5 | user đầu tiên bị ảnh hưởng (ReadTimeout 2011.8ms) | `reports/drill-2-withdr.jsonl:26` |
| 2026-10-09T15:26:08.7Z | 14.9 | health check alert: region-a `UNHEALTHY`, 3 lần timeout liên tiếp | `reports/health-events.jsonl:2` |
| 2026-10-09T15:26:11.2Z | 17.5 | operator confirm cutover (runbook `--auto`), failover bắt đầu `1_verify_target` | `reports/failover-events.jsonl:1` |
| 2026-10-09T15:26:11.4Z | 17.6 | restore snapshot xong — RPO 4.0s / 2 doc | `reports/failover-events.jsonl:2` |
| 2026-10-09T15:26:11.4Z | 17.6 | region-b `pool_state` warm → full | `reports/failover-events.jsonl:3` |
| 2026-10-09T15:26:17.7Z | 24.0 | region-b `/readyz` = 200 sau 6.36s warm-up | `reports/failover-events.jsonl:4` |
| 2026-10-09T15:26:17.7Z | 24.0 | DNS cutover `active_region=b` | `reports/failover-events.jsonl:5` |
| 2026-10-09T15:26:18.7Z | 25.0 | health checker thấy region-b `HEALTHY` | `reports/health-events.jsonl:3` |
| 2026-10-09T15:26:22.5Z | 28.7 | resolved — request đầu tiên OK, `served_by:b` | `reports/drill-2-withdr.jsonl:40` |

Độ trễ thông báo (t_outage → operator biết tin): 17.4s. Golden signals sau cutover
(10 request vào region-b): error rate 0%, p95 28.5ms.

## 2. RTO/RPO đo được vs mục tiêu — gap ở bước nào?

- RTO mục tiêu: 300s · đo được: 28.7s · gap: −271.3s (đạt, còn dư 271.3s)
- RPO mục tiêu: 300s · đo được: 4.0s (2 doc bị mất) · gap: −296.0s (đạt)
- **Bước tốn nhiều giây nhất:** health-check detect floor, 14.9s (~52% RTO). Vì
  `interval=5s × threshold=3` là sàn cứng: dù region chết ngay, checker vẫn phải thấy 3
  lần fail liên tiếp, cách nhau 5s, mỗi lần chờ timeout 2s. Kế tiếp là GPU warm-up 6.4s
  và DNS TTL cache 4.7s (edge vẫn cache `a` tới 5s sau cutover).

| Thành phần RTO | Giây |
|---|---:|
| Health-check detect floor | 14.9 |
| Xác nhận + snapshot restore | 2.7 |
| GPU pool warm-up | 6.4 |
| DNS/LB TTL cache | 4.7 |
| **Tổng = RTO** | **28.7** |

**Lưu ý về con số RPO:** snapshot được restore là `reports/replication.jsonl:2`, chụp lúc
+12.85s — tức là **sau** t_outage. Trong mô phỏng, SIGSTOP chỉ dừng process serving; còn
`state/ingest.py` và `state/replicate.py` vẫn đọc/ghi được file SQLite của region-a. Trong
outage thật (mất cả region), replication cũng chết cùng lúc, nên RPO thật sẽ là tuổi của
snapshot cuối cùng trước outage: tối đa ~30s (`--every 30`) ≈ 15 doc ở tốc độ ingest 0.5 doc/s.

## 3. Root cause (5 whys)

*Nếu đây là outage thật, bước nào trong runbook của tôi sẽ thất bại?*

1. **Vì sao user bị lỗi 28.7s?** Vì edge không tự phát hiện region-a chết; nó chỉ đổi upstream
   khi có người/automation ghi `edge/active_region`.
2. **Vì sao phải chờ automation?** Vì region-b là warm standby: không có data, không có
   weights, pool chưa full. Cutover ngay sẽ cho 503 từ cả hai phía.
3. **Vì sao region-b không có data sẵn?** Vì replication là snapshot định kỳ 30s qua object
   store (`state/_replica/`), không phải streaming; restore chỉ xảy ra lúc failover.
4. **Vì sao snapshot store an toàn?** Trong lab nó nằm trên **cùng một ổ đĩa** với cả 2 region.
   Đây là điểm sẽ thất bại trong outage thật: mất region chính = mất luôn bucket replica nếu
   bucket không nằm ở region khác. Runbook bước 3 sẽ chết ở `2_restore_snapshot`.
5. **Vì sao drill không phát hiện điều này?** Vì `--mock` chỉ dừng process, không mô phỏng
   mất storage. Runbook chưa có bước kiểm tra "replica có nằm ngoài blast radius không".

**Root cause hệ thống:** blast radius của snapshot store chưa tách khỏi region chính, và
replication (tác nhân quyết định RPO) không có alert riêng — nếu `replicate.py` chết âm thầm,
RPO tăng vô hạn mà không ai biết cho tới lúc failover.

## 4. Action items (có owner + deadline)

| # | Action | Owner | Deadline | Giảm RTO/RPO bao nhiêu giây |
|---|---|---|---|---|
| 1 | Đưa snapshot store sang bucket ở region khác (S3 CRR / MinIO riêng) và thêm alert khi `snapshot.py lag` > 60s | Platform/SRE | 2026-10-23 | RPO thật: chặn trần ≤ 60s thay vì không giới hạn |
| 2 | Hạ health-check interval 5s → 2s, giữ threshold=3, thêm jitter để tránh flapping | SRE on-call lead | 2026-10-16 | RTO −9s (floor 15s → 6s) |
| 3 | Hạ `EDGE_TTL_SECONDS` 5s → 1s, hoặc edge tự probe `/readyz` của upstream | Networking | 2026-10-16 | RTO −4s |
| 4 | Runbook bỏ probe xác nhận lại khi đã có alert UNHEALTHY từ health checker | SRE | 2026-10-16 | RTO −2.5s |
| 5 | Giữ region-b ở `pool_state=full` trong giờ cao điểm (hot standby) — cần duyệt chi phí GPU | Eng manager + FinOps | 2026-10-30 | RTO −6.4s |
| 6 | Chạy game day hằng tháng với `--mode stop` và `netblock` ngẫu nhiên, báo cáo mean/stddev RTO | SRE | 2026-11-09 | Không giảm trực tiếp; chứng minh RTO ổn định |

## 5. Ba câu hỏi bắt buộc trả lời

1. **`interval × threshold` của bạn là bao nhiêu giây? Nó chiếm bao nhiêu % RTO?**
   5s × 3 = 15s floor. Phát hiện thực tế ở +14.9s (`reports/health-events.jsonl:2`), chiếm
   14.9 / 28.7 ≈ 52% RTO. Muốn RTO 5 phút thì về lý thuyết có thể chọn interval tới ~90s
   (90 × 3 = 270s floor + ~14s phần còn lại < 300s), nhưng như vậy gần như không còn biên.

2. **Nếu hạ interval xuống 1s, RTO giảm mấy giây — và bạn trả giá gì?**
   Floor còn 3s → RTO giảm khoảng 12s (≈ 28.7s → ~17s). Cái giá: gấp 5 lần số probe, và
   3 lần fail trong 3 giây là quá dễ xảy ra với một GC pause, deploy rolling, hay blip mạng
   ngắn → failover giả, rồi failback, rồi failover lại (flapping §4). Mỗi lần flap lại tốn
   warm-up + TTL và có thể mất dữ liệu ghi dở. Nếu hạ interval phải tăng threshold hoặc
   có circuit breaker/cooldown sau mỗi failover.

3. **Nếu outage kéo dài 6 giờ và region chính mất dữ liệu vĩnh viễn, `docs_lost` có nghĩa gì?**
   `docs_lost` là số document khách hàng đã gửi lên, đã được xác nhận thành công, nhưng
   **vĩnh viễn không tồn tại** trên region-b: không tìm kiếm được, không dùng làm context
   cho inference. Trong drill là 2 doc; trong outage thật là toàn bộ doc ingest sau snapshot
   cuối cùng (tới ~15 doc với chu kỳ 30s). Thời gian outage 6 giờ không làm `docs_lost` tăng
   (region chính đã chết, không nhận ghi nữa) — nhưng phải thông báo cho khách hàng danh sách
   doc cần upload lại, và gap này phải được ghi vào SLA như RPO cam kết.
