"""
This file demonstrates writing tests using the unittest module. These will pass
when you run "manage.py test".
"""

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from unittest.mock import Mock, patch

import django
from django.contrib.auth.models import User
from django.test import SimpleTestCase, TestCase

from app.models import (
    DeviceAssignment,
    DeviceLog,
    ElectricityPrice,
    EVCharger,
    ShellyDevice,
    ShellyTemperature,
)
from app.services.tuya_ev_charger_service import TuyaEVChargerService
from app.thermostat_manager import ThermostatAssignmentManager

# TODO: Configure your database in settings.py and sync before running tests.


class TuyaEVChargerServiceTest(SimpleTestCase):
    def make_response(self, payload):
        response = Mock()
        response.json.return_value = payload
        response.raise_for_status.return_value = None
        return response

    def make_service(self, device_id="device-id"):
        return TuyaEVChargerService(
            client_id="client-id",
            client_secret="client-secret",
            device_id=device_id,
            base_url="https://openapi.tuyaeu.com",
        )

    @patch("app.services.tuya_ev_charger_service.requests.get")
    def test_status_failure_records_tuya_error(self, mock_get):
        mock_get.side_effect = [
            self.make_response({
                "success": True,
                "result": {"access_token": "token-value"},
            }),
            self.make_response({
                "success": False,
                "code": 28841002,
                "msg": "IoT Core service subscription has expired.",
            }),
        ]
        service = self.make_service()

        status = service.get_status()

        self.assertIsNone(status)
        self.assertEqual(
            service.get_last_error(),
            "Tuya status request failed: code=28841002, "
            "msg=IoT Core service subscription has expired.",
        )
        self.assertNotIn("token-value", service.get_last_error())

    @patch("app.services.tuya_ev_charger_service.requests.get")
    def test_token_failure_records_tuya_error(self, mock_get):
        mock_get.return_value = self.make_response({
            "success": False,
            "code": 1010,
            "msg": "token expired",
        })
        service = self.make_service()

        status = service.get_status()

        self.assertIsNone(status)
        self.assertEqual(
            service.get_last_error(),
            "Tuya token request failed: code=1010, msg=token expired",
        )

    @patch("app.services.tuya_ev_charger_service.requests.get")
    def test_missing_device_id_does_not_call_tuya(self, mock_get):
        service = self.make_service(device_id="")

        status = service.get_status()

        self.assertIsNone(status)
        self.assertEqual(
            service.get_last_error(),
            "Tuya credentials are incomplete: missing device_id",
        )
        mock_get.assert_not_called()


class SchedulerStartupTest(SimpleTestCase):
    class ExistingTableCursor:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def execute(self, query):
            self.query = query

        def fetchone(self):
            return ("django_apscheduler_djangojob",)

    def tearDown(self):
        import app.scheduler as scheduler

        if scheduler._scheduler_lock_file is not None:
            scheduler._scheduler_lock_file.close()
        scheduler._scheduler_lock_file = None
        scheduler._scheduler = None

    def test_start_scheduler_skips_when_lock_is_held_elsewhere(self):
        import app.scheduler as scheduler

        scheduler._scheduler = None
        with (
            patch("app.scheduler._is_one_off_management_command", return_value=False),
            patch("app.scheduler.connection") as connection,
            patch("app.scheduler._acquire_scheduler_lock", return_value=False),
            patch("app.scheduler.get_scheduler") as get_scheduler,
        ):
            connection.cursor.return_value = self.ExistingTableCursor()

            scheduler.start_scheduler()

        get_scheduler.assert_not_called()

    def test_start_scheduler_registers_each_job_once(self):
        import app.scheduler as scheduler

        fake_scheduler = Mock()
        fake_scheduler.running = False
        scheduler._scheduler = None
        with (
            patch("app.scheduler._is_one_off_management_command", return_value=False),
            patch("app.scheduler.connection") as connection,
            patch("app.scheduler._acquire_scheduler_lock", return_value=True),
            patch("app.scheduler.get_scheduler", return_value=fake_scheduler),
        ):
            connection.cursor.return_value = self.ExistingTableCursor()

            scheduler.start_scheduler()

        self.assertEqual(fake_scheduler.add_job.call_count, 4)
        self.assertEqual(
            [call.kwargs["id"] for call in fake_scheduler.add_job.call_args_list],
            [
                "fetch_prices",
                "control_shelly",
                "control_ev_chargers",
                "thermostat_assignments",
            ],
        )
        fake_scheduler.start.assert_called_once()

    def test_start_scheduler_skips_one_off_management_command(self):
        import app.scheduler as scheduler

        scheduler._scheduler = None
        with (
            patch("app.scheduler._is_one_off_management_command", return_value=True),
            patch("app.scheduler.connection") as connection,
            patch("app.scheduler.get_scheduler") as get_scheduler,
        ):
            scheduler.start_scheduler()

        connection.cursor.assert_not_called()
        get_scheduler.assert_not_called()


