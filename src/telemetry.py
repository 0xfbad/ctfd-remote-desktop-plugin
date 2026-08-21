from __future__ import annotations

import time
import logging

logger = logging.getLogger(__name__)

# tier 1 (docker stats) has NO PSI and NO oom_kill counts - those need the host
# cgroup tree, which the stats API does not expose. write-rate detection is a
# lower bound: buffered writes through the overlayfs upper layer are not
# attributed to the container cgroup on some kernels (verified on the 6.18 dev
# box) - only direct IO shows up. containment is the storage quota, not this. tier 2 reads the snapshot
# file written by provisioning/compute's rd-telemetry timer on each runner and
# carries PSI, memory.events and pids.events; it is host-side truth a rooted
# student cannot tamper with (handoff section 3/7).

ESCALATE_AFTER = 3  # consecutive over-threshold samples before level=error
MAX_EVENTS_PER_SESSION_PER_PASS = 3
HOST_PRESSURE_AVG60_WARN = 20.0


def _write_bytes_from_stats(stats: dict) -> int | None:
    # MAX across devices, not SUM: layered devices (nvme + dm) report duplicated
    # identical byte counts, so a sum double-counts. lower() because cgroup-v1
    # daemons capitalize the op
    entries = (stats.get("blkio_stats") or {}).get("io_service_bytes_recursive") or []
    writes = [int(e.get("value") or 0) for e in entries if str(e.get("op", "")).lower() == "write"]
    return max(writes) if writes else None


