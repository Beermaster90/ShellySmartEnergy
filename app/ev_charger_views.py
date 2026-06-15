from datetime import timedelta
from collections import defaultdict

from django.contrib.auth.decorators import login_required
from django.http import JsonResponse

from app.models import EVCharger, EVChargerEnergyLog
from app.utils.time_utils import TimeUtils


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
    """Return daily kWh charged for the last 30 days for one EV charger.

    Query params:
        charger_id  — EVCharger.id  (required)
        days        — number of days to look back (default 30)
    """
    charger_id = request.GET.get("charger_id")
    if not charger_id:
        return JsonResponse({"error": "charger_id required"}, status=400)

    try:
        charger_id = int(charger_id)
    except (ValueError, TypeError):
        return JsonResponse({"error": "Invalid charger_id"}, status=400)

    try:
        days = max(1, min(int(request.GET.get("days", 30)), 365))
    except (ValueError, TypeError):
        days = 30

    try:
        if request.user.is_superuser:
            charger = EVCharger.objects.get(id=charger_id)
        else:
            charger = EVCharger.objects.get(id=charger_id, user=request.user)
    except EVCharger.DoesNotExist:
        return JsonResponse({"error": "EV Charger not found"}, status=404)

    user_tz = TimeUtils.get_user_timezone(request.user)
    now_utc = TimeUtils.now_utc()
    cutoff = now_utc - timedelta(days=days)

    logs = (
        EVChargerEnergyLog.objects.filter(charger=charger, recorded_at__gte=cutoff)
        .order_by("recorded_at")
        .values("recorded_at", "session_energy_kwh", "total_energy_kwh", "is_charging", "power_w")
    )

    # Aggregate energy per calendar day (in user's timezone).
    # Strategy: find the peak total_energy_kwh per day — daily delta = peak today - peak yesterday.
    daily_totals: dict[str, float] = {}
    for entry in logs:
        dt_local = entry["recorded_at"].astimezone(user_tz)
        day_key = dt_local.strftime("%Y-%m-%d")
        total = float(entry["total_energy_kwh"])
        if day_key not in daily_totals or total > daily_totals[day_key]:
            daily_totals[day_key] = total

    # Build ordered list covering every day in the window
    labels = []
    kwh_values = []
    sorted_days = sorted(daily_totals.keys())
    for i, day in enumerate(sorted_days):
        prev_total = daily_totals[sorted_days[i - 1]] if i > 0 else daily_totals[day]
        delta = max(0.0, round(daily_totals[day] - prev_total, 3))
        labels.append(day)
        kwh_values.append(delta)

    return JsonResponse({
        "charger_name": charger.familiar_name,
        "labels": labels,
        "kwh": kwh_values,
        "total_kwh": round(daily_totals[sorted_days[-1]] if sorted_days else 0, 3),
    })


@login_required
def ev_charger_monthly_cost(request):
    """Return monthly kWh and cost for the past 12 months for one EV charger.

    Each 15-minute assignment slot uses its exact ElectricityPrice.price_kwh,
    so the cost reflects what the energy actually cost in that slot.

    Cost per slot = energy_kwh × (exact_price_c_kwh + weighted_transfer_c_kwh) × 1.255 / 100
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

    from app.models import EVChargerAssignment
    user_tz = TimeUtils.get_user_timezone(request.user)
    now_utc = TimeUtils.now_utc()
    cutoff = now_utc - timedelta(days=366)

    # Weighted transfer: day 07:00-21:59 = 15h, night 22:00-06:59 = 9h
    weighted_transfer = (
        float(charger.day_transfer_price) * (15 / 24)
        + float(charger.night_transfer_price) * (9 / 24)
    )
    VAT = 1.255

    # All assignments with measured energy in the window
    assignments = (
        EVChargerAssignment.objects
        .filter(charger=charger, energy_kwh__isnull=False,
                electricity_price__start_time__gte=cutoff)
        .select_related("electricity_price")
        .values("energy_kwh", "electricity_price__price_kwh",
                "electricity_price__start_time")
    )

    monthly_kwh: dict = defaultdict(float)
    monthly_cost: dict = defaultdict(float)

    for a in assignments:
        kwh = float(a["energy_kwh"])
        price_c = float(a["electricity_price__price_kwh"] or 0)
        slot_time = a["electricity_price__start_time"].astimezone(user_tz)
        month_key = slot_time.strftime("%Y-%m")
        cost_eur = kwh * (price_c + weighted_transfer) * VAT / 100
        monthly_kwh[month_key] += kwh
        monthly_cost[month_key] += cost_eur

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
