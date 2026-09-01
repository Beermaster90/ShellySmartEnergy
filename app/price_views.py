from django.http import JsonResponse
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from datetime import datetime, timedelta, timezone
from .models import (
    ElectricityPrice,
    ShellyDevice,
    DeviceLog,
    DeviceAssignment,
    AppSetting,
    EVCharger,
    EVChargerAssignment,
)
import pandas as pd
import requests
from entsoe import EntsoeRawClient
from entsoe.parsers import parse_prices
from django.shortcuts import render
from django.utils.timezone import now
from datetime import timedelta
from .logger import log_device_event
from .device_assignment_manager import DeviceAssignmentManager  # Import the class
from app.utils.time_utils import TimeUtils
from app.utils.security_utils import SecurityUtils
from app.utils.db_utils import with_db_retries
import pytz  # pip install pytz
import xml.etree.ElementTree as ET

LOCAL_TZ = pytz.timezone("Europe/Helsinki")  # change if needed

# Fallback day-ahead price source, used only when ENTSO-E is unavailable.
# Elering (Estonia's TSO) republishes Nord Pool prices on its own
# infrastructure, no API key required, so it stays reachable during an
# ENTSO-E outage. Finland-only, matching this app's hardcoded area_code.
ELERING_PRICE_URL = "https://dashboard.elering.ee/api/nps/price"
ELERING_AREA = "fi"


