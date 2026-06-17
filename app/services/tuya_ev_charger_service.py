import hashlib
import hmac
import json
import time
import uuid
from typing import Optional

import requests

from .ev_charger_base import EVChargerServiceBase, EVChargerStatus


class TuyaEVChargerService(EVChargerServiceBase):
    """EV charger service backed by the Tuya IoT OpenAPI."""

    # Energy DP values use scale=2 (divide by 100 to get kWh)
    _ENERGY_SCALE = 100

    def __init__(self, client_id: str, client_secret: str, device_id: str, base_url: str):
        self.client_id = client_id
        self.client_secret = client_secret
        self.device_id = device_id
        self.base_url = base_url.rstrip("/")

    # ------------------------------------------------------------------
    # Signing helpers
    # ------------------------------------------------------------------

    def _sign(self, timestamp: str, nonce: str, access_token: Optional[str],
              method: str, path: str, body: str) -> str:
        body_sha256 = hashlib.sha256(body.encode("utf-8")).hexdigest()
        string_to_sign = "\n".join([method, body_sha256, "", path])
        if access_token:
            full_string = self.client_id + access_token + timestamp + nonce + string_to_sign
        else:
            full_string = self.client_id + timestamp + nonce + string_to_sign
        return hmac.new(
            self.client_secret.encode("utf-8"),
            full_string.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest().upper()

    def _headers(self, path: str, method: str = "GET", body: str = "",
                 access_token: Optional[str] = None) -> dict:
        timestamp = str(int(time.time() * 1000))
        nonce = uuid.uuid4().hex
        sign = self._sign(timestamp, nonce, access_token, method, path, body)
        headers = {
            "client_id": self.client_id,
            "sign": sign,
            "t": timestamp,
            "nonce": nonce,
            "sign_method": "HMAC-SHA256",
            "Content-Type": "application/json",
        }
        if access_token:
            headers["access_token"] = access_token
        return headers

    def _get_token(self) -> Optional[str]:
        path = "/v1.0/token?grant_type=1"
        try:
            resp = requests.get(
                self.base_url + path,
                headers=self._headers(path),
                timeout=15,
            )
            resp.raise_for_status()
            data = resp.json()
            if not data.get("success"):
                return None
            return data["result"]["access_token"]
        except Exception:
            return None

    def _get(self, path: str) -> Optional[dict]:
        token = self._get_token()
        if not token:
            return None
        try:
            resp = requests.get(
                self.base_url + path,
                headers=self._headers(path, access_token=token),
                timeout=15,
            )
            resp.raise_for_status()
            data = resp.json()
            return data if data.get("success") else None
        except Exception:
            return None

    def _post(self, path: str, body: dict) -> Optional[dict]:
        token = self._get_token()
        if not token:
            return None
        body_str = json.dumps(body)
        try:
            resp = requests.post(
                self.base_url + path,
                headers=self._headers(path, method="POST", body=body_str, access_token=token),
                data=body_str,
                timeout=15,
            )
            resp.raise_for_status()
            data = resp.json()
            return data if data.get("success") else None
        except Exception:
            return None

    # ------------------------------------------------------------------
    # EVChargerServiceBase implementation
    # ------------------------------------------------------------------

    def get_status(self) -> Optional[EVChargerStatus]:
        path = f"/v1.0/devices/{self.device_id}/status"
        data = self._get(path)
        if not data:
            return None
        dps = {dp["code"]: dp["value"] for dp in data.get("result", [])}
        work_state = str(dps.get("work_state", ""))
        is_charging = work_state == "charger_charging"
        charge_current_a = int(dps.get("charge_cur_set", 0))
        # power_total is non-functional on this device (always 0).
        # Estimate from set current × 230 V when actively charging.
        power_raw = int(dps.get("power_total", 0))
        if power_raw == 0 and is_charging and charge_current_a > 0:
            power_raw = charge_current_a * 230
        return EVChargerStatus(
            is_charging=is_charging,
            work_state=work_state,
            connection_state=str(dps.get("connection_state", "")),
            power_w=power_raw,
            temp_c=int(dps.get("temp_current", 0)),
            session_energy_kwh=int(dps.get("charge_energy_once", 0)) / self._ENERGY_SCALE,
            total_energy_kwh=int(dps.get("forward_energy_total", 0)) / self._ENERGY_SCALE,
            charge_current_set_a=charge_current_a,
        )

    def start_charging(self, current_a: int = 6) -> bool:
        path = f"/v1.0/devices/{self.device_id}/commands"
        result = self._post(path, {
            "commands": [
                {"code": "switch", "value": True},
                {"code": "charge_cur_set", "value": current_a},
            ]
        })
        return result is not None

    def stop_charging(self) -> bool:
        path = f"/v1.0/devices/{self.device_id}/commands"
        result = self._post(path, {
            "commands": [{"code": "switch", "value": False}]
        })
        return result is not None

    def set_charge_current(self, current_a: int) -> bool:
        path = f"/v1.0/devices/{self.device_id}/commands"
        result = self._post(path, {
            "commands": [{"code": "charge_cur_set", "value": current_a}]
        })
        return result is not None
