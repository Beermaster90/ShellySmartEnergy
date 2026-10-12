from datetime import timedelta

from django.db import transaction

from app.models import DeviceAssignment, ElectricityPrice, ShellyDevice
from app.utils.time_utils import TimeUtils
from app.logger import log_device_event


class DeviceOverrideManager:
    """User-selected Shelly device overrides.

    - Timed override ("on for N hours"): the device runs for the whole window,
      ignoring price, run hours and every thermostat rule.
    - Force Off: the device stays off until released.
    """

    VALID_DURATIONS_HOURS = {1, 5, 12, 24, 48, 120}
    REMOVED_TYPES = {"removed_overheat", "removed_headroom"}

    @staticmethod
    def _period_start(now_utc):
        return now_utc.replace(
            minute=(now_utc.minute // 15) * 15,
            second=0,
            microsecond=0,
        )

    @classmethod
    @transaction.atomic
    def start_scheduled_override(cls, device: ShellyDevice, duration_hours: int):
        if duration_hours not in cls.VALID_DURATIONS_HOURS:
            raise ValueError("Duration must be 1, 5, 12, 24, 48, or 120 hours")

        now_utc = TimeUtils.now_utc()
        override_until = now_utc + timedelta(hours=duration_hours)
        device.scheduled_override_until = override_until
        device.force_off = False
        device.status = 1
        device.save(
            update_fields=[
                "scheduled_override_until",
                "force_off",
                "status",
                "updated_at",
            ]
        )

        # Reapplying a shorter duration must not leave assignments from an older,
        # longer override active beyond the newly selected horizon.
        DeviceAssignment.objects.filter(
            user=device.user,
            device=device,
            assignment_type="scheduled_override",
            electricity_price__start_time__gte=override_until,
        ).delete()
        cls.fill_scheduled_override(device, now_utc=now_utc)
        log_device_event(
            device,
            f"Timed override: device ON for {duration_hours} hours (until {override_until.isoformat()}).",
            "INFO",
        )
        return override_until

    @classmethod
    @transaction.atomic
    def cancel_scheduled_override(cls, device: ShellyDevice):
        """End a timed override early and return to the normal schedule."""
        now_utc = TimeUtils.now_utc()
        device.scheduled_override_until = None
        device.save(update_fields=["scheduled_override_until", "updated_at"])
        DeviceAssignment.objects.filter(
            user=device.user,
            device=device,
            assignment_type="scheduled_override",
            electricity_price__start_time__gte=cls._period_start(now_utc),
        ).delete()
        log_device_event(device, "Timed override cancelled.", "INFO")

    @classmethod
    def fill_scheduled_override(cls, device: ShellyDevice, now_utc=None):
        """Book every 15-minute period from the current one until the override ends.

        The bookings make the override visible on the dashboard; the controller
        itself turns the device on from ``scheduled_override_until`` alone, so a
        period without price data still runs. Called on every scheduler pass so
        periods whose prices arrive later (day-ahead prices only reach ~24 h
        ahead) are booked as they appear. Existing "cheapest" bookings are left
        alone so they survive cancelling the override.
        """
        now_utc = now_utc or TimeUtils.now_utc()
        override_until = device.scheduled_override_until
        if not override_until:
            return 0
        if override_until <= now_utc:
            device.scheduled_override_until = None
            device.save(update_fields=["scheduled_override_until", "updated_at"])
            log_device_event(device, "Timed override ended.", "INFO")
            return 0

        price_ids = list(
            ElectricityPrice.objects.filter(
                start_time__gte=cls._period_start(now_utc),
                start_time__lt=override_until,
            ).values_list("id", flat=True)
        )
        existing_ids = set(
            DeviceAssignment.objects.filter(
                device=device, electricity_price_id__in=price_ids
            ).values_list("electricity_price_id", flat=True)
        )
        DeviceAssignment.objects.bulk_create([
            DeviceAssignment(
                user=device.user,
                device=device,
                electricity_price_id=price_id,
                assignment_type="scheduled_override",
            )
            for price_id in price_ids
            if price_id not in existing_ids
        ])
        # The override beats the thermostat, so earlier thermostat removals in
        # the window are switched back on.
        DeviceAssignment.objects.filter(
            device=device,
            electricity_price_id__in=price_ids,
            assignment_type__in=cls.REMOVED_TYPES,
        ).update(assignment_type="scheduled_override")
        return len(price_ids) - len(existing_ids)

    @classmethod
    @transaction.atomic
    def set_force_off(cls, device: ShellyDevice, enabled: bool):
        now_utc = TimeUtils.now_utc()
        device.force_off = enabled
        if enabled:
            device.scheduled_override_until = None
            device.status = 1
            # Timed-override bookings exist only to hold the relay on.
            DeviceAssignment.objects.filter(
                user=device.user,
                device=device,
                assignment_type="scheduled_override",
                electricity_price__start_time__gte=now_utc - timedelta(minutes=15),
            ).delete()
        device.save(
            update_fields=[
                "force_off",
                "scheduled_override_until",
                "status",
                "updated_at",
            ]
        )

        if enabled:
            cls.remove_force_off_periods(device, now_utc=now_utc)

        log_device_event(
            device,
            f"Force Device Off {'enabled' if enabled else 'disabled'}.",
            "INFO",
        )

    @classmethod
    def remove_force_off_periods(cls, device: ShellyDevice, now_utc=None):
        if not device.force_off:
            return 0

        now_utc = now_utc or TimeUtils.now_utc()
        # Remove the active assignment and exactly one upcoming period before it
        # starts, then advance on the next pass.
        price_ids = list(
            ElectricityPrice.objects.filter(start_time__gte=cls._period_start(now_utc))
            .order_by("start_time")
            .values_list("id", flat=True)[:2]
        )
        deleted_count, _ = DeviceAssignment.objects.filter(
            user=device.user,
            device=device,
            electricity_price_id__in=price_ids,
        ).delete()
        return deleted_count

    @classmethod
    def maintain_all(cls, now_utc=None):
        now_utc = now_utc or TimeUtils.now_utc()
        for device in ShellyDevice.objects.filter(status=1).select_related("user"):
            if device.force_off:
                cls.remove_force_off_periods(device, now_utc=now_utc)
            elif device.scheduled_override_until:
                cls.fill_scheduled_override(device, now_utc=now_utc)
