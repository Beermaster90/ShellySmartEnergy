# Message Examples

These examples document only the external messages used by ShellySmartEnergy
for Shelly Cloud and ENTSO-E. Secrets, hostnames, and device IDs are
placeholders.

Files:

- `Shelly_GetStatus.json`: Shelly Cloud relay device status.
- `Shelly_SetRelayOutput.json`: Shelly Cloud relay control.
- `Shelly_GetTemperatureStatus.json`: Shelly Cloud temperature device status.
- `ENTSOE_GetDayAheadPrices.xml`: raw ENTSO-E day-ahead price response shape
  for Finland.

Shelly Cloud returns JSON. ENTSO-E returns XML from the provider endpoint.
The ENTSO-E example uses 15-minute resolution with 96 price points for a
24-hour delivery window.

The outbound backend calls are covered by mocked tests in
`ExternalApiMessageConstructionTest`; those tests validate request URL, method,
query parameters, body data, and timeout without contacting live services.
