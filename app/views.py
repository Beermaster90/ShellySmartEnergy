from datetime import datetime, timedelta
from django.shortcuts import render
from django.http import HttpRequest, JsonResponse
from django.contrib.auth.decorators import login_required
from django.db import models
from django.contrib.auth.views import LoginView
from django.utils import timezone
from .models import ElectricityPrice, ShellyDevice, DeviceLog, DeviceAssignment, EVCharger, EVChargerAssignment
from .price_views import get_cheapest_hours
from .device_assignment_manager import DeviceAssignmentManager, EVChargerAssignmentManager
from app.utils.time_utils import TimeUtils
from typing import Dict, Any
from django.contrib.auth.models import User
from django.contrib.auth.models import User
from django.contrib.admin.views.decorators import staff_member_required
from django.urls import reverse
from app.forms import BootstrapAuthenticationForm
import json
import pytz
import os
from django.conf import settings


def get_version_info():
    """Read version info from BUILD_INFO file (created during Docker build) or fall back to VERSION file."""
    # First, try to read from BUILD_INFO file (created during Docker build)
    try:
        build_info_file = os.path.join(settings.BASE_DIR, 'BUILD_INFO')
        with open(build_info_file, 'r') as f:
            return f.read().strip()
    except:
        pass
    
    # Fall back to reading VERSION file and generating timestamp
    version = "1.0.0"
    try:
        version_file = os.path.join(settings.BASE_DIR, 'VERSION')
        with open(version_file, 'r') as f:
            version = f.read().strip()
    except:
        pass
    
    # Get build timestamp from VERSION file modification time
    build_timestamp = ""
    try:
        version_file = os.path.join(settings.BASE_DIR, 'VERSION')
        mtime = os.path.getmtime(version_file)
        build_date = datetime.fromtimestamp(mtime)
        build_timestamp = build_date.strftime("%Y%m%d-%H%M%S")
    except:
        build_timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    
    return f"{version}-{build_timestamp}"


class CustomLoginView(LoginView):
    """Custom login view that handles 'remember me' functionality"""
    form_class = BootstrapAuthenticationForm
    template_name = 'app/login.html'
    
    def get_context_data(self, **kwargs):
        """Add version and year to context"""
        context = super().get_context_data(**kwargs)
        context['year'] = datetime.now().year
        context['version'] = get_version_info()
        context['title'] = 'Log in'
        return context
    
    def form_valid(self, form):
        """Handle form validation and set session expiry based on remember me checkbox"""
        remember_me = form.cleaned_data.get('remember_me', False)
        
        # Call parent form_valid first to log the user in
        response = super().form_valid(form)
        
        if remember_me:
            # Set session to last 90 days (90 * 24 * 60 * 60 seconds)
            self.request.session.set_expiry(60 * 60 * 24 * 90)
        else:
            # Session expires when browser closes (set expiry to 0)
            self.request.session.set_expiry(0)
            
        return response


