"""JSON HTTP interface for the yard service."""

from __future__ import annotations

import json
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from .errors import YardError
from .service import SAMPLE_DIR, YardService

STATIC_DIR = Path(__file__).resolve().parent / "static"

PATH_ALIASES = {
    "/dashboard": "/api/dashboard",
    "/capacity": "/api/capacity",
    "/fleets": "/api/fleets",
    "/bookings": "/api/bookings",
    "/vehicles": "/api/vehicles",
    "/ports": "/api/ports",
    "/sessions": "/api/sessions",
    "/plans": "/api/plans",
    "/changes": "/api/changes",
    "/urgent-orders": "/api/urgent-orders",
    "/urgent_orders": "/api/urgent-orders",
    "/urgent": "/api/urgent-orders",
    "/jobs": "/api/jobs",
    "/dispatch": "/api/dispatch",
    "/jobs/dispatch": "/api/dispatch",
    "/handoff": "/api/handoff",
    "/handoffs": "/api/handoff",
    "/boarding": "/api/handoff",
    "/billing": "/api/billing",
    "/finance": "/api/finance",
    "/payments": "/api/payments",
    "/meter": "/api/meter",
    "/pile/messages": "/api/pile/messages",
    "/reviews/dispatch": "/api/reviews/dispatch",
}


def canonical_path(path: str) -> str:
    api_aliases = {
        "/api/urgent": "/api/urgent-orders",
        "/api/urgent_orders": "/api/urgent-orders",
        "/api/jobs/dispatch": "/api/dispatch",
        "/api/boarding": "/api/handoff",
        "/api/export": "/api/handoff",
    }
    if path in api_aliases:
        return api_aliases[path]
    if path.startswith("/api/") or path == "/api":
        return path
    if path in PATH_ALIASES:
        return PATH_ALIASES[path]
    # Common resource spelling aliases with IDs/actions.
    for prefix in (
        "/urgent-orders/",
        "/urgent_orders/",
        "/changes/",
        "/split-proposals/",
        "/pile/sessions/",
        "/orders/",
        "/billing/",
        "/handoff/",
        "/reviews/",
        "/dispatch/",
        "/plans/",
    ):
        if path.startswith(prefix):
            return "/api" + path
    return path


