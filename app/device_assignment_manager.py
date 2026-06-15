from datetime import timedelta
from django.utils import timezone
from .models import DeviceAssignment, EVChargerAssignment, ElectricityPrice
import datetime
from app.utils.time_utils import TimeUtils
from django.utils.timezone import now

class DeviceAssignmentManager:
    """
    Manages device assignments with user context.
    """

    def __init__(self, user):
        """
        Initialize with a logged-in user.
        """
        self.user = user

    def log_assignment(self, device, electricity_price, assignment_type="cheapest"):
        """
        Logs a device assignment for the current user.
        Prevents duplicate assignments for the same price period.
        If the period is already assigned with a lower-priority type, upgrades to forced_min.
        """
        existing = DeviceAssignment.objects.filter(
            user=self.user,
            device=device,
            electricity_price=electricity_price,
        ).first()

        if not existing:
            return DeviceAssignment.objects.create(
                user=self.user,
                device=device,
                electricity_price=electricity_price,
                assignment_type=assignment_type,
            )
        if assignment_type == "forced_min" and existing.assignment_type != "forced_min":
            existing.assignment_type = "forced_min"
            existing.save(update_fields=["assignment_type"])
        return existing

    def get_device_cheapest_hours(self, devices):
        """
        Fetch assigned cheapest hours for each device.
        Only considers assignments for the next 24 hours.
        """
        now = TimeUtils.now_utc()
        next_24h = now + timedelta(hours=24)

        # Preload active assignments for all devices (excluded removed_overheat)
        assignments = DeviceAssignment.objects.filter(
            user=self.user,
            device__in=devices,
            electricity_price__start_time__gte=now,
            electricity_price__start_time__lt=next_24h,
        ).exclude(assignment_type="removed_overheat").select_related("electricity_price")

        # Create a dictionary mapping device_id to assigned hours
        device_assignments = {}
        for assignment in assignments:
            if assignment.device.device_id not in device_assignments:
                device_assignments[assignment.device.device_id] = []
            device_assignments[assignment.device.device_id].append(assignment.electricity_price.start_time.strftime("%H:%M"))

        # Assign cheapest hours to each device
        for device in devices:
            device.cheapest_hours = device_assignments.get(device.device_id, [])

        return devices

    def get_assignments_next_24h(self, device):
        """Fetches all assignments for the given device in the next 24 hours."""
        current_time = now()
        end_time = current_time + timedelta(hours=24)

        return DeviceAssignment.objects.filter(
            user=self.user,
            device=device,
            electricity_price__start_time__range=(current_time, end_time)
        )


class EVChargerAssignmentManager:
    def __init__(self, user):
        self.user = user

    def get_charger_cheapest_hours(self, chargers):
        """Attach cheapest_hours list to each EVCharger in the next 24 h."""
        now_utc = TimeUtils.now_utc()
        next_24h = now_utc + timedelta(hours=24)
        assignments = EVChargerAssignment.objects.filter(
            user=self.user,
            charger__in=chargers,
            electricity_price__start_time__gte=now_utc,
            electricity_price__start_time__lt=next_24h,
        ).select_related("electricity_price")

        charger_hours = {}
        for a in assignments:
            charger_hours.setdefault(a.charger_id, []).append(
                a.electricity_price.start_time.strftime("%H:%M")
            )

        for charger in chargers:
            charger.cheapest_hours = charger_hours.get(charger.id, [])

        return chargers

    def get_assignments_next_24h(self, charger):
        current_time = now()
        end_time = current_time + timedelta(hours=24)
        return EVChargerAssignment.objects.filter(
            user=self.user,
            charger=charger,
            electricity_price__start_time__range=(current_time, end_time),
        )
