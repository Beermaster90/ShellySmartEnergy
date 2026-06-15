from datetime import datetime, timedelta
from django.shortcuts import render
from django.http import HttpRequest, JsonResponse
from django.contrib.auth.decorators import login_required
from django.db.models import Q
from .models import ElectricityPrice, ShellyDevice, DeviceAssignment, ShellyTemperature, TemperatureReading, EVCharger
from app.utils.time_utils import TimeUtils
from .views import get_version_info
import json
from decimal import Decimal
from typing import List, Dict, Any


@login_required(login_url="/login/")
def graphs(request: HttpRequest):
    """Renders the graphs page with cost comparison functionality."""

    # Default values
    fixed_price = request.GET.get("fixed_price", "12.0")  # cents per kWh
    yearly_consumption = request.GET.get("yearly_consumption", "13000")  # kWh per year
    shelly_controlled_percentage = request.GET.get("shelly_controlled_percentage", "30")  # % of usage controlled by Shelly (water heater + floor heating)

    try:
        fixed_price = float(fixed_price)
        yearly_consumption = float(yearly_consumption)
        shelly_controlled_percentage = float(shelly_controlled_percentage)
        # Calculate watts from yearly consumption: (kWh * 1000) / 8760 hours
        watts = int((yearly_consumption * 1000) / 8760)
    except (ValueError, TypeError):
        fixed_price = 12.0
        yearly_consumption = 13000
        shelly_controlled_percentage = 30.0
        watts = 1141

    # Handle user selection for admins
    users = None
    selected_user = request.user
    
    if request.user.is_superuser:
        from django.contrib.auth.models import User
        # Include all users in the dropdown
        users = User.objects.order_by("username")
        selected_user_id = request.GET.get("user_id")
        
        if selected_user_id:
            selected_user = User.objects.filter(id=selected_user_id).first()
        
        # If no user selected or invalid user, default to first user with assigned devices
        if not selected_user:
            # Try to find first user with device assignments
            for user in users:
                if DeviceAssignment.objects.filter(user=user).exists():
                    selected_user = user
                    break
            # If no user has assignments, use first user or request.user
            if not selected_user:
                selected_user = users.first() or request.user

    thermostat_devices = ShellyTemperature.objects.filter(user=selected_user).order_by("familiar_name")
    selected_thermostat_id = request.GET.get("thermostat_device_id")
    selected_thermostat = None
    if selected_thermostat_id:
        selected_thermostat = thermostat_devices.filter(device_id=selected_thermostat_id).first()
    if not selected_thermostat:
        selected_thermostat = thermostat_devices.first()

    shelly_devices = ShellyDevice.objects.filter(user=selected_user).order_by("familiar_name")
    selected_run_history_device_id = request.GET.get("run_history_device_id")
    selected_run_history_device = None
    if selected_run_history_device_id:
        selected_run_history_device = shelly_devices.filter(device_id=selected_run_history_device_id).first()
    if not selected_run_history_device:
        selected_run_history_device = shelly_devices.first()

    ev_chargers = EVCharger.objects.filter(user=selected_user).order_by("familiar_name")
    selected_ev_charger_id = request.GET.get("ev_charger_id")
    selected_ev_charger = None
    if selected_ev_charger_id:
        selected_ev_charger = ev_chargers.filter(id=selected_ev_charger_id).first()
    if not selected_ev_charger:
        selected_ev_charger = ev_chargers.first()

    context = {
        "title": "Cost Graphs",
        "year": datetime.now().year,
        "fixed_price": fixed_price,
        "yearly_consumption": yearly_consumption,
        "watts": watts,
        "shelly_controlled_percentage": shelly_controlled_percentage,
        "thermostat_devices": thermostat_devices,
        "selected_thermostat": selected_thermostat,
        "shelly_devices": shelly_devices,
        "selected_run_history_device": selected_run_history_device,
        "ev_chargers": ev_chargers,
        "selected_ev_charger": selected_ev_charger,
        "version": get_version_info(),
        "users": users,
        "selected_user": selected_user,
    }

    return render(request, "app/graphs.html", context)


