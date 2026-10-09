"""BƯỚC 3b — SINH VIÊN VIẾT. Cutover sang region phụ.

5 bước, THỨ TỰ QUAN TRỌNG (§2 Kiến Trúc Tham Chiếu: DNS/LB, compute, state là 3 lớp riêng):
  1_verify_target    — /v1/state của region phụ: weights? vector count? pool_state?
  2_restore_snapshot — gọi state/snapshot.py get + state/snapshot.py rpo()
                       Log BẮT BUỘC: rpo_seconds, docs_lost, embed_model_version.
                       (§3: "backup index nhưng quên backup embedding model version
                        -> index không tương thích khi restore")
  3_scale_pool       — ghi "full" vào state/region-<t>/pool_state (warm -> full)
  4_wait_ready       — POLL /readyz tới khi 200. Region phụ có WARMUP_SECONDS —
                       đây là GPU pool warm-up của §4, nó nằm trong RTO của bạn.
  5_dns_cutover      — ghi region đích vào edge/active_region

BẪY: nếu bạn đổi edge/active_region TRƯỚC bước 4, user sẽ nhận 503 từ CẢ HAI region
và RTO của bạn dài hơn, không ngắn hơn. Nếu bước 4 timeout -> ABORT, KHÔNG cutover.

Mỗi bước ghi 1 dòng vào reports/failover-events.jsonl với ts + step.
Không có dòng 5_dns_cutover = tools/measure_rto.py không tìm được t_cutover = mất điểm.

Chạy:  python dr/failover.py --target b --backend fs
"""
import argparse
import json
import pathlib
import sys
import time

import httpx

sys.path.insert(0, ".")
from state import snapshot  # noqa: E402

URL = {"a": "http://127.0.0.1:8001", "b": "http://127.0.0.1:8002"}
LOG = pathlib.Path("reports/failover-events.jsonl")


def emit(**kw):
    """Append 1 dòng JSONL có ts + iso vào LOG, và print ra stdout."""
    LOG.parent.mkdir(parents=True, exist_ok=True)
    now = time.time()
    rec = {"ts": now, "iso": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(now)), **kw}
    with LOG.open("a") as f:
        f.write(json.dumps(rec) + "\n")
    print("FAILOVER", json.dumps(rec), flush=True)
    return rec


def state_of(region: str) -> dict:
    """/v1/state của 1 region; lỗi mạng -> dict có `error` thay vì exception."""
    try:
        return httpx.get(f"{URL[region]}/v1/state", timeout=2.0).json()
    except Exception as e:
        return {"region": region, "error": type(e).__name__}


def failover(target: str, backend: str, wait: float) -> dict:
    """5 bước ở trên, đúng thứ tự. Bước 4 timeout -> abort, KHÔNG cutover."""
    primary = "b" if target == "a" else "a"
    t_start = time.time()
    result = {"ok": False, "target": target, "primary": primary, "backend": backend}

    # 1 — region phụ đang có gì?
    before = state_of(target)
    emit(step="1_verify_target", target=target, state=before)
    result["state_before"] = before

    # 2 — restore snapshot + đo RPO thật (dữ liệu primary có mà bản restore không có)
    try:
        meta = snapshot.get(target, backend)
    except BaseException as e:  # snapshot.get dùng SystemExit khi chưa có MANIFEST
        emit(step="2_restore_snapshot", ok=False, error=str(e))
        result.update(aborted_at="2_restore_snapshot", error=str(e))
        return result
    r = snapshot.rpo(pathlib.Path(f"state/region-{primary}/vectors.sqlite"),
                     pathlib.Path(f"state/region-{target}/vectors.sqlite"))
    emit(step="2_restore_snapshot", ok=True, snapshot_at=meta.get("snapshot_at"),
         embed_model_version=meta.get("embed_model_version"),
         rpo_seconds=r["rpo_seconds"], docs_lost=r["docs_lost"],
         primary_latest_doc_ts=r["primary_latest_doc_ts"],
         restored_latest_doc_ts=r["restored_latest_doc_ts"])
    result.update(rpo_seconds=r["rpo_seconds"], docs_lost=r["docs_lost"],
                  embed_model_version=meta.get("embed_model_version"))

    # 3 — warm -> full (serving bắt đầu đếm WARMUP_SECONDS từ lúc này)
    pool = pathlib.Path(f"state/region-{target}/pool_state")
    pool.parent.mkdir(parents=True, exist_ok=True)
    pool.write_text("full")
    emit(step="3_scale_pool", pool_state="full")

    # 4 — poll /readyz tới khi 200
    t_wait, last = time.time(), None
    while True:
        try:
            resp = httpx.get(f"{URL[target]}/readyz", timeout=2.0)
            if resp.status_code == 200:
                break
            last = resp.json().get("reasons")
        except Exception as e:
            last = type(e).__name__
        if time.time() - t_wait >= wait:
            emit(step="4_wait_ready", ok=False, waited_s=round(time.time() - t_wait, 2),
                 last_reason=last, note="timeout -> ABORT, khong cutover")
            result.update(aborted_at="4_wait_ready", last_reason=last)
            return result
        time.sleep(0.5)
    waited = round(time.time() - t_wait, 2)
    emit(step="4_wait_ready", ok=True, waited_s=waited)

    # 5 — chỉ đổi DNS khi target đã ready
    active = pathlib.Path("edge/active_region")
    active.parent.mkdir(parents=True, exist_ok=True)
    active.write_text(target)
    emit(step="5_dns_cutover", active_region=target)

    after = state_of(target)
    result.update(ok=True, waited_s=waited, state_after=after,
                  elapsed_s=round(time.time() - t_start, 2))
    return result


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--target", default="b", choices=["a", "b"])
    p.add_argument("--backend", default="fs", choices=["fs", "minio"])
    p.add_argument("--wait", type=float, default=60)
    a = p.parse_args()
    print(json.dumps(failover(a.target, a.backend, a.wait), indent=2))
