from django import forms
from django.contrib import admin
from django.contrib.auth.models import User
from django.db import models
from .models import (
    ShellyDevice,
    ShellyTemperature,
    ElectricityPrice,
    DeviceAssignment,
    AppSetting,
    UserProfile,
)
from django.contrib.auth.admin import UserAdmin as BaseUserAdmin
from app.utils.time_utils import TimeUtils
import pytz


### SHELLY DEVICE ADMIN ###
class ShellyDeviceAdmin(admin.ModelAdmin):
    list_display = (
        "device_id",
        "familiar_name",
        "shelly_api_key",
        "shelly_device_id",
        "run_hours_per_day",
        "minimum_run_hours_per_day",
        "day_transfer_price",
        "night_transfer_price",
        "auto_assign_price_threshold",
        "created_at",
        "updated_at",
        "user",
        "get_automation_status",
        "last_contact",
        "relay_channel",
        "shelly_server",
        "thermostat_device",
    )

    formfield_overrides = {
        models.DecimalField: {
            "localize": False,
            "widget": forms.NumberInput(attrs={"step": "0.1"}),
        },
    }
    
    def get_automation_status(self, obj):
        """Display automation status in a user-friendly way."""
        return "Enabled" if obj.status == 1 else "Disabled"
    get_automation_status.short_description = "Automation Status"
    get_automation_status.admin_order_field = "status"
    
    search_fields = ("familiar_name",)
    readonly_fields = (
        "device_id",
        "created_at",
        "updated_at",
    )  # 'user' is editable for admins

    fields = (
        "device_id",
        "familiar_name",
        "shelly_api_key",
        "shelly_device_name",
        "run_hours_per_day",
        "minimum_run_hours_per_day",
        "day_transfer_price",
        "night_transfer_price",
        "auto_assign_price_threshold",
        "created_at",
        "updated_at",
        "user",
        "status",
        "last_contact",
        "relay_channel",
        "shelly_server",
        "thermostat_device",
    )

    ordering = ["-device_id"]

    def shelly_device_id(self, obj):
        return obj.shelly_device_name
    shelly_device_id.short_description = "Shelly device id"
    shelly_device_id.admin_order_field = "shelly_device_name"

    def save_model(self, request, obj, form, change):
        """Ensure new devices are owned by the user who creates them if not set."""
        if not change and not request.user.is_superuser:
            obj.user = request.user
        super().save_model(request, obj, form, change)

    def get_queryset(self, request):
        """Limit users to only see their own devices."""
        qs = super().get_queryset(request)
        return qs if request.user.is_superuser else qs.filter(user=request.user)

    def formfield_for_foreignkey(self, db_field, request, **kwargs):
        """
        Allow admins to select any user in dropdown.
        Ensure normal users see only themselves.
        """
        if db_field.name == "user":
            if request.user.is_superuser:
                kwargs["queryset"] = User.objects.all()  # Admins see all users
            else:
                kwargs["queryset"] = User.objects.filter(
                    id=request.user.id
                )  # Users only see themselves
        if db_field.name == "thermostat_device":
            if request.user.is_superuser:
                kwargs["queryset"] = ShellyTemperature.objects.all()
            else:
                kwargs["queryset"] = ShellyTemperature.objects.filter(user=request.user)
        return super().formfield_for_foreignkey(db_field, request, **kwargs)

    def formfield_for_dbfield(self, db_field, request, **kwargs):
        formfield = super().formfield_for_dbfield(db_field, request, **kwargs)
        if db_field.name == "shelly_device_name":
            formfield.label = "Shelly device id"
        return formfield


admin.site.register(ShellyDevice, ShellyDeviceAdmin)