@login_required
def get_graph_data(request: HttpRequest):
    """AJAX endpoint to get updated graph data."""

    fixed_price = request.GET.get("fixed_price", "12.0")
    yearly_consumption = request.GET.get("yearly_consumption", "13000")
    shelly_controlled_percentage = request.GET.get("shelly_controlled_percentage", "30")

    try:
        fixed_price = float(fixed_price)
        yearly_consumption = float(yearly_consumption)
        shelly_controlled_percentage = float(shelly_controlled_percentage)
        # Calculate watts from yearly consumption: (kWh * 1000) / 8760 hours
        watts = int((yearly_consumption * 1000) / 8760)
    except (ValueError, TypeError):
        return JsonResponse({"error": "Invalid input values"}, status=400)

    # Handle user selection for admins
    selected_user = request.user
    
    if request.user.is_superuser:
        from django.contrib.auth.models import User
        selected_user_id = request.GET.get("user_id")
        
        if selected_user_id:
            selected_user = User.objects.filter(id=selected_user_id).first()
        
        # If no user selected or invalid user, default to first user with assigned devices
        if not selected_user:
            users = User.objects.order_by("username")
            # Try to find first user with device assignments
            for user in users:
                if DeviceAssignment.objects.filter(user=user).exists():
                    selected_user = user
                    break
            # If no user has assignments, use first user or request.user
            if not selected_user:
                selected_user = users.first() or request.user

    # Get all available historical data (flexible time period)
    # First, check what data we actually have
    earliest_price = ElectricityPrice.objects.order_by("start_time").first()
    latest_price = ElectricityPrice.objects.order_by("-start_time").first()

    if earliest_price and latest_price:
        # Use actual data range instead of fixed 365 days
        start_date = earliest_price.start_time
        end_date = latest_price.start_time
    else:
        # Fallback to past year if no data
        end_date = TimeUtils.now_utc()
        start_date = end_date - timedelta(days=365)

    # Fetch all available electricity prices
    historical_prices = ElectricityPrice.objects.filter(
        start_time__gte=start_date, start_time__lte=end_date
    ).order_by("start_time")

    # Calculate costs for both scenarios (use selected_user instead of request.user)
    graph_data = calculate_cost_comparison(
        historical_prices, fixed_price, watts, selected_user, shelly_controlled_percentage
    )

    return JsonResponse({"graph_data": graph_data})


@login_required
def get_temperature_data(request: HttpRequest):
    """AJAX endpoint returning temperature chart data for the selected thermostat."""
    selected_user = request.user
    if request.user.is_superuser:
        from django.contrib.auth.models import User
        user_id = request.GET.get("user_id")
        if user_id:
            selected_user = User.objects.filter(id=user_id).first() or request.user

    thermostat_id = request.GET.get("thermostat_device_id")
    thermostat = None
    if thermostat_id:
        thermostat = ShellyTemperature.objects.filter(device_id=thermostat_id, user=selected_user).first()
    if not thermostat:
        thermostat = ShellyTemperature.objects.filter(user=selected_user).order_by("familiar_name").first()

    temp_graph_data = {"labels": [], "values": []}
    temp_year_graph_data = {"labels": [], "values": []}

    if thermostat:
        now_utc = TimeUtils.now_utc()
        user_tz = TimeUtils.get_user_timezone(request.user)

        readings = TemperatureReading.objects.filter(
            thermostat=thermostat,
            recorded_at__gte=now_utc - timedelta(days=15),
            recorded_at__lte=now_utc + timedelta(days=15),
        ).order_by("recorded_at")
        for reading in readings:
            local_dt = reading.recorded_at.astimezone(user_tz)
            temp_graph_data["labels"].append(local_dt.strftime("%d.%m %H:%M"))
            temp_graph_data["values"].append(float(reading.temperature_c))

        year_readings = TemperatureReading.objects.filter(
            thermostat=thermostat,
            recorded_at__gte=now_utc - timedelta(days=365),
        ).order_by("recorded_at")
        monthly = {}
        for reading in year_readings:
            key = reading.recorded_at.astimezone(user_tz).strftime("%Y-%m")
            monthly.setdefault(key, []).append(float(reading.temperature_c))
        for key in sorted(monthly.keys()):
            vals = monthly[key]
            temp_year_graph_data["labels"].append(datetime.strptime(key, "%Y-%m").strftime("%b %Y"))
            temp_year_graph_data["values"].append(round(sum(vals) / len(vals), 2))

    return JsonResponse({
        "temperature_graph_data": temp_graph_data,
        "temperature_year_graph_data": temp_year_graph_data,
    })


