from datetime import timedelta
from collections import defaultdict

from django.contrib.auth.decorators import login_required
from django.http import JsonResponse

from app.models import EVCharger, EVChargerAssignment, EVChargerEnergyLog
from app.utils.time_utils import TimeUtils


@login_required
def ev_charger_raw_dps(request):
    """Return the raw DP codes and values from the Tuya API for debugging."""
    if not request.user.is_superuser:
        return JsonResponse({"error": "Superuser only"}, status=403)
    charger_id = request.GET.get("charger_id")
    if not charger_id:
        return JsonResponse({"error": "charger_id required"}, status=400)
    try:
        charger = EVCharger.objects.get(id=int(charger_id))
    except (EVCharger.DoesNotExist, ValueError):
        return JsonResponse({"error": "Not found"}, status=404)

    from app.services.ev_charger_factory import get_ev_charger_service
    service = get_ev_charger_service(charger)
    path = f"/v1.0/devices/{service.device_id}/status"
    data = service._get(path)
    if not data:
        return JsonResponse({"error": "No response from Tuya"}, status=502)
    return JsonResponse({"result": data.get("result", [])})


@login_required
def ev_charger_refresh_status(request):
    """Live-poll one EV charger and update its cached model fields.

    Query param: charger_id (EVCharger.id)
    Returns the fresh status as JSON so the dashboard panel can update instantly.
    """
    charger_id = request.GET.get("charger_id")
    if not charger_id:
        return JsonResponse({"error": "charger_id required"}, status=400)

    try:
        charger_id = int(charger_id)
    except (ValueError, TypeError):
        return JsonResponse({"error": "Invalid charger_id"}, status=400)

    try:
        if request.user.is_superuser:
            charger = EVCharger.objects.get(id=charger_id)
        else:
            charger = EVCharger.objects.get(id=charger_id, user=request.user)
    except EVCharger.DoesNotExist:
        return JsonResponse({"error": "EV Charger not found"}, status=404)

    from app.services.ev_charger_factory import get_ev_charger_service
    try:
        service = get_ev_charger_service(charger)
        status = service.get_status()
    except Exception as e:
        return JsonResponse({"error": f"Service error: {e}"}, status=500)

    if status is None:
        return JsonResponse({"error": "No response from charger API"}, status=502)

    now = TimeUtils.now_utc()
    charger.is_charging = status.is_charging
    charger.work_state = status.work_state
    charger.connection_state = status.connection_state
    charger.power_w = status.power_w
    charger.temp_c = status.temp_c
    charger.session_energy_kwh = round(status.session_energy_kwh, 3)
    charger.total_energy_kwh = round(status.total_energy_kwh, 3)
    charger.last_contact = now
    charger.save(update_fields=[
        "is_charging", "work_state", "connection_state", "power_w",
        "temp_c", "session_energy_kwh", "total_energy_kwh", "last_contact", "updated_at",
    ])

    return JsonResponse({
        "is_charging": status.is_charging,
        "work_state": status.work_state,
        "connection_state": status.connection_state,
        "power_w": status.power_w,
        "temp_c": status.temp_c,
        "session_energy_kwh": charger.session_energy_kwh,
        "total_energy_kwh": charger.total_energy_kwh,
        "charge_current_set_a": status.charge_current_set_a,
        "last_contact": now.isoformat(),
    })