### SHELLY TEMPERATURE ADMIN ###
class ShellyTemperatureAdmin(admin.ModelAdmin):
    list_display = (
        "device_id",
        "familiar_name",
        "shelly_api_key",
        "shelly_device_id",
        "min_temperature",
        "max_temperature",
        "hoped_temperature",
        "current_temperature",
        "temperature_updated_at",
        "created_at",
        "updated_at",
        "user",
        "shelly_server",
    )
    search_fields = ("familiar_name",)
    readonly_fields = (
        "device_id",
        "created_at",
        "updated_at",
    )
    fields = (
        "device_id",
        "familiar_name",
        "shelly_api_key",
        "shelly_device_name",
        "min_temperature",
        "max_temperature",
        "hoped_temperature",
        "current_temperature",
        "temperature_updated_at",
        "created_at",
        "updated_at",
        "user",
        "shelly_server",
    )
    ordering = ["-device_id"]
    formfield_overrides = {
        models.DecimalField: {
            "localize": False,
            "widget": forms.NumberInput(attrs={"step": "0.1"}),
        },
    }

    def shelly_device_id(self, obj):
        return obj.shelly_device_name
    shelly_device_id.short_description = "Shelly device id"
    shelly_device_id.admin_order_field = "shelly_device_name"

    def save_model(self, request, obj, form, change):
        if not change and not request.user.is_superuser:
            obj.user = request.user
        super().save_model(request, obj, form, change)

    def get_queryset(self, request):
        qs = super().get_queryset(request)
        return qs if request.user.is_superuser else qs.filter(user=request.user)

    def formfield_for_foreignkey(self, db_field, request, **kwargs):
        if db_field.name == "user":
            if request.user.is_superuser:
                kwargs["queryset"] = User.objects.all()
            else:
                kwargs["queryset"] = User.objects.filter(id=request.user.id)
        return super().formfield_for_foreignkey(db_field, request, **kwargs)

    def formfield_for_dbfield(self, db_field, request, **kwargs):
        formfield = super().formfield_for_dbfield(db_field, request, **kwargs)
        if db_field.name == "shelly_device_name":
            formfield.label = "Shelly device id"
        return formfield


admin.site.register(ShellyTemperature, ShellyTemperatureAdmin)


### ELECTRICITY PRICE ADMIN (View Only for Non-Admins) ###
@admin.register(ElectricityPrice)
class ElectricityPriceAdmin(admin.ModelAdmin):
    list_display = (
        "get_start_time_user_tz",
        "get_end_time_user_tz",
        "get_price_c_kwh",
        "get_price_with_vat",
        "get_created_at_user_tz",
    )
    search_fields = ("start_time", "end_time")
    list_filter = ("start_time", "end_time")
    ordering = ("-start_time",)
    fields = ("start_time", "end_time", "price_kwh")  # Actual editable fields
    readonly_fields = (
        "get_start_time_user_tz",
        "get_end_time_user_tz",
        "get_price_c_kwh",
        "get_price_with_vat",
        "get_created_at_user_tz",
    )  # Display-only computed fields

    def has_add_permission(self, request):
        return request.user.is_superuser  # Only admins can add

    def has_change_permission(self, request, obj=None):
        return request.user.is_superuser  # Only admins can edit

    def has_delete_permission(self, request, obj=None):
        return request.user.is_superuser  # Only admins can delete

    def get_start_time_user_tz(self, obj):
        """Display start time in the current user's timezone."""
        # Get current request user from thread-local storage (set in middleware)
        request = getattr(self, "_current_request", None)
        if request and hasattr(request, "user"):
            return TimeUtils.format_datetime_with_tz(obj.start_time, request.user)
        else:
            # Fallback to Helsinki time if no request context
            helsinki_tz = pytz.timezone("Europe/Helsinki")
            local_dt = obj.start_time.astimezone(helsinki_tz)
            return local_dt.strftime("%Y-%m-%d %H:%M %Z")

    get_start_time_user_tz.short_description = "Start Time"

    def get_end_time_user_tz(self, obj):
        """Display end time in the current user's timezone."""
        request = getattr(self, "_current_request", None)
        if request and hasattr(request, "user"):
            return TimeUtils.format_datetime_with_tz(obj.end_time, request.user)
        else:
            # Fallback to Helsinki time if no request context
            helsinki_tz = pytz.timezone("Europe/Helsinki")
            local_dt = obj.end_time.astimezone(helsinki_tz)
            return local_dt.strftime("%Y-%m-%d %H:%M %Z")

    get_end_time_user_tz.short_description = "End Time"

    def get_created_at_user_tz(self, obj):
        """Display created time in the current user's timezone."""
        request = getattr(self, "_current_request", None)
        if request and hasattr(request, "user"):
            return TimeUtils.format_datetime_with_tz(obj.created_at, request.user)
        else:
            # Fallback to Helsinki time if no request context
            helsinki_tz = pytz.timezone("Europe/Helsinki")
            local_dt = obj.created_at.astimezone(helsinki_tz)
            return local_dt.strftime("%Y-%m-%d %H:%M %Z")

    get_created_at_user_tz.short_description = "Created At"

    def get_price_c_kwh(self, obj):
        """Display price as stored (base price without VAT)."""
        if obj.price_kwh is None:
            return "-"
        return f"{float(obj.price_kwh):.3f} c/kWh"

    get_price_c_kwh.short_description = "Base Price (c/kWh)"

    def get_price_with_vat(self, obj):
        """Display price with 25.5% VAT applied (real price users pay)."""
        if obj.price_kwh is None:
            return "-"
        base_price = float(obj.price_kwh)
        price_with_vat = base_price * 1.255  # 25.5% VAT
        return f"{price_with_vat:.2f} c/kWh"

    get_price_with_vat.short_description = "Price with VAT (c/kWh)"

    def changeform_view(self, request, object_id=None, form_url="", extra_context=None):
        """Store request in instance for timezone context."""
        self._current_request = request
        return super().changeform_view(request, object_id, form_url, extra_context)

    def changelist_view(self, request, extra_context=None):
        """Store request in instance for timezone context."""
        self._current_request = request
        return super().changelist_view(request, extra_context)