@login_required
def get_run_history_data(request: HttpRequest):
    """AJAX endpoint returning the 7-day run history calendar for the selected device."""
    selected_user = request.user
    if request.user.is_superuser:
        from django.contrib.auth.models import User
        user_id = request.GET.get("user_id")
        if user_id:
            selected_user = User.objects.filter(id=user_id).first() or request.user

    device_id = request.GET.get("run_history_device_id")
    device = None
    if device_id:
        device = ShellyDevice.objects.filter(device_id=device_id, user=selected_user).first()
    if not device:
        device = ShellyDevice.objects.filter(user=selected_user).order_by("familiar_name").first()

    run_history_data: Dict[str, Any] = {"device_name": "", "days": []}
    if device:
        user_tz = TimeUtils.get_user_timezone(selected_user)
        run_history_data = calculate_device_run_history(device, user_tz, TimeUtils.now_utc())

    return JsonResponse({"run_history_data": run_history_data})


def calculate_cost_comparison(
    historical_prices, fixed_price_cents: float, watts: int, user, shelly_controlled_percentage: float = 28.0
) -> Dict[str, Any]:
    """
    Calculate cost comparison between current dynamic pricing and fixed pricing.
    Includes VAT (25.5%) and transfer costs for both scenarios.

    Args:
        historical_prices: QuerySet of ElectricityPrice objects
        fixed_price_cents: Fixed price in cents per kWh (base price, VAT will be added)
        watts: Power consumption in watts
        user: Current user for device assignments
        shelly_controlled_percentage: Percentage of total consumption controlled by Shelly (default: 28%)

    Returns:
        Dictionary containing graph data with proper cost calculations
    """

    # Constants
    VAT_MULTIPLIER = Decimal("1.255")  # 25.5% VAT
    shelly_multiplier = Decimal(str(shelly_controlled_percentage / 100))  # Convert percentage to decimal
    kwh_per_hour = Decimal(str(watts / 1000))  # Convert watts to kWh per hour
    
    # Seasonal consumption multipliers for South Finland
    # Based on: Summer ~800 kWh/month, Winter ~1375 kWh/month, Total 12234 kWh/year
    # Average = 12234 / 12 = 1019.5 kWh/month
    SEASONAL_MULTIPLIERS = {
        1: 1.35,   # January - Winter high
        2: 1.32,   # February - Winter high
        3: 1.20,   # March - Spring transition
        4: 1.00,   # April - Average
        5: 0.85,   # May - Spring low
        6: 0.78,   # June - Summer low
        7: 0.78,   # July - Summer low
        8: 0.80,   # August - Summer low
        9: 0.90,   # September - Autumn transition
        10: 1.05,  # October - Autumn
        11: 1.20,  # November - Early winter
        12: 1.32,  # December - Winter high
    }
    
    # Detect period length from the first two price records
    # This handles both legacy hourly data and new 15-minute data
    period_minutes = 60  # Default to hourly
    if len(historical_prices) >= 2:
        time_diff = (historical_prices[1].start_time - historical_prices[0].start_time).total_seconds() / 60
        if time_diff > 0:
            period_minutes = int(time_diff)

    # Get user's device assignments to understand when devices were actually running
    if user.is_superuser:
        assignments = DeviceAssignment.objects.select_related(
            "electricity_price", "device"
        ).all()
    else:
        assignments = DeviceAssignment.objects.filter(user=user).select_related(
            "electricity_price", "device"
        )

    # Create a mapping of price periods to assigned devices with their transfer costs
    assigned_periods = {}
    for assignment in assignments:
        price_id = assignment.electricity_price.id
        device = assignment.device
        
        # Determine if this is day or night pricing based on hour
        price_start = assignment.electricity_price.start_time
        hour = price_start.hour
        
        # Day: 07:00 - 21:59, Night: 22:00 - 06:59
        if 7 <= hour < 22:
            transfer_cost = device.day_transfer_price
        else:
            transfer_cost = device.night_transfer_price
            
        assigned_periods[price_id] = {
            'device': device,
            'transfer_cost': transfer_cost
        }

    # If no device assignments exist, simulate usage for demonstration
    simulate_full_usage = len(assigned_periods) == 0
    
    # For simulation, use default transfer costs (we'll need a representative device)
    default_day_transfer = Decimal("3.0")  # Default day transfer cost c/kWh
    default_night_transfer = Decimal("1.5")  # Default night transfer cost c/kWh
    
    # Calculate running percentage: If devices only run during assigned periods,
    # they need to consume at a higher rate to reach the yearly target
    # The percentage is calculated for the CURRENT data period, assuming same pattern continues
    if not simulate_full_usage and len(historical_prices) > 0:
        running_percentage = Decimal(str(len(assigned_periods) / len(historical_prices)))
        # Adjust multiplier: if devices run X% of time, they need target/X power when on
        # Example: 30% target, 27% running time = 30%/27% = 111% power when running
        effective_multiplier = shelly_multiplier / running_percentage if running_percentage > 0 else shelly_multiplier
    else:
        # If simulating or no data, assume devices run 100% of time at target rate
        effective_multiplier = shelly_multiplier

    # Prepare data for the graph
    labels = []
    dynamic_costs = []
    fixed_costs = []
    period_prices = []  # Store price info for each period for tooltips
    
    # Track ONLY controlled devices (Shelly percentage) for everything
    dynamic_cumulative = Decimal("0")
    fixed_cumulative = Decimal("0")
    total_kwh_consumed = Decimal("0")

    for price in historical_prices:
        # Convert price start time to user timezone for display
        user_tz_time = TimeUtils.to_user_timezone(price.start_time, user)
        date_label = user_tz_time.strftime("%m-%d %H:%M")
        labels.append(date_label)

        # Apply seasonal multiplier based on month
        month = price.start_time.month
        seasonal_multiplier = Decimal(str(SEASONAL_MULTIPLIERS.get(month, 1.0)))
        
        # Calculate kWh consumption per period with seasonal adjustment
        # The effective_multiplier is adjusted so that running only during assigned periods
        # still reaches the yearly consumption target (e.g., 30% of 10,000 kWh = 3,000 kWh/year)
        kwh_per_period_base = kwh_per_hour * (Decimal(str(period_minutes)) / 60) * seasonal_multiplier
        # Use effective multiplier to ensure yearly target is met despite part-time running
        kwh_per_period_controlled = kwh_per_period_base * effective_multiplier

        # Determine transfer costs (needed for both controlled and uncontrolled)
        hour = price.start_time.hour
        if price.id in assigned_periods:
            transfer_cost = assigned_periods[price.id]['transfer_cost']
        else:
            transfer_cost = default_day_transfer if 7 <= hour < 22 else default_night_transfer
        
        # Calculate base costs
        base_electricity_price = Decimal(str(price.price_kwh)) / 100  # Convert c/kWh to €/kWh
        transfer_cost_euro = transfer_cost / 100  # Convert c/kWh to €/kWh
        total_cost_per_kwh = base_electricity_price + transfer_cost_euro
        
        # Fixed price already includes VAT and transfer
        fixed_price_per_kwh = Decimal(str(fixed_price_cents)) / 100
        
        # Only track Shelly controlled devices (28% - water heater + floor heating)
        if price.id in assigned_periods or simulate_full_usage:
            # Controlled devices run during this period
            dynamic_cost = total_cost_per_kwh * VAT_MULTIPLIER * kwh_per_period_controlled
            fixed_cost = fixed_price_per_kwh * kwh_per_period_controlled
            
            # Accumulate costs
            dynamic_cumulative += dynamic_cost
            fixed_cumulative += fixed_cost
            total_kwh_consumed += kwh_per_period_controlled
            
            # Store price info for tooltip
            period_prices.append({
                'dynamic': float(total_cost_per_kwh * VAT_MULTIPLIER * 100),
                'fixed': float(fixed_price_per_kwh * 100),
                'base_price': float(price.price_kwh),
                'transfer': float(transfer_cost),
                'has_usage': True
            })
        else:
            # No controlled devices running - don't add any costs
            period_prices.append({
                'dynamic': float(total_cost_per_kwh * VAT_MULTIPLIER * 100),
                'fixed': float(fixed_price_per_kwh * 100),
                'base_price': float(price.price_kwh),
                'transfer': float(transfer_cost),
                'has_usage': False
            })

        # Store cumulative costs (only controlled devices)
        dynamic_costs.append(float(dynamic_cumulative))
        fixed_costs.append(float(fixed_cumulative))

    # Calculate total savings (only controlled devices)
    total_dynamic = float(dynamic_cumulative)
    total_fixed = float(fixed_cumulative)
    savings = total_fixed - total_dynamic
    savings_percentage = (savings / total_fixed * 100) if total_fixed > 0 else 0

    # Count actual usage periods vs simulated
    actual_usage_periods = len(assigned_periods) if not simulate_full_usage else 0
    total_periods = len(historical_prices)
    
    # Calculate average price per kWh (in c/kWh)
    # Only for controlled devices (Shelly-controlled water heater + floor heating)
    total_kwh_decimal = total_kwh_consumed if total_kwh_consumed > 0 else Decimal("1")
    
    # Average only for the controlled portion (28% of total consumption)
    avg_dynamic_price = (dynamic_cumulative * 100 / total_kwh_decimal) if total_kwh_consumed > 0 else Decimal("0")  # Convert € to c
    avg_fixed_price = (fixed_cumulative * 100 / total_kwh_decimal) if total_kwh_consumed > 0 else Decimal("0")

    return {
        "labels": labels,
        "dynamic_costs": dynamic_costs,
        "fixed_costs": fixed_costs,
        "period_prices": period_prices,  # Add price info for each period
        "total_dynamic": total_dynamic,
        "total_fixed": total_fixed,
        "savings": savings,
        "savings_percentage": round(savings_percentage, 2),
        "fixed_price": fixed_price_cents,
        "watts": watts,
        "avg_dynamic_price": round(float(avg_dynamic_price), 2),  # Average dynamic price in c/kWh
        "avg_fixed_price": round(float(avg_fixed_price), 2),  # Average fixed price in c/kWh
        "periods_with_usage": (
            actual_usage_periods if not simulate_full_usage else total_periods
        ),
        "is_simulated": simulate_full_usage,
        "total_periods": total_periods,
    }


