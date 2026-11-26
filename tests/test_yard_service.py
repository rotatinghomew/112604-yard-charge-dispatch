"""Domain tests for whole-group promises, occupancy, meter and billing rules."""

from __future__ import annotations

import unittest
from pathlib import Path

from yard_service import YardError, YardService
from yard_service.pile import DictPileClient, PileTransportError


SAMPLES = Path(__file__).resolve().parents[1] / "samples"
AT = "2026-11-27T01:42:00Z"


def sample_service() -> YardService:
    return YardService(SAMPLES, now=AT)


class SampleReconciliationTest(unittest.TestCase):
    def test_meter_correction_uses_superseding_cumulative_reading(self):
        service = sample_service()
        session = service.session_view("S12")
        self.assertEqual(session["accepted_total_wh"], 8600)
        self.assertEqual(session["accepted_reading_event"], "E3")
        # 8.6 kWh * 125 fen/kWh = 1075 fen.
        self.assertEqual(session["charge_fen"], 1075)

    def test_current_bank_capacity_uses_authorized_active_sessions(self):
        service = sample_service()
        capacity = service.capacity(AT)
        bank_g = next(b for b in capacity["banks"] if b["bank"] == "G")
        self.assertEqual(bank_g["used_kw"], 18.0)
        self.assertEqual(bank_g["available_kw"], 0.0)

    def test_partial_charge_cannot_become_whole_group_handoff(self):
        service = sample_service()
        with self.assertRaises(YardError) as raised:
            service.collect_handoff({"booking": "C-9", "at": AT})
        self.assertEqual(raised.exception.code, "whole_group_not_ready")
        self.assertEqual(set(raised.exception.details["unmet_vins"]), {"V12", "V13"})

        # One car reaching its minimum still cannot be collected alone.
        service.receive_meter(
            {
                "event": "X1",
                "session": "S12",
                "port": "C1",
                "vin": "V12",
                "occurred": "2026-11-27T02:30:00Z",
                "total_wh": 20000,
                "supersedes": "",
            }
        )
        with self.assertRaises(YardError) as raised:
            service.collect_handoff({"booking": "C-9", "vins": ["V12"], "at": AT})
        self.assertEqual(raised.exception.code, "split_agreement_required")

    def test_all_vehicles_can_collect_whole_group(self):
        service = sample_service()
        service.receive_meter(
            {
                "event": "X1",
                "session": "S12",
                "port": "C1",
                "vin": "V12",
                "occurred": "2026-11-27T03:00:00Z",
                "total_wh": 20000,
                "supersedes": "",
            }
        )
        service.receive_meter(
            {
                "event": "X2",
                "session": "S13",
                "port": "C3",
                "vin": "V13",
                "occurred": "2026-11-27T03:00:00Z",
                "total_wh": 15000,
                "supersedes": "",
            }
        )
        record = service.collect_handoff({"booking": "C-9", "collected_by": "loader-1"})
        self.assertEqual(record["mode"], "whole")
        self.assertEqual(set(record["vins"]), {"V12", "V13"})


