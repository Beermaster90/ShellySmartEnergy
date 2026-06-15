from .ev_charger_base import EVChargerServiceBase
from .tuya_ev_charger_service import TuyaEVChargerService


def get_ev_charger_service(charger) -> EVChargerServiceBase:
    """Return the correct service instance for the given EVCharger model object.

    Add new backends here — create a subclass of EVChargerServiceBase,
    add a BACKEND_CHOICES entry to the EVCharger model, and add a branch below.
    """
    if charger.backend == "tuya":
        return TuyaEVChargerService(
            client_id=charger.tuya_client_id,
            client_secret=charger.tuya_client_secret,
            device_id=charger.tuya_device_id,
            base_url=charger.tuya_base_url,
        )
    raise ValueError(f"Unknown EV charger backend: {charger.backend!r}")
