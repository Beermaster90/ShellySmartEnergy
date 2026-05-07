# ShellySmartEnergy Docker Usage Guide
# First-Time Project Setup

## Prerequisites
- Docker installed (required for `docker-prod.sh` and `docker-test.sh`).
- Python 3 (required for local venv setup).

## Quick Install (One Command)
Run this from any Linux host to install prerequisites and set up the project in the current directory:
```bash
bash -c "$(curl -fsSL https://raw.githubusercontent.com/Beermaster90/ShellySmartEnergy/refs/heads/master/Initial_ShellySmartEnergy_Setup.sh)"
```

## 1. Create a Python Virtual Environment (venv)
```bash
python3 -m venv venv
```

## 2. Activate the Virtual Environment
- On Linux/macOS:
	```bash
	source venv/bin/activate
	```
- On Windows:
	```cmd
	venv\Scripts\activate
	```

## 3. Install Project Requirements
```bash
pip install --upgrade pip
pip install -r requirements.txt
```

## 4. Select Python Interpreter in VS Code
- Open the Command Palette (`Ctrl+Shift+P`)
- Type and select `Python: Select Interpreter`
- Choose the interpreter from your `venv` folder (e.g., `venv/bin/python` or `venv\Scripts\python.exe`)

## Overview
This project uses Docker for both production and test environments. Two shell scripts are provided to build and run the containers:
- `docker-prod.sh` for production
## Running the Docker Scripts

### 1. Production
To build and run the production container:
```bash
bash docker-prod.sh
```
- The app will run on port 8000.
- Persistent data is stored in `~/ShellySmartEnergy/data/` in your home directory.

### 2. Test
To build and run the test container:
```bash
bash docker-test.sh
```
- The app will run on port 9000.
- Persistent data is stored in `~/ShellySmartEnergy/data-test/` in your home directory.

## Connecting to the Web UI
Once the container is running, open the web page in your browser:
- Local machine: `http://localhost:8000/`
- LAN access: `http://<lan-ip>:8000/` (use the host machine's IP address on your network)

## First-Time Usage Notes
- Log in with your user account before anything will work.
- Add your Shelly devices and set the ENTSO API key in the admin panel.
- Initial price data appears when the next ENTSO-E day-ahead data is published (around 14:00 Finnish time, which is 12:00 UTC / 13:00 CET depending on DST).

## Thermostat Support
- Thermostat devices are supported.
- Configure a Shelly Temperature device in Admin, then select it as the default thermostat from the Shelly Device dropdown.
- Once configured, thermostat options appear on the device page and are used by the 15-minute automation logic.
- Thermostat fields:
  - Min temperature: if the current temperature is below (min - 0.5°C), the next 15-minute period is assigned (device will run).
  - Max temperature: if the current temperature is above (max + 0.5°C), the next 15-minute period is unassigned (device will stop).
  - Target temperature: stored for future use, currently not enforced in automation.

## Versioning

- The Docker image version is read from the `VERSION` file in the project root.
- Each build appends the current date and time (with seconds) to the version (e.g., `1.0.0-20251002-164123`).
- All built images are available and selectable by their tag.

### Changing the Version
1. Edit the `VERSION` file and set your desired version (use [semver2](https://semver.org/) format, e.g., `1.2.3`).
2. Run either script to build a new image with the updated version.

## Listing Available Versions
To list all available Docker image versions for test or prod:
```bash
docker images django-shelly-test
docker images django-shelly-prod
```


## Running a Specific Version
To run a specific test version (tag):
```bash
docker run -d -p 9000:8000 django-shelly-test:1.0.0-20251002-164123
```
To run a specific production version (tag):
```bash
docker run -d -p 8000:8000 django-shelly-prod:1.0.0-20251002-164123
```
Replace the tag with the desired version (including seconds).


## Default Django Admin Credentials
- Username: `admin`
- Password: `admin12345`

Note: Remember to change password from Admin menu -> Users -> Edit Admin!

## Admin Setup: Add Shelly Devices
1. Open the admin panel: `http://<host>:8000/admin/`
2. Log in with your admin user.
3. Go to `App` -> `Shelly Devices` -> `Add`.
4. Fill in the device fields:
   - `Familiar name`: a friendly name you choose.
   - `Shelly server`: copy this from the Shelly device info page (server/base URL).
   - `Shelly API key`: copy this from the Shelly device info page (auth/API key).
   - `Shelly device name`: device name from Shelly (required).
   - `Relay channel`, `Run hours per day`, and transfer prices as needed.
5. Save the device.

### Where to find Shelly server and API key
Open the Shelly app or web UI, select the device, and open its info/details page.
Copy the `Server` (base URL) and `API key/Auth key` values into the fields above.

## Notes
- The scripts automatically stop and remove any existing container with the same name before starting a new one.
- Data is persistent between runs as long as you do not delete the `~/ShellySmartEnergy/data/` or `~/ShellySmartEnergy/data-test/` folders.
- For production, consider using a real database instead of SQLite for better reliability and scalability.

## License
GNU AGPL v3

### Summary (What You Can Do)
- Use, run, and modify the software.
- Share the software and your modifications.
- Use it for commercial or private purposes.
- You must provide source code and license notices when distributing.
- If you run a modified version as a network service, you must provide the source to users of that service.
- Derivative works must remain under AGPL v3.

## Application Settings

On first start, App Settings are created in the database to control application behavior. You can view and edit these in the Django admin panel under "App Settings".

### Global App Settings

| Key                     | Description                                                                 |
|-------------------------|-----------------------------------------------------------------------------|
| timezone                | Default timezone for new users and system operations.                        |
| clear_logs_onstartup    | If set to 1, clears all device logs on each app startup.                     |
| Shelly_stop_rest_debug  | Enables debug logging for Shelly REST stop operations (1=enabled, 0=disabled)|
| enstoe_apikey           | API key for Ensto device integration.                                        |

### Default Device Settings

| Key                     | Description                                                                 |
|-------------------------|-----------------------------------------------------------------------------|
| shelly_server           | Base URL for Shelly device API communication.                                |
| automation_enabled      | Global flag to enable or disable device automation (1=enabled, 0=disabled).  |
| default_run_hours       | Default number of hours devices should run daily.                            |
| day_transfer_price      | Default transfer price during the day (c/kWh).                               |
| night_transfer_price    | Default transfer price during the night (c/kWh).                             |
| relay_channel           | Default relay channel for Shelly devices.                                    |

**Note:** These settings are created automatically if missing, and can be changed at any time in the Django admin panel under "App Settings".