class StopUncertaintyTest(unittest.TestCase):
    def test_failed_stop_keeps_vin_port_and_bank_capacity_occupied(self):
        service = sample_service()
        client = DictPileClient(stop_result=PileTransportError("connection lost"))
        result = service.request_stop("S12", client=client, at=AT)
        self.assertFalse(result["occupancy_released"])

        session = service.session_view("S12")
        self.assertEqual(session["control_state"], "stop_unconfirmed")
        self.assertTrue(session["needs_field_confirmation"])

        with self.assertRaises(YardError) as raised:
            service.record_start(
                {
                    "session": "S99",
                    "port": "C2",
                    "vin": "V12",
                    "started_at": AT,
                    "authorized_kw": 12,
                }
            )
        self.assertEqual(raised.exception.code, "vin_already_plugged")

        with self.assertRaises(YardError) as raised:
            service.record_start(
                {
                    "session": "S98",
                    "port": "C1",
                    "vin": "V14",
                    "started_at": AT,
                    "authorized_kw": 12,
                }
            )
        self.assertEqual(raised.exception.code, "port_occupied")

        capacity = service.capacity(AT)
        bank_g = next(b for b in capacity["banks"] if b["bank"] == "G")
        self.assertEqual(bank_g["available_kw"], 0.0)
        self.assertGreaterEqual(bank_g["needs_confirmation_kw"], 12.0)
        self.assertTrue(
            any(item["port"] == "C1" for item in capacity["ports_needing_field_confirmation"])
        )

    def test_poll_charging_then_stopped_releases_only_after_proof(self):
        service = sample_service()
        client = DictPileClient(
            stop_result=(0, {}),
            get_results=[
                (200, {"status": "charging", "port": "C1", "vin": "V12", "authorized_kw": 12}),
                (
                    200,
                    {
                        "status": "stopped",
                        "port": "C1",
                        "vin": "V12",
                        "authorized_kw": 12,
                        "at": "2026-11-27T01:50:00Z",
                    },
                ),
            ],
        )
        service.request_stop("S12", client=client, at=AT)
        charging = service.poll_session("S12", client=client, at=AT)
        self.assertEqual(charging["pile_status"], "charging")
        self.assertFalse(charging["stop_confirmed"])
        self.assertEqual(
            next(b for b in service.capacity(AT)["banks"] if b["bank"] == "G")["available_kw"],
            0.0,
        )

        stopped = service.poll_session("S12", client=client, at=AT)
        self.assertTrue(stopped["stop_confirmed"])
        self.assertEqual(stopped["stopped_at"], "2026-11-27T01:50:00Z")
        later = service.capacity("2026-11-27T01:51:00Z")
        bank_g = next(b for b in later["banks"] if b["bank"] == "G")
        # S13 still occupies 6 kW; S12's 12 kW is released.
        self.assertEqual(bank_g["used_kw"], 6.0)
        self.assertEqual(bank_g["available_kw"], 12.0)


class BillingAndPaymentTest(unittest.TestCase):
    def test_correction_after_settlement_keeps_old_payment(self):
        service = sample_service()
        service.receive_stop_receipt("S12", {"at": "2026-11-27T01:50:00Z"})
        settlement = service.settle_session("S12", {"at": "2026-11-27T01:51:00Z"})
        self.assertEqual(settlement["charge_fen"], 1075)
        service.record_payment({"vin": "V12", "amount_fen": 1075})

        service.receive_meter(
            {
                "event": "E4",
                "session": "S12",
                "port": "C1",
                "vin": "V12",
                "occurred": "2026-11-27T02:00:00Z",
                "total_wh": 9000,
                "supersedes": "E3",
            }
        )
        billing = service.billing_view()
        v12 = next(v for v in billing["vehicles"] if v["vin"] == "V12")
        self.assertEqual(v12["charge_fen"], 1125)
        self.assertEqual(v12["paid_fen"], 1075)
        self.assertEqual(v12["balance_fen"], 50)
        self.assertEqual(len(v12["payments"] if "payments" in v12 else billing["payments"]), 1)
        self.assertTrue(billing["adjustments"][-1]["existing_payments_retained"])

    def test_scheduled_but_unmetered_power_is_not_billed(self):
        service = YardService(now="2026-01-01T00:30:00Z")
        service.upsert_port({"port": "P1", "connector": "X", "bank": "B", "max_kw": 10, "bank_kw": 10})
        service.upsert_vehicle(
            {
                "booking": "B1",
                "vin": "V1",
                "connector": "X",
                "ready": "2026-01-01T00:00:00Z",
                "handoff": "2026-01-01T01:00:00Z",
                "initial_wh": 0,
                "minimum_wh": 10000,
                "interruptible": True,
                "fen_per_kwh": 100,
            }
        )
        service.dispatch(
            {
                "assignments": [
                    {
                        "vin": "V1",
                        "port": "P1",
                        "start": "2026-01-01T00:00:00Z",
                        "end": "2026-01-01T01:00:00Z",
                        "kw": 10,
                    }
                ]
            }
        )
        billing = service.billing_view()
        vehicle = billing["vehicles"][0]
        self.assertEqual(vehicle["metered_wh"], 0)
        self.assertEqual(vehicle["charge_fen"], 0)
        self.assertIn("计划但未实送", billing["billing_rule"])

    def test_paid_order_with_actual_shortage_returns_dispatch_review(self):
        service = YardService(now="2026-01-01T00:30:00Z")
        service.upsert_port({"port": "P1", "connector": "X", "bank": "B", "max_kw": 10, "bank_kw": 10})
        service.upsert_vehicle(
            {
                "booking": "B1",
                "vin": "V1",
                "connector": "X",
                "ready": "2026-01-01T00:00:00Z",
                "handoff": "2026-01-01T01:00:00Z",
                "initial_wh": 0,
                "minimum_wh": 10000,
                "interruptible": True,
                "fen_per_kwh": 100,
            }
        )
        service.record_start(
            {
                "session": "S1",
                "port": "P1",
                "vin": "V1",
                "started_at": "2026-01-01T00:00:00Z",
                "authorized_kw": 10,
            }
        )
        service.receive_meter(
            {
                "event": "M1",
                "session": "S1",
                "port": "P1",
                "vin": "V1",
                "occurred": "2026-01-01T00:30:00Z",
                "total_wh": 4000,
                "supersedes": "",
            }
        )
        service.mark_order_paid("V1", {"amount_fen": 1000})
        service.receive_stop_receipt("S1", {"at": "2026-01-01T00:30:00Z"})

        billing = service.billing_view()
        v1 = next(v for v in billing["vehicles"] if v["vin"] == "V1")
        self.assertTrue(v1["order_paid"])
        self.assertFalse(v1["requirement_met"])
        reviews = [r for r in service.dispatch_reviews.values() if r["kind"] == "under_delivery"]
        self.assertEqual(len(reviews), 1)
        review = reviews[0]
        self.assertEqual(review["remaining_wh"], 6000)
        self.assertEqual(review["remaining_minutes"], 30)
        self.assertGreater(review["required_kw_by_deadline"], 10)
        resolved = service.resolve_dispatch_review(review["id"], {"decision": "change_handoff"})
        self.assertEqual(resolved["status"], "decided")


