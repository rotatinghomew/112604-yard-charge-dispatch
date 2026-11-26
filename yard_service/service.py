"""Core yard scheduling, reconciliation, handoff and billing service.

The class is intentionally framework-free.  :class:`YardService` can be embedded
in tests or exposed through :mod:`yard_service.server`.
"""

from __future__ import annotations

import json
import threading
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable

from .errors import YardError
from .pile import HttpPileClient, PileTransportError
from .planner import best_schedule, feasible_subsets
from .timeutil import decimal_to_milli_fen, iso, money_json, parse_dt

SAMPLE_DIR = Path(__file__).resolve().parents[1] / "samples"

OCCUPYING_JOB_STATUSES = {
    "committed",
    "dispatched",
    "in_progress",
    "stop_pending",
    "unconfirmed",
}
ACTIVE_SESSION_STATES = {"active", "stop_pending", "stop_unconfirmed"}


class YardService:
    def __init__(
        self,
        data_dir: str | Path | None = None,
        *,
        now: datetime | str | None = None,
        pile_client: Any | None = None,
        peer_url: str | None = None,
    ) -> None:
        self._lock = threading.RLock()
        self.clock_explicit = now is not None
        self.now_value = parse_dt(now) if now is not None else None
        self.pile_client = pile_client
        if self.pile_client is None and peer_url:
            self.pile_client = HttpPileClient(peer_url)

        self.ports: dict[str, dict[str, Any]] = {}
        self.vehicles: dict[str, dict[str, Any]] = {}
        self.bookings: dict[str, dict[str, Any]] = {}
        self.sessions: dict[str, dict[str, Any]] = {}
        self.meter_events: dict[str, dict[str, Any]] = {}
        self.meter_order: list[str] = []
        self.plans: dict[str, dict[str, Any]] = {}
        self.changes: dict[str, dict[str, Any]] = {}
        self.urgent_orders: dict[str, dict[str, Any]] = {}
        self.jobs: dict[str, dict[str, Any]] = {}
        self.payments: list[dict[str, Any]] = []
        self.settlements: dict[str, dict[str, Any]] = {}
        self.adjustments: list[dict[str, Any]] = []
        self.handoffs: list[dict[str, Any]] = []
        self.dispatch_reviews: dict[str, dict[str, Any]] = {}
        self.audit_events: list[dict[str, Any]] = []
        self.counters: dict[str, int] = defaultdict(int)

        if data_dir is not None:
            self.load_feeds(Path(data_dir))

    # ------------------------------------------------------------------ utils
    def _next_id(self, prefix: str) -> str:
        self.counters[prefix] += 1
        return f"{prefix}{self.counters[prefix]}"

    def _time(self, at: datetime | str | None = None) -> datetime:
        if at is not None:
            result = parse_dt(at)
            assert result is not None
            return result
        if self.now_value is not None:
            return self.now_value
        return datetime.now().astimezone()

    def set_clock(self, now: datetime | str) -> None:
        with self._lock:
            self.now_value = parse_dt(now)

    def _audit(self, action: str, *, at: datetime | None = None, **details: Any) -> None:
        event = {
            "id": self._next_id("A"),
            "at": iso(at or self._time()),
            "action": action,
            **details,
        }
        self.audit_events.append(event)

    @staticmethod
    def _read_jsonl(path: Path) -> list[dict[str, Any]]:
        if not path.exists():
            return []
        rows = []
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                rows.append(json.loads(line))
        return rows

    def load_feeds(self, data_dir: Path) -> None:
        with self._lock:
            for row in self._read_jsonl(data_dir / "ports.jsonl"):
                self.upsert_port(row, audit=False)
            for row in self._read_jsonl(data_dir / "fleets.jsonl"):
                self.upsert_vehicle(row, audit=False)
            for row in self._read_jsonl(data_dir / "active_sessions.jsonl"):
                self.record_start(row, audit=False)
            for row in self._read_jsonl(data_dir / "meter.jsonl"):
                self.receive_meter(row, audit=False)
            if not self.clock_explicit:
                timestamps = [
                    parse_dt(row.get("started_at"))
                    for row in self._read_jsonl(data_dir / "active_sessions.jsonl")
                ]
                timestamps.extend(
                    parse_dt(row.get("occurred"))
                    for row in self._read_jsonl(data_dir / "meter.jsonl")
                )
                timestamps = [ts for ts in timestamps if ts is not None]
                if timestamps:
                    self.now_value = max(timestamps)
            self._audit("feeds_loaded", source=str(data_dir))

    # ------------------------------------------------------------- master data
    def upsert_port(self, row: dict[str, Any], *, audit: bool = True) -> dict[str, Any]:
        required = ("port", "connector", "bank", "max_kw", "bank_kw")
        missing = [key for key in required if key not in row]
        if missing:
            raise YardError(f"端口资料缺少字段: {', '.join(missing)}", code="invalid_port")
        with self._lock:
            port_id = str(row["port"])
            self.ports[port_id] = {
                "port": port_id,
                "connector": str(row["connector"]),
                "bank": str(row["bank"]),
                "max_kw": float(row["max_kw"]),
                "bank_kw": float(row["bank_kw"]),
            }
            if audit:
                self._audit("port_upserted", port=port_id)
            return self.ports[port_id]

    def upsert_vehicle(
        self, row: dict[str, Any], *, audit: bool = True
    ) -> dict[str, Any]:
        required = (
            "booking",
            "vin",
            "connector",
            "ready",
            "handoff",
            "initial_wh",
            "minimum_wh",
            "interruptible",
            "fen_per_kwh",
        )
        missing = [key for key in required if key not in row]
        if missing:
            raise YardError(
                f"车辆资料缺少字段: {', '.join(missing)}", code="invalid_vehicle"
            )
        with self._lock:
            vin = str(row["vin"])
            booking_id = str(row["booking"])
            vehicle = {
                "booking": booking_id,
                "vin": vin,
                "connector": str(row["connector"]),
                "ready": row["ready"],
                "handoff": row["handoff"],
                "initial_wh": int(row["initial_wh"]),
                "minimum_wh": int(row["minimum_wh"]),
                "interruptible": bool(row["interruptible"]),
                "interruption_conditions": row.get("interruption_conditions"),
                "fen_per_kwh": int(row["fen_per_kwh"]),
                "power_limit_kw": row.get("power_limit_kw"),
                "order_paid": bool(row.get("order_paid", False)),
            }
            self.vehicles[vin] = vehicle
            booking = self.bookings.setdefault(
                booking_id,
                {
                    "booking": booking_id,
                    "vins": [],
                    "handoff": row["handoff"],
                    "original_handoff": row["handoff"],
                    "agreement_mode": "whole",
                    "split_agreements": [],
                    "status": "active",
                    "promised_plan_id": None,
                },
            )
            if vin not in booking["vins"]:
                booking["vins"].append(vin)
            # Reports for a booking should share the latest handoff unless a
            # confirmed change already set a different one.
            if booking.get("status") != "handoff_changed":
                booking["handoff"] = row["handoff"]
                booking["original_handoff"] = row["handoff"]
            if audit:
                self._audit("vehicle_upserted", vin=vin, booking=booking_id)
            return vehicle

    def report_fleet(self, payload: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            rows = payload.get("vehicles") if isinstance(payload.get("vehicles"), list) else [payload]
            saved = [self.upsert_vehicle(row) for row in rows if "vin" in row]
            if not saved:
                raise YardError("没有可登记的车辆", code="empty_fleet_report")
            return {"saved": len(saved), "vehicles": [v["vin"] for v in saved]}

    # ------------------------------------------------------------- occupancy
    def _session_occupies_at(self, session: dict[str, Any], at: datetime) -> bool:
        started = parse_dt(session["started_at"])
        assert started is not None
        if started > at:
            return False
        if not session.get("stop_confirmed"):
            return True
        stopped_at = parse_dt(session.get("stopped_at"))
        if stopped_at is None:
            return True
        return stopped_at > at

    def occupying_session_for_vin(
        self, vin: str, at: datetime | None = None
    ) -> dict[str, Any] | None:
        at = at or self._time()
        with self._lock:
            for session in self.sessions.values():
                if session["vin"] == vin and self._session_occupies_at(session, at):
                    return session
            return None

    def occupying_session_for_port(
        self, port: str, at: datetime | None = None
    ) -> dict[str, Any] | None:
        at = at or self._time()
        with self._lock:
            for session in self.sessions.values():
                if session["port"] == port and self._session_occupies_at(session, at):
                    return session
            return None

    def capacity(self, at: datetime | str | None = None) -> dict[str, Any]:
        at_dt = self._time(at)
        with self._lock:
            banks: dict[str, dict[str, Any]] = {}
            for port in self.ports.values():
                bank = banks.setdefault(
                    port["bank"],
                    {
                        "bank": port["bank"],
                        "bank_kw": port["bank_kw"],
                        "used_kw": 0.0,
                        "session_authorized_kw": 0.0,
                        "planned_job_kw": 0.0,
                        "available_kw": port["bank_kw"],
                        "ports": [],
                        "needs_confirmation_kw": 0.0,
                    },
                )
                occupant = self.occupying_session_for_port(port["port"], at_dt)
                job = None
                if occupant is None:
                    job = self._occupying_job_for_port(port["port"], at_dt)
                used_kw = 0.0
                source = "available"
                session_view = None
                if occupant is not None:
                    used_kw = float(occupant.get("authorized_kw") or 0.0)
                    source = "session"
                    bank["session_authorized_kw"] += used_kw
                    if occupant.get("control_state") in {"stop_unconfirmed", "unknown"} or (
                        occupant.get("stop_requested")
                        and not occupant.get("stop_confirmed")
                    ):
                        bank["needs_confirmation_kw"] += used_kw
                    session_view = self.session_view(occupant["session"])
                elif job is not None:
                    used_kw = float(job["kw"])
                    source = "job"
                    bank["planned_job_kw"] += used_kw
                bank["used_kw"] += used_kw
                bank["ports"].append(
                    {
                        "port": port["port"],
                        "connector": port["connector"],
                        "max_kw": port["max_kw"],
                        "status": "occupied" if used_kw else "available",
                        "used_kw": used_kw,
                        "source": source,
                        "session": session_view,
                        "job_id": None if job is None else job["id"],
                        "field_confirmation_required": bool(
                            occupant and occupant.get("needs_field_confirmation")
                        ),
                    }
                )
            for bank in banks.values():
                bank["used_kw"] = round(bank["used_kw"], 6)
                bank["available_kw"] = round(
                    max(0.0, bank["bank_kw"] - bank["used_kw"]), 6
                )
                bank["needs_confirmation_kw"] = round(
                    bank["needs_confirmation_kw"], 6
                )
            ports_needing = [
                self.field_confirmation_view(session)
                for session in self.sessions.values()
                if session.get("needs_field_confirmation")
                and self._session_occupies_at(session, at_dt)
            ]
            return {
                "at": iso(at_dt),
                "banks": list(banks.values()),
                "ports_needing_field_confirmation": ports_needing,
            }

    def _occupying_job_for_port(
        self, port_id: str, at: datetime
    ) -> dict[str, Any] | None:
        for job in self.jobs.values():
            if job["port"] != port_id or job["status"] not in OCCUPYING_JOB_STATUSES:
                continue
            start = parse_dt(job["start"])
            end = parse_dt(job["end"])
            assert start and end
            if start <= at < end:
                return job
        return None

    def field_confirmation_view(self, session: dict[str, Any]) -> dict[str, Any]:
        reasons = []
        if session.get("control_state") == "stop_unconfirmed":
            reasons.append("停机命令未收到应答，端口仍按占用处理")
        if session.get("pile_status") == "unknown":
            reasons.append("桩端会话状态为 unknown")
        if session.get("stop_confirmed") and not session.get("accepted_total_wh"):
            reasons.append("已停机但尚未收到有效累积计量")
        return {
            "session": session["session"],
            "port": session["port"],
            "vin": session["vin"],
            "control_state": session.get("control_state"),
            "pile_status": session.get("pile_status"),
            "authorized_kw": session.get("authorized_kw"),
            "reasons": reasons or ["需要现场核对端口状态"],
        }

    # ------------------------------------------------------------- pile starts
    def record_start(
        self, payload: dict[str, Any], *, audit: bool = True
    ) -> dict[str, Any]:
        required = ("session", "port", "vin", "started_at", "authorized_kw")
        missing = [key for key in required if key not in payload]
        if missing:
            raise YardError(
                f"启动回执缺少字段: {', '.join(missing)}", code="invalid_start"
            )
        session_id = str(payload["session"])
        port_id = str(payload["port"])
        vin = str(payload["vin"])
        with self._lock:
            if session_id in self.sessions:
                existing = self.sessions[session_id]
                if (
                    existing["port"] != port_id
                    or existing["vin"] != vin
                    or existing["started_at"] != payload["started_at"]
                ):
                    raise YardError(
                        "会话标识已用于不同的端口或车辆",
                        code="session_conflict",
                        status=409,
                        details={"session": session_id},
                    )
                return self.session_view(session_id)

            if port_id not in self.ports:
                raise YardError(
                    f"未知端口 {port_id}；请先报告端口资料",
                    code="unknown_port",
                    status=422,
                )
            if vin not in self.vehicles:
                raise YardError(
                    f"未知车辆 {vin}；请先报告车队资料或急单",
                    code="unknown_vin",
                    status=422,
                )

            at_dt = parse_dt(payload["started_at"])
            assert at_dt is not None
            vin_occupant = self.occupying_session_for_vin(vin, at_dt)
            if vin_occupant is not None:
                raise YardError(
                    "同一车辆已有未确认停止的接桩会话，不能在另一工位重复接桩",
                    code="vin_already_plugged",
                    status=409,
                    details={
                        "vin": vin,
                        "existing_session": vin_occupant["session"],
                        "existing_port": vin_occupant["port"],
                        "control_state": vin_occupant.get("control_state"),
                    },
                )
            port_occupant = self.occupying_session_for_port(port_id, at_dt)
            if port_occupant is not None:
                raise YardError(
                    "端口仍被未确认停止的会话占用",
                    code="port_occupied",
                    status=409,
                    details={
                        "port": port_id,
                        "existing_session": port_occupant["session"],
                        "vin": port_occupant["vin"],
                    },
                )

            authorized_kw = float(payload["authorized_kw"])
            port = self.ports[port_id]
            if authorized_kw > port["max_kw"] + 1e-9:
                raise YardError(
                    "授权功率超过端口上限",
                    code="authorized_power_exceeds_port",
                    status=422,
                    details={"port_max_kw": port["max_kw"]},
                )
            job_id = payload.get("job") or payload.get("job_id")
            linked_job = self.jobs.get(job_id) if job_id else None
            if linked_job is None:
                linked_job = next(
                    (
                        job
                        for job in self.jobs.values()
                        if job.get("vin") == vin
                        and job.get("port") == port_id
                        and job["status"]
                        in {"committed", "dispatched", "in_progress"}
                        and parse_dt(job["start"]) <= at_dt < parse_dt(job["end"])
                    ),
                    None,
                )
                if linked_job is not None:
                    job_id = linked_job["id"]

            capacity_now = self.capacity(at_dt)
            bank = next(b for b in capacity_now["banks"] if b["bank"] == port["bank"])
            available_kw = float(bank["available_kw"])
            # The linked job already holds this capacity; converting it into a
            # session must not count the same kilowatts twice.
            if (
                linked_job is not None
                and linked_job.get("port") == port_id
                and self._occupying_job_for_port(port_id, at_dt) is linked_job
            ):
                available_kw += float(linked_job["kw"])
            if authorized_kw > available_kw + 1e-9:
                raise YardError(
                    "启动功率会超过供电组当前可证明容量；未确认停止的容量不得提前释放",
                    code="bank_capacity_exceeded",
                    status=409,
                    details={
                        "bank": port["bank"],
                        "available_kw": round(available_kw, 6),
                        "requested_kw": authorized_kw,
                        "needs_confirmation_kw": bank["needs_confirmation_kw"],
                    },
                )

            session = {
                "session": session_id,
                "port": port_id,
                "vin": vin,
                "booking": self.vehicles[vin]["booking"],
                "started_at": payload["started_at"],
                "authorized_kw": authorized_kw,
                "pile_status": payload.get("status", "charging"),
                "control_state": "active",
                "stop_requested": False,
                "stop_requested_at": None,
                "stop_acknowledged": False,
                "stop_confirmed": False,
                "stopped_at": None,
                "needs_field_confirmation": False,
                "accepted_total_wh": None,
                "accepted_reading_event": None,
                "meter_anomalies": [],
                "settled": False,
                "settlement_id": None,
                "job_id": job_id,
                "raw_start": dict(payload),
            }
            self.sessions[session_id] = session
            if linked_job is not None:
                self.jobs[linked_job["id"]]["status"] = "in_progress"
                self.jobs[linked_job["id"]]["session"] = session_id
            if audit:
                self._audit("pile_start_recorded", session=session_id, port=port_id, vin=vin)
            return self.session_view(session_id)

    # ------------------------------------------------------------- pile stops
    def request_stop(
        self,
        session_id: str,
        *,
        client: Any | None = None,
        at: datetime | str | None = None,
    ) -> dict[str, Any]:
        with self._lock:
            if session_id not in self.sessions:
                raise YardError("会话不存在", code="session_not_found", status=404)
            session = self.sessions[session_id]
            at_iso = iso(self._time(at))
            session["stop_requested"] = True
            session["stop_requested_at"] = at_iso
            if not session.get("stop_confirmed"):
                session["control_state"] = "stop_pending"

            selected_client = client or self.pile_client
            transport_status = None
            transport_response: Any = None
            transport_error = None
            if selected_client is not None:
                try:
                    transport_status, transport_response = selected_client.stop(session_id)
                    session["stop_acknowledged"] = 200 <= int(transport_status) < 300
                except (PileTransportError, Exception) as exc:  # network/no reply
                    transport_error = str(exc)
                    session["control_state"] = "stop_unconfirmed"
                    session["pile_status"] = "unknown"
                    session["needs_field_confirmation"] = True

            self._audit(
                "stop_requested",
                session=session_id,
                transport_status=transport_status,
                transport_error=transport_error,
            )
            return {
                "session": self.session_view(session_id),
                "transport_status": transport_status,
                "transport_response": transport_response,
                "transport_error": transport_error,
                "occupancy_released": False,
                "note": "停机命令应答不证明已停机；未确认前端口和供电组容量仍被占用",
            }

    def poll_session(
        self,
        session_id: str,
        *,
        client: Any | None = None,
        at: datetime | str | None = None,
    ) -> dict[str, Any]:
        with self._lock:
            if session_id not in self.sessions:
                raise YardError("会话不存在", code="session_not_found", status=404)
            selected_client = client or self.pile_client
            if selected_client is None:
                raise YardError(
                    "未配置桩端客户端；可改用停机回执接口",
                    code="no_pile_client",
                    status=409,
                )
            try:
                status, payload = selected_client.get_session(session_id)
            except Exception as exc:
                session = self.sessions[session_id]
                session["pile_status"] = "unknown"
                session["control_state"] = "stop_unconfirmed"
                session["needs_field_confirmation"] = True
                self._audit("session_poll_failed", session=session_id, error=str(exc))
                return self.session_view(session_id)

            if status == 404:
                session = self.sessions[session_id]
                session["pile_status"] = "unknown"
                session["needs_field_confirmation"] = True
                return self.session_view(session_id)
            if status != 200 or not isinstance(payload, dict):
                raise YardError(
                    "桩端查询返回异常",
                    code="pile_bad_response",
                    status=502,
                    details={"status": status, "payload": payload},
                )
            return self.receive_session_status(session_id, payload, at=at)

    def receive_session_status(
        self,
        session_id: str,
        payload: dict[str, Any],
        *,
        at: datetime | str | None = None,
    ) -> dict[str, Any]:
        with self._lock:
            if session_id not in self.sessions:
                raise YardError("会话不存在", code="session_not_found", status=404)
            session = self.sessions[session_id]
            for field in ("port", "vin", "authorized_kw"):
                if field in payload and payload[field] is not None:
                    if field == "authorized_kw":
                        if float(payload[field]) != float(session[field]):
                            session["meter_anomalies"].append(
                                f"状态回执中的 {field} 与启动回执不一致"
                            )
                    elif str(payload[field]) != str(session[field]):
                        raise YardError(
                            "桩端状态与会话标识引用不一致",
                            code="session_identity_conflict",
                            status=409,
                            details={"field": field, "received": payload[field]},
                        )
            status = payload.get("status")
            if status == "stopped":
                return self.receive_stop_receipt(
                    session_id,
                    {"at": payload.get("at") or iso(self._time(at)), **payload},
                    at=at,
                )
            if status == "charging":
                session["pile_status"] = "charging"
                if session.get("stop_requested") and not session.get("stop_confirmed"):
                    # We have positive proof that the port is still charging.
                    session["control_state"] = "stop_pending"
                    session["needs_field_confirmation"] = False
                elif not session.get("stop_confirmed"):
                    session["control_state"] = "active"
                return self.session_view(session_id)
            if status == "unknown":
                session["pile_status"] = "unknown"
                session["control_state"] = "stop_unconfirmed"
                session["needs_field_confirmation"] = True
                return self.session_view(session_id)
            raise YardError("未知桩端状态", code="invalid_pile_status", status=422)

    def receive_stop_receipt(
        self,
        session_id: str,
        payload: dict[str, Any] | None = None,
        *,
        at: datetime | str | None = None,
    ) -> dict[str, Any]:
        payload = payload or {}
        with self._lock:
            if session_id not in self.sessions:
                raise YardError("会话不存在", code="session_not_found", status=404)
            session = self.sessions[session_id]
            stopped_at = payload.get("at") or payload.get("stopped_at") or iso(self._time(at))
            if session.get("stop_confirmed"):
                # Idempotent/late receipt: keep the earliest proven stop time.
                old = parse_dt(session["stopped_at"])
                new = parse_dt(stopped_at)
                if new and old and new < old:
                    session["stopped_at"] = stopped_at
                return self.session_view(session_id)

            session["stop_requested"] = True
            session["stop_confirmed"] = True
            session["stopped_at"] = stopped_at
            session["pile_status"] = "stopped"
            session["control_state"] = "stopped"
            session["needs_field_confirmation"] = False
            if session.get("job_id") in self.jobs:
                self.jobs[session["job_id"]]["status"] = "completed"
                self.jobs[session["job_id"]]["actual_end"] = stopped_at
            if session.get("accepted_total_wh") is None:
                session["meter_anomalies"].append("停机已确认，等待有效累积计量")
                review_id = self._next_id("DR")
                self.dispatch_reviews[review_id] = {
                    "id": review_id,
                    "kind": "meter_required",
                    "session": session_id,
                    "port": session["port"],
                    "vin": session["vin"],
                    "status": "awaiting_meter",
                    "created_at": iso(self._time(at)),
                }
            self._maybe_create_under_delivery_review(session)
            self._audit("stop_confirmed", session=session_id, stopped_at=stopped_at)
            return self.session_view(session_id)

    # ------------------------------------------------------------- meter
    def receive_meter(
        self, payload: dict[str, Any], *, audit: bool = True
    ) -> dict[str, Any]:
        required = ("event", "session", "port", "vin", "occurred", "total_wh", "supersedes")
        missing = [key for key in required if key not in payload]
        if missing:
            raise YardError(
                f"计量消息缺少字段: {', '.join(missing)}", code="invalid_meter"
            )
        event_id = str(payload["event"])
        with self._lock:
            if event_id in self.meter_events:
                old = self.meter_events[event_id]
                if old.get("raw") != payload:
                    raise YardError(
                        "计量事件 ID 已存在但内容不同",
                        code="meter_event_conflict",
                        status=409,
                    )
                return old

            event = {
                "event": event_id,
                "session": str(payload["session"]),
                "port": str(payload["port"]),
                "vin": str(payload["vin"]),
                "occurred": payload["occurred"],
                "total_wh": int(payload["total_wh"]),
                "supersedes": str(payload.get("supersedes") or ""),
                "sequence": len(self.meter_order),
                "status": "accepted",
                "superseded_by": None,
                "validation_errors": [],
                "raw": dict(payload),
            }
            self.meter_events[event_id] = event
            self.meter_order.append(event_id)

            session = self.sessions.get(event["session"])
            if session is None:
                event["status"] = "quarantined"
                event["validation_errors"].append("没有匹配的有效启动会话")
            elif (
                session["port"] != event["port"]
                or session["vin"] != event["vin"]
                or session["session"] != event["session"]
            ):
                event["status"] = "quarantined"
                event["validation_errors"].append("会话、端口或车辆三元组不一致")

            target_id = event["supersedes"]
            if target_id:
                target = self.meter_events.get(target_id)
                if target is None:
                    event["status"] = "buffered_correction"
                    event["validation_errors"].append("被更正事件尚未到达，先缓冲")
                elif target["session"] != event["session"]:
                    event["status"] = "quarantined"
                    event["validation_errors"].append("更正消息不能跨会话取代读数")
                elif event["status"] == "accepted":
                    target["superseded_by"] = event_id

            # A late event may complete one or more buffered corrections that
            # name it as their supersession target.  This applies whether the
            # late event is an original or is itself a correction.
            for other_id, other in self.meter_events.items():
                if (
                    other.get("supersedes") == event_id
                    and other["status"] == "buffered_correction"
                ):
                    if other["session"] == event["session"]:
                        other["status"] = "accepted"
                        event["superseded_by"] = other_id

            self._reconcile_session_meter(event["session"])
            if audit:
                self._audit("meter_received", event=event_id, session=event["session"])
            return self.meter_events[event_id]

    def _session_effective_reading(self, session_id: str) -> dict[str, Any] | None:
        events = [
            e
            for e in self.meter_events.values()
            if e["session"] == session_id
            and e["status"] == "accepted"
            and not e.get("superseded_by")
        ]
        if not events:
            return None
        # Cumulative Wh must not be summed.  Max protects against an out-of-order
        # older cumulative frame; an explicit superseding correction is excluded
        # only through superseded_by.
        total = max(int(e["total_wh"]) for e in events)
        latest = max(events, key=lambda e: (parse_dt(e["occurred"]), e["sequence"]))
        chosen = max(events, key=lambda e: int(e["total_wh"]))
        return {"total_wh": total, "latest_event": latest, "max_event": chosen, "events": events}

    def _reconcile_session_meter(self, session_id: str) -> None:
        session = self.sessions.get(session_id)
        if session is None:
            return
        reading = self._session_effective_reading(session_id)
        old_total = session.get("accepted_total_wh")
        if reading is None:
            session["accepted_total_wh"] = None
            session["accepted_reading_event"] = None
            return
        new_total = int(reading["total_wh"])
        session["accepted_total_wh"] = new_total
        session["accepted_reading_event"] = reading["max_event"]["event"]
        latest_total = int(reading["latest_event"]["total_wh"])
        anomalies = [
            a for a in session.get("meter_anomalies", []) if "累积读数回退" not in a
        ]
        if latest_total < new_total:
            anomalies.append(
                f"最新未取代读数为 {latest_total}Wh，低于有效累积最大值 {new_total}Wh，已保留较大累积值"
            )
        session["meter_anomalies"] = anomalies

        if session.get("settled") and old_total != new_total:
            self._create_meter_adjustment(session, old_total or 0, new_total)
        self._maybe_create_under_delivery_review(session)

    # ------------------------------------------------------------- vehicle views
    def delivered_wh(self, vin: str) -> int:
        with self._lock:
            total = 0
            for session in self.sessions.values():
                if session["vin"] == vin:
                    total += int(session.get("accepted_total_wh") or 0)
            return total

    def vehicle_view(
        self, vin: str, at: datetime | str | None = None
    ) -> dict[str, Any]:
        at_dt = self._time(at)
        with self._lock:
            vehicle = self.vehicles[vin]
            metered = self.delivered_wh(vin)
            achieved = int(vehicle["initial_wh"]) + metered
            remaining = max(0, int(vehicle["minimum_wh"]) - achieved)
            deadline = parse_dt(vehicle["handoff"])
            booking = self.bookings[vehicle["booking"]]
            if booking.get("effective_handoff"):
                deadline = parse_dt(booking["effective_handoff"])
            remaining_seconds = (deadline - at_dt).total_seconds() if deadline else 0
            remaining_minutes = max(0, int(remaining_seconds // 60))
            compatible_ports = [
                p["port"]
                for p in self.ports.values()
                if p["connector"] == vehicle["connector"]
            ]
            current = self.occupying_session_for_vin(vin, at_dt)
            return {
                **vehicle,
                "at": iso(at_dt),
                "metered_wh": metered,
                "achieved_wh": achieved,
                "remaining_wh": remaining,
                "requirement_met": remaining == 0,
                "deadline": iso(deadline),
                "remaining_minutes": remaining_minutes,
                "required_kw_by_deadline": (
                    round(remaining * 60.0 / (1000.0 * remaining_minutes), 3)
                    if remaining_minutes > 0
                    else None
                ),
                "compatible_ports": compatible_ports,
                "current_session": None
                if current is None
                else self.session_view(current["session"]),
            }

    def booking_view(
        self, booking_id: str, at: datetime | str | None = None
    ) -> dict[str, Any]:
        at_dt = self._time(at)
        with self._lock:
            if booking_id not in self.bookings:
                raise YardError("车队/订单不存在", code="booking_not_found", status=404)
            booking = self.bookings[booking_id]
            vehicles = [self.vehicle_view(vin, at_dt) for vin in booking["vins"]]
            unmet = [v for v in vehicles if not v["requirement_met"]]
            return {
                **booking,
                "vehicles": vehicles,
                "all_requirements_met": not unmet,
                "unmet_vins": [v["vin"] for v in unmet],
                "collectable": not unmet and booking["agreement_mode"] == "whole",
            }

    def fleet_views(self, at: datetime | str | None = None) -> list[dict[str, Any]]:
        with self._lock:
            return [self.booking_view(bid, at) for bid in sorted(self.bookings)]

    # ------------------------------------------------------------- sessions view
    def session_view(self, session_id: str) -> dict[str, Any]:
        session = self.sessions[session_id]
        meter_events = [
            e
            for e in self.meter_events.values()
            if e["session"] == session_id and e["status"] != "quarantined"
        ]
        charge_milli = int(session.get("accepted_total_wh") or 0) * int(
            self.vehicles[session["vin"]]["fen_per_kwh"]
        ) if session["vin"] in self.vehicles else 0
        return {
            "session": session_id,
            "port": session["port"],
            "vin": session["vin"],
            "booking": session.get("booking"),
            "started_at": session["started_at"],
            "authorized_kw": session["authorized_kw"],
            "pile_status": session.get("pile_status"),
            "control_state": session.get("control_state"),
            "stop_requested": session.get("stop_requested"),
            "stop_requested_at": session.get("stop_requested_at"),
            "stop_acknowledged": session.get("stop_acknowledged"),
            "stop_confirmed": session.get("stop_confirmed"),
            "stopped_at": session.get("stopped_at"),
            "needs_field_confirmation": session.get("needs_field_confirmation"),
            "accepted_total_wh": session.get("accepted_total_wh"),
            "accepted_reading_event": session.get("accepted_reading_event"),
            "meter_event_count": len(meter_events),
            "meter_anomalies": session.get("meter_anomalies", []),
            "charge_milli_fen": charge_milli,
            "charge_fen": money_json(charge_milli),
            "charge_fen_exact": str(money_json(charge_milli))
            if isinstance(money_json(charge_milli), int)
            else f"{charge_milli / 1000:.3f}".rstrip("0").rstrip("."),
            "settled": session.get("settled"),
            "settlement_id": session.get("settlement_id"),
            "job_id": session.get("job_id"),
        }

    # ------------------------------------------------------------- planning
    def _schedule_inputs(
        self,
        booking_ids: list[str] | None = None,
        *,
        at: datetime,
        deadline_overrides: dict[str, str] | None = None,
        remaining_overrides: dict[str, int] | None = None,
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
        deadline_overrides = deadline_overrides or {}
        remaining_overrides = remaining_overrides or {}
        with self._lock:
            if booking_ids is None:
                booking_ids = sorted(self.bookings)
            vehicles: list[dict[str, Any]] = []
            for booking_id in booking_ids:
                booking = self.bookings[booking_id]
                for vin in booking["vins"]:
                    view = self.vehicle_view(vin, at)
                    deadline = deadline_overrides.get(vin) or deadline_overrides.get(
                        booking_id
                    ) or booking.get("effective_handoff") or booking["handoff"]
                    remaining = remaining_overrides.get(vin, view["remaining_wh"])
                    vehicles.append(
                        {
                            "vin": vin,
                            "booking": booking_id,
                            "connector": view["connector"],
                            "ready": view["ready"],
                            "deadline": deadline,
                            "remaining_wh": remaining,
                            "interruptible": view["interruptible"],
                            "power_limit_kw": view.get("power_limit_kw"),
                        }
                    )
            ports = list(self.ports.values())
            reservations = self._protected_reservations(exclude_bookings=set(booking_ids))
            return vehicles, ports, reservations

    def _protected_reservations(
        self,
        *,
        exclude_bookings: set[str] | None = None,
        include_soft_holds: bool = False,
    ) -> list[dict[str, Any]]:
        exclude_bookings = exclude_bookings or set()
        result = []
        for job in self.jobs.values():
            status = str(job["status"])
            if status == "draft" or status == "completed" or status.startswith("cancelled"):
                continue
            if job["status"] == "soft_hold" and not include_soft_holds:
                continue
            if job.get("booking") in exclude_bookings:
                continue
            if job.get("booking") in exclude_bookings:
                continue
            result.append(
                {
                    "uid": job["id"],
                    "vin": job.get("vin"),
                    "booking": job.get("booking"),
                    "port": job["port"],
                    "start": job["start"],
                    "end": job["end"],
                    "kw": job["kw"],
                    "kind": job.get("status", "reserved"),
                }
            )
        return result

    def evaluate_bookings(
        self,
        booking_ids: list[str] | str | None = None,
        payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        payload = payload or {}
        with self._lock:
            at = self._time(payload.get("at"))
            if isinstance(booking_ids, str):
                ids = [booking_ids]
            elif booking_ids is None:
                ids = payload.get("booking_ids") or sorted(self.bookings)
            else:
                ids = list(booking_ids)
            overrides = {}
            if payload.get("handoff"):
                for booking_id in ids:
                    overrides[booking_id] = payload["handoff"]
            overrides.update(payload.get("deadline_overrides") or {})
            vehicles, ports, reservations = self._schedule_inputs(
                ids, at=at, deadline_overrides=overrides
            )
            # Active sessions are field-proven occupancy.  For bookings being
            # replanned we only block the current minute and then let the
            # optimizer propose power/port changes.  Sessions belonging to other
            # promises are conservatively held at their authorized power until
            # their proven stop or booking deadline; an unconfirmed stop never
            # creates forward free capacity by itself.
            active_sessions = [
                session
                for session in self.sessions.values()
                if self._session_occupies_at(session, at)
            ]
            external_vins = {
                session["vin"]
                for session in active_sessions
                if session.get("booking") not in ids
            }
            if external_vins:
                reservations = [r for r in reservations if r.get("vin") not in external_vins]

            port_changes: list[dict[str, Any]] = []
            for session in active_sessions:
                booking_id = session.get("booking")
                if booking_id in ids:
                    start = max(parse_dt(session["started_at"]), at)
                    end = at + timedelta(minutes=1)
                    reservations.append(
                        {
                            "uid": f"current-{session['session']}",
                            "vin": session["vin"],
                            "port": session["port"],
                            "start": iso(start),
                            "end": iso(end),
                            "kw": session["authorized_kw"],
                        }
                    )
                else:
                    booking = self.bookings.get(booking_id or "")
                    if session.get("stop_confirmed") and session.get("stopped_at"):
                        end = parse_dt(session["stopped_at"])
                    elif booking:
                        end = parse_dt(booking.get("effective_handoff") or booking["handoff"])
                    else:
                        end = at + timedelta(hours=24)
                    assert end is not None
                    if end <= at:
                        end = at + timedelta(minutes=1)
                    reservations.append(
                        {
                            "uid": f"external-session-{session['session']}",
                            "vin": session["vin"],
                            "booking": booking_id,
                            "port": session["port"],
                            "start": iso(at),
                            "end": iso(end),
                            "kw": session["authorized_kw"],
                        }
                    )

            allow_after = bool(payload.get("allow_after_deadline", False))
            result = best_schedule(
                vehicles=vehicles,
                ports=ports,
                reservations=reservations,
                start=at,
                allow_after_deadline=allow_after,
            )
            result["booking_ids"] = ids
            result["whole_group_promisable"] = result["feasible"]
            result["unmet"] = [
                self.vehicle_view(vin, at)
                for vin, item in result["vehicles"].items()
                if not item["on_time"]
            ]
            result["port_changes"] = self._port_changes_from_schedule(result, at)
            return result

    def _port_changes_from_schedule(
        self, result: dict[str, Any], at: datetime
    ) -> list[dict[str, Any]]:
        changes = []
        jobs_by_vin: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for job in result.get("jobs", []):
            jobs_by_vin[job["vin"]].append(job)
        for vin, jobs in jobs_by_vin.items():
            jobs.sort(key=lambda job: job["start"])
            current = self.occupying_session_for_vin(vin, at)
            previous_port = current["port"] if current else None
            for job in jobs:
                if previous_port and job["port"] != previous_port:
                    changes.append(
                        {
                            "vin": vin,
                            "from_port": previous_port,
                            "to_port": job["port"],
                            "stop_session": current["session"] if current else None,
                            "new_job_start": job["start"],
                            "requires_stop_confirmation": bool(current),
                            "rule": "旧会话未确认停止前，新车位不能把该 VIN 记为已接桩",
                        }
                    )
                previous_port = job["port"]
        return changes

    def commit_plan(
        self, evaluation: dict[str, Any], *, name: str | None = None
    ) -> dict[str, Any]:
        with self._lock:
            if not evaluation.get("feasible"):
                raise YardError(
                    "不能承诺不可兑现的整组计划",
                    code="plan_not_feasible",
                    status=409,
                    details={"unmet": evaluation.get("unmet", [])},
                )
            plan_id = self._next_id("P")
            plan = {
                "id": plan_id,
                "name": name,
                "status": "committed",
                "committed_at": iso(self._time()),
                "booking_ids": evaluation["booking_ids"],
                "jobs": [],
                "evaluation": evaluation,
            }
            self.plans[plan_id] = plan
            for job_spec in evaluation.get("jobs", []):
                job_id = self._next_id("J")
                job = {
                    "id": job_id,
                    "plan_id": plan_id,
                    "booking": job_spec.get("booking"),
                    "vin": job_spec["vin"],
                    "port": job_spec["port"],
                    "bank": job_spec["bank"],
                    "start": job_spec["start"],
                    "end": job_spec["end"],
                    "kw": job_spec["kw"],
                    "planned_wh": job_spec["planned_wh"],
                    "status": "committed",
                    "session": None,
                }
                self.jobs[job_id] = job
                plan["jobs"].append(job_id)
            for booking_id in evaluation["booking_ids"]:
                if booking_id in self.bookings:
                    self.bookings[booking_id]["promised_plan_id"] = plan_id
            self._audit("plan_committed", plan=plan_id, bookings=evaluation["booking_ids"])
            return self.plan_view(plan_id)

    def plan_view(self, plan_id: str) -> dict[str, Any]:
        plan = self.plans[plan_id]
        return {
            **plan,
            "job_views": [self.jobs[jid] for jid in plan["jobs"] if jid in self.jobs],
        }

    def dispatch(self, payload: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            plan_id = payload.get("plan_id")
            if plan_id:
                if plan_id not in self.plans:
                    raise YardError("计划不存在", code="plan_not_found", status=404)
                plan = self.plans[plan_id]
                if plan["status"] != "committed":
                    raise YardError("只能派发已承诺计划", code="plan_not_committed", status=409)
                dispatched = []
                for job_id in plan["jobs"]:
                    job = self.jobs[job_id]
                    if job["status"] in {"committed", "draft"}:
                        job["status"] = "dispatched"
                    dispatched.append(job)
                plan["status"] = "dispatched"
                return {"dispatched": len(dispatched), "jobs": dispatched}

            assignments = payload.get("assignments") or []
            if not assignments:
                raise YardError("请提供 plan_id 或 assignments", code="empty_dispatch")
            created = []
            at = self._time(payload.get("at"))
            for assignment in assignments:
                self._validate_assignment(assignment, at)
                job_id = self._next_id("J")
                job = {
                    "id": job_id,
                    "plan_id": None,
                    "booking": assignment.get("booking"),
                    "vin": assignment["vin"],
                    "port": assignment["port"],
                    "bank": self.ports[assignment["port"]]["bank"],
                    "start": assignment["start"],
                    "end": assignment["end"],
                    "kw": float(assignment["kw"]),
                    "planned_wh": assignment.get("planned_wh"),
                    "status": "dispatched",
                    "session": None,
                }
                self.jobs[job_id] = job
                created.append(job)
            return {"dispatched": len(created), "jobs": created}

    def _validate_assignment(self, assignment: dict[str, Any], at: datetime) -> None:
        for key in ("vin", "port", "start", "end", "kw"):
            if key not in assignment:
                raise YardError(f"派工作业缺少 {key}", code="invalid_job")
        if assignment["port"] not in self.ports:
            raise YardError("未知端口", code="unknown_port", status=422)
        if assignment["vin"] not in self.vehicles:
            raise YardError("未知车辆", code="unknown_vin", status=422)
        start = parse_dt(assignment["start"])
        end = parse_dt(assignment["end"])
        assert start and end
        if end <= start:
            raise YardError("作业结束时间必须晚于开始时间", code="invalid_job_window")
        port = self.ports[assignment["port"]]
        if assignment["vin"] in self.vehicles:
            vehicle = self.vehicles[assignment["vin"]]
            if vehicle["connector"] != port["connector"]:
                raise YardError("接口型号不匹配", code="connector_mismatch", status=409)
        if float(assignment["kw"]) > port["max_kw"] + 1e-9:
            raise YardError("作业功率超过端口上限", code="port_power_exceeded", status=409)
        # Conflict against persisted jobs/sessions is checked over the interval.
        for job in self.jobs.values():
            if job["status"] == "completed" or str(job["status"]).startswith("cancelled"):
                continue
            other_start = parse_dt(job["start"])
            other_end = parse_dt(job["end"])
            assert other_start and other_end
            if job["port"] == assignment["port"] and start < other_end and end > other_start:
                raise YardError(
                    "派工时间与同一端口的未结束作业冲突",
                    code="job_port_conflict",
                    status=409,
                    details={"existing_job": job["id"]},
                )

    def reassign_vehicle(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Create a port/power reassignment and optionally issue the old stop."""
        with self._lock:
            vin = payload.get("vin")
            if vin not in self.vehicles:
                raise YardError("未知车辆", code="unknown_vin", status=404)
            at = self._time(payload.get("at"))
            old_session = self.occupying_session_for_vin(vin, at)
            new_port = payload.get("port") or payload.get("new_port")
            if not new_port:
                raise YardError("换桩需要 new_port/port", code="port_required")
            if old_session and old_session["port"] == new_port and not payload.get("force"):
                raise YardError("车辆已在该端口；如需调整功率请直接派工作业", code="same_port")

            start = payload.get("start") or iso(at + timedelta(minutes=1))
            end = payload.get("end")
            if not end:
                minutes = int(payload.get("minutes", 60))
                end = iso(parse_dt(start) + timedelta(minutes=minutes))
            assignment = {
                "vin": vin,
                "booking": self.vehicles[vin]["booking"],
                "port": new_port,
                "start": start,
                "end": end,
                "kw": payload.get("kw", self.ports[new_port]["max_kw"]),
            }
            created = self.dispatch({"assignments": [assignment], "at": iso(at)})
            stop_result = None
            if old_session and payload.get("stop_old", True):
                stop_result = self.request_stop(old_session["session"], at=iso(at))
            return {
                "job": created["jobs"][0],
                "old_session": None if old_session is None else self.session_view(old_session["session"]),
                "stop_result": stop_result,
                "start_rule": "旧会话确认停止前，新端口的启动回执会被拒绝，避免同一车辆重复接桩",
            }

    # ------------------------------------------------------------- urgent orders
    def report_urgent_orders(self, payload: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            at = self._time(payload.get("at"))
            orders = payload.get("orders")
            if orders is None and "booking" in payload:
                orders = [payload]
            if not isinstance(orders, list) or not orders:
                raise YardError("急单内容为空", code="empty_urgent_order")

            order_records = []
            candidate_bookings: list[str] = []
            deadline_overrides: dict[str, str] = {}
            for order in orders:
                booking_id = str(order.get("booking") or self._next_id("U"))
                vehicles = order.get("vehicles") or []
                for vehicle in vehicles:
                    row = dict(vehicle)
                    row["booking"] = booking_id
                    row.setdefault("handoff", order.get("handoff"))
                    self.upsert_vehicle(row)
                if booking_id not in self.bookings:
                    raise YardError(
                        "急单必须引用已有 booking，或在 vehicles 中提供至少一辆完整车辆资料",
                        code="empty_urgent_order",
                    )
                if order.get("handoff") and booking_id in self.bookings:
                    self.bookings[booking_id]["requested_handoff"] = order["handoff"]
                    deadline_overrides[booking_id] = order["handoff"]
                order_id = order.get("order_id") or self._next_id("UO")
                record = {
                    "id": order_id,
                    "booking": booking_id,
                    "kind": "urgent",
                    "reported_at": iso(at),
                    "requested_handoff": order.get("handoff"),
                    "priority": order.get("priority", "normal"),
                    "status": "reported",
                    "raw": order,
                }
                self.urgent_orders[order_id] = record
                order_records.append(record)
                candidate_bookings.append(booking_id)

            assessment = self.assess_capacity(
                {
                    "at": iso(at),
                    "candidate_bookings": candidate_bookings,
                    "deadline_overrides": deadline_overrides,
                }
            )
            for record in order_records:
                record["assessment_id"] = assessment["id"]
                if payload.get("auto_accept") and assessment["all_original_available"]:
                    self.accept_urgent_order(record["id"], {"at": iso(at)})
            return {
                "orders": list(self.urgent_orders[o["id"]] for o in order_records),
                "assessment": assessment,
            }

    def assess_capacity(self, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        payload = payload or {}
        with self._lock:
            at = self._time(payload.get("at"))
            candidate_bookings = list(
                payload.get("candidate_bookings") or payload.get("booking_ids") or []
            )
            if not candidate_bookings:
                candidate_bookings = [
                    bid
                    for bid, booking in self.bookings.items()
                    if booking.get("kind") == "urgent" or booking.get("status") == "active"
                ]
            assessment_id = self._next_id("CA")
            decisions: list[dict[str, Any]] = []
            deadline_overrides = payload.get("deadline_overrides") or {}

            # Existing promises are protected and reported as kept.
            protected_ids = [
                bid
                for bid, booking in self.bookings.items()
                if booking.get("promised_plan_id") and bid not in candidate_bookings
            ]
            for bid in protected_ids:
                evaluation = self.evaluate_bookings([bid], {"at": iso(at)})
                decisions.append(
                    {
                        "booking": bid,
                        "classification": "original_plan_kept",
                        "label_zh": "能继续交原方案",
                        "whole_group_feasible": evaluation["feasible"],
                        "plan_id": self.bookings[bid].get("promised_plan_id"),
                    }
                )

            joint = self.evaluate_bookings(
                candidate_bookings,
                {"at": iso(at), "deadline_overrides": deadline_overrides},
            )
            if joint["feasible"]:
                for bid in candidate_bookings:
                    decisions.append(
                        {
                            "booking": bid,
                            "classification": "original_plan_available",
                            "label_zh": "可按原整组时间兑现",
                            "whole_group_feasible": True,
                            "schedule": self._schedule_summary_for_booking(joint, bid),
                        }
                    )
                assessment = {
                    "id": assessment_id,
                    "at": iso(at),
                    "candidate_bookings": candidate_bookings,
                    "all_original_available": True,
                    "admissible": True,
                    "joint_schedule": joint,
                    "decisions": decisions,
                }
                self._audit("capacity_assessed", id=assessment_id, admissible=True)
                return assessment

            # Triage deterministically by deadline then booking id.
            sorted_ids = sorted(
                candidate_bookings,
                key=lambda bid: (deadline_overrides.get(bid) or self.bookings[bid]["handoff"], bid),
            )
            soft_reservation_extra: list[dict[str, Any]] = []
            for bid in sorted_ids:
                bid_override = (
                    {bid: deadline_overrides[bid]} if bid in deadline_overrides else {}
                )
                full = self.evaluate_bookings(
                    [bid],
                    {"at": iso(at), "deadline_overrides": bid_override},
                )
                # Include accepted soft holds by temporarily adding them as jobs?
                # Simpler: evaluate_bookings only sees persisted jobs. To keep
                # batch interactions, commit holds as draft jobs.
                if full["feasible"]:
                    decisions.append(
                        {
                            "booking": bid,
                            "classification": "original_plan_available",
                            "label_zh": "能继续交原方案",
                            "whole_group_feasible": True,
                            "schedule": self._schedule_summary_for_booking(full, bid),
                        }
                    )
                    self._store_soft_hold(bid, full, assessment_id)
                    continue

                vins = list(self.bookings[bid]["vins"])
                vehicles, ports, reservations = self._schedule_inputs(
                    [bid], at=at, deadline_overrides=bid_override
                )
                reservations.extend(
                    r for r in self._soft_holds(assessment_id)
                )
                subsets = feasible_subsets(
                    vins=vins,
                    vehicles=vehicles,
                    ports=ports,
                    reservations=reservations,
                    start=at,
                )
                eta = self.evaluate_bookings(
                    [bid],
                    {
                        "at": iso(at),
                        "allow_after_deadline": True,
                        "deadline_overrides": bid_override,
                    },
                )
                if subsets:
                    subset, subset_schedule = subsets[0]
                    subset_set = set(subset)
                    rest = [v for v in vins if v not in subset_set]
                    proposal_id = self._next_id("SP")
                    proposal = {
                        "id": proposal_id,
                        "booking": bid,
                        "status": "awaiting_buyer",
                        "original_handoff": self.bookings[bid]["handoff"],
                        "assessed_deadline": deadline_overrides.get(bid)
                        or self.bookings[bid]["handoff"],
                        "first_group_vins": list(subset),
                        "remaining_vins": rest,
                        "first_group_schedule": subset_schedule,
                        "whole_group_eta": eta.get("max_finish_at"),
                        "buyer_confirmed": False,
                    }
                    self.bookings[bid].setdefault("split_proposals", []).append(proposal)
                    decisions.append(
                        {
                            "booking": bid,
                            "classification": "split_requires_buyer",
                            "label_zh": "可拆出一组，但须等买方同意拆组",
                            "whole_group_feasible": False,
                            "partial_group_available": list(subset),
                            "remaining_vins": rest,
                            "proposal_id": proposal_id,
                            "proposal": proposal,
                            "whole_group_eta": eta.get("max_finish_at"),
                        }
                    )
                    self._store_soft_hold(bid, subset_schedule, assessment_id, suffix=list(subset))
                else:
                    decisions.append(
                        {
                            "booking": bid,
                            "classification": "handoff_time_change_required",
                            "label_zh": "当前移交时间无法兑现，需要改移交时间",
                            "whole_group_feasible": False,
                            "proposed_handoff": eta.get("max_finish_at"),
                            "eta_schedule": eta,
                            "unmet": full.get("unmet", []),
                        }
                    )

            assessment = {
                "id": assessment_id,
                "at": iso(at),
                "candidate_bookings": candidate_bookings,
                "all_original_available": False,
                "admissible": False,
                "joint_schedule": joint,
                "decisions": decisions,
                "note": "部分车辆有电不等于整组已兑现；拆组必须取得买方确认",
            }
            self._audit("capacity_assessed", id=assessment_id, admissible=False)
            return assessment

    def _schedule_summary_for_booking(
        self, evaluation: dict[str, Any], booking_id: str
    ) -> dict[str, Any]:
        jobs = [j for j in evaluation.get("jobs", []) if j.get("booking") == booking_id]
        return {
            "feasible": evaluation["feasible"],
            "max_finish_at": evaluation.get("max_finish_at"),
            "job_count": len(jobs),
            "jobs": jobs,
        }

    def _store_soft_hold(
        self,
        booking_id: str,
        schedule: dict[str, Any],
        assessment_id: str,
        *,
        suffix: list[str] | None = None,
    ) -> None:
        # Draft jobs are visible to _protected_reservations? It excludes draft.
        # We therefore store separate soft holds in counters? Simpler: create
        # jobs with status ``soft_hold`` and include that status in reservations.
        for spec in schedule.get("jobs", []):
            if suffix is not None and spec["vin"] not in suffix:
                continue
            job_id = self._next_id("SH")
            self.jobs[job_id] = {
                "id": job_id,
                "assessment_id": assessment_id,
                "plan_id": None,
                "booking": booking_id,
                "vin": spec["vin"],
                "port": spec["port"],
                "bank": spec["bank"],
                "start": spec["start"],
                "end": spec["end"],
                "kw": spec["kw"],
                "planned_wh": spec.get("planned_wh"),
                "status": "soft_hold",
                "session": None,
            }

    def _soft_holds(self, assessment_id: str) -> list[dict[str, Any]]:
        return [
            {
                "uid": job["id"],
                "vin": job.get("vin"),
                "port": job["port"],
                "start": job["start"],
                "end": job["end"],
                "kw": job["kw"],
            }
            for job in self.jobs.values()
            if job.get("assessment_id") == assessment_id and job["status"] == "soft_hold"
        ]

    def accept_urgent_order(
        self, order_id: str, payload: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        payload = payload or {}
        with self._lock:
            if order_id not in self.urgent_orders:
                raise YardError("急单不存在", code="urgent_order_not_found", status=404)
            order = self.urgent_orders[order_id]
            booking_id = order["booking"]
            evaluation_payload = dict(payload)
            if (
                not payload.get("allow_original_deadline")
                and not payload.get("handoff")
                and order.get("requested_handoff")
            ):
                evaluation_payload["handoff"] = order["requested_handoff"]
            evaluation = self.evaluate_bookings([booking_id], evaluation_payload)
            if not evaluation["feasible"]:
                raise YardError(
                    "急单整组计划不可兑现；请先确认拆组或修改移交时间",
                    code="urgent_not_admissible",
                    status=409,
                    details={"evaluation": evaluation},
                )
            plan = self.commit_plan(evaluation, name=f"urgent:{order_id}")
            if order.get("requested_handoff") and not payload.get("allow_original_deadline"):
                self.bookings[booking_id]["effective_handoff"] = order["requested_handoff"]
            order["status"] = "accepted"
            order["plan_id"] = plan["id"]
            return {"order": order, "plan": plan}

    def confirm_split_proposal(
        self, proposal_id: str, payload: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        payload = payload or {}
        with self._lock:
            booking_id: str | None = None
            proposal: dict[str, Any] | None = None
            for bid, booking in self.bookings.items():
                for candidate in booking.get("split_proposals", []):
                    if candidate.get("id") == proposal_id:
                        booking_id = bid
                        proposal = candidate
                        break
            if proposal is None or booking_id is None:
                raise YardError("拆组方案不存在", code="split_proposal_not_found", status=404)
            if not payload.get("buyer_confirmed"):
                raise YardError(
                    "拆组必须取得买方确认",
                    code="buyer_confirmation_required",
                    status=409,
                )
            at = self._time(payload.get("at"))
            agreement_id = self._next_id("SA")
            agreement = {
                "id": agreement_id,
                "proposal": proposal_id,
                "booking": booking_id,
                "groups": [
                    {
                        "name": "confirmed_first_group",
                        "vins": proposal["first_group_vins"],
                        "handoff": proposal.get("original_handoff"),
                    },
                    {
                        "name": "remaining_group",
                        "vins": proposal["remaining_vins"],
                        "handoff": proposal.get("whole_group_eta"),
                    },
                ],
                "confirmed_at": iso(at),
            }
            booking = self.bookings[booking_id]
            booking["agreement_mode"] = "split"
            booking["split_agreements"].append(agreement)
            proposal["status"] = "buyer_confirmed"
            proposal["buyer_confirmed"] = True

            plan_id = self._next_id("P")
            job_ids = []
            schedule = proposal.get("first_group_schedule") or {}
            for spec in schedule.get("jobs", []):
                job_id = self._next_id("J")
                self.jobs[job_id] = {
                    "id": job_id,
                    "plan_id": plan_id,
                    "booking": booking_id,
                    "vin": spec["vin"],
                    "port": spec["port"],
                    "bank": spec["bank"],
                    "start": spec["start"],
                    "end": spec["end"],
                    "kw": spec["kw"],
                    "planned_wh": spec.get("planned_wh"),
                    "status": "committed",
                    "session": None,
                }
                job_ids.append(job_id)
            self.plans[plan_id] = {
                "id": plan_id,
                "status": "split_committed",
                "booking_ids": [booking_id],
                "split_agreement": agreement_id,
                "jobs": job_ids,
                "evaluation": schedule,
            }
            proposal["plan_id"] = plan_id
            self._audit("split_confirmed", proposal=proposal_id, agreement=agreement_id)
            return {
                "proposal": proposal,
                "agreement": agreement,
                "plan": self.plan_view(plan_id),
            }

    # ------------------------------------------------------------- changes
    def request_change(self, payload: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            at = self._time(payload.get("at"))
            booking_id = payload.get("booking")
            if booking_id not in self.bookings:
                raise YardError("车队不存在", code="booking_not_found", status=404)
            change_id = self._next_id("CH")
            requested_handoff = payload.get("requested_handoff") or payload.get("handoff")
            change = {
                "id": change_id,
                "booking": booking_id,
                "kind": payload.get("kind", "early_handoff"),
                "reason": payload.get("reason", "接到提前出库通知"),
                "requested_handoff": requested_handoff,
                "status": "assessed",
                "buyer_confirmed": False,
                "created_at": iso(at),
            }
            evaluation_payload = {"at": iso(at)}
            if requested_handoff:
                evaluation_payload["handoff"] = requested_handoff
            evaluation = self.evaluate_bookings([booking_id], evaluation_payload)
            change["assessment"] = {
                "feasible": evaluation["feasible"],
                "max_finish_at": evaluation.get("max_finish_at"),
                "unmet": evaluation.get("unmet", []),
                "jobs": evaluation.get("jobs", []),
            }
            if evaluation["feasible"]:
                change["classification"] = "original_plan_available"
                change["label_zh"] = "存在可兑现的整组换档计划，待调度确认"
            else:
                eta = self.evaluate_bookings(
                    [booking_id], {"at": iso(at), "allow_after_deadline": True}
                )
                change["whole_group_eta"] = eta.get("max_finish_at")
                vehicles = self.bookings[booking_id]["vins"]
                inputs, ports, reservations = self._schedule_inputs(
                    [booking_id],
                    at=at,
                    deadline_overrides={booking_id: requested_handoff}
                    if requested_handoff
                    else {},
                )
                subsets = feasible_subsets(
                    vins=vehicles,
                    vehicles=inputs,
                    ports=ports,
                    reservations=reservations,
                    start=at,
                )
                if subsets:
                    subset, schedule = subsets[0]
                    change["classification"] = "split_requires_buyer"
                    change["label_zh"] = "只能先交部分车辆，等待买方同意拆组"
                    change["split_proposal"] = {
                        "first_group_vins": list(subset),
                        "remaining_vins": [v for v in vehicles if v not in subset],
                        "schedule": schedule,
                        "whole_group_eta": eta.get("max_finish_at"),
                        "status": "awaiting_buyer",
                    }
                else:
                    change["classification"] = "handoff_time_change_required"
                    change["label_zh"] = "提前时间无法整组兑现，需要调整移交时间"
                    change["proposed_handoff"] = eta.get("max_finish_at")
            self.changes[change_id] = change
            self._audit("change_requested", change=change_id, booking=booking_id)
            return change

    def confirm_change(
        self, change_id: str, payload: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        payload = payload or {}
        with self._lock:
            if change_id not in self.changes:
                raise YardError("变更不存在", code="change_not_found", status=404)
            change = self.changes[change_id]
            booking_id = change["booking"]
            choice = payload.get("choice", "commit")
            at = self._time(payload.get("at"))

            if choice in {"split", "split_proposal"}:
                if not payload.get("buyer_confirmed"):
                    raise YardError(
                        "拆组必须取得买方确认",
                        code="buyer_confirmation_required",
                        status=409,
                    )
                proposal = change.get("split_proposal")
                if not proposal:
                    raise YardError("该变更没有可确认的拆组方案", code="no_split_proposal")
                agreement_id = self._next_id("SA")
                agreement = {
                    "id": agreement_id,
                    "change": change_id,
                    "groups": [
                        {
                            "name": "confirmed_first_group",
                            "vins": proposal["first_group_vins"],
                            "handoff": change.get("requested_handoff"),
                        },
                        {
                            "name": "remaining_group",
                            "vins": proposal["remaining_vins"],
                            "handoff": proposal.get("whole_group_eta"),
                        },
                    ],
                    "confirmed_at": iso(at),
                }
                self.bookings[booking_id]["agreement_mode"] = "split"
                self.bookings[booking_id]["split_agreements"].append(agreement)
                change["status"] = "split_confirmed"
                change["buyer_confirmed"] = True
                change["agreement_id"] = agreement_id
                return {"change": change, "agreement": agreement}

            if choice in {"new_handoff", "handoff_time"}:
                new_handoff = payload.get("handoff") or change.get("proposed_handoff")
                if not new_handoff:
                    raise YardError("请提供新的移交时间", code="handoff_required")
                if payload.get("buyer_confirmed") is False:
                    raise YardError("移交时间变更需买方确认", code="buyer_confirmation_required")
                self.bookings[booking_id]["effective_handoff"] = new_handoff
                self.bookings[booking_id]["handoff"] = new_handoff
                self.bookings[booking_id]["status"] = "handoff_changed"
                change["status"] = "handoff_changed"
                change["confirmed_handoff"] = new_handoff

            # Cancel old active dispatch jobs for this booking and commit a
            # fresh plan based on actual meter/session state.
            for job in self.jobs.values():
                if job.get("booking") == booking_id and job["status"] != "completed" and not str(
                    job["status"]
                ).startswith("cancelled"):
                    job["status"] = "cancelled_by_change"
            evaluation_payload = {"at": iso(at)}
            if change.get("requested_handoff") and choice == "commit":
                evaluation_payload["handoff"] = change["requested_handoff"]
            evaluation = self.evaluate_bookings([booking_id], evaluation_payload)
            if not evaluation["feasible"]:
                change["status"] = "confirmation_blocked"
                raise YardError(
                    "确认后的整组计划仍不可兑现",
                    code="plan_not_feasible",
                    status=409,
                    details={"change": change, "evaluation": evaluation},
                )
            plan = self.commit_plan(evaluation, name=f"change:{change_id}")
            if change.get("requested_handoff") and choice == "commit":
                self.bookings[booking_id]["effective_handoff"] = change["requested_handoff"]
            change["status"] = "confirmed_and_planned"
            change["plan_id"] = plan["id"]
            return {"change": change, "plan": plan}

    # ------------------------------------------------------------- underdelivery
    def _maybe_create_under_delivery_review(self, session: dict[str, Any]) -> None:
        vin = session["vin"]
        if vin not in self.vehicles:
            return
        at = self._time()
        view = self.vehicle_view(vin, at)
        if view["requirement_met"]:
            return
        if not session.get("stop_confirmed") and session.get("pile_status") == "charging":
            # Active charging can still recover; finance/dispatch view still
            # shows progress, but don't spam a final review on every meter frame.
            return
        existing = next(
            (
                review
                for review in self.dispatch_reviews.values()
                if review.get("vin") == vin
                and review.get("kind") == "under_delivery"
                and review.get("status") == "awaiting_dispatch"
            ),
            None,
        )
        payload = {
            "kind": "under_delivery",
            "booking": session.get("booking"),
            "vin": vin,
            "session": session["session"],
            "at": iso(at),
            "delivered_wh": view["achieved_wh"],
            "metered_wh": view["metered_wh"],
            "minimum_wh": view["minimum_wh"],
            "remaining_wh": view["remaining_wh"],
            "deadline": view["deadline"],
            "remaining_minutes": view["remaining_minutes"],
            "required_kw_by_deadline": view["required_kw_by_deadline"],
            "compatible_ports": view["compatible_ports"],
            "order_paid": bool(view.get("order_paid"))
            or self._paid_milli_for_vin(vin) > 0,
            "status": "awaiting_dispatch",
            "options": [
                "reassign_port_or_power",
                "continue_session",
                "request_handoff_change",
                "request_buyer_split",
            ],
        }
        if existing:
            existing.update(payload)
        else:
            review_id = self._next_id("DR")
            payload["id"] = review_id
            self.dispatch_reviews[review_id] = payload

    def resolve_dispatch_review(
        self, review_id: str, payload: dict[str, Any]
    ) -> dict[str, Any]:
        with self._lock:
            if review_id not in self.dispatch_reviews:
                raise YardError("调度复核不存在", code="review_not_found", status=404)
            review = self.dispatch_reviews[review_id]
            decision = payload.get("decision")
            if decision not in {
                "reassign",
                "continue",
                "change_handoff",
                "split",
                "accepted_short",
            }:
                raise YardError("未知调度决定", code="invalid_dispatch_decision")
            review["status"] = "decided"
            review["decision"] = decision
            review["decision_note"] = payload.get("note")
            review["decided_at"] = iso(self._time(payload.get("at")))
            if payload.get("assignment"):
                created = self.dispatch({"assignments": [payload["assignment"]]})
                review["jobs"] = [j["id"] for j in created["jobs"]]
            return review

    # ------------------------------------------------------------- billing
    def _charge_milli_for_session(self, session_id: str) -> int:
        session = self.sessions[session_id]
        if session["vin"] not in self.vehicles:
            return 0
        return int(session.get("accepted_total_wh") or 0) * int(
            self.vehicles[session["vin"]]["fen_per_kwh"]
        )

    def record_payment(self, payload: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            amount = payload.get("amount_fen", payload.get("amount"))
            if payload.get("full_order") or amount == "full":
                vin = payload.get("vin")
                if not vin:
                    raise YardError("整单支付需要 vin", code="vin_required")
                amount_milli = max(0, self.vehicles[vin]["minimum_wh"] - self.vehicles[vin]["initial_wh"]) * int(
                    self.vehicles[vin]["fen_per_kwh"]
                )
            else:
                if amount is None:
                    raise YardError("支付缺少 amount_fen", code="invalid_payment")
                amount_milli = decimal_to_milli_fen(amount)
            payment = {
                "id": self._next_id("PAY"),
                "booking": payload.get("booking"),
                "vin": payload.get("vin"),
                "session": payload.get("session"),
                "amount_milli_fen": amount_milli,
                "amount_fen": money_json(amount_milli),
                "paid_at": payload.get("paid_at") or iso(self._time(payload.get("at"))),
                "method": payload.get("method", "recorded"),
                "note": payload.get("note"),
            }
            if payment["session"] and not payment["vin"]:
                payment["vin"] = self.sessions[payment["session"]]["vin"]
            if payment["vin"] and not payment["booking"]:
                payment["booking"] = self.vehicles[payment["vin"]]["booking"]
            self.payments.append(payment)
            if payload.get("mark_order_paid") and payment["vin"]:
                self.vehicles[payment["vin"]]["order_paid"] = True
            self._audit("payment_recorded", payment=payment["id"])
            return payment

    def mark_order_paid(self, vin: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        payload = payload or {}
        with self._lock:
            if vin not in self.vehicles:
                raise YardError("车辆不存在", code="vin_not_found", status=404)
            self.vehicles[vin]["order_paid"] = True
            payment = None
            if payload.get("record_payment", True):
                payment = self.record_payment(
                    {
                        "vin": vin,
                        "booking": self.vehicles[vin]["booking"],
                        "amount_fen": payload.get("amount_fen", "full"),
                        "full_order": "amount_fen" not in payload,
                        "method": payload.get("method", "order_paid"),
                    }
                )
            return {"vin": vin, "order_paid": True, "payment": payment}

    def _paid_milli_for_vin(self, vin: str) -> int:
        return sum(
            int(p["amount_milli_fen"])
            for p in self.payments
            if p.get("vin") == vin
        )

    def settle_session(
        self, session_id: str, payload: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        payload = payload or {}
        with self._lock:
            if session_id not in self.sessions:
                raise YardError("会话不存在", code="session_not_found", status=404)
            session = self.sessions[session_id]
            if session.get("accepted_total_wh") is None:
                raise YardError(
                    "没有有效累积计量，不能完成结算",
                    code="meter_required_for_settlement",
                    status=409,
                )
            if not session.get("stop_confirmed") and not payload.get("allow_provisional"):
                raise YardError(
                    "端口尚未确认停机；可做暂估但不能完成最终结算",
                    code="stop_not_confirmed",
                    status=409,
                    details=self.session_view(session_id),
                )
            settlement_id = self._next_id("ST")
            charge_milli = self._charge_milli_for_session(session_id)
            settlement = {
                "id": settlement_id,
                "scope": "session",
                "session": session_id,
                "vin": session["vin"],
                "booking": session.get("booking"),
                "settled_meter_wh": session["accepted_total_wh"],
                "fen_per_kwh": self.vehicles[session["vin"]]["fen_per_kwh"],
                "charge_milli_fen": charge_milli,
                "charge_fen": money_json(charge_milli),
                "provisional": not session.get("stop_confirmed"),
                "status": "settled" if session.get("stop_confirmed") else "provisional",
                "settled_at": iso(self._time(payload.get("at"))),
            }
            self.settlements[settlement_id] = settlement
            session["settled"] = True
            session["settlement_id"] = settlement_id
            self._audit("session_settled", settlement=settlement_id, session=session_id)
            return settlement

    def _create_meter_adjustment(
        self, session: dict[str, Any], old_wh: int, new_wh: int
    ) -> dict[str, Any]:
        rate = int(self.vehicles[session["vin"]]["fen_per_kwh"])
        old_charge = old_wh * rate
        new_charge = new_wh * rate
        delta = new_charge - old_charge
        adjustment = {
            "id": self._next_id("ADJ"),
            "session": session["session"],
            "vin": session["vin"],
            "booking": session.get("booking"),
            "old_meter_wh": old_wh,
            "new_meter_wh": new_wh,
            "old_charge_milli_fen": old_charge,
            "new_charge_milli_fen": new_charge,
            "delta_milli_fen": delta,
            "kind": "supplement_due" if delta > 0 else "refund_due" if delta < 0 else "none",
            "status": "due",
            "existing_payments_retained": True,
            "created_at": iso(self._time()),
        }
        self.adjustments.append(adjustment)
        return adjustment

    def billing_view(self, at: datetime | str | None = None) -> dict[str, Any]:
        at_dt = self._time(at)
        with self._lock:
            vehicle_rows = []
            total_charge = 0
            total_paid = sum(int(p["amount_milli_fen"]) for p in self.payments)
            for vin, vehicle in sorted(self.vehicles.items()):
                view = self.vehicle_view(vin, at_dt)
                session_rows = []
                vin_charge = 0
                for session in self.sessions.values():
                    if session["vin"] != vin:
                        continue
                    charge = self._charge_milli_for_session(session["session"])
                    vin_charge += charge
                    total_charge += charge
                    reasons = []
                    if session["control_state"] in ACTIVE_SESSION_STATES:
                        reasons.append("会话仍在进行或停机尚未确认，计量可能继续变化")
                    if session.get("stop_confirmed") and session.get("accepted_total_wh") is None:
                        reasons.append("已停机但缺有效累积计量")
                    if session.get("accepted_total_wh") is not None and not session.get("settled"):
                        reasons.append("已有计量但尚未结算；停止本身不等于结算完成")
                    if session.get("settled") and any(
                        a["session"] == session["session"] and a["status"] == "due"
                        for a in self.adjustments
                    ):
                        reasons.append("计量更正产生补款/退差，旧付款仍保留")
                    if session.get("needs_field_confirmation"):
                        reasons.append("需要现场确认端口/桩端状态")
                    session_rows.append({**self.session_view(session["session"]), "why_pricing": reasons})
                paid = self._paid_milli_for_vin(vin)
                vehicle_rows.append(
                    {
                        "vin": vin,
                        "booking": vehicle["booking"],
                        "rate_fen_per_kwh": vehicle["fen_per_kwh"],
                        "initial_wh": vehicle["initial_wh"],
                        "minimum_wh": vehicle["minimum_wh"],
                        "metered_wh": view["metered_wh"],
                        "achieved_wh": view["achieved_wh"],
                        "remaining_wh": view["remaining_wh"],
                        "requirement_met": view["requirement_met"],
                        "order_paid": vehicle.get("order_paid", False),
                        "charge_milli_fen": vin_charge,
                        "charge_fen": money_json(vin_charge),
                        "paid_milli_fen": paid,
                        "paid_fen": money_json(paid),
                        "balance_milli_fen": vin_charge - paid,
                        "balance_fen": money_json(vin_charge - paid),
                        "payment_records_retained": True,
                        "sessions": session_rows,
                    }
                )
            booking_billing = []
            for booking_id in sorted(self.bookings):
                charge = sum(
                    row["charge_milli_fen"]
                    for row in vehicle_rows
                    if row["booking"] == booking_id
                )
                paid = sum(
                    int(p["amount_milli_fen"])
                    for p in self.payments
                    if p.get("booking") == booking_id
                )
                booking = self.bookings[booking_id]
                booking_billing.append(
                    {
                        "booking": booking_id,
                        "charge_milli_fen": charge,
                        "charge_fen": money_json(charge),
                        "paid_milli_fen": paid,
                        "paid_fen": money_json(paid),
                        "balance_milli_fen": charge - paid,
                        "balance_fen": money_json(charge - paid),
                        "agreement_mode": booking.get("agreement_mode", "whole"),
                        "handoff_status": booking.get("status"),
                        "payments": [p for p in self.payments if p.get("booking") == booking_id],
                    }
                )
            ports_confirm = [
                self.field_confirmation_view(s)
                for s in self.sessions.values()
                if s.get("needs_field_confirmation")
            ]
            return {
                "at": iso(at_dt),
                "vehicles": vehicle_rows,
                "bookings": booking_billing,
                "payments": list(self.payments),
                "settlements": list(self.settlements.values()),
                "adjustments": self.adjustments,
                "totals": {
                    "charge_milli_fen": total_charge,
                    "charge_fen": money_json(total_charge),
                    "paid_milli_fen": total_paid,
                    "paid_fen": money_json(total_paid),
                    "balance_milli_fen": total_charge - total_paid,
                    "balance_fen": money_json(total_charge - total_paid),
                },
                "ports_needing_field_confirmation": ports_confirm,
                "dispatch_reviews": list(self.dispatch_reviews.values()),
                "billing_rule": "按有效会话的累积实送电量和有效订单费率计价；计划但未实送的时段不计费",
            }

    # ------------------------------------------------------------- handoff
    def handoff_view(self, at: datetime | str | None = None) -> dict[str, Any]:
        at_dt = self._time(at)
        with self._lock:
            bookings = [self.booking_view(bid, at_dt) for bid in sorted(self.bookings)]
            handoff_records = list(self.handoffs)
            for booking in bookings:
                bid = booking["booking"]
                collected = self._collected_vins(bid)
                booking["collected_vins"] = sorted(collected)
                booking["remaining_on_site_vins"] = [
                    vin for vin in booking["vins"] if vin not in collected
                ]
                vehicle_map = {v["vin"]: v for v in booking["vehicles"]}
                booking["collectable"] = (
                    booking["agreement_mode"] == "whole"
                    and booking["all_requirements_met"]
                    and not set(booking["vins"]).issubset(collected)
                )
                group_views = []
                for agreement in booking.get("split_agreements", []):
                    for group in agreement.get("groups", []):
                        vins = group["vins"]
                        unmet = [v for v in vins if not vehicle_map[v]["requirement_met"]]
                        already = [v for v in vins if v in collected]
                        group_views.append(
                            {
                                "agreement_id": agreement["id"],
                                "name": group.get("name"),
                                "vins": vins,
                                "unmet_vins": unmet,
                                "collected_vins": already,
                                "collectable": not unmet and len(already) != len(vins),
                            }
                        )
                booking["collectable_groups"] = group_views
            return {
                "at": iso(at_dt),
                "bookings": bookings,
                "handoffs": handoff_records,
                "rule": "默认整车队领车；部分达到最低电量不能宣称整组兑现，拆组须有买方确认协议",
            }

    def _collected_vins(self, booking_id: str) -> set[str]:
        result: set[str] = set()
        for record in self.handoffs:
            if record.get("booking") == booking_id:
                result.update(record.get("vins", []))
        return result

    def collect_handoff(self, payload: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            booking_id = payload.get("booking")
            if booking_id not in self.bookings:
                raise YardError("车队不存在", code="booking_not_found", status=404)
            booking = self.bookings[booking_id]
            at = self._time(payload.get("at"))
            view = self.booking_view(booking_id, at)
            subgroup_id = payload.get("agreement_id") or payload.get("subgroup_id")
            vins = payload.get("vins")

            if booking["agreement_mode"] == "whole" and not subgroup_id and not vins:
                existing = next(
                    (
                        record
                        for record in self.handoffs
                        if record.get("booking") == booking_id
                        and record.get("mode") == "whole"
                    ),
                    None,
                )
                if existing is not None:
                    return {**existing, "idempotent_replay": True}
                if not view["all_requirements_met"]:
                    raise YardError(
                        "整组仍有车辆未达到最低交车电量，不能办理整组领车",
                        code="whole_group_not_ready",
                        status=409,
                        details={"unmet_vins": view["unmet_vins"]},
                    )
                record = {
                    "id": self._next_id("HO"),
                    "booking": booking_id,
                    "mode": "whole",
                    "vins": booking["vins"],
                    "collected_by": payload.get("collected_by"),
                    "at": iso(at),
                }
                self.handoffs.append(record)
                booking["status"] = "handed_off"
                self._audit("whole_group_handoff", **{"handoff": record["id"]})
                return record

            # Split collection requires a confirmed agreement.
            agreement = None
            for candidate in booking.get("split_agreements", []):
                if subgroup_id is None or candidate["id"] == subgroup_id:
                    for group in candidate["groups"]:
                        if vins is None or set(group["vins"]) == set(vins):
                            agreement = candidate
                            target_group = group
                            break
            if agreement is None:
                raise YardError(
                    "没有买方确认的拆组协议，不能只领部分已充电车辆",
                    code="split_agreement_required",
                    status=409,
                    details={"eligible_vins": [v["vin"] for v in view["vehicles"] if v["requirement_met"]]},
                )
            target_vins = target_group["vins"]
            vehicle_views = {v["vin"]: v for v in view["vehicles"]}
            unmet = [vin for vin in target_vins if not vehicle_views[vin]["requirement_met"]]
            if unmet:
                raise YardError(
                    "拆组中仍有车辆未达到要求",
                    code="subgroup_not_ready",
                    status=409,
                    details={"unmet_vins": unmet},
                )
            collected = self._collected_vins(booking_id)
            already = [vin for vin in target_vins if vin in collected]
            if len(already) == len(target_vins):
                existing = next(
                    record
                    for record in self.handoffs
                    if record.get("booking") == booking_id
                    and record.get("agreement") == agreement["id"]
                    and list(record.get("vins", [])) == list(target_vins)
                )
                return {**existing, "idempotent_replay": True}
            if already:
                raise YardError(
                    "该组已有部分车辆办理领车，不能重复或交叉领取",
                    code="handoff_overlap",
                    status=409,
                    details={"already_collected": already},
                )
            record = {
                "id": self._next_id("HO"),
                "booking": booking_id,
                "mode": "split_group",
                "agreement": agreement["id"],
                "group_name": target_group.get("name"),
                "vins": target_vins,
                "collected_by": payload.get("collected_by"),
                "at": iso(at),
            }
            self.handoffs.append(record)
            return record

    # ------------------------------------------------------------- message facade
    def handle_pile_message(self, payload: dict[str, Any]) -> dict[str, Any]:
        kind = payload.get("type") or payload.get("kind")
        if kind in {"start", "session_started"}:
            return self.record_start(payload.get("message") or payload)
        if kind in {"stop", "stop_receipt", "stopped"}:
            message = payload.get("message") or payload
            return self.receive_stop_receipt(message["session"], message)
        if kind in {"status", "session_status"}:
            message = payload.get("message") or payload
            return self.receive_session_status(message["session"], message)
        if kind in {"meter", "meter_reading"}:
            return self.receive_meter(payload.get("message") or payload)
        raise YardError("无法识别的桩端消息类型", code="unknown_pile_message")

    process_pile_message = handle_pile_message
    report_urgent_order = report_urgent_orders
    evaluate_capacity = assess_capacity
    create_change = request_change
    process_meter_reading = receive_meter
    add_meter_reading = receive_meter
    collect_vehicle = collect_handoff

    # ------------------------------------------------------------- dashboard
    def dashboard(self, at: datetime | str | None = None) -> dict[str, Any]:
        with self._lock:
            at_dt = self._time(at)
            capacity = self.capacity(at_dt)
            handoff = self.handoff_view(at_dt)
            billing = self.billing_view(at_dt)
            return {
                "at": iso(at_dt),
                "counts": {
                    "ports": len(self.ports),
                    "vehicles": len(self.vehicles),
                    "bookings": len(self.bookings),
                    "active_sessions": sum(
                        1
                        for s in self.sessions.values()
                        if self._session_occupies_at(s, at_dt)
                    ),
                    "open_dispatch_reviews": sum(
                        1
                        for r in self.dispatch_reviews.values()
                        if r.get("status") == "awaiting_dispatch"
                    ),
                },
                "capacity": capacity,
                "handoff_ready": [
                    b["booking"] for b in handoff["bookings"] if b.get("collectable")
                ],
                "finance_totals": billing["totals"],
                "dispatch_reviews": [
                    r
                    for r in self.dispatch_reviews.values()
                    if r.get("status") == "awaiting_dispatch"
                ],
                "urgent_orders": list(self.urgent_orders.values()),
                "changes": list(self.changes.values()),
            }

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return json.loads(
                json.dumps(
                    {
                        "ports": self.ports,
                        "vehicles": self.vehicles,
                        "bookings": self.bookings,
                        "sessions": {sid: self.session_view(sid) for sid in self.sessions},
                        "meter_events": self.meter_events,
                        "plans": {pid: self.plan_view(pid) for pid in self.plans},
                        "changes": self.changes,
                        "urgent_orders": self.urgent_orders,
                        "jobs": self.jobs,
                        "payments": self.payments,
                        "settlements": self.settlements,
                        "adjustments": self.adjustments,
                        "handoffs": self.handoffs,
                        "dispatch_reviews": self.dispatch_reviews,
                        "audit": self.audit_events,
                    },
                    ensure_ascii=False,
                    default=str,
                )
            )
