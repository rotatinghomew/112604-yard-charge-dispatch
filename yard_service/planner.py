"""Tiny deterministic capacity scheduler.

The scheduler uses one-minute slots.  It deliberately needs no external solver:
yard fleets in this problem are small, while the important rules are connector
matching, one car per port, shared bank caps, release only after confirmation,
and whole-group deadlines.
"""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timedelta
from itertools import combinations
from typing import Any

from .timeutil import as_minute, iso

EPS_WH = 0.01
EPS_KW = 1e-9


def _dt(value: datetime | str) -> datetime:
    if isinstance(value, datetime):
        return value
    from .timeutil import parse_dt

    parsed = parse_dt(value)
    assert parsed is not None
    return parsed


def normalize_vehicle(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "vin": row["vin"],
        "booking": row.get("booking"),
        "connector": row["connector"],
        "ready": _dt(row["ready"]),
        "deadline": _dt(row.get("deadline") or row["handoff"]),
        "remaining_wh": float(row.get("remaining_wh", row.get("need_wh", 0))),
        "interruptible": bool(row.get("interruptible", True)),
        "power_limit_kw": row.get("power_limit_kw"),
    }


def normalize_port(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "port": row["port"],
        "connector": row["connector"],
        "bank": row["bank"],
        "max_kw": float(row["max_kw"]),
        "bank_kw": float(row["bank_kw"]),
    }


def normalize_reservation(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "uid": row.get("uid") or row.get("id") or row.get("job_id"),
        "vin": row.get("vin"),
        "port": row["port"],
        "bank": row.get("bank"),
        "start": _dt(row["start"]),
        "end": _dt(row["end"]),
        "kw": float(row["kw"]),
        "kind": row.get("kind", "reserved"),
    }


def _order_vehicles(
    vehicles: list[dict[str, Any]],
    t: datetime,
    strategy: str,
) -> list[dict[str, Any]]:
    active = [v for v in vehicles if v["remaining_wh"] > EPS_WH and v["ready"] <= t]

    def required_kw(v: dict[str, Any]) -> float:
        seconds = max(60.0, (v["deadline"] - t).total_seconds())
        return v["remaining_wh"] * 3600.0 / (seconds * 1000.0)

    if strategy == "constrained":
        return sorted(
            active,
            key=lambda v: (
                len(v.get("compatible_ports", ())) or 99,
                v["deadline"],
                -required_kw(v),
                v["vin"],
            ),
        )
    if strategy == "power":
        return sorted(active, key=lambda v: (-required_kw(v), v["deadline"], v["vin"]))
    return sorted(active, key=lambda v: (v["deadline"], -required_kw(v), v["vin"]))


