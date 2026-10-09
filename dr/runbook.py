"""BƯỚC 3c — SINH VIÊN VIẾT. Tự động hoá runbook §4 "Runbook: Region Chính Down".

7 bước trên slide, mỗi bước 1 dòng log có ts. Log này CHÍNH LÀ timeline của postmortem.
  1 xac_nhan_outage          — probe cả 2 region, đừng tin 1 lần fail (dùng nhiều lần
                              hoặc gọi health_checker.probe nếu đã viết xong 3a)
  2 thong_bao_incident       — ts của dòng này là mốc "operator biết tin", LUÔN LUÔN
                              SAU t_outage trong chaos-events (không thể trùng — operator
                              không thể biết ngay giây outage xảy ra). Ghi cả 2 ts vào
                              log để postmortem tính được "độ trễ thông báo".
  3 scale_gpu_pool           — gọi HÀM `failover.failover(...)` MỘT LẦN DUY NHẤT. Hàm
                              đó tự làm đủ 5 bước con (verify/restore/scale/wait/cutover)
                              và tự ghi log riêng vào reports/failover-events.jsonl.
  4 verify_state_replica     — KHÔNG gọi lại failover — chỉ ĐỌC kết quả (vector count +
                              weights ở region phụ) từ dict mà bước 3 trả về, để log vào
                              runbook-run.jsonl cho postmortem đọc 1 chỗ duy nhất.
  5 dns_cutover              — cũng chỉ đọc lại: kết quả cutover có ok hay không.
  6 verify_golden_signals    — 10 request thật vào region phụ: p95 latency + error rate
  7 post_incident            — elapsed_s + lệnh đo RTO

BÁN TỰ ĐỘNG, KHÔNG FULL-AUTO (§4: "failover đầu tiên nên là bán tự động — alert +
1-click confirm — tránh flapping gây failover 2 chiều liên tục"). Mặc định phải hỏi
người vận hành confirm; --auto chỉ dùng trong CI/khi chấm điểm.

Chạy:  python dr/runbook.py --primary a --target b --backend fs
"""
import argparse
import json
import pathlib
import sys
import time

import httpx

sys.path.insert(0, ".")
from dr import failover as fo  # noqa: E402

LOG = pathlib.Path("reports/runbook-run.jsonl")
URL = {"a": "http://127.0.0.1:8001", "b": "http://127.0.0.1:8002"}


CHAOS = pathlib.Path("chaos/chaos-events.jsonl")
HEALTH = pathlib.Path("reports/health-events.jsonl")


def step(n, name, **kw):
    """Ghi 1 dòng {ts, iso, step, name, ...} vào LOG."""
    LOG.parent.mkdir(parents=True, exist_ok=True)
    now = time.time()
    rec = {"ts": now, "iso": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(now)),
           "step": n, "name": name, **kw}
    with LOG.open("a") as f:
        f.write(json.dumps(rec) + "\n")
    print("RUNBOOK", json.dumps(rec), flush=True)
    return rec


def confirm(auto: bool, msg: str) -> bool:
    """auto=True -> True; ngược lại hỏi y/N. Đừng bỏ hàm này đi."""
    if auto:
        return True
    try:
        return input(f"{msg} [y/N] ").strip().lower() in ("y", "yes")
    except EOFError:
        return False


def _jsonl(p: pathlib.Path) -> list:
    if not p.exists():
        return []
    return [json.loads(l) for l in p.read_text().splitlines() if l.strip()]


def _last_kill(region: str):
    kills = [e for e in _jsonl(CHAOS) if e.get("action") == "kill" and e.get("region") == region]
    return kills[-1]["ts"] if kills else None


def _health_alert(region: str, since: float):
    for e in _jsonl(HEALTH):
        if (e.get("event") == "state_change" and e.get("region") == region
                and e.get("to") == "UNHEALTHY" and e["ts"] >= since):
            return e
    return None


def _probe(region: str, path: str = "/readyz", timeout: float = 2.0) -> tuple[bool, str]:
    try:
        r = httpx.get(f"{URL[region]}{path}", timeout=timeout)
        return r.status_code == 200, f"http_{r.status_code}"
    except Exception as e:
        return False, type(e).__name__


