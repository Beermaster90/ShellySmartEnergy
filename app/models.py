from django.db import models
from django.contrib.auth.models import User
from app.utils.time_utils import TimeUtils
from django.conf import settings
import pytz
from django.db.models.signals import post_save
from django.dispatch import receiver


class AppSetting(models.Model):
    key = models.CharField(max_length=128, unique=True)
    value = models.TextField()

    def __str__(self):
        return f"{self.key}: {self.value[:16]}..."

    class Meta:
        verbose_name = "App Setting"
        verbose_name_plural = "App Settings"


# Create your models here.


class ShellyDevice(models.Model):
    device_id = models.AutoField(primary_key=True)  # Auto-generated device ID
    familiar_name = models.CharField(max_length=255)  # User-defined familiar name
    shelly_api_key = models.CharField(max_length=255)  # API key for the device
    shelly_device_name = models.CharField(
        max_length=255, blank=True, null=True
    )  # Device name from the API

    # Automatically store the creation time in UTC
    created_at = models.DateTimeField(auto_now_add=True)

    # Automatically store the last modification time in UTC
    updated_at = models.DateTimeField(auto_now=True)

    # Django User and Shelly relationship
    user = models.ForeignKey(User, on_delete=models.CASCADE)

    status = models.IntegerField(default=1)  # 1 = automation enabled, 0 = automation disabled
    last_contact = models.DateTimeField(default=TimeUtils.now_utc)  # Ensure UTC storage

    # New field for how many hours the device should run daily
    run_hours_per_day = models.IntegerField(
        default=0, help_text="Set how many hours the device should run daily (0-24)"
    )

    minimum_run_hours_per_day = models.IntegerField(
        default=0,
        help_text="Minimum hours to keep assigned from the cheapest periods (0-24)",
    )

    # New fields for transfer prices
    day_transfer_price = models.DecimalField(
        max_digits=6,
        decimal_places=1,
        help_text="Transfer price during the day (c/kWh)",
    )

    night_transfer_price = models.DecimalField(
        max_digits=6,
        decimal_places=1,
        help_text="Transfer price during the night (c/kWh)",
    )

    auto_assign_price_threshold = models.DecimalField(
        max_digits=6,
        decimal_places=1,
        default=0,
        help_text=(
            "Always assign periods when total price is at or below this threshold (c/kWh)"
        ),
    )

    relay_channel = models.IntegerField(
        default=0,
        help_text="Default relay channel for the Shelly device (e.g., 0 for switch:0)",
    )

    shelly_server = models.URLField(
        max_length=512,
        default="https://yourapiaddress.shelly.cloud",
        help_text="Base URL of the Shelly server used for device communication",
    )

    thermostat_device = models.ForeignKey(
        "ShellyTemperature",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="controlled_devices",
        help_text="Optional default thermostat device for this Shelly device",
    )

    def __str__(self):
        return self.familiar_name


class ShellyTemperature(models.Model):
    device_id = models.AutoField(primary_key=True)  # Auto-generated device ID
    familiar_name = models.CharField(max_length=255)  # User-defined familiar name
    shelly_api_key = models.CharField(max_length=255)  # API key for the device
    shelly_device_name = models.CharField(
        max_length=255, blank=True, null=True
    )  # Device name from the API

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    user = models.ForeignKey(User, on_delete=models.CASCADE)

    shelly_server = models.URLField(
        max_length=512,
        default="https://yourapiaddress.shelly.cloud",
        help_text="Base URL of the Shelly server used for device communication",
    )
    min_temperature_winter = models.DecimalField(
        max_digits=5,
        decimal_places=2,
        default=0,
        verbose_name="Min temperature (winter)",
        help_text="Failsafe temperature Sep 1 – Mar 31 — forced heating activates below this",
    )
    min_temperature_summer = models.DecimalField(
        max_digits=5,
        decimal_places=2,
        default=0,
        verbose_name="Min temperature (summer)",
        help_text="Failsafe temperature Apr 1 – Aug 31 — forced heating activates below this",
    )
    max_temperature = models.DecimalField(
        max_digits=5,
        decimal_places=2,
        default=0,
        help_text="Maximum temperature threshold — assignments are removed above this",
    )
    headroom_winter = models.DecimalField(
        max_digits=4,
        decimal_places=1,
        default=0,
        verbose_name="Headroom (winter, °C)",
        help_text="Sep 1 – Mar 31: stop assigning this many degrees below max temperature (0 = disabled)",
    )
    headroom_summer = models.DecimalField(
        max_digits=4,
        decimal_places=1,
        default=4,
        verbose_name="Headroom (summer, °C)",
        help_text="Apr 1 – Aug 31: stop assigning this many degrees below max temperature (0 = disabled)",
    )
    current_temperature = models.DecimalField(
        max_digits=5,
        decimal_places=2,
        default=0,
        help_text="Last recorded temperature reading",
    )
    temperature_updated_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text="Timestamp of the last temperature update",
    )

    def get_effective_min_temperature(self):
        """Return summer or winter min temperature based on the current date.
        Summer: April 1 – August 31 (months 4–8).
        Winter: September 1 – March 31 (months 9–3).
        """
        from app.utils.time_utils import TimeUtils
        month = TimeUtils.now_utc().month
        if 4 <= month <= 8:
            return self.min_temperature_summer
        return self.min_temperature_winter

    def get_effective_headroom(self):
        """Return summer or winter headroom based on the current date."""
        from app.utils.time_utils import TimeUtils
        month = TimeUtils.now_utc().month
        if 4 <= month <= 8:
            return self.headroom_summer
        return self.headroom_winter

    def __str__(self):
        return self.familiar_name


