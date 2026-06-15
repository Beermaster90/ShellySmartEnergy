from .models import DeviceLog
from .utils.security_utils import SecurityUtils


def log_device_event(device, message, status="INFO"):
    """Log events for a ShellyDevice (or system-level when device=None)."""
    safe_message = SecurityUtils.sanitize_message(message)
    DeviceLog.objects.create(device=device, ev_charger=None, message=safe_message, status=status)


def log_ev_event(ev_charger, message, status="INFO"):
    """Log events for an EVCharger."""
    safe_message = SecurityUtils.sanitize_message(message)
    DeviceLog.objects.create(device=None, ev_charger=ev_charger, message=safe_message, status=status)