def get_common_context(request: HttpRequest) -> Dict[str, Any]:
    """Fetches shared context data, converting times to user's timezone."""
    now_utc = TimeUtils.now_utc()
    user_timezone = TimeUtils.get_user_timezone(request.user)
    now_user_tz = TimeUtils.to_user_timezone(now_utc, request.user)
    start_range = now_utc - timedelta(hours=12)
    end_range = now_utc + timedelta(hours=24)

    prices = list(
        ElectricityPrice.objects.filter(start_time__range=(start_range, end_range))
        .order_by("start_time")
        .values("id", "start_time", "end_time", "price_kwh")
    )

    users = None
    selected_user = request.user

    if request.user.is_superuser:
        # Include all users in the dropdown
        users = User.objects.order_by("username")
        selected_user_id = request.GET.get("user_id")

        if selected_user_id:
            selected_user = User.objects.filter(id=selected_user_id).first()

        # If no user selected or invalid user, default to first user with assigned devices
        if not selected_user:
            for user in users:
                if DeviceAssignment.objects.filter(user=user).exists():
                    selected_user = user
                    break
            if not selected_user:
                selected_user = users.first() or request.user

        devices = ShellyDevice.objects.filter(user=selected_user).select_related("thermostat_device")
        assignments = DeviceAssignment.objects.select_related(
            "device", "electricity_price"
        ).filter(user=selected_user)
        ev_chargers = EVCharger.objects.filter(user=selected_user)
        ev_assignments = EVChargerAssignment.objects.select_related(
            "charger", "electricity_price"
        ).filter(user=selected_user)
    else:
        devices = ShellyDevice.objects.filter(user=request.user).select_related("thermostat_device")
        assignments = DeviceAssignment.objects.filter(user=request.user)
        ev_chargers = EVCharger.objects.filter(user=request.user)
        ev_assignments = EVChargerAssignment.objects.select_related(
            "charger", "electricity_price"
        ).filter(user=request.user)

    selected_device = devices.first()
    selected_ev_charger = None

    raw_device_id = request.GET.get("device_id", "")
    if str(raw_device_id).startswith("ev_"):
        ev_id = int(raw_device_id[3:])
        selected_ev_charger = ev_chargers.filter(id=ev_id).first()
        selected_device = None
    elif raw_device_id:
        selected_device = devices.filter(device_id=raw_device_id).first()

    day_transfer_price = selected_device.day_transfer_price if selected_device else 0
    night_transfer_price = selected_device.night_transfer_price if selected_device else 0
    hours_needed = selected_device.run_hours_per_day if selected_device else 0

    # Build maps of price_id -> device keys (Shelly: "N", EV: "ev_N")
    assigned_devices_map = {}
    forced_devices_map = {}
    removed_overheat_map = {}
    removed_headroom_map = {}
    for assignment in assignments:
        price_id = assignment.electricity_price.id
        device_key = str(assignment.device.device_id)
        if assignment.assignment_type == "removed_overheat":
            removed_overheat_map.setdefault(price_id, []).append(device_key)
        elif assignment.assignment_type == "removed_headroom":
            removed_headroom_map.setdefault(price_id, []).append(device_key)
        else:
            assigned_devices_map.setdefault(price_id, []).append(device_key)
            if assignment.assignment_type == "forced_min":
                forced_devices_map.setdefault(price_id, []).append(device_key)

    for ev_assignment in ev_assignments:
        price_id = ev_assignment.electricity_price.id
        charger_key = f"ev_{ev_assignment.charger.id}"
        assigned_devices_map.setdefault(price_id, []).append(charger_key)

    for price in prices:
        price["assigned_devices"] = ",".join(assigned_devices_map.get(price["id"], []))
        price["forced_devices"] = ",".join(forced_devices_map.get(price["id"], []))
        price["removed_overheat_devices"] = ",".join(removed_overheat_map.get(price["id"], []))
        price["removed_headroom_devices"] = ",".join(removed_headroom_map.get(price["id"], []))
        price_user_tz = TimeUtils.to_user_timezone(price["start_time"], request.user)
        price["hour"] = str(price_user_tz.hour)
        price["time_key"] = f"{price_user_tz.hour:02d}:{price_user_tz.minute:02d}"
        price["start_time"] = price["start_time"].isoformat()
        price["end_time"] = price["end_time"].isoformat()

    assignment_manager = DeviceAssignmentManager(request.user)
    devices = assignment_manager.get_device_cheapest_hours(devices)

    ev_assignment_manager = EVChargerAssignmentManager(request.user)
    ev_chargers = ev_assignment_manager.get_charger_cheapest_hours(ev_chargers)

    rounded_minutes = (now_user_tz.minute // 15) * 15
    current_time_key = f"{now_user_tz.hour:02d}:{rounded_minutes:02d}"
    current_time = now_utc.strftime("%Y-%m-%d %H:%M")
    user_timezone_name = TimeUtils.get_user_timezone_name(request.user)

    return {
        "prices": prices,
        "devices": devices,
        "ev_chargers": ev_chargers,
        "users": users,
        "selected_user": selected_user,
        "selected_device": selected_device,
        "selected_ev_charger": selected_ev_charger,
        "day_transfer_price": day_transfer_price,
        "night_transfer_price": night_transfer_price,
        "hours_needed": hours_needed,
        "current_time_key": current_time_key,
        "current_time": current_time,
        "user_timezone": user_timezone_name,
        "title": "Landing Page",
        "year": now_utc.year,
        "version": get_version_info(),
    }


@login_required(login_url="/login/")
def index(request: HttpRequest):
    """Landing page view."""
    return render(request, "app/index.html", get_common_context(request))


@login_required
def about(request: HttpRequest):
    """Renders the logs page with device logs in user's timezone."""

    # Admin user selection (same pattern as graphs page)
    users = None
    selected_user = request.user
    if request.user.is_superuser:
        users = User.objects.order_by("username")
        selected_user_id = request.GET.get("user_id")
        if selected_user_id:
            selected_user = User.objects.filter(id=selected_user_id).first()
        if not selected_user:
            selected_user = users.first() or request.user

    # Device selection
    shelly_devices = ShellyDevice.objects.filter(user=selected_user).order_by("familiar_name")
    ev_charger_devices = EVCharger.objects.filter(user=selected_user).order_by("familiar_name")
    selected_device_id = request.GET.get("device_id", "")

    # Base queryset
    if selected_device_id == "system":
        logs = DeviceLog.objects.filter(device__isnull=True, ev_charger__isnull=True)
    elif str(selected_device_id).startswith("ev_"):
        ev_id = int(selected_device_id[3:])
        logs = DeviceLog.objects.filter(ev_charger__id=ev_id, ev_charger__user=selected_user)
    elif selected_device_id:
        logs = DeviceLog.objects.filter(device__device_id=selected_device_id, device__user=selected_user)
    elif request.user.is_superuser and not request.GET.get("user_id"):
        logs = DeviceLog.objects.all()
    else:
        logs = DeviceLog.objects.filter(
            models.Q(device__user=selected_user) | models.Q(ev_charger__user=selected_user)
        )

    # Status filter
    status_filter = request.GET.get("status", "")
    if status_filter:
        logs = logs.filter(status=status_filter)

    logs = logs.select_related("device", "ev_charger").order_by("-created_at")[:500]

    user_timezone_name = TimeUtils.get_user_timezone_name(request.user)
    user_tz = TimeUtils.get_user_timezone(request.user)
    for log in logs:
        local_dt = log.created_at.astimezone(user_tz)
        log.created_at_local = local_dt.strftime("%Y-%m-%d %H:%M:%S")
        if log.device:
            log.device_display_name = log.device.familiar_name
        elif log.ev_charger:
            log.device_display_name = f"[EV] {log.ev_charger.familiar_name}"
        else:
            log.device_display_name = "System"

    return render(
        request,
        "app/about.html",
        {
            "title": "Logs",
            "year": datetime.now().year,
            "logs": logs,
            "user_timezone": user_timezone_name,
            "users": users,
            "selected_user": selected_user,
            "shelly_devices": shelly_devices,
            "ev_charger_devices": ev_charger_devices,
            "selected_device_id": selected_device_id,
            "status_filter": status_filter,
            "version": get_version_info(),
        },
    )


def contact(request: HttpRequest):
    """Renders the contact page."""
    return render(
        request,
        "app/contact.html",
        {
            "title": "Contact",
            "message": "Your contact page.",
            "year": datetime.now().year,
            "version": get_version_info(),
        },
    )


@staff_member_required
def admin_test_page(request: HttpRequest):
    """Admin test page for triggering backend functionalities."""
    result = None
    assigned_hours = None
    devices = ShellyDevice.objects.all()
    ev_chargers = EVCharger.objects.all()
    prices = ElectricityPrice.objects.order_by("start_time")[:48]  # Show next 48 hours
    time_format = request.POST.get("time_format", "utc")
    local_tz = pytz.timezone("Europe/Helsinki")
    if request.method == "POST":
        action = request.POST.get("action")
        device_id = request.POST.get("device_id")
        if action == "fetch_prices":
            from .price_views import call_fetch_prices

            response = call_fetch_prices(request)
            if hasattr(response, "content"):
                result = response.content.decode("utf-8")
            else:
                result = str(response)
        elif action == "get_status" and device_id:
            from .shelly_views import fetch_device_status
            from django.test import RequestFactory

            rf = RequestFactory()
            fake_request = rf.get("/fake", {"device_id": device_id})
            response = fetch_device_status(fake_request)
            if hasattr(response, "content"):
                result = response.content.decode("utf-8")
            else:
                result = str(response)
            # Show assigned hours for the selected device
            device = devices.filter(device_id=device_id).first()
            if device:
                assignment_manager = DeviceAssignmentManager(device.user)
                hours = assignment_manager.get_device_cheapest_hours([device])[
                    0
                ].cheapest_hours
                if time_format == "local":
                    assigned_hours = [
                        local_tz.localize(datetime.strptime(h, "%H:%M"))
                        .astimezone(local_tz)
                        .strftime("%H:%M")
                        for h in hours
                    ]
                else:
                    assigned_hours = hours
        elif action == "run_schedule":
            from app.tasks import DeviceController

            try:
                DeviceController.control_shelly_devices()
                result = "15-minute schedule executed."
            except Exception as e:
                result = f"Failed to run 15-minute schedule: {e}"
        elif action == "assign_device":
            assign_device_id = request.POST.get("assign_device_id")
            assign_price_id = request.POST.get("assign_price_id")
            device = devices.filter(device_id=assign_device_id).first()
            price = prices.filter(id=assign_price_id).first()
            if device and price:
                assignment, created = DeviceAssignment.objects.get_or_create(
                    user=device.user,  # assign to device owner
                    device=device,
                    electricity_price=price,
                )
                if created:
                    result = f"Device {device.familiar_name} assigned to {price.start_time} for user {device.user.username}"
                else:
                    result = f"Device {device.familiar_name} was already assigned to {price.start_time} for user {device.user.username}"
            else:
                result = "Invalid device or price selection."
        elif action == "assign_cheapest_hours":
            cheapest_device_id = request.POST.get("cheapest_device_id")
            device = devices.filter(device_id=cheapest_device_id).first()
            if device:
                assignment_manager = DeviceAssignmentManager(device.user)
                # Only consider prices from now forward
                now_utc = TimeUtils.now_utc()
                prices_list = list(
                    ElectricityPrice.objects.filter(start_time__gte=now_utc)
                    .order_by("start_time")
                    .values("start_time", "price_kwh", "id")
                )
                cheapest_hours = get_cheapest_hours(
                    prices_list,
                    device.day_transfer_price,
                    device.night_transfer_price,
                    device.run_hours_per_day,
                    device.auto_assign_price_threshold,
                    local_tz,
                )
                assigned_count = 0
                for hour in cheapest_hours:
                    price_entry = next(
                        (
                            p
                            for p in prices_list
                            if TimeUtils.to_utc(p["start_time"]).strftime(
                                "%Y-%m-%d %H:%M"
                            )
                            == hour.strftime("%Y-%m-%d %H:%M")
                        ),
                        None,
                    )
                    if price_entry:
                        assignment, created = DeviceAssignment.objects.get_or_create(
                            user=device.user,  # assign to device owner
                            device=device,
                            electricity_price_id=price_entry["id"],
                        )
                        if created:
                            assigned_count += 1
                result = f"Assigned {assigned_count} cheapest hours to {device.familiar_name} for user {device.user.username} (override 24h check)"
            else:
                result = "Invalid device selection for cheapest hours assignment."

        elif action == "ev_assign_cheapest_hours":
            charger_id = request.POST.get("ev_charger_id", "").strip() or None
            charger = ev_chargers.filter(id=charger_id).first()
            if charger:
                now_utc = TimeUtils.now_utc()
                prices_list = list(
                    ElectricityPrice.objects.filter(start_time__gte=now_utc)
                    .order_by("start_time")
                    .values("start_time", "price_kwh", "id")
                )
                cheapest_hours = get_cheapest_hours(
                    prices_list,
                    charger.day_transfer_price,
                    charger.night_transfer_price,
                    charger.run_hours_per_day,
                    charger.auto_assign_price_threshold,
                    local_tz,
                )
                assigned_count = 0
                for hour in cheapest_hours:
                    price_entry = next(
                        (
                            p for p in prices_list
                            if TimeUtils.to_utc(p["start_time"]).strftime("%Y-%m-%d %H:%M")
                            == hour.strftime("%Y-%m-%d %H:%M")
                        ),
                        None,
                    )
                    if price_entry:
                        _, created = EVChargerAssignment.objects.get_or_create(
                            user=charger.user,
                            charger=charger,
                            electricity_price_id=price_entry["id"],
                        )
                        if created:
                            assigned_count += 1
                result = f"Assigned {assigned_count} cheapest hours to {charger.familiar_name} (override 24h check)"
            else:
                result = "Invalid EV charger selection."

        # --- EV Charger actions ---
        elif action == "ev_get_status":
            from app.services.ev_charger_factory import get_ev_charger_service
            import json as _json
            charger_id = request.POST.get("ev_charger_id", "").strip() or None
            charger = ev_chargers.filter(id=charger_id).first()
            if charger:
                try:
                    service = get_ev_charger_service(charger)
                    status = service.get_status()
                    if status:
                        result = _json.dumps({
                            "charger": charger.familiar_name,
                            "is_charging": status.is_charging,
                            "work_state": status.work_state,
                            "connection_state": status.connection_state,
                            "power_w": status.power_w,
                            "temp_c": status.temp_c,
                            "session_energy_kwh": status.session_energy_kwh,
                            "total_energy_kwh": status.total_energy_kwh,
                        }, indent=2)
                    else:
                        result = f"ERROR: get_status() returned None for {charger.familiar_name}"
                except Exception as e:
                    result = f"ERROR: {e}"
            else:
                result = "Invalid EV charger selection."

        elif action == "ev_start_charging":
            from app.services.ev_charger_factory import get_ev_charger_service
            charger_id = request.POST.get("ev_charger_id", "").strip() or None
            current_a = int(request.POST.get("ev_current_a", 6))
            charger = ev_chargers.filter(id=charger_id).first()
            if charger:
                try:
                    ok = get_ev_charger_service(charger).start_charging(current_a)
                    result = f"start_charging({current_a} A) → {'OK' if ok else 'FAILED'}"
                except Exception as e:
                    result = f"ERROR: {e}"
            else:
                result = "Invalid EV charger selection."

        elif action == "ev_stop_charging":
            from app.services.ev_charger_factory import get_ev_charger_service
            charger_id = request.POST.get("ev_charger_id", "").strip() or None
            charger = ev_chargers.filter(id=charger_id).first()
            if charger:
                try:
                    ok = get_ev_charger_service(charger).stop_charging()
                    result = f"stop_charging() → {'OK' if ok else 'FAILED'}"
                except Exception as e:
                    result = f"ERROR: {e}"
            else:
                result = "Invalid EV charger selection."

        elif action == "ev_set_current":
            from app.services.ev_charger_factory import get_ev_charger_service
            charger_id = request.POST.get("ev_charger_id", "").strip() or None
            current_a = int(request.POST.get("ev_current_a", 6))
            charger = ev_chargers.filter(id=charger_id).first()
            if charger:
                try:
                    ok = get_ev_charger_service(charger).set_charge_current(current_a)
                    result = f"set_charge_current({current_a} A) → {'OK' if ok else 'FAILED'}"
                except Exception as e:
                    result = f"ERROR: {e}"
            else:
                result = "Invalid EV charger selection."

        elif action == "ev_run_schedule":
            from app.tasks import DeviceController
            try:
                DeviceController.control_ev_chargers()
                result = "EV charger schedule executed."
            except Exception as e:
                result = f"ERROR running EV charger schedule: {e}"

    return render(
        request,
        "app/admin_test_page.html",
        {
            "devices": devices,
            "ev_chargers": ev_chargers,
            "prices": prices,
            "result": result,
            "assigned_hours": assigned_hours,
            "year": datetime.now().year,
            "version": get_version_info(),
        },
    )


from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_http_methods
import json

@csrf_exempt
@require_http_methods(["POST"])
def toggle_device_assignment(request):
    """
    Toggle device assignment for a specific hour.
    Assigns the device if not assigned, unassigns if already assigned.
    """
    if not request.user.is_authenticated:
        return JsonResponse({"success": False, "error": "User not authenticated"})
    
    try:
        data = json.loads(request.body)
        device_id = data.get('device_id')
        price_id = data.get('price_id')
        
        if not device_id or not price_id:
            return JsonResponse({"success": False, "error": "Missing device_id or price_id"})
        
        is_ev = str(device_id).startswith("ev_")

        try:
            electricity_price = ElectricityPrice.objects.get(id=price_id)
        except ElectricityPrice.DoesNotExist:
            return JsonResponse({"success": False, "error": "Electricity price not found"})

        if is_ev:
            ev_id = int(str(device_id)[3:])
            try:
                if request.user.is_staff or request.user.is_superuser:
                    charger = EVCharger.objects.get(id=ev_id)
                else:
                    charger = EVCharger.objects.get(id=ev_id, user=request.user)
            except EVCharger.DoesNotExist:
                return JsonResponse({"success": False, "error": "EV Charger not found or access denied"})

            if charger.status != 1:
                return JsonResponse({"success": False, "error": "EV Charger automation is disabled."})

            assignment_user = charger.user if (request.user.is_staff or request.user.is_superuser) else request.user
            assignment = EVChargerAssignment.objects.filter(
                user=assignment_user, charger=charger, electricity_price=electricity_price
            ).first()

            if assignment:
                assignment.delete()
                action, assigned = "unassigned", False
            else:
                EVChargerAssignment.objects.create(
                    user=assignment_user, charger=charger,
                    electricity_price=electricity_price, assignment_type="manual",
                )
                action, assigned = "assigned", True

            message = f"EV Charger {charger.familiar_name} {action} at {TimeUtils.format_datetime_with_tz(electricity_price.start_time, request.user, '%H:%M')}"
            return JsonResponse({"success": True, "action": action, "assigned": assigned, "message": message})

        # --- Shelly device ---
        try:
            if request.user.is_staff or request.user.is_superuser:
                device = ShellyDevice.objects.get(device_id=device_id)
            else:
                device = ShellyDevice.objects.get(device_id=device_id, user=request.user)
        except ShellyDevice.DoesNotExist:
            return JsonResponse({"success": False, "error": "Device not found or access denied"})

        if device.status != 1:
            return JsonResponse({"success": False, "error": "Device automation is disabled. Enable it first to manage assignments."})

        assignment_user = device.user if (request.user.is_staff or request.user.is_superuser) else request.user

        assignment = DeviceAssignment.objects.filter(
            user=assignment_user, device=device, electricity_price=electricity_price
        ).first()

        if assignment:
            assignment.delete()
            action, assigned = "unassigned", False
        else:
            DeviceAssignment.objects.create(
                user=assignment_user, device=device,
                electricity_price=electricity_price, assignment_type="manual",
            )
            action, assigned = "assigned", True

        if (request.user.is_staff or request.user.is_superuser) and assignment_user != request.user:
            message = f"Device {device.familiar_name} {action} for {assignment_user.username} at {TimeUtils.format_datetime_with_tz(electricity_price.start_time, request.user, '%H:%M')}"
        else:
            message = f"Device {device.familiar_name} {action} for {TimeUtils.format_datetime_with_tz(electricity_price.start_time, request.user, '%H:%M')}"

        return JsonResponse({"success": True, "action": action, "assigned": assigned, "message": message})
        
    except json.JSONDecodeError:
        return JsonResponse({"success": False, "error": "Invalid JSON data"})
    except Exception as e:
        return JsonResponse({"success": False, "error": str(e)})


@csrf_exempt
@require_http_methods(["POST"])
def toggle_device_status(request):
    """
    Toggle device automation status (enabled/disabled).
    When disabled, the device will not respond to any automation features.
    """
    if not request.user.is_authenticated:
        return JsonResponse({"success": False, "error": "User not authenticated"})
    
    try:
        data = json.loads(request.body)
        device_id = data.get('device_id')
        enabled = data.get('enabled', False)
        
        if not device_id:
            return JsonResponse({"success": False, "error": "Missing device_id"})

        is_ev = str(device_id).startswith("ev_")
        action = "enabled" if enabled else "disabled"

        if is_ev:
            ev_id = int(str(device_id)[3:])
            try:
                charger = EVCharger.objects.get(id=ev_id, user=request.user)
            except EVCharger.DoesNotExist:
                return JsonResponse({"success": False, "error": "EV Charger not found or access denied"})
            charger.status = 1 if enabled else 0
            charger.save()
            return JsonResponse({
                "success": True, "enabled": enabled,
                "message": f"EV Charger {charger.familiar_name} automation {action}",
            })

        try:
            device = ShellyDevice.objects.get(device_id=device_id, user=request.user)
        except ShellyDevice.DoesNotExist:
            return JsonResponse({"success": False, "error": "Device not found or access denied"})

        device.status = 1 if enabled else 0
        device.save()

        return JsonResponse({
            "success": True,
            "enabled": enabled,
            "message": f"Device {device.familiar_name} automation {action}",
        })
        
    except json.JSONDecodeError:
        return JsonResponse({"success": False, "error": "Invalid JSON data"})
    except Exception as e:
        return JsonResponse({"success": False, "error": str(e)})
