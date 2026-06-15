from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Optional


@dataclass
class EVChargerStatus:
    is_charging: bool
    work_state: str
    connection_state: str
    power_w: int
    temp_c: int
    session_energy_kwh: float
    total_energy_kwh: float
    charge_current_set_a: int = 0


class EVChargerServiceBase(ABC):
    """Abstract base for EV charger backend integrations.

    To add a new charger brand/protocol, subclass this, implement the three
    abstract methods, and register the backend key in ev_charger_factory.py.
    """

    @abstractmethod
    def get_status(self) -> Optional[EVChargerStatus]:
        """Return current charger status, or None on error."""
        ...

    @abstractmethod
    def start_charging(self, current_a: int = 6) -> bool:
        """Start charging at the given current (A). Returns True on success."""
        ...

    @abstractmethod
    def stop_charging(self) -> bool:
        """Stop charging. Returns True on success."""
        ...

    @abstractmethod
    def set_charge_current(self, current_a: int) -> bool:
        """Update charging current while already charging (no switch toggle). Returns True on success."""
        ...