def simulate(
    *,
    vehicles: list[dict[str, Any]],
    ports: list[dict[str, Any]],
    start: datetime | str,
    horizon: datetime | str | None = None,
    reservations: list[dict[str, Any]] | None = None,
    allow_after_deadline: bool = False,
    max_days_after_deadline: int = 7,
    strategy: str = "deadline",
) -> dict[str, Any]:
    """Simulate charging and return slots and projected finish times."""

    start_dt = _dt(start)
    vehicle_rows = [normalize_vehicle(v) for v in vehicles]
    port_rows = [normalize_port(p) for p in ports]
    reservation_rows = [normalize_reservation(r) for r in (reservations or [])]

    port_by_id = {p["port"]: p for p in port_rows}
    for reservation in reservation_rows:
        port = port_by_id.get(reservation["port"])
        if port is None:
            return {
                "feasible": False,
                "conflicts": [f"预留占用了不存在的端口 {reservation['port']}"],
                "slots": [],
                "vehicles": {},
            }
        reservation["bank"] = port["bank"]

    deadlines = [v["deadline"] for v in vehicle_rows]
    base_horizon = max(deadlines) if deadlines else start_dt + timedelta(hours=1)
    if horizon is not None:
        horizon_dt = _dt(horizon)
    elif allow_after_deadline:
        horizon_dt = base_horizon + timedelta(days=max_days_after_deadline)
    else:
        horizon_dt = base_horizon

    # Reports/clock may predate every vehicle's plug-in window.  Jumping to the
    # first ready time keeps the deterministic minute simulator cheap.
    effective_start = start_dt
    if vehicle_rows:
        effective_start = max(effective_start, min(v["ready"] for v in vehicle_rows))
    t0 = as_minute(effective_start)

    remaining = {v["vin"]: float(v["remaining_wh"]) for v in vehicle_rows}
    finish: dict[str, datetime | None] = {v["vin"]: None for v in vehicle_rows}
    planned_wh = {v["vin"]: 0.0 for v in vehicle_rows}
    locked_port: dict[str, str] = {}
    slots: list[dict[str, Any]] = []
    conflicts: list[str] = []
    interruptions: list[dict[str, Any]] = []

    t = t0
    while t < horizon_dt:
        slot_begin = max(t, effective_start)
        slot_end = t + timedelta(minutes=1)
        if slot_begin >= slot_end:
            t = slot_end
            continue
        hours = (slot_end - slot_begin).total_seconds() / 3600.0

        occupied: dict[str, str] = {}
        bank_load: dict[str, float] = {}
        reserved_vins: set[str] = set()
        minute_reservations = []
        for reservation in reservation_rows:
            if reservation["start"] < slot_end and reservation["end"] > slot_begin:
                owner = occupied.get(reservation["port"])
                if owner is not None and owner != reservation.get("uid"):
                    conflicts.append(
                        f"{iso(slot_begin)} 端口 {reservation['port']} 被多个预留占用"
                    )
                occupied[reservation["port"]] = reservation.get("uid") or "reserved"
                bank_load[reservation["bank"]] = (
                    bank_load.get(reservation["bank"], 0.0) + reservation["kw"]
                )
                overlap_begin = max(reservation["start"], slot_begin)
                overlap_end = min(reservation["end"], slot_end)
                overlap_hours = max(
                    0.0, (overlap_end - overlap_begin).total_seconds() / 3600.0
                )
                if reservation.get("vin"):
                    energy = reservation["kw"] * 1000.0 * overlap_hours
                    vin = reservation["vin"]
                    reserved_vins.add(vin)
                    if vin in remaining:
                        remaining[vin] = max(0.0, remaining[vin] - energy)
                        planned_wh[vin] += energy
                        if remaining[vin] <= EPS_WH and finish[vin] is None:
                            remaining[vin] = 0.0
                            finish[vin] = overlap_end
                minute_reservations.append(reservation)

        for bank, load in bank_load.items():
            cap = next((p["bank_kw"] for p in port_rows if p["bank"] == bank), None)
            if cap is not None and load > cap + EPS_KW:
                conflicts.append(
                    f"{iso(slot_begin)} 供电组 {bank} 预留功率 {load:g}kW 超过上限 {cap:g}kW"
                )

        # Annotate connector options for stable ordering.
        for vehicle in vehicle_rows:
            vehicle["compatible_ports"] = [
                p["port"]
                for p in port_rows
                if p["connector"] == vehicle["connector"] and p["port"] not in occupied
            ]

        ordered = _order_vehicles(vehicle_rows, slot_begin, strategy)
        # Non-interruptible cars that were already running get first claim.
        ordered.sort(key=lambda v: 0 if v["vin"] in locked_port else 1)

        assigned_ports: set[str] = set()
        for vehicle in ordered:
            vin = vehicle["vin"]
            if vin in reserved_vins:
                continue
            if remaining[vin] <= EPS_WH:
                continue
            if not allow_after_deadline and slot_begin >= vehicle["deadline"]:
                continue

            forced_port = locked_port.get(vin)
            compatible = [
                p
                for p in port_rows
                if p["connector"] == vehicle["connector"]
                and p["port"] not in occupied
                and p["port"] not in assigned_ports
                and (forced_port is None or p["port"] == forced_port)
            ]
            if forced_port and not compatible:
                interruptions.append(
                    {
                        "vin": vin,
                        "at": iso(slot_begin),
                        "reason": f"不可中断车辆无法继续使用 {forced_port}",
                    }
                )
                locked_port.pop(vin, None)
                continue

            if not compatible:
                if forced_port:
                    interruptions.append(
                        {
                            "vin": vin,
                            "at": iso(slot_begin),
                            "reason": "不可中断车辆失去可用端口",
                        }
                    )
                    locked_port.pop(vin, None)
                continue

            # Preserve scarce banks: choose the bank with most free capacity,
            # then the smallest port that can still take the requested power.
            def port_score(p: dict[str, Any]) -> tuple[float, float, str]:
                free_bank = p["bank_kw"] - bank_load.get(p["bank"], 0.0)
                return (-free_bank, p["max_kw"], p["port"])

            port = sorted(compatible, key=port_score)[0]
            bank = port["bank"]
            free_bank = port["bank_kw"] - bank_load.get(bank, 0.0)
            limit = port["max_kw"]
            if vehicle.get("power_limit_kw") is not None:
                limit = min(limit, float(vehicle["power_limit_kw"]))
            max_kw = max(0.0, min(limit, free_bank))
            needed_kw = remaining[vin] / 1000.0 / hours
            kw = min(max_kw, needed_kw)
            if kw <= EPS_KW:
                if forced_port:
                    interruptions.append(
                        {
                            "vin": vin,
                            "at": iso(slot_begin),
                            "reason": "不可中断车辆在供电组上限内得到零功率",
                        }
                    )
                continue

            energy = kw * 1000.0 * hours
            remaining[vin] = max(0.0, remaining[vin] - energy)
            planned_wh[vin] += energy
            bank_load[bank] = bank_load.get(bank, 0.0) + kw
            assigned_ports.add(port["port"])
            occupied[port["port"]] = vin
            if not vehicle["interruptible"]:
                locked_port[vin] = port["port"]

            finish_at = slot_end
            if remaining[vin] <= EPS_WH:
                remaining[vin] = 0.0
                # If the final allocation was smaller than a full slot, finish
                # at the point energy actually reaches the minimum.
                if needed_kw > EPS_KW and kw + EPS_KW < needed_kw:
                    # kw was capacity-limited, so slot was fully used.
                    finish[vin] = slot_end
                else:
                    used_hours = (remaining_before := energy) and energy / (kw * 1000.0)
                    finish[vin] = slot_begin + timedelta(hours=used_hours)
                finish_at = finish[vin]
            slots.append(
                {
                    "vin": vin,
                    "booking": vehicle.get("booking"),
                    "port": port["port"],
                    "bank": bank,
                    "start": iso(slot_begin),
                    "end": iso(finish_at if remaining[vin] == 0 else slot_end),
                    "kw": round(kw, 6),
                    "planned_wh": round(energy, 6),
                }
            )

            if remaining[vin] == 0.0:
                locked_port.pop(vin, None)

        t = slot_end

    vehicle_results: dict[str, Any] = {}
    all_feasible = not conflicts and not interruptions
    for vehicle in vehicle_rows:
        vin = vehicle["vin"]
        met = remaining[vin] <= EPS_WH
        deadline = vehicle["deadline"]
        finished = finish.get(vin)
        on_time = met and finished is not None and finished <= deadline + timedelta(seconds=1)
        if not allow_after_deadline and not on_time:
            all_feasible = False
        if allow_after_deadline and not met:
            all_feasible = False
        vehicle_results[vin] = {
            "booking": vehicle.get("booking"),
            "remaining_wh_after": round(max(0.0, remaining[vin]), 3),
            "planned_wh": round(planned_wh[vin], 3),
            "finished": met,
            "finish_at": iso(finished),
            "deadline": iso(deadline),
            "on_time": on_time,
        }

    compressed = compress_slots(slots)
    return {
        "feasible": all_feasible,
        "strategy": strategy,
        "start": iso(effective_start),
        "requested_start": iso(start_dt),
        "horizon": iso(horizon_dt),
        "slots": slots,
        "jobs": compressed,
        "vehicles": vehicle_results,
        "conflicts": conflicts,
        "interruptions": interruptions,
        "max_finish_at": max(
            (f for f in finish.values() if f is not None), default=None
        )
        and iso(max(f for f in finish.values() if f is not None)),
    }