def calculate_device_run_history(device, user_tz, now_utc) -> Dict[str, Any]:
    """
    Build a 7-day × 96-slot (24h × 4 quarter-hours) run history grid for a device.
    A slot is True when a DeviceAssignment exists for that 15-minute period.
    Deleted assignments (e.g. removed by thermostat logic) correctly appear as not-running.
    """
    week_ago_utc = now_utc - timedelta(days=7)

    assignments = DeviceAssignment.objects.filter(
        device=device,
        electricity_price__start_time__gte=week_ago_utc,
        electricity_price__start_time__lte=now_utc,
    ).exclude(assignment_type="removed_overheat").select_related("electricity_price")

    running_slots: set = set()
    for assignment in assignments:
        local_dt = assignment.electricity_price.start_time.astimezone(user_tz)
        date_str = local_dt.strftime("%Y-%m-%d")
        slot = local_dt.hour * 4 + local_dt.minute // 15
        running_slots.add((date_str, slot))

    days = []
    for i in range(6, -1, -1):
        day_utc = now_utc - timedelta(days=i)
        local_day = day_utc.astimezone(user_tz)
        date_str = local_day.strftime("%Y-%m-%d")
        label = local_day.strftime("%a %d.%m")
        slots = [(date_str, s) in running_slots for s in range(96)]
        days.append({"label": label, "slots": slots})

    return {
        "device_name": device.familiar_name,
        "days": days,
    }
