from collections import defaultdict
from datetime import timedelta

from django.db import transaction

from app.models import DeviceAssignment, ElectricityPrice, ShellyDevice
from app.price_views import get_cheapest_hours
from app.utils.time_utils import TimeUtils
from app.logger import log_device_event


class DeviceOverrideManager:
    """Create and maintain user-selected Shelly device override assignments."""

    VALID_DURATIONS_HOURS = {12, 24, 48, 120}
    REMOVED_TYPES = {"removed_overheat", "removed_headroom"}

    @classmethod
    @transaction.atomic
    def start_scheduled_override(cls, device: ShellyDevice, duration_hours: int):
        if duration_hours not in cls.VALID_DURATIONS_HOURS:
            raise ValueError("Duration must be 12, 24, 48, or 120 hours")

        now_utc = TimeUtils.now_utc()
        override_until = now_utc + timedelta(hours=duration_hours)
        device.scheduled_override_until = override_until
        device.force_on = False
        device.force_off = False
        device.status = 1
        device.save(
            update_fields=[
                "scheduled_override_until",
                "force_on",
                "force_off",
                "status",
                "updated_at",
            ]
        )

        DeviceAssignment.objects.filter(
            user=device.user,
            device=device,
            assignment_type="force_on",
            electricity_price__start_time__gte=now_utc,
        ).delete()
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
            f"Scheduled override enabled for {duration_hours} hours (until {override_until.isoformat()}).",
            "INFO",
        )
        return override_until

    @classmethod
    def fill_scheduled_override(cls, device: ShellyDevice, now_utc=None):
        now_utc = now_utc or TimeUtils.now_utc()
        override_until = device.scheduled_override_until
        if not override_until:
            return 0
        if override_until <= now_utc:
            device.scheduled_override_until = None
            device.save(update_fields=["scheduled_override_until", "updated_at"])
            return 0

        prices = list(
            ElectricityPrice.objects.filter(
                start_time__gte=now_utc,
                start_time__lt=override_until,
            )
            .order_by("start_time")
            .values("id", "start_time", "price_kwh")
        )
        if not prices:
            return 0

        user_tz = TimeUtils.get_user_timezone(device.user)
        prices_by_day = defaultdict(list)
        for price in prices:
            local_day = TimeUtils.to_user_timezone(price["start_time"], device.user).date()
            prices_by_day[local_day].append(price)

        created_count = 0
        for day_prices in prices_by_day.values():
            tagged_slots = get_cheapest_hours(
                day_prices,
                device.day_transfer_price,
                device.night_transfer_price,
                device.run_hours_per_day,
                device.auto_assign_price_threshold,
                local_tz=user_tz,
                return_tagged=True,
            )
            price_by_minute = {
                TimeUtils.to_utc(price["start_time"]).replace(second=0, microsecond=0): price
                for price in day_prices
            }
            for slot, _assignment_type in tagged_slots:
                price = price_by_minute.get(
                    TimeUtils.to_utc(slot).replace(second=0, microsecond=0)
                )
                if not price:
                    continue
                assignment, created = DeviceAssignment.objects.get_or_create(
                    user=device.user,
                    device=device,
                    electricity_price_id=price["id"],
                    defaults={"assignment_type": "scheduled_override"},
                )
                if not created and assignment.assignment_type in cls.REMOVED_TYPES:
                    assignment.assignment_type = "scheduled_override"
                    assignment.save(update_fields=["assignment_type"])
                created_count += int(created)
        return created_count

    @classmethod
    @transaction.atomic
    def set_force_on(cls, device: ShellyDevice, enabled: bool):
        now_utc = TimeUtils.now_utc()
        device.force_on = enabled
        if enabled:
            device.force_off = False
        device.scheduled_override_until = None
        if enabled:
            device.status = 1
        device.save(
            update_fields=[
                "force_on",
                "force_off",
                "scheduled_override_until",
                "status",
                "updated_at",
            ]
        )

        DeviceAssignment.objects.filter(
            user=device.user,
            device=device,
            assignment_type="scheduled_override",
            electricity_price__start_time__gte=now_utc,
        ).delete()
        if enabled:
            created_count = cls.queue_force_on_periods(device, now_utc=now_utc)
            log_device_event(device, "Force Device On enabled.", "INFO")
            return created_count

        DeviceAssignment.objects.filter(
            user=device.user,
            device=device,
            assignment_type="force_on",
            electricity_price__start_time__gte=now_utc - timedelta(minutes=15),
        ).delete()
        log_device_event(device, "Force Device On disabled.", "INFO")
        return 0

    @classmethod
    @transaction.atomic
    def set_force_off(cls, device: ShellyDevice, enabled: bool):
        now_utc = TimeUtils.now_utc()
        device.force_off = enabled
        if enabled:
            device.force_on = False
            device.scheduled_override_until = None
            device.status = 1
        device.save(
            update_fields=[
                "force_off",
                "force_on",
                "scheduled_override_until",
                "status",
                "updated_at",
            ]
        )

        if enabled:
            # Force On entries exist only to hold the relay on, so remove them
            # before applying the rolling inverse operation.
            DeviceAssignment.objects.filter(
                user=device.user,
                device=device,
                assignment_type="force_on",
                electricity_price__start_time__gte=now_utc - timedelta(minutes=15),
            ).delete()
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
        period_start = now_utc.replace(
            minute=(now_utc.minute // 15) * 15,
            second=0,
            microsecond=0,
        )
        # Exact inverse of Force On: remove the active assignment and exactly
        # one upcoming period before it starts, then advance on the next pass.
        price_ids = list(
            ElectricityPrice.objects.filter(start_time__gte=period_start)
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
    def queue_force_on_periods(cls, device: ShellyDevice, now_utc=None):
        if not device.force_on:
            return 0

        now_utc = now_utc or TimeUtils.now_utc()
        period_start = now_utc.replace(
            minute=(now_utc.minute // 15) * 15,
            second=0,
            microsecond=0,
        )
        # Queue the active slot and exactly one upcoming slot. On every scheduler
        # pass the rolling upcoming slot is added before it starts.
        prices = ElectricityPrice.objects.filter(start_time__gte=period_start).order_by(
            "start_time"
        )[:2]
        created_count = 0
        for price in prices:
            assignment, created = DeviceAssignment.objects.get_or_create(
                user=device.user,
                device=device,
                electricity_price=price,
                defaults={"assignment_type": "force_on"},
            )
            if not created and assignment.assignment_type in cls.REMOVED_TYPES:
                assignment.assignment_type = "force_on"
                assignment.save(update_fields=["assignment_type"])
            created_count += int(created)
        return created_count

    @classmethod
    def maintain_all(cls, now_utc=None):
        now_utc = now_utc or TimeUtils.now_utc()
        for device in ShellyDevice.objects.filter(status=1).select_related("user"):
            if device.force_on:
                cls.queue_force_on_periods(device, now_utc=now_utc)
            elif device.force_off:
                cls.remove_force_off_periods(device, now_utc=now_utc)
            elif device.scheduled_override_until:
                cls.fill_scheduled_override(device, now_utc=now_utc)