def compress_slots(slots: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Merge adjacent identical one-minute allocations into dispatch jobs."""
    ordered_slots = sorted(
        slots,
        key=lambda s: (
            s["vin"],
            s["port"],
            round(float(s["kw"]), 6),
            s["start"],
        ),
    )
    jobs: list[dict[str, Any]] = []
    for slot in ordered_slots:
        key = (slot["vin"], slot["port"], slot["bank"], round(float(slot["kw"]), 6))
        start = slot["start"]
        end = slot["end"]
        if jobs:
            previous = jobs[-1]
            previous_end = previous["end"]
            same_key = (
                previous["vin"],
                previous["port"],
                previous["bank"],
                round(float(previous["kw"]), 6),
            ) == key
            if same_key and previous_end == start:
                previous["end"] = end
                previous["planned_wh"] = previous["planned_wh"] + slot["planned_wh"]
                continue
        jobs.append(
            {
                "vin": slot["vin"],
                "booking": slot.get("booking"),
                "port": slot["port"],
                "bank": slot["bank"],
                "start": start,
                "end": end,
                "kw": slot["kw"],
                "planned_wh": slot["planned_wh"],
            }
        )
    for job in jobs:
        job["planned_wh"] = round(float(job["planned_wh"]), 3)
    jobs.sort(key=lambda j: (j["start"], j["vin"], j["port"]))
    for index, job in enumerate(jobs, start=1):
        job["seq"] = index
    return jobs


def best_schedule(**kwargs: Any) -> dict[str, Any]:
    """Try several deterministic priority rules and select the best schedule."""
    candidates = []
    for strategy in ("deadline", "constrained", "power"):
        result = simulate(strategy=strategy, **deepcopy(kwargs))
        candidates.append(result)
        if result["feasible"]:
            # The first feasible EDF-like schedule is acceptable; still try
            # all strategies and choose the one with earliest completion.
            pass

    feasible = [r for r in candidates if r["feasible"]]
    if feasible:
        return min(
            feasible,
            key=lambda r: (
                r.get("max_finish_at") or "9999",
                len(r.get("jobs", [])),
                r["strategy"],
            ),
        )

    def unfinished_key(result: dict[str, Any]) -> float:
        return sum(v["remaining_wh_after"] for v in result["vehicles"].values())

    return min(candidates, key=lambda r: (unfinished_key(r), r["strategy"]))


def feasible_subsets(
    *,
    vins: list[str],
    max_subset_size: int | None = None,
    **schedule_kwargs: Any,
) -> list[tuple[tuple[str, ...], dict[str, Any]]]:
    """Return feasible non-empty subsets, largest first."""
    results: list[tuple[tuple[str, ...], dict[str, Any]]] = []
    upper = len(vins) if max_subset_size is None else max_subset_size
    for size in range(min(len(vins), upper), 0, -1):
        for subset in combinations(vins, size):
            subset_set = set(subset)
            vehicles = [
                v for v in schedule_kwargs["vehicles"] if v["vin"] in subset_set
            ]
            kwargs = dict(schedule_kwargs)
            kwargs["vehicles"] = vehicles
            result = best_schedule(**kwargs)
            if result["feasible"]:
                results.append((subset, result))
        if results:
            # Largest size is enough; callers can tie-break.
            break
    results.sort(
        key=lambda item: (
            item[1].get("max_finish_at") or "9999",
            item[0],
        )
    )
    return results