class ViewTest(TestCase):
    """Tests for the application views."""

    if django.VERSION[:2] >= (1, 7):
        # Django 1.7 requires an explicit setup() when running tests in PTVS
        @classmethod
        def setUpClass(cls):
            super(ViewTest, cls).setUpClass()
            django.setup()

    def setUp(self):
        self.user = User.objects.create_user(username="view-user")

    def test_home(self):
        """Tests the home page."""
        self.client.force_login(self.user)
        response = self.client.get('/')
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "app/index.html")

    def test_contact(self):
        """Tests the contact page."""
        response = self.client.get('/contact/')
        self.assertContains(response, 'Contact', 3, 200)

    def test_about(self):
        """Tests the about page."""
        self.client.force_login(self.user)
        response = self.client.get('/about/')
        self.assertContains(response, 'Device Logs', 1, 200)


class LogsPageFilterTest(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="logs-user")
        self.shelly_device = ShellyDevice.objects.create(
            familiar_name="Heater",
            shelly_api_key="shelly-key",
            shelly_device_name="device-id",
            user=self.user,
            day_transfer_price=Decimal("0.0"),
            night_transfer_price=Decimal("0.0"),
        )
        self.ev_charger = EVCharger.objects.create(
            familiar_name="Charger",
            user=self.user,
        )
        DeviceLog.objects.create(
            device=self.shelly_device,
            message="Shelly-only log",
            status="INFO",
        )
        DeviceLog.objects.create(
            ev_charger=self.ev_charger,
            message="EV-only log",
            status="ERROR",
        )
        self.client.force_login(self.user)

    def test_ev_charger_filter_shows_only_selected_ev_logs(self):
        response = self.client.get("/about/", {"device_id": f"ev_{self.ev_charger.id}"})

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "EV-only log")
        self.assertContains(response, "[EV] Charger")
        self.assertNotContains(response, "Shelly-only log")
        self.assertContains(response, f'value="ev_{self.ev_charger.id}" selected')

    def test_invalid_ev_charger_filter_returns_empty_logs(self):
        response = self.client.get("/about/", {"device_id": "ev_not-a-number"})

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "No logs found.")
        self.assertNotContains(response, "EV-only log")
        self.assertNotContains(response, "Shelly-only log")


class ThermostatAssignmentManagerTest(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="thermostat-user")
        self.now = datetime(2026, 1, 1, 12, 3, tzinfo=UTC)
        self.next_start = self.now.replace(minute=0, second=0, microsecond=0) + timedelta(
            minutes=15
        )
        self.thermostat = ShellyTemperature.objects.create(
            familiar_name="Thermostat",
            shelly_api_key="thermostat-key",
            user=self.user,
            min_temperature_winter=Decimal("18.0"),
            min_temperature_summer=Decimal("15.0"),
            max_temperature=Decimal("20.0"),
            current_temperature=Decimal("21.0"),
            temperature_updated_at=self.now,
        )

    def create_device(self, minimum_hours):
        return ShellyDevice.objects.create(
            familiar_name="Heater",
            shelly_api_key="device-key",
            shelly_device_name="device-id",
            user=self.user,
            status=1,
            run_hours_per_day=2,
            minimum_run_hours_per_day=minimum_hours,
            day_transfer_price=Decimal("0.0"),
            night_transfer_price=Decimal("0.0"),
            thermostat_device=self.thermostat,
        )

    def create_price(self, start_time, price):
        return ElectricityPrice.objects.create(
            start_time=start_time,
            end_time=start_time + timedelta(minutes=15),
            price_kwh=Decimal(price),
        )

    def test_above_max_keeps_assignment_for_minimum_cheapest_period(self):
        device = self.create_device(minimum_hours=1)
        next_price = self.create_price(self.next_start, "1.0")
        self.create_price(self.next_start + timedelta(minutes=15), "2.0")
        self.create_price(self.next_start + timedelta(minutes=30), "3.0")
        self.create_price(self.next_start + timedelta(minutes=45), "4.0")
        self.create_price(self.next_start + timedelta(minutes=60), "100.0")
        DeviceAssignment.objects.create(
            user=self.user,
            device=device,
            electricity_price=next_price,
        )

        with patch("app.thermostat_manager.TimeUtils.now_utc", return_value=self.now):
            ThermostatAssignmentManager.apply_next_period_assignments()

        self.assertTrue(
            DeviceAssignment.objects.filter(
                user=self.user,
                device=device,
                electricity_price=next_price,
            ).exists()
        )

    def test_above_max_unassigns_period_outside_minimum_cheapest_periods(self):
        device = self.create_device(minimum_hours=1)
        next_price = self.create_price(self.next_start, "100.0")
        self.create_price(self.next_start + timedelta(minutes=15), "1.0")
        self.create_price(self.next_start + timedelta(minutes=30), "2.0")
        self.create_price(self.next_start + timedelta(minutes=45), "3.0")
        self.create_price(self.next_start + timedelta(minutes=60), "4.0")
        DeviceAssignment.objects.create(
            user=self.user,
            device=device,
            electricity_price=next_price,
        )

        with patch("app.thermostat_manager.TimeUtils.now_utc", return_value=self.now):
            ThermostatAssignmentManager.apply_next_period_assignments()

        assignment = DeviceAssignment.objects.filter(
            user=self.user,
            device=device,
            electricity_price=next_price,
        ).first()
        self.assertIsNotNone(assignment)
        self.assertEqual(assignment.assignment_type, "removed_overheat")
