from django.http import JsonResponse
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

        devices = ShellyDevice.objects.select_related("thermostat_device").all()
        print("Found", len(devices), "devices.")

        for device in devices:
            print(f"Processing device: {device.device_id} ({device.familiar_name})")

            # Thermostat takes priority: skip all assignments if temperature is above max or in headroom zone
            if device.thermostat_device:
                thermostat = device.thermostat_device
                if thermostat.temperature_updated_at and (current_time - thermostat.temperature_updated_at) <= timedelta(minutes=15):
                    max_trigger = thermostat.max_temperature + Decimal("0.5")
                    effective_headroom = thermostat.get_effective_headroom()
                    headroom_trigger = thermostat.max_temperature - effective_headroom

                    if thermostat.current_temperature > max_trigger:
                        log_device_event(
                            device,
                            f"Skipping assignments: thermostat {thermostat.familiar_name} above max "
                            f"({thermostat.current_temperature} > {max_trigger}).",
                            "INFO",
                        )
                        continue
                    elif effective_headroom > 0 and thermostat.current_temperature > headroom_trigger:
                        log_device_event(
                            device,
                            f"Skipping assignments: thermostat {thermostat.familiar_name} in headroom zone "
                            f"({thermostat.current_temperature} > {headroom_trigger}, headroom={effective_headroom}°C).",
                            "INFO",
                        )
                        continue

            # Get cheapest hours for this device (tagged with assignment type)
            tagged_hours = get_cheapest_hours(
                prices,
                device.day_transfer_price,
                device.night_transfer_price,
                device.run_hours_per_day,
                device.auto_assign_price_threshold,
                return_tagged=True,
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
            tagged_hours = get_cheapest_hours(
                prices,
                charger.day_transfer_price,
                charger.night_transfer_price,
                charger.run_hours_per_day,
                charger.auto_assign_price_threshold,
                return_tagged=True,
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


def get_cheapest_hours(
    prices: list[dict],
    day_transfer_price: float,
    night_transfer_price: float,
    hours_needed: int,
    price_threshold: float | None = None,
    local_tz: timezone = LOCAL_TZ,
    return_tagged: bool = False,
):
    """Return cheapest + threshold slots.

    When return_tagged=True, returns list of (timestamp, assignment_type) tuples
    where assignment_type is 'cheapest' or 'threshold'.
    When return_tagged=False (default), returns plain list of timestamps.
    """
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

    enriched.sort(key=lambda x: x[0])
    periods_needed = hours_needed * 4
    cheapest_slots = [slot for _, slot in enriched[:periods_needed]]
    cheapest_set = set(cheapest_slots)
    forced_extras = [slot for slot in forced_slots if slot not in cheapest_set]

    if return_tagged:
        return [(slot, "cheapest") for slot in cheapest_slots] + \
               [(slot, "threshold") for slot in forced_extras]
    return cheapest_slots + forced_extras
