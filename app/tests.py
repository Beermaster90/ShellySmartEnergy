"""
This file demonstrates writing tests using the unittest module. These will pass
when you run "manage.py test".
"""

from datetime import UTC, datetime, timedelta
from decimal import Decimal
import json
import os
import tempfile
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
from app.device_override_manager import DeviceOverrideManager
from app.utils.time_utils import TimeUtils
from project.settings import _get_secret_key

# TODO: Configure your database in settings.py and sync before running tests.


class PersistentSecretKeyTest(SimpleTestCase):
    def test_configured_secret_key_file_survives_container_base_directory_change(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            key_path = os.path.join(temporary_directory, ".django_secret_key")
            with patch.dict(
                os.environ,
                {"DJANGO_SECRET_KEY_FILE": key_path},
                clear=False,
            ):
                os.environ.pop("DJANGO_SECRET_KEY", None)
                first_key = _get_secret_key("/first-image/app")

                # Simulate a new image/container process using the same /data file.
                os.environ.pop("DJANGO_SECRET_KEY", None)
                second_key = _get_secret_key("/second-image/app")

            self.assertEqual(first_key, second_key)
            self.assertTrue(os.path.exists(key_path))
            self.assertEqual(os.stat(key_path).st_mode & 0o777, 0o600)


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


class DeviceOverrideManagerTest(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="override-user", password="test-pass")
        self.device = ShellyDevice.objects.create(
            familiar_name="Override heater",
            shelly_api_key="device-key",
            shelly_device_name="device-id",
            user=self.user,
            status=1,
            run_hours_per_day=1,
            day_transfer_price=Decimal("0.0"),
            night_transfer_price=Decimal("0.0"),
        )
        self.now = datetime(2026, 1, 1, 10, 1, tzinfo=UTC)

    def create_prices(self, count):
        period_start = self.now.replace(minute=0, second=0, microsecond=0)
        return [
            ElectricityPrice.objects.create(
                start_time=period_start + timedelta(minutes=15 * index),
                end_time=period_start + timedelta(minutes=15 * (index + 1)),
                price_kwh=Decimal(str(count - index)),
            )
            for index in range(count)
        ]

    def test_scheduled_override_adds_normal_daily_run_hours(self):
        prices = self.create_prices(12)
        with patch.object(TimeUtils, "now_utc", return_value=self.now):
            until = DeviceOverrideManager.start_scheduled_override(self.device, 12)

        assignments = DeviceAssignment.objects.filter(
            device=self.device,
            assignment_type="scheduled_override",
        ).order_by("electricity_price__price_kwh")
        self.assertEqual(assignments.count(), 4)
        self.assertEqual(
            list(assignments.values_list("electricity_price_id", flat=True)),
            [price.id for price in reversed(prices[-4:])],
        )
        self.device.refresh_from_db()
        self.assertEqual(self.device.scheduled_override_until, until)
        self.assertFalse(self.device.force_on)
        self.assertFalse(self.device.force_off)

    def test_force_on_keeps_exactly_one_upcoming_period_queued(self):
        prices = self.create_prices(4)
        with patch.object(TimeUtils, "now_utc", return_value=self.now):
            DeviceOverrideManager.set_force_on(self.device, True)

        forced_ids = list(
            DeviceAssignment.objects.filter(
                device=self.device, assignment_type="force_on"
            ).values_list("electricity_price_id", flat=True)
        )
        self.assertEqual(forced_ids, [prices[0].id, prices[1].id])

        next_now = self.now + timedelta(minutes=15)
        DeviceOverrideManager.queue_force_on_periods(self.device, now_utc=next_now)
        self.assertEqual(
            set(DeviceAssignment.objects.filter(device=self.device).values_list("electricity_price_id", flat=True)),
            {prices[0].id, prices[1].id, prices[2].id},
        )

    def test_shorter_scheduled_override_removes_old_assignments_past_horizon(self):
        late_price = ElectricityPrice.objects.create(
            start_time=self.now + timedelta(hours=13),
            end_time=self.now + timedelta(hours=13, minutes=15),
            price_kwh=Decimal("1.0"),
        )
        DeviceAssignment.objects.create(
            user=self.user,
            device=self.device,
            electricity_price=late_price,
            assignment_type="scheduled_override",
        )

        with patch.object(TimeUtils, "now_utc", return_value=self.now):
            DeviceOverrideManager.start_scheduled_override(self.device, 12)

        self.assertFalse(
            DeviceAssignment.objects.filter(
                device=self.device, electricity_price=late_price
            ).exists()
        )

    def test_force_off_removes_current_and_one_upcoming_assignment(self):
        prices = self.create_prices(3)
        for price in prices:
            DeviceAssignment.objects.create(
                user=self.user,
                device=self.device,
                electricity_price=price,
                assignment_type="cheapest",
            )
        self.device.force_on = True
        self.device.save(update_fields=["force_on"])

        with patch.object(TimeUtils, "now_utc", return_value=self.now):
            DeviceOverrideManager.set_force_off(self.device, True)

        self.device.refresh_from_db()
        self.assertTrue(self.device.force_off)
        self.assertFalse(self.device.force_on)
        self.assertEqual(
            set(DeviceAssignment.objects.filter(device=self.device).values_list(
                "electricity_price_id", flat=True
            )),
            {prices[2].id},
        )

        DeviceOverrideManager.remove_force_off_periods(
            self.device, now_utc=self.now + timedelta(minutes=15)
        )
        self.assertFalse(DeviceAssignment.objects.filter(device=self.device).exists())

    def test_force_off_makes_an_assigned_running_device_turn_off(self):
        from app.tasks import DeviceController

        price = self.create_prices(1)[0]
        DeviceAssignment.objects.create(
            user=self.user,
            device=self.device,
            electricity_price=price,
            assignment_type="cheapest",
        )
        self.device.force_off = True
        self.device.save(update_fields=["force_off"])

        status = {
            "data": {"device_status": {"switch:0": {"output": True}}}
        }
        with (
            patch("app.tasks.ShellyService") as service_class,
            patch.object(DeviceController, "toggle_shelly_device_with_state") as toggle,
        ):
            service_class.return_value.get_device_status.return_value = status
            DeviceController._process_single_device(
                self.device, [price.id], price.start_time
            )

        toggle.assert_called_once_with(self.device, "off", True)

    def test_force_on_endpoint_cannot_change_another_users_device(self):
        other = User.objects.create_user(username="other-user", password="test-pass")
        self.client.force_login(other)
        response = self.client.post(
            "/shellyapp/toggle-force-device-on/",
            data=json.dumps({"device_id": self.device.device_id, "enabled": True}),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 404)
        self.device.refresh_from_db()
        self.assertFalse(self.device.force_on)

    def test_scheduled_override_endpoint_returns_assigned_price_ids(self):
        prices = self.create_prices(8)
        self.client.force_login(self.user)
        with patch.object(TimeUtils, "now_utc", return_value=self.now):
            response = self.client.post(
                "/shellyapp/set-scheduled-device-override/",
                data=json.dumps({
                    "device_id": self.device.device_id,
                    "duration_hours": 12,
                }),
                content_type="application/json",
            )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertTrue(payload["success"])
        self.assertEqual(
            set(payload["assigned_price_ids"]),
            set(
                DeviceAssignment.objects.filter(device=self.device).values_list(
                    "electricity_price_id", flat=True
                )
            ),
        )
        self.assertTrue(set(payload["assigned_price_ids"]).issubset({p.id for p in prices}))


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