class ElectricityPrice(models.Model):
    id = models.AutoField(primary_key=True)  # Explicit ID field
    start_time = models.DateTimeField(default=TimeUtils.now_utc)  # Store in UTC
    end_time = models.DateTimeField(default=TimeUtils.now_utc)  # Store in UTC
    price_kwh = models.DecimalField(max_digits=12, decimal_places=5)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        # Handle case where price is not set yet
        if self.price_kwh is None:
            return f"No price set from {self.start_time} to {self.end_time}"
        # The prices are already stored in c/kWh format, not €/MWh
        return f"{float(self.price_kwh):.3f} c/kWh from {self.start_time} to {self.end_time}"


class TemperatureReading(models.Model):
    thermostat = models.ForeignKey(
        ShellyTemperature,
        on_delete=models.CASCADE,
        related_name="temperature_readings",
    )
    temperature_c = models.DecimalField(max_digits=5, decimal_places=2)
    recorded_at = models.DateTimeField(default=TimeUtils.now_utc)

    def __str__(self):
        return f"{self.thermostat.familiar_name} at {self.recorded_at}: {self.temperature_c} C"


class EVCharger(models.Model):
    BACKEND_CHOICES = [("tuya", "Tuya IoT Platform")]

    id = models.AutoField(primary_key=True)
    familiar_name = models.CharField(max_length=255)
    user = models.ForeignKey(User, on_delete=models.CASCADE)
    backend = models.CharField(max_length=50, choices=BACKEND_CHOICES, default="tuya")

    # Tuya cloud credentials
    tuya_client_id = models.CharField(max_length=255, blank=True, default="")
    tuya_client_secret = models.CharField(max_length=255, blank=True, default="")
    tuya_device_id = models.CharField(max_length=255, blank=True, default="")
    tuya_base_url = models.URLField(
        max_length=512,
        default="https://openapi.tuyaeu.com",
        help_text="Regional Tuya OpenAPI URL (e.g. https://openapi.tuyaeu.com for Europe)",
    )

    # Charging settings
    charge_current_a = models.IntegerField(
        default=6, help_text="Normal charging current in amperes (1-32 A)"
    )
    charge_current_reduced_a = models.IntegerField(
        default=3,
        help_text="Reduced charging current used when other high-power devices are running, to protect fuses (1-32 A)",
    )
    run_hours_per_day = models.IntegerField(
        default=0, help_text="Hours to charge per day (0 = no automatic scheduling)"
    )
    day_transfer_price = models.DecimalField(
        max_digits=6, decimal_places=1, default=0,
        help_text="Day-time transfer price (c/kWh, 07:00-21:59)",
    )
    night_transfer_price = models.DecimalField(
        max_digits=6, decimal_places=1, default=0,
        help_text="Night-time transfer price (c/kWh, 22:00-06:59)",
    )
    auto_assign_price_threshold = models.DecimalField(
        max_digits=6, decimal_places=1, default=0,
        help_text="Always assign periods at or below this total price (c/kWh, 0 = disabled)",
    )
    status = models.IntegerField(default=1, help_text="1 = automation enabled, 0 = disabled")

    # Session tracking — set when charging starts, used to distribute session energy at end
    session_started_at = models.DateTimeField(
        null=True, blank=True,
        help_text="When the current/last charging session started (UTC)",
    )

    # Cached live state — refreshed every 15 minutes by the scheduler
    is_charging = models.BooleanField(default=False)
    work_state = models.CharField(max_length=64, blank=True, default="")
    connection_state = models.CharField(max_length=64, blank=True, default="")
    power_w = models.IntegerField(default=0)
    temp_c = models.IntegerField(default=0)
    session_energy_kwh = models.DecimalField(max_digits=8, decimal_places=3, default=0)
    total_energy_kwh = models.DecimalField(max_digits=10, decimal_places=3, default=0)
    last_contact = models.DateTimeField(null=True, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return self.familiar_name

    class Meta:
        verbose_name = "EV Charger"
        verbose_name_plural = "EV Chargers"


class EVChargerAssignment(models.Model):
    ASSIGNMENT_TYPES = [
        ("cheapest", "Cheapest Hours"),
        ("threshold", "Price Threshold"),
        ("manual", "Manual"),
    ]

    user = models.ForeignKey(User, on_delete=models.CASCADE)
    charger = models.ForeignKey(EVCharger, on_delete=models.CASCADE)
    electricity_price = models.ForeignKey(ElectricityPrice, on_delete=models.CASCADE)
    assigned_at = models.DateTimeField(auto_now_add=True)
    assignment_type = models.CharField(
        max_length=20, choices=ASSIGNMENT_TYPES, default="cheapest",
        help_text="Reason this period was assigned",
    )
    energy_kwh = models.DecimalField(
        max_digits=8, decimal_places=3, null=True, blank=True,
        help_text="Energy used during this period (kWh), filled in after session ends",
    )
    charge_current_a = models.IntegerField(
        null=True, blank=True,
        help_text="Charge current (A) recorded during this slot — used to weight session energy split",
    )

    def __str__(self):
        return (
            f"{self.charger.familiar_name} assigned at "
            f"{self.electricity_price.start_time} by {self.user.username}"
        )

    class Meta:
        verbose_name = "EV Charger Assignment"
        verbose_name_plural = "EV Charger Assignments"


class EVChargerEnergyLog(models.Model):
    charger = models.ForeignKey(EVCharger, on_delete=models.CASCADE, related_name="energy_logs")
    recorded_at = models.DateTimeField(default=TimeUtils.now_utc)
    session_energy_kwh = models.DecimalField(max_digits=8, decimal_places=3, default=0)
    total_energy_kwh = models.DecimalField(max_digits=10, decimal_places=3, default=0)
    is_charging = models.BooleanField(default=False)
    power_w = models.IntegerField(default=0)

    def __str__(self):
        return f"{self.charger.familiar_name} at {self.recorded_at}: {self.session_energy_kwh} kWh"

    class Meta:
        verbose_name = "EV Charger Energy Log"
        verbose_name_plural = "EV Charger Energy Logs"


class DeviceLog(models.Model):
    STATUS_CHOICES = [
        ("INFO", "Info"),
        ("WARN", "Warning"),
        ("ERROR", "Error"),
    ]

    device = models.ForeignKey(
        "ShellyDevice",
        on_delete=models.CASCADE,
        null=True,
        blank=True,
    )
    ev_charger = models.ForeignKey(
        "EVCharger",
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="logs",
    )
    message = models.TextField()
    status = models.CharField(max_length=5, choices=STATUS_CHOICES, default="INFO")
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        if self.device:
            return f"Log for {self.device.familiar_name} - {self.status}"
        if self.ev_charger:
            return f"Log for [EV] {self.ev_charger.familiar_name} - {self.status}"
        return f"System log - {self.status}"


class DeviceAssignment(models.Model):
    ASSIGNMENT_TYPES = [
        ("cheapest", "Cheapest Hours"),
        ("threshold", "Price Threshold"),
        ("forced_min", "Forced — Min Temperature"),
        ("manual", "Manual"),
        ("removed_overheat", "Removed — Overheating"),
        ("removed_headroom", "Removed — Headroom"),
    ]

    user = models.ForeignKey(User, on_delete=models.CASCADE)
    device = models.ForeignKey(ShellyDevice, on_delete=models.CASCADE)
    electricity_price = models.ForeignKey(ElectricityPrice, on_delete=models.CASCADE)
    assigned_at = models.DateTimeField(auto_now_add=True)
    assignment_type = models.CharField(
        max_length=20,
        choices=ASSIGNMENT_TYPES,
        default="cheapest",
        help_text="Reason this period was assigned",
    )
    energy_kwh = models.DecimalField(
        max_digits=8, decimal_places=3, null=True, blank=True,
        help_text="Energy used during this period (kWh), reserved for future use",
    )

    def __str__(self):
        return f"{self.device.familiar_name} assigned at {self.electricity_price.start_time} by {self.user.username}"


class UserProfile(models.Model):
    """Extended user profile with timezone and other preferences."""

    TIMEZONE_CHOICES = [
        ("UTC", "UTC (Coordinated Universal Time)"),
        ("Europe/Helsinki", "Helsinki (Finland)"),
        ("Europe/Stockholm", "Stockholm (Sweden)"),
        ("Europe/Oslo", "Oslo (Norway)"),
        ("Europe/Copenhagen", "Copenhagen (Denmark)"),
        ("Europe/London", "London (UK)"),
        ("Europe/Berlin", "Berlin (Germany)"),
        ("Europe/Paris", "Paris (France)"),
        ("Europe/Rome", "Rome (Italy)"),
        ("Europe/Madrid", "Madrid (Spain)"),
        ("America/New_York", "New York (EST/EDT)"),
        ("America/Chicago", "Chicago (CST/CDT)"),
        ("America/Denver", "Denver (MST/MDT)"),
        ("America/Los_Angeles", "Los Angeles (PST/PDT)"),
        ("Asia/Tokyo", "Tokyo (Japan)"),
        ("Asia/Shanghai", "Shanghai (China)"),
        ("Asia/Kolkata", "Mumbai/Delhi (India)"),
        ("Australia/Sydney", "Sydney (Australia)"),
    ]

    user = models.OneToOneField(User, on_delete=models.CASCADE, related_name="profile")
    timezone = models.CharField(
        max_length=50,
        choices=TIMEZONE_CHOICES,
        default="Europe/Helsinki",
        help_text="User's preferred timezone for displaying dates and times",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"{self.user.username} - {self.timezone}"

    def get_timezone(self):
        """Returns the pytz timezone object for this user."""
        return pytz.timezone(self.timezone)

    class Meta:
        verbose_name = "User Profile"
        verbose_name_plural = "User Profiles"



# Signal to automatically create UserProfile and add user to 'commoneers' group as staff
@receiver(post_save, sender=User)
def create_or_update_user_profile_and_group(sender, instance, created, **kwargs):
    """Create or update user profile and add to commoneers group as staff when user is saved. Always assign required permissions to the group."""
    from django.contrib.auth.models import Group, Permission
    from django.contrib.contenttypes.models import ContentType

    # Define required permissions (example: view, change, add, delete for ShellyDevice)
    required_perms = [
        # ShellyDevice permissions
        "view_shellydevice",
        "change_shellydevice",
        "add_shellydevice",
        "delete_shellydevice",
        # ShellyTemperature permissions
        "view_shellytemperature",
        "change_shellytemperature",
        "add_shellytemperature",
        "delete_shellytemperature",
        # DeviceAssignment permissions
        "view_deviceassignment",
        "change_deviceassignment",
        "add_deviceassignment",
        "delete_deviceassignment",
        # ElectricityPrice permissions
        "view_electricityprice",
        # EVCharger permissions
        "view_evcharger",
        "change_evcharger",
        "add_evcharger",
        "delete_evcharger",
        # EVChargerAssignment permissions
        "view_evchargerassignment",
        "change_evchargerassignment",
        "add_evchargerassignment",
        "delete_evchargerassignment",
    ]

    group, _ = Group.objects.get_or_create(name="commoneers")
    # Collect permissions from all relevant models
    from itertools import chain
    model_cts = [
        ContentType.objects.get(app_label="app", model="shellydevice"),
        ContentType.objects.get(app_label="app", model="shellytemperature"),
        ContentType.objects.get(app_label="app", model="deviceassignment"),
        ContentType.objects.get(app_label="app", model="electricityprice"),
        ContentType.objects.get(app_label="app", model="evcharger"),
        ContentType.objects.get(app_label="app", model="evchargerassignment"),
    ]
    perms = Permission.objects.filter(content_type__in=model_cts, codename__in=required_perms)
    group.permissions.set(perms)

    if created:
        UserProfile.objects.create(user=instance)
        instance.groups.add(group)
        instance.is_staff = True
        instance.save()
        # Create a dummy ShellyDevice for the new user
        from app.models import ShellyDevice
        ShellyDevice.objects.create(
            familiar_name="Demo Device",
            shelly_api_key="demo-api-key",
            shelly_device_name="Demo Shelly",
            user=instance,
            status=1,
            run_hours_per_day=1,
            day_transfer_price=0.12345,
            night_transfer_price=0.06789,
            relay_channel=0,
            shelly_server="https://yourapiaddress.shelly.cloud"
        )
    else:
        # Update existing profile if it exists
        if hasattr(instance, "profile"):
            instance.profile.save()
        else:
            # Create profile if it doesn't exist (for existing users)
            UserProfile.objects.create(user=instance)
