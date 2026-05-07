# Message Examples

These JSON examples document only the external messages used by
ShellySmartEnergy for Shelly Cloud and ENTSO-E. Secrets, hostnames, and device
IDs are placeholders.

Files:

- `Shelly_GetStatus.json`: Shelly Cloud relay device status.
- `Shelly_SetRelayOutput.json`: Shelly Cloud relay control.
- `Shelly_GetTemperatureStatus.json`: Shelly Cloud temperature device status.
- `ENTSOE_GetDayAheadPrices.json`: ENTSO-E day-ahead price request and parsed
  price data shape.

Shelly Cloud returns JSON. ENTSO-E returns XML from the provider endpoint, so
the ENTSO-E example stores the outbound request details and the parsed JSON
shape used by this app after `entsoe.parsers.parse_prices`.

The outbound backend calls are covered by mocked tests in
`ExternalApiMessageConstructionTest`; those tests validate request URL, method,
query parameters, body data, and timeout without contacting live services.