def run(primary: str, target: str, backend: str, auto: bool,
        alert_wait: float = 60.0, probes: int = 3, probe_interval: float = 5.0) -> dict:
    """7 bước của runbook §4 'Region Chính Down'."""
    t_start = time.time()
    t_outage = _last_kill(primary)

    # 1 — xác nhận outage: chờ alert của health checker (nguồn phát hiện chính thức),
    #     sau đó tự probe lại. Không có alert -> tự probe `probes` lần liên tiếp.
    alert, deadline = None, time.time() + alert_wait
    while time.time() < deadline:
        alert = _health_alert(primary, t_outage or t_start)
        if alert:
            break
        time.sleep(0.5)
    n_probes, checks = (1 if alert else probes), []
    for i in range(n_probes):
        ok, reason = _probe(primary)
        checks.append("ready" if ok else reason)
        if i < n_probes - 1:
            time.sleep(probe_interval)
    primary_down = all(c != "ready" for c in checks)
    target_alive, _ = _probe(target, "/healthz")
    step(1, "xac_nhan_outage", primary=primary, primary_down=primary_down,
         probe_results=checks, health_alert_ts=alert["ts"] if alert else None,
         target_alive=target_alive)
    if not primary_down:
        return {"ok": False, "aborted_at": "xac_nhan_outage",
                "reason": f"region-{primary} van ready -> khong phai outage, khong failover"}
    if not target_alive:
        return {"ok": False, "aborted_at": "xac_nhan_outage",
                "reason": f"region-{target} khong song -> khong co cho de failover"}

    # 2 — thông báo incident, bắt đầu đồng hồ (luôn SAU t_outage)
    t_notify = time.time()
    step(2, "thong_bao_incident", severity="SEV1",
         message=f"region-{primary} down, chuan bi failover sang region-{target}",
         t_outage=t_outage, t_notify=t_notify,
         notify_delay_s=None if t_outage is None else round(t_notify - t_outage, 2))

    # 3 — failover (5 bước con) — gọi ĐÚNG MỘT LẦN, sau khi người vận hành confirm
    if not confirm(auto, f"Failover region-{primary} -> region-{target}?"):
        step(3, "scale_gpu_pool", confirmed=False, note="operator tu choi -> dung runbook")
        return {"ok": False, "aborted_at": "scale_gpu_pool", "reason": "operator_declined"}
    res = fo.failover(target, backend, wait=60)
    step(3, "scale_gpu_pool", confirmed=True, auto=auto, failover_ok=res.get("ok"),
         waited_s=res.get("waited_s"), aborted_at=res.get("aborted_at"))

    # 4 — chỉ ĐỌC lại kết quả state ở region phụ
    after = res.get("state_after") or {}
    step(4, "verify_state_replica", vectors=after.get("count"), weights=after.get("weights"),
         pool_state=after.get("pool_state"), rpo_seconds=res.get("rpo_seconds"),
         docs_lost=res.get("docs_lost"), embed_model_version=res.get("embed_model_version"))

    # 5 — chỉ ĐỌC lại kết quả cutover
    step(5, "dns_cutover", ok=bool(res.get("ok")),
         active_region=target if res.get("ok") else None)
    if not res.get("ok"):
        step(7, "post_incident", ok=False, elapsed_s=round(time.time() - t_start, 2),
             note="failover abort -> escalate, KHONG doi DNS")
        return {"ok": False, "aborted_at": res.get("aborted_at"), "failover": res}

    # 6 — golden signals: 10 request thật vào region phụ
    lat, errors = [], 0
    with httpx.Client(timeout=3.0) as c:
        for i in range(10):
            t0 = time.time()
            try:
                r = c.get(f"{URL[target]}/v1/infer", params={"q": f"golden {i}"})
                if r.status_code != 200 or r.json().get("error"):
                    errors += 1
            except Exception:
                errors += 1
            lat.append((time.time() - t0) * 1000)
    lat.sort()
    p95 = round(lat[max(0, int(round(0.95 * len(lat))) - 1)], 1)
    step(6, "verify_golden_signals", requests=len(lat), errors=errors,
         error_rate=errors / len(lat), p95_latency_ms=p95, healthy=errors == 0)

    # 7 — post-incident
    end = time.time()
    step(7, "post_incident", ok=True, elapsed_s=round(end - t_start, 2),
         since_outage_s=None if t_outage is None else round(end - t_outage, 2),
         measure_cmd="python3 tools/measure_rto.py --loadgen reports/drill-2-withdr.jsonl "
                     "--target-rto 300")
    return {"ok": errors == 0, "primary": primary, "target": target, "failover": res,
            "golden_signals": {"errors": errors, "p95_latency_ms": p95},
            "elapsed_s": round(end - t_start, 2)}


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--primary", default="a")
    p.add_argument("--target", default="b")
    p.add_argument("--backend", default="fs", choices=["fs", "minio"])
    p.add_argument("--auto", action="store_true")
    a = p.parse_args()
    print(json.dumps(run(a.primary, a.target, a.backend, a.auto), indent=2))
