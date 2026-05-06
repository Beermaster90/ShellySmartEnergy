from datetime import timedelta

from app.models import ShellyDevice, ElectricityPrice, DeviceAssignment
from app.price_views import get_cheapest_hours
from app.utils.time_utils import TimeUtils
from app.logger import log_device_event


class ThermostatAssignmentManager:
    """Manage thermostat-driven device assignments for upcoming 15-minute periods."""

    HYSTERESIS_C = 0.5

    @staticmethod
    def apply_next_period_assignments() -> None:
        now = TimeUtils.now_utc()
        period_start_minutes = (now.minute // 15) * 15
        current_start = now.replace(
            minute=period_start_minutes,
            second=0,
            microsecond=0,
        )
        next_start = current_start + timedelta(minutes=15)
        next_end = next_start + timedelta(minutes=15)

        next_price = (
            ElectricityPrice.objects.filter(
                start_time__gte=next_start,
                start_time__lt=next_end,
            )
            .order_by("start_time")
            .first()
        )
        if not next_price:
            log_device_event(
                None,
                f"No electricity price found for next period {next_start} to {next_end}",
                "WARN",
            )
            return

        devices = ShellyDevice.objects.filter(status=1, thermostat_device__isnull=False).select_related(
            "thermostat_device"
        )

        for device in devices:
            thermostat = device.thermostat_device
            if not thermostat or not thermostat.temperature_updated_at:
                continue

            if (now - thermostat.temperature_updated_at) > timedelta(minutes=15):
                continue

            current_temp = thermostat.current_temperature
            min_temp = thermostat.min_temperature
            max_temp = thermostat.max_temperature
            hysteresis = type(min_temp)(str(ThermostatAssignmentManager.HYSTERESIS_C))
            min_trigger = min_temp - hysteresis
            max_trigger = max_temp + hysteresis

            if current_temp < min_trigger:
                assignment, created = DeviceAssignment.objects.get_or_create(
                    user=device.user,
                    device=device,
                    electricity_price=next_price,
                )
                if created:
                    log_device_event(
                        device,
                        f"Thermostat below min ({current_temp} < {min_trigger}). Assigned next period {next_price.start_time} UTC.",
                        "INFO",
                    )
            elif current_temp > max_trigger:
                assignment_qs = DeviceAssignment.objects.filter(
                    user=device.user,
                    device=device,
                    electricity_price=next_price,
                )
                is_protected_minimum_period = (
                    assignment_qs.exists()
                    and ThermostatAssignmentManager._is_minimum_run_period(
                        device, next_price, now
                    )
                )
                if is_protected_minimum_period:
                    log_device_event(
                        device,
                        f"Thermostat above max ({current_temp} > {max_trigger}). Kept protected minimum-run period {next_price.start_time} UTC.",
                        "INFO",
                    )
                    continue

                deleted, _ = assignment_qs.delete()
                if deleted:
                    log_device_event(
                        device,
                        f"Thermostat above max ({current_temp} > {max_trigger}). Unassigned next period {next_price.start_time} UTC.",
                        "INFO",
                    )
            else:
                # Within bounds: no assignment change.
                continue

    @staticmethod
    def _is_minimum_run_period(device: ShellyDevice, price: ElectricityPrice, now) -> bool:
        minimum_hours = device.minimum_run_hours_per_day or 0
        if minimum_hours <= 0:
            return False

        prices = list(
            ElectricityPrice.objects.filter(
                start_time__gte=now,
                start_time__lt=now + timedelta(hours=24),
            )
            .order_by("start_time")
            .values("start_time", "price_kwh", "id")
        )
        if not prices:
            return False

        cheapest_periods = get_cheapest_hours(
            prices,
            device.day_transfer_price,
            device.night_transfer_price,
            minimum_hours,
            price_threshold=None,
        )
        protected_periods = {
            TimeUtils.to_utc(period).strftime("%Y-%m-%d %H:%M")
            for period in cheapest_periods
        }
        price_period = TimeUtils.to_utc(price.start_time).strftime("%Y-%m-%d %H:%M")
        return price_period in protected_periods
