import requests
import time
from datetime import datetime, timedelta
from django.urls import reverse
from django.utils.timezone import now
from django.conf import settings
from django.test import RequestFactory
from django.http import JsonResponse
from app.models import (
    ShellyDevice,
    ShellyTemperature,
    TemperatureReading,
    ElectricityPrice,
    DeviceLog,
    DeviceAssignment,
    EVCharger,
    EVChargerAssignment,
    EVChargerEnergyLog,
)
from app.shelly_views import toggle_device_output, fetch_device_status
from app.services.shelly_service import (
    ShellyService,
    ShellyTemperatureService,
    extract_temperature_c,
)
from app.thermostat_manager import ThermostatAssignmentManager
from app.price_views import call_fetch_prices, get_cheapest_hours
from .logger import log_device_event, log_ev_event
from app.utils.time_utils import TimeUtils
from app.utils.db_utils import with_db_retries
import pytz
from typing import Optional


class DeviceController:
    """Controller for scheduled device and price operations."""

    @staticmethod
    def fetch_electricity_prices() -> None:
        """Calls the Django view to fetch electricity prices internally."""
        try:
            response = call_fetch_prices(None)
            if isinstance(response, JsonResponse) and response.status_code != 200:
                log_device_event(
                    None,
                    f"Schedule failed to fetch electricity prices. Response: {response.content}",
                    "ERROR",
                )
        except Exception as e:
            log_device_event(None, f"Error calling fetch-prices: {e}", "ERROR")

    @staticmethod
    @with_db_retries(max_attempts=3, delay=1)
    def control_shelly_devices() -> None:
        """Loops through all Shelly devices and toggles them based on pre-assigned cheapest 15-minute periods."""
        try:
            current_time = TimeUtils.now_utc()
            
            # Find the current 15-minute period
            current_minutes = current_time.minute
            period_start_minutes = (current_minutes // 15) * 15
            
            # Calculate start and end of current period
            start_time = current_time.replace(
                minute=period_start_minutes,
                second=0,
                microsecond=0
            )
            end_time = start_time + timedelta(minutes=14, seconds=59)
            
            # Log period boundaries for debugging
            log_device_event(
                None,
                f"Checking device states for period {start_time.strftime('%Y-%m-%d %H:%M')} to {end_time.strftime('%Y-%m-%d %H:%M')}",
                "INFO"
            )
            
            # Get active price period and any assignments for this period
            active_prices = ElectricityPrice.objects.filter(
                start_time__range=(start_time, end_time)
            )
            active_price_ids = list(active_prices.values_list("id", flat=True))
            
            # Only process devices with automation enabled (status = 1)
            devices = ShellyDevice.objects.filter(status=1)
            
            if not devices.exists():
                log_device_event(None, "No devices with automation enabled found", "INFO")
                return
            
            # Group devices by server+token combination for optimal parallel processing
            from collections import defaultdict
            import hashlib
            
            device_groups = defaultdict(list)
            for device in devices:
                # Create a key for server+token combination
                key_hash = hashlib.md5(device.shelly_api_key.encode()).hexdigest()[:8]
                group_key = f"{device.shelly_server}:{key_hash}"
                device_groups[group_key].append(device)
            
            log_device_event(
                None,
                f"Processing {len(devices)} devices in {len(device_groups)} parallel groups by server+token combination",
                "INFO"
            )
            
            # Process each group with internal staggering, but groups can run in parallel
            from concurrent.futures import ThreadPoolExecutor, as_completed
            
            def process_device_group(group_info):
                group_key, device_list = group_info
                log_device_event(
                    None,
                    f"Processing group {group_key} with {len(device_list)} devices",
                    "INFO"
                )
                
                for index, device in enumerate(device_list):
                    try:
                        # Small stagger within the group (only 1 second between devices in same group)
                        if index > 0:
                            time.sleep(1)
                        
                        DeviceController._process_single_device(device, active_price_ids, start_time)
                        
                    except Exception as e:
                        log_device_event(
                            device,
                            f"Error processing device in group: {str(e)}",
                            "ERROR"
                        )
            
            # Fetch fresh temperatures BEFORE device processing so overheat checks use current data
            DeviceController.fetch_thermostat_temperatures()

            # Execute groups in parallel (max 5 concurrent groups to be conservative)
            with ThreadPoolExecutor(max_workers=min(5, len(device_groups))) as executor:
                futures = [executor.submit(process_device_group, group_info) for group_info in device_groups.items()]

                # Wait for all groups to complete
                for future in as_completed(futures):
                    try:
                        future.result()
                    except Exception as e:
                        log_device_event(
                            None,
                            f"Error in device group processing: {str(e)}",
                            "ERROR"
                        )

            ThermostatAssignmentManager.apply_next_period_assignments()
                        
        except Exception as e:
            log_device_event(None, f"Error controlling Shelly devices: {e}", "ERROR")

    @staticmethod
    def fetch_thermostat_temperatures() -> None:
        """Fetch temperature data for all thermostat devices."""
        try:
            temperature_devices = ShellyTemperature.objects.all()
            if not temperature_devices.exists():
                return

            for temperature_device in temperature_devices:
                shelly_service = ShellyTemperatureService(temperature_device.device_id)
                status = shelly_service.get_device_status()
                if "error" in status:
                    log_device_event(
                        None,
                        f"Temperature fetch error for {temperature_device.familiar_name}: {status['error']}",
                        "ERROR",
                    )
                    continue

                temperature_c = extract_temperature_c(status)
                if temperature_c is None:
                    log_device_event(
                        None,
                        f"Temperature not found for {temperature_device.familiar_name}",
                        "WARN",
                    )
                    continue

                temperature_device.current_temperature = temperature_c
                temperature_device.temperature_updated_at = TimeUtils.now_utc()
                temperature_device.save(
                    update_fields=[
                        "current_temperature",
                        "temperature_updated_at",
                        "updated_at",
                    ]
                )
                TemperatureReading.objects.create(
                    thermostat=temperature_device,
                    temperature_c=temperature_c,
                    recorded_at=temperature_device.temperature_updated_at,
                )

                log_device_event(
                    None,
                    f"Temperature for {temperature_device.familiar_name}: {temperature_c:.2f} C",
                    "INFO",
                )

        except Exception as e:
            log_device_event(None, f"Error fetching thermostat temperatures: {e}", "ERROR")

    @staticmethod
    def _process_single_device(device: ShellyDevice, active_price_ids: list, start_time) -> None:
        """Process a single device - extracted for use in parallel processing."""
        try:
            # Check if this 15-minute period is actively assigned (ignore removed assignments)
            assigned = DeviceAssignment.objects.filter(
                device=device,
                electricity_price_id__in=active_price_ids,
            ).exclude(assignment_type__in=["removed_overheat", "removed_headroom"]).exists()

            # Thermostat temperature overrides
            if device.thermostat_device_id:
                thermostat = device.thermostat_device
                if thermostat and thermostat.temperature_updated_at:
                    now_utc = TimeUtils.now_utc()
                    if (now_utc - thermostat.temperature_updated_at) <= timedelta(minutes=15):
                        from decimal import Decimal
                        min_trigger = thermostat.get_effective_min_temperature() - Decimal("0.5")
                        max_trigger = thermostat.max_temperature + Decimal("0.5")
                        effective_headroom = thermostat.get_effective_headroom()
                        headroom_trigger = thermostat.max_temperature - effective_headroom

                        if not assigned and thermostat.current_temperature < min_trigger:
                            # Min temperature override: force ON regardless of price or schedule.
                            # Also re-activates any period previously marked removed.
                            price_obj = ElectricityPrice.objects.filter(id__in=active_price_ids).first()
                            if price_obj:
                                obj, created = DeviceAssignment.objects.get_or_create(
                                    user=device.user,
                                    device=device,
                                    electricity_price=price_obj,
                                    defaults={"assignment_type": "forced_min"},
                                )
                                if not created and obj.assignment_type in ("removed_overheat", "removed_headroom"):
                                    obj.assignment_type = "forced_min"
                                    obj.save(update_fields=["assignment_type"])
                                assigned = True
                                log_device_event(
                                    device,
                                    f"Thermostat below min ({thermostat.current_temperature} < {min_trigger}). "
                                    f"Forcing ON for period {start_time.strftime('%Y-%m-%d %H:%M')} UTC.",
                                    "INFO",
                                )

                        elif thermostat.current_temperature > max_trigger:
                            # Force device off for this period. Future-assignment removal is
                            # handled one-by-one by ThermostatAssignmentManager.apply_next_period_assignments.
                            if assigned:
                                log_device_event(
                                    device,
                                    f"Thermostat above max ({thermostat.current_temperature} > {max_trigger}). "
                                    f"Forcing OFF for period {start_time.strftime('%Y-%m-%d %H:%M')} UTC.",
                                    "INFO",
                                )
                            assigned = False

                        elif effective_headroom > 0 and thermostat.current_temperature > headroom_trigger:
                            # Headroom zone: apartment warm enough — don't run this period.
                            if assigned:
                                log_device_event(
                                    device,
                                    f"Thermostat in headroom zone ({thermostat.current_temperature} > {headroom_trigger}). "
                                    f"Not running for period {start_time.strftime('%Y-%m-%d %H:%M')} UTC.",
                                    "INFO",
                                )
                            assigned = False
            
            # Get initial device state (ONLY ONE STATUS CHECK)
            shelly_service = ShellyService(device.device_id)
            device_status = shelly_service.get_device_status()
            
            if "error" in device_status:
                log_device_event(
                    device,
                    f"Error fetching initial status: {device_status['error']}",
                    "ERROR"
                )
                return
            
            is_running = (
                device_status.get("data", {})
                .get("device_status", {})
                .get("switch:0", {})
                .get("output", False)
            )
            
            # Log detailed state information
            log_device_event(
                device,
                f"Period {start_time.strftime('%Y-%m-%d %H:%M')} - "
                f"Assignment: {assigned}, Current State: {'running' if is_running else 'stopped'}",
                "INFO"
            )
            
            # Determine if action is needed
            action_needed = (assigned and not is_running) or (not assigned and is_running)
            desired_state = "on" if assigned else "off"
            
            if action_needed:
                log_device_event(
                    device,
                    f"State change needed. Setting to {desired_state.upper()}",
                    "INFO"
                )
                # Call toggle with the device state we just fetched (pass it to avoid re-fetching)
                DeviceController.toggle_shelly_device_with_state(device, desired_state, is_running)
            else:
                log_device_event(
                    device,
                    f"No action needed. Current state matches desired state ({desired_state.upper()})",
                    "INFO"
                )
                
        except Exception as e:
            log_device_event(
                device,
                f"Error processing device: {str(e)}",
                "ERROR"
            )

    @staticmethod
    def toggle_shelly_device_with_state(device: ShellyDevice, action: str, current_is_running: bool) -> None:
        """Helper function to toggle a Shelly device ON or OFF when current state is already known."""
        # Only make the toggle call if the device is not already in the desired state
        if (action == "off" and current_is_running) or (action == "on" and not current_is_running):
            log_device_event(
                device,
                f"Device state needs change: currently {'ON' if current_is_running else 'OFF'}, setting to {action.upper()}",
                "INFO"
            )
            
            shelly_service = ShellyService(device.device_id)
            result = shelly_service.set_device_output(state=action)
            
            if "error" in result:
                log_device_event(
                    device,
                    f"Failed to turn {action.upper()} device: {result['error']}",
                    "ERROR",
                )
            elif result.get("status") == "blocked":
                log_device_event(
                    device,
                    f"Device toggle BLOCKED by SHELLY_STOP_REST_DEBUG: {result.get('message', 'No message')}",
                    "INFO",
                )
            else:
                log_device_event(device, f"Device turned {action.upper()}", "INFO")
        else:
            log_device_event(
                device,
                f"Device already in desired state ({action.upper()}), no action needed",
                "INFO"
            )

    @staticmethod
    @with_db_retries(max_attempts=3, delay=1)
    def control_ev_chargers() -> None:
        """Check each enabled EV charger against its price assignments and start/stop accordingly."""
        from app.services.ev_charger_factory import get_ev_charger_service

        try:
            current_time = TimeUtils.now_utc()
            period_start_minutes = (current_time.minute // 15) * 15
            start_time = current_time.replace(minute=period_start_minutes, second=0, microsecond=0)
            end_time = start_time + timedelta(minutes=14, seconds=59)

            active_prices = ElectricityPrice.objects.filter(start_time__range=(start_time, end_time))
            active_price_ids = list(active_prices.values_list("id", flat=True))

            chargers = EVCharger.objects.filter(status=1)
            if not chargers.exists():
                return

            log_device_event(
                None,
                f"EV charger check for period {start_time.strftime('%Y-%m-%d %H:%M')} — "
                f"{chargers.count()} charger(s)",
                "INFO",
            )

            for charger in chargers:
                try:
                    DeviceController._process_single_ev_charger(
                        charger, active_price_ids, start_time
                    )
                except Exception as e:
                    log_ev_event(charger, f"Error processing EV charger: {e}", "ERROR")

        except Exception as e:
            log_device_event(None, f"Error in control_ev_chargers: {e}", "ERROR")

    @staticmethod
    def _process_single_ev_charger(charger: EVCharger, active_price_ids: list, start_time) -> None:
        """Control one EV charger for the current 15-minute period."""
        from app.services.ev_charger_factory import get_ev_charger_service

        assigned = EVChargerAssignment.objects.filter(
            charger=charger,
            electricity_price_id__in=active_price_ids,
        ).exists()

        service = get_ev_charger_service(charger)
        status = service.get_status()

        if status is None:
            log_ev_event(charger, "Failed to fetch status from charger API", "ERROR")
            return

        previous_work_state = charger.work_state

        # Update cached state on the model
        charger.is_charging = status.is_charging
        charger.work_state = status.work_state
        charger.connection_state = status.connection_state
        charger.power_w = status.power_w
        charger.temp_c = status.temp_c
        charger.session_energy_kwh = round(status.session_energy_kwh, 3)
        charger.total_energy_kwh = round(status.total_energy_kwh, 3)
        charger.last_contact = TimeUtils.now_utc()

        # Detect session start
        if status.work_state == "charger_charging" and previous_work_state != "charger_charging":
            charger.session_started_at = TimeUtils.now_utc()
            log_ev_event(charger, "Charging session started", "INFO")

        charger.save(update_fields=[
            "is_charging", "work_state", "connection_state", "power_w",
            "temp_c", "session_energy_kwh", "total_energy_kwh", "last_contact",
            "session_started_at", "updated_at",
        ])

        # Log energy reading
        EVChargerEnergyLog.objects.create(
            charger=charger,
            session_energy_kwh=charger.session_energy_kwh,
            total_energy_kwh=charger.total_energy_kwh,
            is_charging=status.is_charging,
            power_w=status.power_w,
        )

        # On session end: distribute charge_energy_once across session assignments weighted by current
        if previous_work_state == "charger_charging" and status.work_state == "charger_end":
            try:
                session_kwh = float(status.session_energy_kwh)
                if session_kwh > 0 and charger.session_started_at:
                    session_assignments = list(
                        EVChargerAssignment.objects.filter(
                            charger=charger,
                            electricity_price__start_time__gte=charger.session_started_at,
                            charge_current_a__isnull=False,
                        )
                    )
                    if session_assignments:
                        total_current = sum(a.charge_current_a for a in session_assignments)
                        for a in session_assignments:
                            weight = a.charge_current_a / total_current
                            a.energy_kwh = round(session_kwh * weight, 3)
                            a.save(update_fields=["energy_kwh"])
                        log_ev_event(
                            charger,
                            f"Session ended — {session_kwh:.3f} kWh split across "
                            f"{len(session_assignments)} slot(s)",
                            "INFO",
                        )
            except Exception as e:
                log_ev_event(charger, f"Error distributing session energy: {e}", "ERROR")

        # Determine if other Shelly devices belonging to this user are running this period.
        # If so, use the reduced current to protect the fuse.
        other_devices_running = DeviceAssignment.objects.filter(
            user=charger.user,
            electricity_price_id__in=active_price_ids,
        ).exclude(assignment_type__in=["removed_overheat", "removed_headroom"]).exists()

        target_current = (
            charger.charge_current_reduced_a if other_devices_running
            else charger.charge_current_a
        )
        current_reason = "reduced (other devices running)" if other_devices_running else "normal"

        log_ev_event(
            charger,
            f"Period {start_time.strftime('%Y-%m-%d %H:%M')} — "
            f"assigned={assigned}, charging={status.is_charging}, "
            f"target={target_current} A ({current_reason}), "
            f"state={status.work_state}, session={charger.session_energy_kwh} kWh",
            "INFO",
        )

        # States where a car is physically connected and ready to accept a charge command.
        # Do not attempt start_charging if the car is not plugged in.
        CAR_CONNECTED_STATES = {"charger_insert", "charger_charging", "charger_pause", "charger_end"}
        car_connected = status.work_state in CAR_CONNECTED_STATES

        # Record charge current on the active assignment so session-end split is weighted correctly
        if status.is_charging:
            EVChargerAssignment.objects.filter(
                charger=charger,
                electricity_price_id__in=active_price_ids,
            ).update(charge_current_a=target_current)

        if assigned and not status.is_charging:
            if not car_connected:
                log_ev_event(
                    charger,
                    f"Skipping start_charging — car not connected (work_state={status.work_state!r})",
                    "INFO",
                )
            else:
                ok = service.start_charging(target_current)
                log_ev_event(
                    charger,
                    f"Started charging at {target_current} A ({current_reason}) — {'OK' if ok else 'FAILED'}",
                    "INFO" if ok else "ERROR",
                )
        elif assigned and status.is_charging:
            # Already charging — adjust current every cycle so it reacts to devices turning on/off
            ok = service.set_charge_current(target_current)
            log_ev_event(
                charger,
                f"Current set to {target_current} A ({current_reason}) — {'OK' if ok else 'FAILED'}",
                "INFO" if ok else "ERROR",
            )
        elif not assigned and status.is_charging:
            ok = service.stop_charging()
            log_ev_event(
                charger,
                f"Stopped charging — {'OK' if ok else 'FAILED'}",
                "INFO" if ok else "ERROR",
            )

    @staticmethod
    def toggle_shelly_device(device: ShellyDevice, action: str) -> None:
        """Helper function to toggle a Shelly device ON or OFF."""
        # Get the last logged state to minimize API calls
        last_log = DeviceLog.objects.filter(device=device).order_by('-created_at').first()
        last_action = None
        if last_log and (datetime.now(pytz.UTC) - last_log.created_at).total_seconds() < 120:  # Trust state for 2 minutes
            message = last_log.message.lower()
            if "turned on" in message:
                last_action = "on"
            elif "turned off" in message:
                last_action = "off"
                
        # If the last action matches what we want to do, skip the API call
        if last_action == action:
            log_device_event(
                device,
                f"Skipping {action} command - device already in desired state (cached)",
                "INFO"
            )
            return

        # Only proceed with API calls if we really need to change state
        shelly_service = ShellyService(device.device_id)
        
        # First get current status to verify we need to make a change
        device_status = shelly_service.get_device_status()
        
        if "error" in device_status:
            log_device_event(
                device, f"Error fetching status for toggle: {device_status['error']}", "ERROR"
            )
            return
            
        is_running = (
            device_status.get("data", {})
            .get("device_status", {})
            .get("switch:0", {})
            .get("output", False)
        )
        
        # Use the new method to avoid duplicate code
        DeviceController.toggle_shelly_device_with_state(device, action, is_running)