@login_required
def ev_charger_energy_history(request):
    """Return hourly kWh for yesterday + today + tomorrow.

    Past/current hours: actual energy from EVChargerEnergyLog odometer deltas.
    Future hours: assigned slots shown as a separate dataset (no kWh yet).
    """
    charger_id = request.GET.get("charger_id")
    if not charger_id:
        return JsonResponse({"error": "charger_id required"}, status=400)
    try:
        charger_id = int(charger_id)
    except (ValueError, TypeError):
        return JsonResponse({"error": "Invalid charger_id"}, status=400)

    try:
        if request.user.is_superuser:
            charger = EVCharger.objects.get(id=charger_id)
        else:
            charger = EVCharger.objects.get(id=charger_id, user=request.user)
    except EVCharger.DoesNotExist:
        return JsonResponse({"error": "EV Charger not found"}, status=404)

    import pytz
    user_tz = TimeUtils.get_user_timezone(request.user)
    now_utc = TimeUtils.now_utc()
    now_local = now_utc.astimezone(user_tz)

    VAT = 1.255
    day_transfer = float(charger.day_transfer_price)
    night_transfer = float(charger.night_transfer_price)

    # Window: start of yesterday → end of tomorrow (local time)
    yesterday_start = now_local.replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(days=1)
    tomorrow_end = now_local.replace(hour=23, minute=59, second=59, microsecond=0) + timedelta(days=1)
    window_start_utc = yesterday_start.astimezone(pytz.utc)
    window_end_utc = tomorrow_end.astimezone(pytz.utc)

    # --- Assignments where charging actually happened (charge_current_a is set) ---
    assignments = list(
        EVChargerAssignment.objects
        .filter(
            charger=charger,
            electricity_price__start_time__gte=window_start_utc,
            electricity_price__start_time__lte=window_end_utc,
            charge_current_a__isnull=False,
        )
        .select_related("electricity_price")
    )

    # For active session: energy_kwh is still null until charger_end.
    # Estimate per slot from current × 230V × 15min — realistic without needing session total.
    for a in assignments:
        if a.energy_kwh is None and a.charge_current_a:
            a.energy_kwh = round(a.charge_current_a * 230 * 0.25 / 1000, 3)  # kWh

    # Build hourly kWh and cost from assignments
    hourly_kwh: dict = defaultdict(float)
    hourly_cost: dict = defaultdict(float)
    for a in assignments:
        if a.energy_kwh is None:
            continue
        slot_local = a.electricity_price.start_time.astimezone(user_tz)
        hour_key = slot_local.strftime("%Y-%m-%d %H:00")
        kwh = float(a.energy_kwh)
        hourly_kwh[hour_key] += kwh
        transfer = day_transfer if 7 <= slot_local.hour < 22 else night_transfer
        price = float(a.electricity_price.price_kwh or 0)
        hourly_cost[hour_key] += kwh * (price + transfer) * VAT / 100

    # --- Build hourly result ---
    labels = []
    kwh_actual = []
    cost_actual = []
    day_costs: dict = defaultdict(float)

    slot = yesterday_start
    while slot <= tomorrow_end:
        hour_key = slot.strftime("%Y-%m-%d %H:00")
        day_key = slot.strftime("%d.%m")
        labels.append(slot.strftime("%d.%m %H:%M"))

        kwh = round(hourly_kwh.get(hour_key, 0.0), 3)
        kwh_actual.append(kwh if kwh > 0 else None)

        cost_eur = round(hourly_cost.get(hour_key, 0.0), 4)
        cost_actual.append(cost_eur if cost_eur > 0 else None)
        if cost_eur > 0:
            day_costs[day_key] += cost_eur

        slot += timedelta(hours=1)

    yesterday_label = yesterday_start.strftime("%d.%m")
    today_label = now_local.strftime("%d.%m")
    tomorrow_label = (now_local + timedelta(days=1)).strftime("%d.%m")

    return JsonResponse({
        "charger_name": charger.familiar_name,
        "labels": labels,
        "kwh_actual": kwh_actual,
        "cost_actual": cost_actual,
        "now_label": now_local.strftime("%d.%m %H:%M"),
        "day_costs": {
            "yesterday": {"label": yesterday_label, "cost_eur": round(day_costs.get(yesterday_label, 0.0), 2)},
            "today":     {"label": today_label,     "cost_eur": round(day_costs.get(today_label, 0.0), 2)},
            "tomorrow":  {"label": tomorrow_label,  "cost_eur": round(day_costs.get(tomorrow_label, 0.0), 2)},
        },
    })


@login_required
def ev_charger_monthly_cost(request):
    """Return monthly kWh and cost for the past 12 months for one EV charger.

    Uses the forward_energy_total odometer from EVChargerEnergyLog — daily peak deltas
    summed into months. Cost uses the average electricity price for the hours the charger
    was actually assigned, falling back to overall monthly average.

    Cost = monthly_kwh × (avg_assigned_price_c_kwh + weighted_transfer_c_kwh) × 1.255 / 100
    """
    charger_id = request.GET.get("charger_id")
    if not charger_id:
        return JsonResponse({"error": "charger_id required"}, status=400)
    try:
        charger_id = int(charger_id)
    except (ValueError, TypeError):
        return JsonResponse({"error": "Invalid charger_id"}, status=400)

    try:
        if request.user.is_superuser:
            charger = EVCharger.objects.get(id=charger_id)
        else:
            charger = EVCharger.objects.get(id=charger_id, user=request.user)
    except EVCharger.DoesNotExist:
        return JsonResponse({"error": "EV Charger not found"}, status=404)

    user_tz = TimeUtils.get_user_timezone(request.user)
    now_utc = TimeUtils.now_utc()
    cutoff = now_utc - timedelta(days=366)

    VAT = 1.255
    day_transfer = float(charger.day_transfer_price)
    night_transfer = float(charger.night_transfer_price)

    # --- Monthly kWh and cost from assignments where charging actually happened ---
    assignments = (
        EVChargerAssignment.objects
        .filter(
            charger=charger,
            electricity_price__start_time__gte=cutoff,
            charge_current_a__isnull=False,
            energy_kwh__isnull=False,
        )
        .select_related("electricity_price")
    )

    monthly_kwh: dict = defaultdict(float)
    monthly_cost: dict = defaultdict(float)
    for a in assignments:
        slot_local = a.electricity_price.start_time.astimezone(user_tz)
        month_key = slot_local.strftime("%Y-%m")
        kwh = float(a.energy_kwh)
        monthly_kwh[month_key] += kwh
        transfer = day_transfer if 7 <= slot_local.hour < 22 else night_transfer
        price = float(a.electricity_price.price_kwh or 0)
        monthly_cost[month_key] += kwh * (price + transfer) * VAT / 100

    # Build 12-month result
    labels = []
    kwh_out = []
    cost_out = []

    for months_back in range(11, -1, -1):
        year = now_utc.year
        month = now_utc.month - months_back
        while month <= 0:
            month += 12
            year -= 1
        month_key = f"{year:04d}-{month:02d}"
        labels.append(month_key)
        kwh_out.append(round(monthly_kwh.get(month_key, 0.0), 3))
        cost_out.append(round(monthly_cost.get(month_key, 0.0), 2))

    return JsonResponse({
        "charger_name": charger.familiar_name,
        "labels": labels,
        "kwh": kwh_out,
        "cost_eur": cost_out,
        "total_kwh": round(sum(kwh_out), 3),
        "total_cost_eur": round(sum(cost_out), 2),
    })