class CapacityClassificationTest(unittest.TestCase):
    def _service(self) -> YardService:
        service = YardService(now="2026-01-01T00:00:00Z")
        service.upsert_port({"port": "P1", "connector": "X", "bank": "B", "max_kw": 10, "bank_kw": 10})
        return service

    def test_full_group_split_and_time_change_classes(self):
        service = self._service()
        for vin in ("V1", "V2"):
            service.upsert_vehicle(
                {
                    "booking": "F1",
                    "vin": vin,
                    "connector": "X",
                    "ready": "2026-01-01T00:00:00Z",
                    "handoff": "2026-01-01T01:00:00Z",
                    "initial_wh": 0,
                    "minimum_wh": 10000,
                    "interruptible": True,
                    "fen_per_kwh": 100,
                }
            )
        assessment = service.assess_capacity(
            {"at": "2026-01-01T00:00:00Z", "candidate_bookings": ["F1"]}
        )
        decision = assessment["decisions"][0]
        self.assertEqual(decision["classification"], "split_requires_buyer")
        self.assertEqual(len(decision["partial_group_available"]), 1)
        proposal_id = decision["proposal_id"]

        with self.assertRaises(YardError):
            service.confirm_split_proposal(proposal_id, {"buyer_confirmed": False})
        confirmed = service.confirm_split_proposal(proposal_id, {"buyer_confirmed": True})
        partial_vin = decision["partial_group_available"][0]
        self.assertEqual(confirmed["agreement"]["groups"][0]["vins"], decision["partial_group_available"])
        service.record_start(
            {
                "session": "S-SPLIT",
                "port": "P1",
                "vin": partial_vin,
                "started_at": "2026-01-01T00:00:00Z",
                "authorized_kw": 10,
            }
        )
        service.receive_meter(
            {
                "event": "M-SPLIT",
                "session": "S-SPLIT",
                "port": "P1",
                "vin": partial_vin,
                "occurred": "2026-01-01T01:00:00Z",
                "total_wh": 10000,
                "supersedes": "",
            }
        )
        group_id = confirmed["agreement"]["id"]
        handoff = service.collect_handoff(
            {"booking": "F1", "agreement_id": group_id, "at": "2026-01-01T01:00:00Z"}
        )
        self.assertEqual(handoff["mode"], "split_group")
        self.assertEqual(handoff["vins"], decision["partial_group_available"])
        view = service.handoff_view("2026-01-01T01:00:00Z")
        booking = next(b for b in view["bookings"] if b["booking"] == "F1")
        self.assertFalse(booking["collectable"])
        self.assertEqual(len(booking["collectable_groups"]), 2)

    def test_split_without_buyer_agreement_cannot_collect_partial(self):
        service = self._service()
        service.upsert_vehicle(
            {
                "booking": "F1",
                "vin": "V1",
                "connector": "X",
                "ready": "2026-01-01T00:00:00Z",
                "handoff": "2026-01-01T01:00:00Z",
                "initial_wh": 0,
                "minimum_wh": 5000,
                "interruptible": True,
                "fen_per_kwh": 100,
            }
        )
        # Prove energy through a valid session/meter pair.
        service.record_start(
            {
                "session": "S1",
                "port": "P1",
                "vin": "V1",
                "started_at": "2026-01-01T00:00:00Z",
                "authorized_kw": 10,
            }
        )
        service.receive_meter(
            {
                "event": "M1",
                "session": "S1",
                "port": "P1",
                "vin": "V1",
                "occurred": "2026-01-01T00:30:00Z",
                "total_wh": 5000,
                "supersedes": "",
            }
        )
        with self.assertRaises(YardError) as raised:
            service.collect_handoff({"booking": "F1", "vins": ["V1"]})
        self.assertEqual(raised.exception.code, "split_agreement_required")

    def test_single_impossible_vehicle_needs_new_handoff(self):
        service = self._service()
        service.upsert_vehicle(
            {
                "booking": "F2",
                "vin": "V9",
                "connector": "X",
                "ready": "2026-01-01T00:00:00Z",
                "handoff": "2026-01-01T01:00:00Z",
                "initial_wh": 0,
                "minimum_wh": 20000,
                "interruptible": True,
                "fen_per_kwh": 100,
            }
        )
        assessment = service.assess_capacity(
            {"at": "2026-01-01T00:00:00Z", "candidate_bookings": ["F2"]}
        )
        self.assertEqual(
            assessment["decisions"][0]["classification"],
            "handoff_time_change_required",
        )
        self.assertEqual(
            assessment["decisions"][0]["proposed_handoff"],
            "2026-01-01T02:00:00Z",
        )

    def test_existing_promise_is_protected_from_rush_order(self):
        service = self._service()
        service.upsert_vehicle(
            {
                "booking": "OLD",
                "vin": "OLD1",
                "connector": "X",
                "ready": "2026-01-01T00:00:00Z",
                "handoff": "2026-01-01T01:00:00Z",
                "initial_wh": 5000,
                "minimum_wh": 10000,
                "interruptible": True,
                "fen_per_kwh": 100,
            }
        )
        evaluation = service.evaluate_bookings(["OLD"], {"at": "2026-01-01T00:00:00Z"})
        service.commit_plan(evaluation)
        service.upsert_vehicle(
            {
                "booking": "RUSH",
                "vin": "R1",
                "connector": "X",
                "ready": "2026-01-01T00:00:00Z",
                "handoff": "2026-01-01T00:30:00Z",
                "initial_wh": 0,
                "minimum_wh": 10000,
                "interruptible": True,
                "fen_per_kwh": 200,
            }
        )
        assessment = service.assess_capacity(
            {"at": "2026-01-01T00:00:00Z", "candidate_bookings": ["RUSH"]}
        )
        classes = {d["booking"]: d["classification"] for d in assessment["decisions"]}
        self.assertEqual(classes["OLD"], "original_plan_kept")
        self.assertEqual(classes["RUSH"], "handoff_time_change_required")


if __name__ == "__main__":
    unittest.main()