def _fetch_elering_prices(start_utc, end_utc):
    """Day-ahead prices for Finland from Elering, in EUR/MWh, UTC-indexed.

    Raises on any failure (network, bad response, no data) so callers can
    treat it the same way as an ENTSO-E failure.
    """
    params = {
        "start": start_utc.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "end": end_utc.strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    response = requests.get(ELERING_PRICE_URL, params=params, timeout=15)
    response.raise_for_status()
    points = response.json().get("data", {}).get(ELERING_AREA, [])
    if not points:
        raise ValueError("Elering returned no Finnish price data")
    index = pd.to_datetime([p["timestamp"] for p in points], unit="s", utc=True)
    values = [p["price"] for p in points]
    return pd.Series(values, index=index).sort_index()


def get_entsoe_api_key():
    setting = AppSetting.objects.filter(key="ENTSOE_API_KEY").first()
    if not setting:
        # Create with default value if not found
        setting = AppSetting.objects.create(key="ENTSOE_API_KEY", value="ABC123")
    return setting.value


def _format_entsoe_series_preview(series, limit=3):
    if series is None:
        return "no series"
    total = len(series)
    if total == 0:
        return "count=0"

    def _format_items(items):
        return {
            TimeUtils.to_utc(ts).strftime("%Y-%m-%dT%H:%M:%SZ"): float(price)
            for ts, price in items
        }

    items = list(series.items())
    preview = {"count": total, "first": _format_items(items[:limit])}
    if total > limit:
        preview["last"] = _format_items(items[-limit:])
    return preview


def _summarize_entsoe_xml(raw_xml):
    try:
        root = ET.fromstring(raw_xml)
    except ET.ParseError as e:
        return {"parse_error": str(e)}

    def local_name(tag):
        return tag.split("}")[-1] if "}" in tag else tag

    time_series_count = 0
    time_series = []
    reasons = []
    for elem in root.iter():
        name = local_name(elem.tag)
        if name == "TimeSeries":
            time_series_count += 1
            ts_info = {}
            for child in list(elem):
                child_name = local_name(child.tag)
                if child_name == "Period":
                    period = child
                    start = None
                    resolution = None
                    positions = []
                    for pchild in list(period):
                        pchild_name = local_name(pchild.tag)
                        if pchild_name == "timeInterval":
                            for tchild in list(pchild):
                                tchild_name = local_name(tchild.tag)
                                if tchild_name == "start":
                                    start = tchild.text
                        elif pchild_name == "resolution":
                            resolution = pchild.text
                        elif pchild_name == "Point":
                            pos = None
                            for point_child in list(pchild):
                                point_child_name = local_name(point_child.tag)
                                if point_child_name == "position":
                                    try:
                                        pos = int(point_child.text)
                                    except (TypeError, ValueError):
                                        pos = None
                            if pos is not None:
                                positions.append(pos)
                    ts_info = {
                        "start": start,
                        "resolution": resolution,
                        "min_position": min(positions) if positions else None,
                        "max_position": max(positions) if positions else None,
                        "points": len(positions),
                    }
            if ts_info:
                time_series.append(ts_info)
        if name == "Reason":
            code = None
            text = None
            for child in list(elem):
                child_name = local_name(child.tag)
                if child_name == "code":
                    code = child.text
                elif child_name == "text":
                    text = child.text
            reasons.append({"code": code, "text": text})

    return {"time_series_count": time_series_count, "series": time_series, "reasons": reasons}


def call_fetch_prices(request):
    api_key = get_entsoe_api_key()
    if not api_key:
        return JsonResponse(
            {"error": "ENTSO-E API key not set in admin settings."}, status=400
        )
    area_code = "10YFI-1--------U"  # Finland, modify as needed

    # Use ENTSO-E publication window: 14:00 local time forward 25 hours
    now_utc = TimeUtils.now_utc()
    now_local = now_utc.astimezone(LOCAL_TZ)
    publication_local = now_local.replace(hour=14, minute=0, second=0, microsecond=0)
    if now_local < publication_local:
        start_local = publication_local - timedelta(days=1)
    else:
        start_local = publication_local
    end_local = start_local + timedelta(hours=25)

    future_cutoff = now_utc + timedelta(hours=12)

    future_prices_exist = ElectricityPrice.objects.filter(
        start_time__gt=future_cutoff
    ).exists()
    if future_prices_exist:
        print(f"Skipping fetch: Prices already exist beyond {future_cutoff}.")
        return JsonResponse({"message": "Prices already up-to-date."}, status=200)

    # Convert to Pandas Timestamp (ensuring UTC consistency)
    start = pd.Timestamp(start_local)
    end = pd.Timestamp(end_local)

    try:
        log_device_event(
            None,
            f"ENTSOE debug: now_utc={now_utc.isoformat()}, now_local={now_local.isoformat()}, tz={LOCAL_TZ}",
            "DEBUG",
        )
        log_device_event(
            None,
            f"ENTSOE debug: api_key_present={'yes' if api_key else 'no'}",
            "DEBUG",
        )
        log_device_event(
            None,
            f"ENTSOE request: area={area_code}, start={start}, end={end}",
            "INFO",
        )
        raw_client = EntsoeRawClient(api_key=api_key)
        raw_response = raw_client.query_day_ahead_prices(
            country_code=area_code, start=start, end=end
        )

        parsed = parse_prices(raw_response)
        preferred_keys = ("15min", "15T", "60min", "60T", "30min", "30T")
        price_series = None
        selected_resolution = None
        for key in preferred_keys:
            series = parsed.get(key)
            if series is not None and len(series) > 0:
                price_series = series
                selected_resolution = key
                break

        if price_series is None:
            raise ValueError("Parsed ENTSOE price series is empty")

        if price_series.index.tz is None:
            price_series = price_series.tz_localize(pytz.UTC)

        start_utc = TimeUtils.to_utc(start_local)
        end_utc = TimeUtils.to_utc(end_local)
        price_series = price_series.loc[(price_series.index >= start_utc) & (price_series.index < end_utc)]

        log_device_event(
            None,
            f"ENTSOE parsed resolution: {selected_resolution}",
            "DEBUG",
        )

        log_device_event(
            None,
            f"ENTSOE raw result preview: {_format_entsoe_series_preview(price_series)}",
            "DEBUG",
        )
        
        # Resample to 15-minute intervals using forward fill (each 15-min gets same price as the hour)
        price_series = price_series.resample('15min').ffill()

        log_device_event(
            None,
            f"ENTSOE resampled preview: {_format_entsoe_series_preview(price_series)}",
            "DEBUG",
        )
        
    except Exception as e:
        # ENTSO-E scheduled maintenance answers with HTTP 503 and a maintenance
        # page. That is expected and transient, not a fault in this app. Either
        # way, try the Elering fallback (separate infrastructure, no API key)
        # before giving up.
        response = getattr(e, "response", None)
        status_code = getattr(response, "status_code", None)
        body = ""
        try:
            body = (getattr(response, "text", "") or "")[:500]
        except Exception:
            body = ""
        is_maintenance = status_code == 503 or "temporarily unavailable" in body.lower() \
            or "maintenance" in body.lower()

        fallback_series = None
        try:
            fallback_start_utc = TimeUtils.to_utc(start_local)
            fallback_end_utc = TimeUtils.to_utc(end_local)
            fallback_series = _fetch_elering_prices(fallback_start_utc, fallback_end_utc)
            fallback_series = fallback_series.loc[
                (fallback_series.index >= fallback_start_utc) & (fallback_series.index < fallback_end_utc)
            ]
            fallback_series = fallback_series.resample('15min').ffill()
        except Exception:
            fallback_series = None

        if fallback_series is not None and not fallback_series.empty:
            price_series = fallback_series
            log_device_event(
                None,
                "ENTSO-E unavailable"
                + (" (under maintenance, HTTP 503)" if is_maintenance else "")
                + " — used Elering as a fallback price source for Finland.",
                "WARN",
            )
        elif is_maintenance:
            log_device_event(
                None,
                "ENTSO-E Transparency Platform is under maintenance (HTTP 503) and the "
                "Elering fallback failed too. No new prices this cycle — the scheduler "
                "will retry each hour and resume automatically when either returns.",
                "WARN",
            )
            return JsonResponse(
                {"error": "ENTSO-E under maintenance", "status": "maintenance", "retry": True},
                status=503,
            )
        else:
            try:
                raw_client = EntsoeRawClient(api_key=api_key)
                raw_response = raw_client.query_day_ahead_prices(
                    country_code=area_code, start=start, end=end
                )
                raw_text = SecurityUtils.sanitize_message(str(raw_response))
                preview = raw_text[:500]
                log_device_event(
                    None,
                    f"ENTSOE raw response preview: length={len(raw_text)} preview={preview}",
                    "DEBUG",
                )
                xml_summary = _summarize_entsoe_xml(raw_text)
                log_device_event(
                    None,
                    f"ENTSOE raw response summary: {xml_summary}",
                    "DEBUG",
                )
                try:
                    parsed = parse_prices(raw_text)
                    parsed_summary = {
                        key: {
                            "count": len(series),
                            "start": series.index.min().isoformat() if len(series) else None,
                            "end": series.index.max().isoformat() if len(series) else None,
                        }
                        for key, series in parsed.items()
                    }
                    log_device_event(
                        None,
                        f"ENTSOE parsed price summary: {parsed_summary}",
                        "DEBUG",
                    )
                except Exception as parse_error:
                    parse_error_safe = SecurityUtils.get_safe_error_message(
                        parse_error, "ENTSOE parse_prices failed"
                    )
                    log_device_event(None, parse_error_safe, "ERROR")
            except Exception as raw_error:
                raw_error_safe = SecurityUtils.get_safe_error_message(
                    raw_error, "ENTSOE raw response fetch failed"
                )
                log_device_event(None, raw_error_safe, "ERROR")
            # Sanitize error to hide API key and other sensitive information
            safe_error = SecurityUtils.get_safe_error_message(
                e, "ENTSOE price fetch failed"
            )
            safe_error = SecurityUtils.sanitize_message(safe_error)
            error_type = type(e).__name__
            if safe_error.endswith(":"):
                safe_error = f"{safe_error} {error_type}"
            else:
                safe_error = f"{safe_error} (type={error_type})"
            log_device_event(
                None,
                f"ENTSOE request failed for area={area_code}, start={start}, end={end}",
                "ERROR",
            )
            log_device_event(None, safe_error, "ERROR")
            return JsonResponse(
                {"error": "Failed to fetch electricity prices from ENTSOE"}, status=500
            )

    # **Ensure price_series is not empty before proceeding**
    if price_series.empty:
        return JsonResponse({"error": "Price series is empty"}, status=400)

    # Get the first timestamp from the price series
    period_start = TimeUtils.to_utc(price_series.index[0])

    # Convert `period_start` to string format for database saving
    period_start_str = period_start.strftime("%Y%m%d%H%M")

    # Save prices directly from the resampled series
    conversion_factor = Decimal("0.1")  # Convert from EUR/MWh to cents/kWh
    new_entries_added = False
    
    for timestamp, price in price_series.items():
        start_time = TimeUtils.to_utc(timestamp)
        end_time = TimeUtils.to_utc(timestamp + pd.Timedelta(minutes=15))
        price_c_per_kwh = Decimal(str(price)) * conversion_factor
        
        _, created = ElectricityPrice.objects.update_or_create(
            start_time=start_time,
            end_time=end_time,
            defaults={"price_kwh": price_c_per_kwh},
        )
        if created:
            new_entries_added = True
            
    # Update cheapest hours if new prices were added
    if new_entries_added:
        log_device_event(None, "New electricity prices fetched. Updating cheapest hours.", "INFO")
        set_cheapest_hours()
        
    # Import here to avoid circular import
    from app.tasks import DeviceController
    
    # Always run device control at the hour mark, regardless of new prices
    log_device_event(None, "Running scheduled device control check.", "INFO")
    DeviceController.control_shelly_devices()

    # Convert price timestamps to UTC formatted strings
    prices_dict = {
        TimeUtils.to_utc(ts).strftime("%Y-%m-%dT%H:%M:%SZ"): price
        for ts, price in price_series.items()
    }

    # Return the raw prices as JSON (converted to a dict)
    return JsonResponse({"prices": prices_dict})



@with_db_retries(max_attempts=3, delay=1)
def set_cheapest_hours():
    """Assigns devices to the cheapest hours for the next 24 hours."""
    try:
        # Get current UTC time
        current_time = TimeUtils.now_utc()
        print("Current Time:", current_time)

        print("Fetching electricity prices...")
        # Fetch electricity prices starting from the current time
        prices = list(
            ElectricityPrice.objects.filter(start_time__gte=current_time)
            .order_by("start_time")
            .values("start_time", "price_kwh", "id")
        )

        print("Found", len(prices), "prices.")

        if not prices:
            log_device_event(
                None, "No electricity prices available. Skipping assignment.", "WARN"
            )
            return

        devices = ShellyDevice.objects.all()
        print("Found", len(devices), "devices.")

        for device in devices:
            print(f"Processing device: {device.device_id} ({device.familiar_name})")

            # Note: current temperature is deliberately NOT considered here. This
            # plans the device's cheap hours for the whole day (including hours
            # long after "now", e.g. overnight once a currently-warm room has
            # cooled). ThermostatAssignmentManager.apply_next_period_assignments
            # runs every 15 minutes and adds/removes the *next* period's
            # assignment against the live reading (forced_min / removed_overheat /
            # removed_headroom) — that's where temperature actually gates
            # assignments, one period at a time.

            # Get cheapest hours for this device (tagged with assignment type)
            plan = plan_run_slots(
                prices,
                device.day_transfer_price,
                device.night_transfer_price,
                device.run_hours_per_day,
                device.auto_assign_price_threshold,
                block_scheduling=device.block_scheduling,
                min_run_block_minutes=device.min_run_block_minutes,
                switch_on_cost=device.switch_on_cost,
            )
            tagged_hours = plan.slots
            if device.block_scheduling:
                log_device_event(
                    device,
                    f"Block scheduling: {plan.summary()}",
                    "WARN" if plan.over_budget else "INFO",
                )

            # Create an assignment manager for the device's user
            assignment_manager = DeviceAssignmentManager(device.user)

            for hour, a_type in tagged_hours:
                # Normalize both timestamps to ensure minute-level matching
                price_entry = next(
                    (
                        p
                        for p in prices
                        if TimeUtils.to_utc(p["start_time"]).strftime("%Y-%m-%d %H:%M")
                        == TimeUtils.to_utc(hour).strftime("%Y-%m-%d %H:%M")
                    ),
                    None,
                )

                if price_entry:
                    # Fetch assignments for the next 24 hours
                    assignments = assignment_manager.get_assignments_next_24h(device)

                    # Ensure assignments is a valid queryset before filtering
                    if assignments is not None and hasattr(assignments, "filter"):
                        existing_assignment = assignments.filter(
                            electricity_price_id=price_entry["id"]
                        ).exists()
                    else:
                        print(
                            f"Error: `get_assignments_next_24h(device)` returned invalid data for device {device.device_id}"
                        )
                        existing_assignment = False

                    if not existing_assignment:
                        assignment_manager.log_assignment(
                            device,
                            ElectricityPrice.objects.get(id=price_entry["id"]),
                            assignment_type=a_type,
                        )

        # --- EV Chargers ---
        ev_chargers = EVCharger.objects.all()
        for charger in ev_chargers:
            plan = plan_run_slots(
                prices,
                charger.day_transfer_price,
                charger.night_transfer_price,
                charger.run_hours_per_day,
                charger.auto_assign_price_threshold,
                block_scheduling=charger.block_scheduling,
                min_run_block_minutes=charger.min_run_block_minutes,
                switch_on_cost=charger.switch_on_cost,
            )
            tagged_hours = plan.slots
            if charger.block_scheduling:
                from .logger import log_ev_event

                log_ev_event(
                    charger,
                    f"Block scheduling: {plan.summary()}",
                    "WARN" if plan.over_budget else "INFO",
                )

            for hour, a_type in tagged_hours:
                price_entry = next(
                    (
                        p for p in prices
                        if TimeUtils.to_utc(p["start_time"]).strftime("%Y-%m-%d %H:%M")
                        == TimeUtils.to_utc(hour).strftime("%Y-%m-%d %H:%M")
                    ),
                    None,
                )
                if price_entry:
                    existing = EVChargerAssignment.objects.filter(
                        user=charger.user,
                        charger=charger,
                        electricity_price_id=price_entry["id"],
                        electricity_price__start_time__gte=current_time,
                        electricity_price__start_time__lt=current_time + timedelta(hours=24),
                    ).exists()
                    if not existing:
                        EVChargerAssignment.objects.create(
                            user=charger.user,
                            charger=charger,
                            electricity_price_id=price_entry["id"],
                            assignment_type=a_type,
                        )

        print("Assignments successfully updated at", current_time)
        log_device_event(
            None, f"Assignments successfully updated at {current_time}", "INFO"
        )

    except Exception as e:
        # Sanitize error message to hide sensitive information
        safe_error = SecurityUtils.get_safe_error_message(
            e, "Error in set_cheapest_hours"
        )
        print("Error in set_cheapest_hours:", safe_error)
        log_device_event(None, safe_error, "ERROR")


SLOT_DURATION = timedelta(minutes=15)
SLOT_MINUTES = 15

# A smoothed plan is never allowed to cost more than this much over the plain
# cheapest-periods plan. Backtested over a year of Finnish prices it never
# triggers; it exists to bound price shapes we have not seen yet.
MAX_BLOCK_COST_PREMIUM_PERCENT = Decimal("5.0")


@dataclass
class RunPlan:
    """The periods chosen for one device, plus what the choice cost."""

    slots: list[tuple[datetime, str]]
    plain_cost: Decimal
    chosen_cost: Decimal
    plain_runs: int
    chosen_runs: int
    smoothed: bool = False
    over_budget: bool = False

    @property
    def premium_percent(self) -> Decimal:
        if not self.plain_cost:
            return Decimal("0")
        return (self.chosen_cost - self.plain_cost) / abs(self.plain_cost) * 100

    def summary(self) -> str:
        if not self.smoothed:
            reason = " (over budget, smoothing skipped)" if self.over_budget else ""
            return f"{self.chosen_runs} run(s), cheapest periods{reason}"
        return (
            f"{self.chosen_runs} run(s) instead of {self.plain_runs}, "
            f"cost +{self.premium_percent:.2f}%"
        )


def count_runs(slots: list[datetime]) -> int:
    """Number of separate switch-ons, i.e. maximal stretches of back-to-back slots."""
    ordered = sorted(slots)
    return sum(
        1
        for index, ts in enumerate(ordered)
        if index == 0 or ts - ordered[index - 1] != SLOT_DURATION
    )


def select_price_slots(
    ordered: list[tuple[Decimal, datetime]],
    periods_needed: int,
    switch_on_cost: Decimal,
    min_block_periods: int = 1,
) -> list[datetime]:
    """Cheapest way to book `periods_needed` slots, charging for every switch-on.

    `ordered` is a chronologically sorted list of (total_price, timestamp).
    Minimises `sum(period prices) + switch_on_cost * number of switch-ons`, so a
    short run is taken only when it saves more than it costs to start the device.
    Exact, via dynamic programming over (first free slot, periods still needed).

    With switch_on_cost=0 and min_block_periods=1 this returns the plain cheapest
    periods, which keeps the unsmoothed path on the same code.
    """
    slot_count = len(ordered)
    periods_needed = min(periods_needed, slot_count)
    if periods_needed <= 0:
        return []

    min_block_periods = max(1, min_block_periods)
    switch_cost = float(switch_on_cost)
    prefix = [0.0] * (slot_count + 1)
    for index, (price, _ts) in enumerate(ordered):
        prefix[index + 1] = prefix[index] + float(price)

    # Prices can have gaps, so reach[i] is the last slot still back-to-back with i.
    reach = [0] * slot_count
    reach[-1] = slot_count - 1
    for index in range(slot_count - 2, -1, -1):
        joins = ordered[index + 1][1] - ordered[index][1] == SLOT_DURATION
        reach[index] = reach[index + 1] if joins else index

    # best[i][k]  = cheapest way to book k more periods using slots i..end
    # taken[i][k] = length of the run starting at slot i, or 0 if slot i is skipped
    infinity = float("inf")
    best = [[infinity] * (periods_needed + 1) for _ in range(slot_count + 1)]
    taken = [[0] * (periods_needed + 1) for _ in range(slot_count + 1)]
    for index in range(slot_count + 1):
        best[index][0] = 0.0

    for index in range(slot_count - 1, -1, -1):
        current, following = best[index], best[index + 1]
        longest_run = reach[index] - index + 1
        for needed in range(1, periods_needed + 1):
            current[needed] = following[needed]  # leave this slot off
            for length in range(min_block_periods, min(needed, longest_run) + 1):
                cost = (
                    switch_cost
                    + (prefix[index + length] - prefix[index])
                    + best[index + length][needed - length]
                )
                if cost < current[needed]:
                    current[needed] = cost
                    taken[index][needed] = length

    chosen: list[datetime] = []
    index, needed = 0, periods_needed
    while index < slot_count and needed > 0:
        length = taken[index][needed]
        if not length:
            index += 1
            continue
        chosen.extend(ts for _price, ts in ordered[index : index + length])
        index += length
        needed -= length
    return chosen


def plan_run_slots(
    prices: list[dict],
    day_transfer_price: float,
    night_transfer_price: float,
    hours_needed: int,
    price_threshold: float | None = None,
    local_tz: timezone = LOCAL_TZ,
    block_scheduling: bool = False,
    min_run_block_minutes: int = 30,
    switch_on_cost: float = 1.0,
    max_premium_percent: float | None = None,
) -> RunPlan:
    """Choose the periods a device should run, and report what the choice cost."""
    if max_premium_percent is None:
        max_premium_percent = MAX_BLOCK_COST_PREMIUM_PERCENT
    day_tp = Decimal(str(day_transfer_price))
    night_tp = Decimal(str(night_transfer_price))
    threshold = Decimal(str(price_threshold)) if price_threshold is not None else None

    enriched: list[tuple[Decimal, datetime]] = []
    forced_slots: list[datetime] = []

    for entry in prices:
        ts: datetime = entry["start_time"]

        if ts.tzinfo is None:
            ts = local_tz.localize(ts)
        local_ts = ts.astimezone(local_tz)

        is_daytime = (7 <= local_ts.hour < 22) or (local_ts.hour == 22 and local_ts.minute == 0)
        transfer = day_tp if is_daytime else night_tp
        total = Decimal(str(entry["price_kwh"])) + transfer

        enriched.append((total, ts))
        if threshold is not None and total <= threshold:
            forced_slots.append(ts)

    periods_needed = hours_needed * 4
    by_time = sorted(enriched, key=lambda entry: entry[1])
    price_by_slot = {ts: total for total, ts in enriched}

    def cost_of(slots):
        return sum((price_by_slot[ts] for ts in slots), Decimal("0"))

    # The plain cheapest periods are both the fallback and the cost floor that
    # any smoothed plan is measured against. Picking them by price keeps devices
    # with block scheduling off on exactly the behaviour they had before.
    plain_slots = [ts for _total, ts in sorted(enriched, key=lambda entry: entry[0])[:periods_needed]]
    plain_cost = cost_of(plain_slots)
    plain_runs = count_runs(plain_slots)

    cheapest_slots = plain_slots
    chosen_cost = plain_cost
    chosen_runs = plain_runs
    smoothed = False
    over_budget = False

    if block_scheduling and periods_needed:
        min_block_periods = max(1, round((min_run_block_minutes or SLOT_MINUTES) / SLOT_MINUTES))
        smoothed_slots = select_price_slots(
            by_time, periods_needed, Decimal(str(switch_on_cost)), min_block_periods
        )
        smoothed_cost = cost_of(smoothed_slots)
        budget = plain_cost + abs(plain_cost) * Decimal(str(max_premium_percent)) / 100
        if smoothed_cost <= budget:
            cheapest_slots = smoothed_slots
            chosen_cost = smoothed_cost
            chosen_runs = count_runs(smoothed_slots)
            smoothed = True
        else:
            over_budget = True

    cheapest_set = set(cheapest_slots)
    slots = [(slot, "cheapest") for slot in cheapest_slots]
    slots += [(slot, "threshold") for slot in forced_slots if slot not in cheapest_set]

    return RunPlan(
        slots=slots,
        plain_cost=plain_cost,
        chosen_cost=chosen_cost,
        plain_runs=plain_runs,
        chosen_runs=chosen_runs,
        smoothed=smoothed,
        over_budget=over_budget,
    )


def get_cheapest_hours(
    prices: list[dict],
    day_transfer_price: float,
    night_transfer_price: float,
    hours_needed: int,
    price_threshold: float | None = None,
    local_tz: timezone = LOCAL_TZ,
    return_tagged: bool = False,
    block_scheduling: bool = False,
    min_run_block_minutes: int = 30,
    switch_on_cost: float = 1.0,
    max_premium_percent: float | None = None,
):
    """Return cheapest + threshold slots.

    Thin wrapper over plan_run_slots for callers that only need the periods.

    When return_tagged=True, returns list of (timestamp, assignment_type) tuples
    where assignment_type is 'cheapest' or 'threshold'.
    When return_tagged=False (default), returns plain list of timestamps.
    """
    plan = plan_run_slots(
        prices,
        day_transfer_price,
        night_transfer_price,
        hours_needed,
        price_threshold=price_threshold,
        local_tz=local_tz,
        block_scheduling=block_scheduling,
        min_run_block_minutes=min_run_block_minutes,
        switch_on_cost=switch_on_cost,
        max_premium_percent=max_premium_percent,
    )
    if return_tagged:
        return plan.slots
    return [slot for slot, _assignment_type in plan.slots]