def create_server(
    *,
    host: str = "127.0.0.1",
    port: int = 8088,
    data_dir: str | Path | None = SAMPLE_DIR,
    now: str | None = None,
    peer_url: str | None = None,
    service: YardService | None = None,
) -> ThreadingHTTPServer:
    yard = service or YardService(data_dir, now=now, peer_url=peer_url)

    class Handler(BaseHTTPRequestHandler):
        server_version = "YardService/0.1"

        @property
        def yard(self) -> YardService:
            return yard

        def _send_json(self, status: int, payload: Any) -> None:
            body = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _send_html(self, status: int, body: str) -> None:
            encoded = body.encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)

        def _body(self) -> dict[str, Any]:
            size = int(self.headers.get("Content-Length", 0))
            if size > 4_194_304:
                raise YardError("请求体过大", code="body_too_large", status=413)
            if size == 0:
                return {}
            raw = self.rfile.read(size)
            try:
                data = json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise YardError("请求体不是有效 JSON", code="invalid_json", status=400) from exc
            if data is None:
                return {}
            if not isinstance(data, dict):
                raise YardError("JSON 请求体必须是对象", code="invalid_json_body")
            return data

        def _query(self) -> dict[str, Any]:
            parsed = urlparse(self.path)
            values = parse_qs(parsed.query)
            return {key: values[key][-1] for key in values}

        def do_GET(self) -> None:  # noqa: N802
            try:
                path = canonical_path(urlparse(self.path).path)
                query = self._query()
                if path == "/health":
                    self._send_json(200, {"ok": True, "service": "yard"})
                    return
                if path == "/" or path == "/app":
                    index = STATIC_DIR / "index.html"
                    if index.exists():
                        self._send_html(200, index.read_text(encoding="utf-8"))
                    else:
                        self._send_json(200, {"service": "yard", "api": "/api"})
                    return
                if path == "/api":
                    self._send_json(200, api_index())
                    return
                if path == "/api/state":
                    self._send_json(200, self.yard.snapshot())
                    return
                if path in {"/api/dashboard", "/api/views/dashboard"}:
                    self._send_json(200, self.yard.dashboard(query.get("at")))
                    return
                if path == "/api/capacity":
                    self._send_json(200, self.yard.capacity(query.get("at")))
                    return
                if path in {"/api/fleets", "/api/bookings"}:
                    self._send_json(200, {"bookings": self.yard.fleet_views(query.get("at"))})
                    return
                if path == "/api/vehicles":
                    at = query.get("at")
                    self._send_json(
                        200,
                        {
                            "vehicles": [
                                self.yard.vehicle_view(vin, at)
                                for vin in sorted(self.yard.vehicles)
                            ]
                        },
                    )
                    return
                if path == "/api/ports":
                    self._send_json(200, {"ports": list(self.yard.ports.values())})
                    return
                if path == "/api/sessions":
                    self._send_json(
                        200,
                        {"sessions": [self.yard.session_view(sid) for sid in sorted(self.yard.sessions)]},
                    )
                    return
                if path == "/api/meter/events":
                    self._send_json(200, {"events": list(self.yard.meter_events.values())})
                    return
                if path == "/api/plans":
                    self._send_json(200, {"plans": [self.yard.plan_view(pid) for pid in sorted(self.yard.plans)]})
                    return
                match = re.fullmatch(r"/api/plans/([^/]+)", path)
                if match:
                    self._send_json(200, self.yard.plan_view(match.group(1)))
                    return
                if path == "/api/changes":
                    self._send_json(200, {"changes": list(self.yard.changes.values())})
                    return
                if path == "/api/urgent-orders":
                    self._send_json(200, {"orders": list(self.yard.urgent_orders.values())})
                    return
                if path == "/api/jobs":
                    self._send_json(200, {"jobs": list(self.yard.jobs.values())})
                    return
                if path in {"/api/handoff", "/api/handoffs"}:
                    self._send_json(200, self.yard.handoff_view(query.get("at")))
                    return
                if path in {"/api/billing", "/api/finance"}:
                    self._send_json(200, self.yard.billing_view(query.get("at")))
                    return
                if path == "/api/reviews/dispatch":
                    self._send_json(200, {"reviews": list(self.yard.dispatch_reviews.values())})
                    return
                if path == "/api/audit":
                    self._send_json(200, {"events": self.yard.audit_events})
                    return
                poll_match = re.fullmatch(r"/api/pile/sessions/([^/]+)/poll", path)
                if poll_match:
                    self._send_json(200, self.yard.poll_session(poll_match.group(1)))
                    return
                self._send_json(404, {"error": "not_found", "path": path})
            except YardError as exc:
                self._send_json(exc.status, exc.to_payload())
            except Exception as exc:  # pragma: no cover - defensive HTTP boundary
                self._send_json(500, {"error": "internal_error", "message": str(exc)})

        def do_POST(self) -> None:  # noqa: N802, C901
            try:
                path = canonical_path(urlparse(self.path).path)
                body = self._body()

                if path == "/api/action":
                    action = body.pop("action", None)
                    self._send_json(200, dispatch_named_action(self.yard, action, body))
                    return
                if path == "/api/clock":
                    self.yard.set_clock(body["now"])
                    self._send_json(200, {"now": body["now"]})
                    return
                if path == "/api/fleet-reports":
                    self._send_json(200, self.yard.report_fleet(body))
                    return
                if path == "/api/urgent-orders":
                    self._send_json(200, self.yard.report_urgent_orders(body))
                    return
                match = re.fullmatch(r"/api/urgent-orders/([^/]+)/accept", path)
                if match:
                    self._send_json(200, self.yard.accept_urgent_order(match.group(1), body))
                    return
                if path == "/api/capacity/assess":
                    self._send_json(200, self.yard.assess_capacity(body))
                    return
                if path == "/api/plans/evaluate":
                    ids = body.pop("booking_ids", None)
                    self._send_json(200, self.yard.evaluate_bookings(ids, body))
                    return
                if path == "/api/plans/commit":
                    evaluation = body.get("evaluation")
                    if evaluation is None:
                        ids = body.get("booking_ids")
                        evaluation = self.yard.evaluate_bookings(ids, body)
                    result = self.yard.commit_plan(evaluation, name=body.get("name"))
                    self._send_json(201, result)
                    return
                if path == "/api/changes":
                    self._send_json(201, self.yard.request_change(body))
                    return
                match = re.fullmatch(r"/api/changes/([^/]+)/confirm", path)
                if match:
                    self._send_json(200, self.yard.confirm_change(match.group(1), body))
                    return
                match = re.fullmatch(r"/api/split-proposals/([^/]+)/confirm", path)
                if match:
                    self._send_json(200, self.yard.confirm_split_proposal(match.group(1), body))
                    return
                if path == "/api/dispatch/reassign":
                    self._send_json(201, self.yard.reassign_vehicle(body))
                    return
                if path == "/api/dispatch":
                    self._send_json(201, self.yard.dispatch(body))
                    return
                match = re.fullmatch(r"/api/reviews/dispatch/([^/]+)/decision", path)
                if match:
                    self._send_json(200, self.yard.resolve_dispatch_review(match.group(1), body))
                    return
                if path == "/api/pile/sessions":
                    self._send_json(201, self.yard.record_start(body))
                    return
                if path == "/api/pile/messages":
                    self._send_json(200, handle_pile_message(self.yard, body))
                    return
                match = re.fullmatch(r"/api/pile/sessions/([^/]+)/stop", path)
                if match:
                    self._send_json(200, self.yard.request_stop(match.group(1), at=body.get("at")))
                    return
                match = re.fullmatch(r"/api/pile/sessions/([^/]+)/poll", path)
                if match:
                    self._send_json(200, self.yard.poll_session(match.group(1), at=body.get("at")))
                    return
                match = re.fullmatch(r"/api/pile/sessions/([^/]+)/status", path)
                if match:
                    self._send_json(200, self.yard.receive_session_status(match.group(1), body))
                    return
                match = re.fullmatch(r"/api/pile/sessions/([^/]+)/stop-receipt", path)
                if match:
                    self._send_json(200, self.yard.receive_stop_receipt(match.group(1), body))
                    return
                if path == "/api/meter":
                    self._send_json(202, self.yard.receive_meter(body))
                    return
                if path == "/api/payments":
                    self._send_json(201, self.yard.record_payment(body))
                    return
                match = re.fullmatch(r"/api/orders/([^/]+)/pay", path)
                if match:
                    self._send_json(200, self.yard.mark_order_paid(match.group(1), body))
                    return
                if path == "/api/billing/settle":
                    if body.get("booking"):
                        results = []
                        for sid, session in list(self.yard.sessions.items()):
                            if session.get("booking") == body["booking"]:
                                try:
                                    results.append(self.yard.settle_session(sid, body))
                                except YardError as exc:
                                    results.append({"session": sid, "error": exc.code, "message": exc.message})
                        self._send_json(200, {"settlements": results})
                    else:
                        self._send_json(201, self.yard.settle_session(body["session"], body))
                    return
                if path == "/api/handoff/collect":
                    self._send_json(201, self.yard.collect_handoff(body))
                    return
                self._send_json(404, {"error": "not_found", "path": path})
            except YardError as exc:
                self._send_json(exc.status, exc.to_payload())
            except Exception as exc:  # pragma: no cover
                self._send_json(500, {"error": "internal_error", "message": str(exc)})

        def log_message(self, fmt: str, *args: Any) -> None:
            return

    httpd = ThreadingHTTPServer((host, port), Handler)
    httpd.yard = yard  # type: ignore[attr-defined]
    return httpd