class ResourceTelemetry:
    def __init__(self, host_manager, event_logger, app=None) -> None:
        self.host_manager = host_manager
        self.event_logger = event_logger
        self.app = app
        # per-container sampler state, pruned when rows disappear
        self._state: dict[str, dict] = {}
        # per-host tier-2 state (event deltas keyed by container id)
        self._host_state: dict[str, dict[str, dict]] = {}
        self._last_alert: dict[tuple[str, str], float] = {}

    def _get_setting(self, key: str):
        from .models import get_setting

        return get_setting(key)

    def _alert(
        self,
        metric_key: tuple[str, str],
        event_type: str,
        message: str,
        *,
        level: str,
        user_id=None,
        username=None,
        metadata=None,
    ) -> bool:
        realert = int(self._get_setting("telemetry_realert_seconds") or 600)
        now = time.time()
        last = self._last_alert.get(metric_key, 0.0)
        if now - last < realert:
            return False
        self._last_alert[metric_key] = now
        self.event_logger.log_event(
            event_type, message, user_id=user_id, username=username, level=level, metadata=metadata
        )
        return True

    def run_once(self) -> None:
        # both tiers gate on the master telemetry switch; tier 2 additionally
        # self-skips when read_host_telemetry finds no runner-side provisioning
        if not self._get_setting("telemetry_enabled"):
            return
        try:
            self._sample_containers()
        except Exception:
            logger.exception("tier-1 telemetry pass failed")
        try:
            self._sample_hosts()
        except Exception:
            logger.exception("tier-2 telemetry pass failed")

    # -- tier 1: docker stats per session container --------------------------

    def _sample_containers(self) -> None:
        from .models import DesktopContainerInfoModel, username_or_fallback
        from CTFd.models import Users

        interval = int(self._get_setting("telemetry_interval") or 60)
        mem_warn_pct = float(self._get_setting("telemetry_mem_warn_pct") or 90)
        pids_warn_pct = float(self._get_setting("telemetry_pids_warn_pct") or 80)
        write_warn_bps = float(self._get_setting("telemetry_write_mbps_warn") or 200) * 1e6

        rows = DesktopContainerInfoModel.query.all()
        connected = set(self.host_manager.get_connected_contexts())
        seen_ids = set()

        for row in rows:
            if row.docker_context not in connected:
                continue
            stats = self.host_manager.container_stats(row.docker_context, row.container_id)
            if not stats:
                continue
            seen_ids.add(row.container_id)
            state = self._state.setdefault(row.container_id, {"over": {}, "prev_wbytes": None, "prev_ts": None})
            now = time.time()
            user = Users.query.filter_by(id=row.user_id).first()
            username = username_or_fallback(user, row.user_id)
            emitted = 0

            def _fire(metric: str, event_type: str, message: str, metadata: dict) -> None:
                nonlocal emitted
                if emitted >= MAX_EVENTS_PER_SESSION_PER_PASS:
                    return
                over = state["over"].get(metric, 0) + 1
                state["over"][metric] = over
                level = "error" if over >= ESCALATE_AFTER else "warning"
                metadata = dict(metadata)
                metadata.update(
                    {"container_id": row.container_id, "docker_context": row.docker_context}
                )
                if over >= ESCALATE_AFTER:
                    metadata["escalated"] = True
                if self._alert(
                    (row.container_id, metric),
                    event_type,
                    message,
                    level=level,
                    user_id=row.user_id,
                    username=username,
                    metadata=metadata,
                ):
                    emitted += 1

            def _clear(metric: str) -> None:
                state["over"].pop(metric, None)

            mem = stats.get("memory_stats") or {}
            usage, limit = mem.get("usage"), mem.get("limit")
            if usage and limit and usage / limit * 100 >= mem_warn_pct:
                _fire(
                    "mem",
                    "resource_mem_high",
                    f"memory at {usage / limit * 100:.0f}% of limit",
                    {"usage": int(usage), "limit": int(limit)},
                )
            else:
                _clear("mem")

            pids = stats.get("pids_stats") or {}
            pcur, plim = pids.get("current"), pids.get("limit")
            if pcur and plim and pcur / plim * 100 >= pids_warn_pct:
                _fire(
                    "pids",
                    "resource_pids_high",
                    f"pids at {pcur}/{plim}",
                    {"current": int(pcur), "limit": int(plim)},
                )
            else:
                _clear("pids")

            wbytes = _write_bytes_from_stats(stats)
            if wbytes is not None:
                prev_w, prev_ts = state.get("prev_wbytes"), state.get("prev_ts")
                if prev_w is not None and prev_ts and now > prev_ts:
                    rate = (wbytes - prev_w) / (now - prev_ts)
                    if rate >= write_warn_bps:
                        _fire(
                            "write",
                            "resource_write_flood",
                            f"sustained writes at {rate / 1e6:.0f} MB/s",
                            {"rate_bps": int(rate), "total_wbytes": int(wbytes), "interval": interval},
                        )
                    else:
                        _clear("write")
                state["prev_wbytes"], state["prev_ts"] = wbytes, now

        # prune state for rows that disappeared
        for cid in list(self._state.keys()):
            if cid not in seen_ids:
                self._state.pop(cid, None)

    # -- tier 2: host-side PSI / cgroup events snapshot ----------------------

    def _sample_hosts(self) -> None:
        from .models import DesktopContainerInfoModel, username_or_fallback
        from CTFd.models import Users

        rows_by_id = {r.container_id: r for r in DesktopContainerInfoModel.query.all()}

        for ctx in self.host_manager.get_connected_contexts():
            snapshot = self.host_manager.read_host_telemetry(ctx)
            if not snapshot:
                # provisioning absent on this host; silent by design
                logger.debug(f"no host telemetry from {ctx}")
                continue

            if snapshot.get("slice_unconfigured"):
                self._alert(
                    (ctx, "slice"),
                    "rd_slice_unconfigured",
                    f"rd.slice has no limits on {ctx} - run provisioning/compute/install.sh",
                    level="error",
                    metadata={"context_name": ctx},
                )

            prev = self._host_state.setdefault(ctx, {})
            current: dict[str, dict] = {}
            for entry in snapshot.get("containers") or []:
                cid = str(entry.get("id") or "")
                current[cid] = entry
                row = rows_by_id.get(cid)
                if row is None:
                    continue
                user = Users.query.filter_by(id=row.user_id).first()
                username = username_or_fallback(user, row.user_id)

                mem_events = entry.get("memory_events") or {}
                prev_mem = (prev.get(cid) or {}).get("memory_events") or {}
                if int(mem_events.get("oom_kill") or 0) > int(prev_mem.get("oom_kill") or 0):
                    self._alert(
                        (cid, "oom_kill"),
                        "session_oom_kill",
                        "process killed by the memory limit (OOM)",
                        level="error",
                        user_id=row.user_id,
                        username=username,
                        metadata={"container_id": cid, "docker_context": ctx, **{k: int(v) for k, v in mem_events.items()}},
                    )

                pids_events = entry.get("pids_events") or {}
                prev_pids = (prev.get(cid) or {}).get("pids_events") or {}
                if int(pids_events.get("max") or 0) > int(prev_pids.get("max") or 0):
                    self._alert(
                        (cid, "pids_max"),
                        "pids_limit_hit",
                        "fork blocked by the pids limit",
                        level="warning",
                        user_id=row.user_id,
                        username=username,
                        metadata={"container_id": cid, "docker_context": ctx, "pids_max_events": int(pids_events.get("max") or 0)},
                    )

                for kind in ("memory_pressure", "io_pressure"):
                    psi = entry.get(kind) or {}
                    avg60 = float(psi.get("avg60") or 0)
                    if avg60 > HOST_PRESSURE_AVG60_WARN:
                        self._alert(
                            (cid, kind),
                            "host_pressure",
                            f"{kind.replace('_', ' ')} avg60 {avg60:.0f} on {ctx}",
                            level="warning",
                            user_id=row.user_id,
                            username=username,
                            metadata={"container_id": cid, "docker_context": ctx, "kind": kind, "avg60": avg60},
                        )

            self._host_state[ctx] = current