### DEVICE ASSIGNMENT ADMIN (Users Can Manage Their Own Assignments) ###
class DeviceAssignmentAdmin(admin.ModelAdmin):
    list_display = (
        "user",
        "device",
        "get_start_time_local",
        "get_end_time_local",
        "get_assigned_at_user_tz",
    )
    search_fields = (
        "user__username",
        "device__familiar_name",
        "electricity_price__start_time",
    )
    list_filter = ("user", "device", "electricity_price__start_time")
    ordering = ("-assigned_at",)
    readonly_fields = (
        "get_assigned_at_user_tz",
    )  # Users cannot modify the assignment timestamp

    def get_queryset(self, request):
        """Limit users to only see their own assignments."""
        qs = super().get_queryset(request)
        return qs if request.user.is_superuser else qs.filter(user=request.user)

    def formfield_for_foreignkey(self, db_field, request, **kwargs):
        """
        Ensure admins can assign devices to any user.
        Regular users can only assign devices to themselves.
        Show electricity price dropdown in local (Finnish) time.
        """
        if db_field.name == "device" and not request.user.is_superuser:
            kwargs["queryset"] = ShellyDevice.objects.filter(user=request.user)

        if db_field.name == "user":
            if request.user.is_superuser:
                kwargs["queryset"] = User.objects.all()  # Admins see all users
            else:
                kwargs["queryset"] = User.objects.filter(
                    id=request.user.id
                )  # Users only see themselves

        if db_field.name == "electricity_price":
            # Use user's timezone instead of hardcoded Helsinki
            user_tz = TimeUtils.get_user_timezone(request.user)
            queryset = kwargs.get("queryset", ElectricityPrice.objects.all())
            # Attach a display string for user's local time
            for price in queryset:
                dt = price.start_time
                if dt.tzinfo is None or dt.tzinfo.utcoffset(dt) is None:
                    dt = dt.replace(tzinfo=pytz.UTC)
                local_dt = dt.astimezone(user_tz)
                price.local_time_display = local_dt.strftime("%Y-%m-%d %H:%M %Z")
                price.utc_time_display = dt.strftime("%Y-%m-%d %H:%M UTC")
            kwargs["queryset"] = queryset

        return super().formfield_for_foreignkey(db_field, request, **kwargs)

    def label_from_instance(self, obj):
        # Show user's local time and UTC time in dropdown with VAT price
        if hasattr(obj, "local_time_display") and hasattr(obj, "utc_time_display"):
            # Show the real price with VAT that users pay
            if obj.price_kwh is None:
                price_display = "- c/kWh"
            else:
                base_price = float(obj.price_kwh)
                price_with_vat = base_price * 1.255  # 25.5% VAT
                price_display = f"{price_with_vat:.2f} c/kWh (incl. VAT)"
            return f"{obj.local_time_display} [{obj.utc_time_display}] - {price_display}"
        return super().label_from_instance(obj)

    def save_model(self, request, obj, form, change):
        """Ensure non-admin users can only assign devices to themselves."""
        if not request.user.is_superuser:
            obj.user = request.user  # Force user field to be request.user
        super().save_model(request, obj, form, change)

    def has_delete_permission(self, request, obj=None):
        """Allow users to delete **only their own** assignments."""
        if request.user.is_superuser:
            return True  # Admins can delete everything
        return (
            obj is None or obj.user == request.user
        )  # Users can delete only their own assignments

    def get_readonly_fields(self, request, obj=None):
        """Hide the 'user' field from non-admins (automatically set to request.user)."""
        readonly = super().get_readonly_fields(request, obj)
        return readonly if request.user.is_superuser else readonly + ("user",)

    def get_start_time_local(self, obj):
        """Display start time in the current user's timezone."""
        request = getattr(self, "_current_request", None)
        if request and hasattr(request, "user"):
            return TimeUtils.format_datetime_with_tz(
                obj.electricity_price.start_time, request.user
            )
        else:
            # Fallback to Helsinki time if no request context
            helsinki_tz = pytz.timezone("Europe/Helsinki")
            local_dt = obj.electricity_price.start_time.astimezone(helsinki_tz)
            return local_dt.strftime("%Y-%m-%d %H:%M %Z")

    get_start_time_local.short_description = "Start Time"

    def get_end_time_local(self, obj):
        """Display end time in the current user's timezone."""
        request = getattr(self, "_current_request", None)
        if request and hasattr(request, "user"):
            return TimeUtils.format_datetime_with_tz(
                obj.electricity_price.end_time, request.user
            )
        else:
            # Fallback to Helsinki time if no request context
            helsinki_tz = pytz.timezone("Europe/Helsinki")
            local_dt = obj.electricity_price.end_time.astimezone(helsinki_tz)
            return local_dt.strftime("%Y-%m-%d %H:%M %Z")

    get_end_time_local.short_description = "End Time"

    def get_assigned_at_user_tz(self, obj):
        """Display assignment time in the current user's timezone."""
        request = getattr(self, "_current_request", None)
        if request and hasattr(request, "user"):
            return TimeUtils.format_datetime_with_tz(obj.assigned_at, request.user)
        else:
            # Fallback to Helsinki time if no request context
            helsinki_tz = pytz.timezone("Europe/Helsinki")
            local_dt = obj.assigned_at.astimezone(helsinki_tz)
            return local_dt.strftime("%Y-%m-%d %H:%M %Z")

    get_assigned_at_user_tz.short_description = "Assigned At"

    def changeform_view(self, request, object_id=None, form_url="", extra_context=None):
        """Store request in instance for timezone context."""
        self._current_request = request
        return super().changeform_view(request, object_id, form_url, extra_context)

    def changelist_view(self, request, extra_context=None):
        """Store request in instance for timezone context."""
        self._current_request = request
        return super().changelist_view(request, extra_context)


admin.site.register(DeviceAssignment, DeviceAssignmentAdmin)

admin.site.register(AppSetting)


### USER PROFILE ADMIN ###
class UserProfileInline(admin.StackedInline):
    """Inline admin for user profile."""

    model = UserProfile
    can_delete = False
    verbose_name_plural = "Profile Settings"
    fields = ("timezone",)


class UserProfileAdmin(admin.ModelAdmin):
    """Standalone admin for user profiles."""

    list_display = ("user", "timezone", "created_at", "updated_at")
    list_filter = ("timezone",)
    search_fields = ("user__username", "user__email")
    readonly_fields = ("created_at", "updated_at")
    ordering = ["user__username"]


### EXTENDED USER ADMIN ###
class ExtendedUserAdmin(BaseUserAdmin):
    """Extended User Admin with profile inline."""

    inlines = (UserProfileInline,)

    def get_inline_instances(self, request, obj=None):
        if not obj:
            return list()
        return super().get_inline_instances(request, obj)


# Unregister the default User admin and register our extended version
admin.site.unregister(User)
admin.site.register(User, ExtendedUserAdmin)
admin.site.register(UserProfile, UserProfileAdmin)