def handle_pile_message(service: YardService, body: dict[str, Any]) -> dict[str, Any]:
    kind = body.get("type") or body.get("kind")
    if kind in {"start", "session_started"}:
        return service.record_start(body.get("message") or body)
    if kind in {"stop", "stop_receipt", "stopped"}:
        message = body.get("message") or body
        return service.receive_stop_receipt(message["session"], message)
    if kind in {"status", "session_status"}:
        message = body.get("message") or body
        return service.receive_session_status(message["session"], message)
    if kind in {"meter", "meter_reading"}:
        return service.receive_meter(body.get("message") or body)
    raise YardError("无法识别的桩端消息类型", code="unknown_pile_message")


def dispatch_named_action(service: YardService, action: str | None, payload: dict[str, Any]) -> Any:
    aliases = {
        "dashboard": service.dashboard,
        "assess_capacity": service.assess_capacity,
        "report_urgent_orders": service.report_urgent_orders,
        "request_change": service.request_change,
        "confirm_split_proposal": service.confirm_split_proposal,
        "evaluate_bookings": service.evaluate_bookings,
        "dispatch": service.dispatch,
        "record_start": service.record_start,
        "receive_meter": service.receive_meter,
        "collect_handoff": service.collect_handoff,
        "billing_view": service.billing_view,
        "handoff_view": service.handoff_view,
    }
    if not action or action not in aliases:
        raise YardError(f"未知 action: {action}", code="unknown_action", status=404)
    method = aliases[action]
    if action in {"dashboard", "billing_view", "handoff_view"}:
        return method(payload.get("at"))
    return method(payload)


def api_index() -> dict[str, Any]:
    return {
        "service": "yard-charging",
        "endpoints": [
            "GET /api/dashboard?at=",
            "POST /api/fleet-reports",
            "POST /api/urgent-orders",
            "POST /api/capacity/assess",
            "POST /api/plans/evaluate",
            "POST /api/plans/commit",
            "POST /api/changes",
            "POST /api/changes/{id}/confirm",
            "POST /api/dispatch",
            "POST /api/pile/sessions",
            "POST /api/pile/sessions/{id}/stop",
            "POST|GET /api/pile/sessions/{id}/poll",
            "POST /api/pile/sessions/{id}/stop-receipt",
            "POST /api/meter",
            "POST /api/payments",
            "POST /api/orders/{vin}/pay",
            "POST /api/billing/settle",
            "GET /api/billing",
            "GET /api/handoff",
            "POST /api/handoff/collect",
        ],
    }


def main() -> None:
    import argparse
    import os

    parser = argparse.ArgumentParser(description="库区充电调度与整组交车服务")
    parser.add_argument("--host", default=os.environ.get("YARD_HOST", "127.0.0.1"))
    parser.add_argument(
        "--port",
        type=int,
        default=int(os.environ.get("YARD_PORT", os.environ.get("PORT", "8088"))),
    )
    parser.add_argument("--data", default=str(SAMPLE_DIR), help="JSONL 资料目录")
    parser.add_argument("--peer", default=None, help="桩端 HTTP 对端地址，例如 http://127.0.0.1:8087")
    parser.add_argument("--now", default=None, help="固定服务时钟，便于回放合成资料")
    args = parser.parse_args()
    server = create_server(
        host=args.host,
        port=args.port,
        data_dir=args.data,
        now=args.now,
        peer_url=args.peer,
    )
    print(f"库区服务监听 http://{args.host}:{server.server_port}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
