"""
Definition of forms.
"""

from django import forms
from django.conf import settings
from django.contrib.auth.forms import AuthenticationForm
from django.utils.translation import gettext_lazy as _

from datetime import datetime

from app.models import ShellyDevice, ShellyTemperature, EVCharger, EVChargerAlwaysOnSlot


class _BootstrapModelForm(forms.ModelForm):
    """ModelForm that applies Bootstrap classes to every widget.

    Accepts an optional ``user`` so create forms (no saved instance yet) can
    still scope related choices to the owner.
    """

    def __init__(self, *args, user=None, **kwargs):
        self._owner = user
        super().__init__(*args, **kwargs)
        for field in self.fields.values():
            widget = field.widget
            if isinstance(widget, forms.CheckboxInput):
                widget.attrs.setdefault("class", "form-check-input")
            elif isinstance(widget, forms.Select):
                widget.attrs.setdefault("class", "form-control")
            else:
                widget.attrs.setdefault("class", "form-control")


class ShellyDeviceSettingsForm(_BootstrapModelForm):
    """Owner-editable settings for a Shelly device."""

    class Meta:
        model = ShellyDevice
        fields = [
            "familiar_name",
            "status",
            "run_hours_per_day",
            "minimum_run_hours_per_day",
            "price_fallback_enabled",
            "block_scheduling",
            "switch_on_cost",
            "min_run_block_minutes",
            "day_transfer_price",
            "night_transfer_price",
            "auto_assign_price_threshold",
            "relay_channel",
            "thermostat_device",
            "shelly_server",
            "shelly_api_key",
            "shelly_device_name",
        ]
        widgets = {
            "status": forms.Select(choices=[(1, "Enabled"), (0, "Disabled")]),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # A device may only be linked to a thermostat its owner also owns.
        owner = self._owner or (self.instance.user if self.instance and self.instance.pk else None)
        qs = ShellyTemperature.objects.filter(user=owner) if owner else ShellyTemperature.objects.none()
        self.fields["thermostat_device"].queryset = qs
        self.fields["thermostat_device"].required = False


class ShellyTemperatureSettingsForm(_BootstrapModelForm):
    """Owner-editable settings for a thermostat."""

    class Meta:
        model = ShellyTemperature
        fields = [
            "familiar_name",
            "min_temperature_winter",
            "min_temperature_summer",
            "max_temperature",
            "headroom_winter",
            "headroom_summer",
            "shelly_server",
            "shelly_api_key",
            "shelly_device_name",
        ]


class EVChargerSettingsForm(_BootstrapModelForm):
    """Owner-editable settings for an EV charger."""

    class Meta:
        model = EVCharger
        fields = [
            "familiar_name",
            "status",
            "charge_current_a",
            "charge_current_reduced_a",
            "run_hours_per_day",
            "price_fallback_enabled",
            "block_scheduling",
            "switch_on_cost",
            "min_run_block_minutes",
            "day_transfer_price",
            "night_transfer_price",
            "auto_assign_price_threshold",
            "tuya_client_id",
            "tuya_client_secret",
            "tuya_device_id",
            "tuya_base_url",
        ]
        widgets = {
            "status": forms.Select(choices=[(1, "Enabled"), (0, "Disabled")]),
        }


# A fixed 24 h list in 15-minute steps (the scheduler's resolution). A plain
# <input type="time"> would show AM/PM in some browser locales.
_QUARTER_HOURS = [(f"{h:02d}:{m:02d}", f"{h:02d}:{m:02d}") for h in range(24) for m in (0, 15, 30, 45)]


class EVChargerAlwaysOnSlotForm(_BootstrapModelForm):
    start_time = forms.TypedChoiceField(
        choices=_QUARTER_HOURS,
        coerce=lambda s: datetime.strptime(s, "%H:%M").time(),
        label="Start",
    )

    class Meta:
        model = EVChargerAlwaysOnSlot
        fields = ["enabled", "start_time", "duration_hours"]
        labels = {"duration_hours": "Run for"}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if self.instance.pk:
            self.initial["start_time"] = self.instance.start_time.strftime("%H:%M")


# Slots are always bound to the charger being edited (inline formset sets the
# FK), so a client can't attach one to someone else's charger. max_num keeps a
# crafted POST from creating an unbounded number of rows.
EVChargerAlwaysOnSlotFormSet = forms.inlineformset_factory(
    EVCharger,
    EVChargerAlwaysOnSlot,
    form=EVChargerAlwaysOnSlotForm,
    extra=0,
    can_delete=True,
    max_num=24,
    validate_max=True,
    absolute_max=24,
)


class BootstrapAuthenticationForm(AuthenticationForm):
    """Authentication form which uses boostrap CSS."""
    username = forms.CharField(max_length=254,
                               widget=forms.TextInput({
                                   'class': 'form-control',
                                   'placeholder': 'User name'}))
    password = forms.CharField(label=_("Password"),
                               widget=forms.PasswordInput({
                                   'class': 'form-control',
                                   'placeholder':'Password'}))
    remember_me = forms.BooleanField(
        required=False,
        initial=True,  # Default to checked
        widget=forms.CheckboxInput(attrs={'class': 'form-check-input'}),
        # Kept in step with DJANGO_SESSION_DAYS so the label never overstates
        # how long the session actually survives.
        label=_('Remember me for %(days)d days') % {
            "days": getattr(settings, "SESSION_COOKIE_DAYS", 30)
        },
    )
