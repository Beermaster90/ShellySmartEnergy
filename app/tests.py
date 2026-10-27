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
    EVChargerAlwaysOnSlot,
    EVChargerAssignment,
    ShellyDevice,
    ShellyTemperature,
)
from app.price_views import (
    get_cheapest_hours,
    plan_run_slots,
    select_price_slots,
    set_cheapest_hours,
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

        self.assertEqual(fake_scheduler.add_job.call_count, 5)
        self.assertEqual(
            [call.kwargs["id"] for call in fake_scheduler.add_job.call_args_list],
            [
                "fetch_prices",
                "control_shelly",
                "control_ev_chargers",
                "thermostat_assignments",
                "prune_old_logs",
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

    def test_scheduled_override_books_every_period_in_window(self):
        prices = self.create_prices(12)
        beyond = ElectricityPrice.objects.create(
            start_time=self.now + timedelta(hours=12, minutes=14),
            end_time=self.now + timedelta(hours=12, minutes=29),
            price_kwh=Decimal("0.1"),
        )
        with patch.object(TimeUtils, "now_utc", return_value=self.now):
            until = DeviceOverrideManager.start_scheduled_override(self.device, 12)

        booked = set(
            DeviceAssignment.objects.filter(
                device=self.device, assignment_type="scheduled_override"
            ).values_list("electricity_price_id", flat=True)
        )
        # All periods, including the one already in progress, not only the cheapest.
        self.assertEqual(booked, {price.id for price in prices})
        self.assertNotIn(beyond.id, booked)
        self.device.refresh_from_db()
        self.assertEqual(self.device.scheduled_override_until, until)
        self.assertFalse(self.device.force_off)

    def test_scheduled_override_keeps_existing_and_reactivates_removed(self):
        prices = self.create_prices(3)
        DeviceAssignment.objects.create(
            user=self.user, device=self.device,
            electricity_price=prices[0], assignment_type="cheapest",
        )
        DeviceAssignment.objects.create(
            user=self.user, device=self.device,
            electricity_price=prices[1], assignment_type="removed_headroom",
        )
        with patch.object(TimeUtils, "now_utc", return_value=self.now):
            DeviceOverrideManager.start_scheduled_override(self.device, 12)

        types = dict(
            DeviceAssignment.objects.filter(device=self.device).values_list(
                "electricity_price_id", "assignment_type"
            )
        )
        self.assertEqual(types, {
            prices[0].id: "cheapest",
            prices[1].id: "scheduled_override",
            prices[2].id: "scheduled_override",
        })

    def test_override_bypasses_thermostat_max_temperature(self):
        from app.tasks import DeviceController

        thermostat = ShellyTemperature.objects.create(
            familiar_name="Hot room", shelly_api_key="k", shelly_device_name="t",
            user=self.user, max_temperature=Decimal("20"),
            current_temperature=Decimal("25"),
            temperature_updated_at=self.now,
        )
        self.device.thermostat_device = thermostat
        self.device.save(update_fields=["thermostat_device"])
        price = self.create_prices(1)[0]
        status = {"data": {"device_status": {"switch:0": {"output": False}}}}
        with patch.object(TimeUtils, "now_utc", return_value=self.now):
            DeviceOverrideManager.start_scheduled_override(self.device, 12)
            with (
                patch("app.tasks.ShellyService") as service_class,
                patch.object(DeviceController, "toggle_shelly_device_with_state") as toggle,
            ):
                service_class.return_value.get_device_status.return_value = status
                DeviceController._process_single_device(
                    self.device, [price.id], price.start_time
                )
        toggle.assert_called_once_with(self.device, "on", False)

    def test_cancel_override_removes_bookings_and_clears_until(self):
        self.create_prices(4)
        with patch.object(TimeUtils, "now_utc", return_value=self.now):
            DeviceOverrideManager.start_scheduled_override(self.device, 12)
            DeviceOverrideManager.cancel_scheduled_override(self.device)
        self.device.refresh_from_db()
        self.assertIsNone(self.device.scheduled_override_until)
        self.assertFalse(DeviceAssignment.objects.filter(device=self.device).exists())

    def test_one_hour_override_books_only_that_hour(self):
        prices = self.create_prices(8)
        with patch.object(TimeUtils, "now_utc", return_value=self.now):
            DeviceOverrideManager.start_scheduled_override(self.device, 1)
        booked = set(
            DeviceAssignment.objects.filter(device=self.device).values_list(
                "electricity_price_id", flat=True
            )
        )
        # now = 10:01, override until 11:01 → periods 10:00–11:00 (5 slots).
        self.assertEqual(booked, {price.id for price in prices[:5]})

    def test_unsupported_override_duration_is_rejected(self):
        with self.assertRaises(ValueError):
            DeviceOverrideManager.start_scheduled_override(self.device, 3)

    def test_dashboard_marks_whole_override_window_forced(self):
        from django.test import RequestFactory
        from app.views import get_common_context

        prices = self.create_prices(4)
        DeviceAssignment.objects.create(
            user=self.user, device=self.device,
            electricity_price=prices[1], assignment_type="cheapest",
        )
        with patch.object(TimeUtils, "now_utc", return_value=self.now):
            DeviceOverrideManager.start_scheduled_override(self.device, 1)
            request = RequestFactory().get("/")
            request.user = self.user
            context = get_common_context(request)

        key = str(self.device.device_id)
        forced = {p["id"] for p in context["prices"] if key in p["forced_devices"].split(",")}
        # The "cheapest" period inside the window is shown as forced too.
        self.assertEqual(forced, {price.id for price in prices})

    def test_apply_current_period_turns_override_device_on_now(self):
        from app.tasks import DeviceController

        self.create_prices(2)
        with patch.object(TimeUtils, "now_utc", return_value=self.now):
            DeviceOverrideManager.start_scheduled_override(self.device, 12)
            status = {"data": {"device_status": {"switch:0": {"output": False}}}}
            with (
                patch("app.tasks.ShellyService") as service_class,
                patch.object(DeviceController, "toggle_shelly_device_with_state") as toggle,
            ):
                service_class.return_value.get_device_status.return_value = status
                DeviceController.apply_current_period(self.device)

        toggle.assert_called_once_with(self.device, "on", False)

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
        self.device.scheduled_override_until = self.now + timedelta(hours=12)
        self.device.save(update_fields=["scheduled_override_until"])

        with patch.object(TimeUtils, "now_utc", return_value=self.now):
            DeviceOverrideManager.set_force_off(self.device, True)

        self.device.refresh_from_db()
        self.assertTrue(self.device.force_off)
        self.assertIsNone(self.device.scheduled_override_until)
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

    def test_override_endpoint_cannot_change_another_users_device(self):
        other = User.objects.create_user(username="other-user", password="test-pass")
        self.client.force_login(other)
        response = self.client.post(
            "/shellyapp/set-scheduled-device-override/",
            data=json.dumps({"device_id": self.device.device_id, "duration_hours": 12}),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 404)
        self.device.refresh_from_db()
        self.assertIsNone(self.device.scheduled_override_until)

    def test_scheduled_override_endpoint_returns_assigned_price_ids(self):
        prices = self.create_prices(8)
        self.client.force_login(self.user)
        with (
            patch.object(TimeUtils, "now_utc", return_value=self.now),
            patch("app.tasks.DeviceController.apply_current_period") as apply_now,
        ):
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
        apply_now.assert_called_once()


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


class SetCheapestHoursIgnoresCurrentTemperatureTest(TestCase):
    """set_cheapest_hours plans the whole day regardless of the current reading.

    A room reading above max right now doesn't mean it'll still be above max
    overnight — the day's cheap-hour plan should still be created. Per-period
    overheat/underheat handling is ThermostatAssignmentManager's job, run every
    15 minutes against the live reading (see ThermostatAssignmentManagerTest).
    """

    def test_creates_assignments_for_device_above_max_right_now(self):
        user = User.objects.create_user(username="cheapest-hours-user")
        now = TimeUtils.now_utc()
        thermostat = ShellyTemperature.objects.create(
            familiar_name="HotRoom",
            shelly_api_key="thermostat-key",
            user=user,
            min_temperature_winter=Decimal("18.0"),
            min_temperature_summer=Decimal("15.0"),
            max_temperature=Decimal("20.0"),
            current_temperature=Decimal("30.0"),  # well above max right now
            temperature_updated_at=now,
        )
        device = ShellyDevice.objects.create(
            familiar_name="Floors",
            shelly_api_key="device-key",
            shelly_device_name="device-id",
            user=user,
            status=1,
            run_hours_per_day=2,
            day_transfer_price=Decimal("0.0"),
            night_transfer_price=Decimal("0.0"),
            thermostat_device=thermostat,
        )
        for i in range(4):
            start = now + timedelta(hours=1, minutes=15 * i)
            ElectricityPrice.objects.create(
                start_time=start,
                end_time=start + timedelta(minutes=15),
                price_kwh=Decimal("1.0"),
            )

        set_cheapest_hours()

        self.assertTrue(DeviceAssignment.objects.filter(device=device).exists())


class BlockSchedulingTest(SimpleTestCase):
    """Block scheduling trades a little money for far fewer switch-ons."""

    START = datetime(2026, 1, 1, 0, 0, tzinfo=UTC)

    def build_prices(self, prices):
        return [
            {
                "id": index,
                "start_time": self.START + timedelta(minutes=15 * index),
                "price_kwh": Decimal(str(price)),
            }
            for index, price in enumerate(prices)
        ]

    def slot_indexes(self, slots):
        return sorted(int((slot - self.START).total_seconds() // 900) for slot in slots)

    def smoothed(self, prices, hours_needed=1, switch_on_cost=1.0, min_block=30):
        # These fixtures use far wider price spreads than real days, so the
        # budget is lifted out of the way; the guard has its own test below.
        return get_cheapest_hours(
            prices,
            0,
            0,
            hours_needed=hours_needed,
            block_scheduling=True,
            min_run_block_minutes=min_block,
            switch_on_cost=switch_on_cost,
            max_premium_percent=1000,
        )

    def test_without_block_scheduling_slots_are_scattered(self):
        # Cheap slots deliberately sit at 0, 4, 8 and 12 with expensive gaps.
        prices = self.build_prices([1, 9, 9, 9, 1, 9, 9, 9, 1, 9, 9, 9, 1, 9, 9, 9])

        slots = get_cheapest_hours(prices, 0, 0, hours_needed=1)

        self.assertEqual(self.slot_indexes(slots), [0, 4, 8, 12])

    def test_switch_on_cost_pulls_slots_into_one_run(self):
        # Slot 0 is the single cheapest period, but taking it costs an extra
        # switch-on that the neighbouring cheap hour does not.
        prices = self.build_prices([1, 9, 9, 9, 2, 2, 2, 2, 9, 9, 9, 9, 3, 3, 3, 3])

        slots = self.smoothed(prices)

        self.assertEqual(self.slot_indexes(slots), [4, 5, 6, 7])

    def test_a_big_enough_saving_still_wins_a_second_run(self):
        # Two clearly separate cheap valleys: worth paying to start twice.
        prices = self.build_prices([9, 9, 1, 1, 1, 1, 9, 9, 9, 9, 1, 1, 1, 1, 9, 9])

        slots = self.smoothed(prices, hours_needed=2)

        self.assertEqual(self.slot_indexes(slots), [2, 3, 4, 5, 10, 11, 12, 13])

    def test_zero_switch_on_cost_matches_plain_selection(self):
        prices = self.build_prices([1, 9, 9, 9, 1, 9, 9, 9, 1, 9, 9, 9, 1, 9, 9, 9])

        plain = get_cheapest_hours(prices, 0, 0, hours_needed=1)
        smoothed = self.smoothed(prices, switch_on_cost=0.0, min_block=15)

        self.assertEqual(self.slot_indexes(smoothed), self.slot_indexes(plain))

    def test_minimum_run_block_rules_out_lone_periods(self):
        # Slot 5 is an isolated cheap period a 30-minute minimum cannot use.
        prices = self.build_prices([9, 2, 2, 2, 9, 1, 9, 2, 2, 2, 9, 9, 9, 9, 9, 9])

        slots = self.smoothed(prices, switch_on_cost=0.0, min_block=30)

        self.assertNotIn(5, self.slot_indexes(slots))
        self.assertEqual(len(slots), 4)

    def test_does_not_join_a_run_across_a_price_gap(self):
        prices = self.build_prices([1, 1, 1, 1, 1, 1, 1, 1])
        # Drop slots 2 and 3 so slots 0-1 and 4-7 are not back-to-back.
        prices = [entry for entry in prices if entry["id"] not in (2, 3)]

        slots = self.smoothed(prices)

        self.assertEqual(self.slot_indexes(slots), [4, 5, 6, 7])

    def test_threshold_slots_are_still_added(self):
        prices = self.build_prices([9, 9, 1, 1, 1, 1, 9, 9, 0, 9, 9, 9, 9, 9, 9, 9])

        tagged = get_cheapest_hours(
            prices,
            0,
            0,
            hours_needed=1,
            price_threshold=Decimal("0.5"),
            block_scheduling=True,
            return_tagged=True,
            max_premium_percent=1000,
        )

        by_type = {}
        for slot, assignment_type in tagged:
            by_type.setdefault(assignment_type, []).append(slot)

        self.assertEqual(self.slot_indexes(by_type["cheapest"]), [2, 3, 4, 5])
        self.assertEqual(self.slot_indexes(by_type["threshold"]), [8])

    def test_smoothing_is_skipped_when_it_costs_too_much(self):
        # Scattered cheap slots with a brutal penalty for running between them:
        # smoothing here would blow far past the 5% budget, so it must not apply.
        prices = self.build_prices([1, 90, 90, 90, 1, 90, 90, 90, 1, 90, 90, 90, 1, 9, 9, 9])

        plan = plan_run_slots(
            prices, 0, 0, hours_needed=1, block_scheduling=True, switch_on_cost=50.0
        )

        self.assertTrue(plan.over_budget)
        self.assertFalse(plan.smoothed)
        self.assertEqual(self.slot_indexes(s for s, _t in plan.slots), [0, 4, 8, 12])

    def test_plan_reports_the_premium_it_paid(self):
        prices = self.build_prices([1, 9, 9, 9, 2, 2, 2, 2, 9, 9, 9, 9, 3, 3, 3, 3])

        plan = plan_run_slots(
            prices,
            0,
            0,
            hours_needed=1,
            block_scheduling=True,
            switch_on_cost=1.0,
            max_premium_percent=1000,
        )

        self.assertTrue(plan.smoothed)
        self.assertEqual(plan.chosen_runs, 1)
        self.assertLess(plan.chosen_runs, plan.plain_runs)
        self.assertGreater(plan.premium_percent, 0)

    def test_selection_is_cost_optimal_not_merely_greedy(self):
        # A greedy pass that grabs the single cheapest window first would take
        # slots 4-7 and then be unable to reach the true optimum.
        values = [5, 1, 1, 5, 1, 1, 1, 1, 5, 1, 1, 5, 9, 9, 9, 9]
        prices = self.build_prices(values)
        ordered = [
            (Decimal(str(entry["price_kwh"])), entry["start_time"]) for entry in prices
        ]
        periods, switch_cost, min_block = 8, Decimal("1.0"), 2

        chosen = select_price_slots(ordered, periods, switch_cost, min_block)
        indexes = self.slot_indexes(chosen)
        cost = sum(Decimal(str(values[i])) for i in indexes) + switch_cost * len(
            [i for n, i in enumerate(indexes) if n == 0 or i - indexes[n - 1] != 1]
        )

        # Brute force every legal selection and confirm the DP found the best.
        import itertools

        best = None
        for combo in itertools.combinations(range(len(values)), periods):
            runs, ok = [], True
            for index in combo:
                if runs and index == runs[-1][-1] + 1:
                    runs[-1].append(index)
                else:
                    runs.append([index])
            if any(len(run) < min_block for run in runs):
                continue
            total = sum(Decimal(str(values[i])) for i in combo) + switch_cost * len(runs)
            if best is None or total < best:
                best = total

        self.assertEqual(len(chosen), periods)
        self.assertEqual(cost, best)


class EVChargerBlockSchedulingTest(TestCase):
    """set_cheapest_hours honours the charger's own block scheduling setting."""

    def setUp(self):
        self.user = User.objects.create_user(username="ev-block-user")
        self.now = datetime(2026, 1, 1, 0, 0, tzinfo=UTC)
        # A realistic day: one isolated cheapest period, and a slightly dearer
        # but continuous cheap hour. Spreads are small enough that smoothing
        # stays inside the real cost guard.
        curve = [5.0, 6, 6, 6, 5.1, 5.1, 5.1, 5.1, 6, 6, 6, 6, 5.2, 6, 6, 6]
        for index, price in enumerate(curve):
            start_time = self.now + timedelta(minutes=15 * index)
            ElectricityPrice.objects.create(
                start_time=start_time,
                end_time=start_time + timedelta(minutes=15),
                price_kwh=Decimal(str(price)),
            )

    def create_charger(self, block_scheduling):
        return EVCharger.objects.create(
            familiar_name="Charger",
            user=self.user,
            run_hours_per_day=1,
            block_scheduling=block_scheduling,
            min_run_block_minutes=60,
            day_transfer_price=Decimal("0.0"),
            night_transfer_price=Decimal("0.0"),
        )

    def assigned_slot_indexes(self, charger):
        return sorted(
            int(
                (assignment.electricity_price.start_time - self.now).total_seconds()
                // 900
            )
            for assignment in EVChargerAssignment.objects.filter(
                charger=charger
            ).select_related("electricity_price")
        )

    def run_assignment(self):
        with patch("app.price_views.TimeUtils.now_utc", return_value=self.now):
            set_cheapest_hours()

    def test_block_scheduling_off_assigns_scattered_slots(self):
        charger = self.create_charger(block_scheduling=False)

        self.run_assignment()

        self.assertEqual(self.assigned_slot_indexes(charger), [0, 4, 5, 6])

    def test_block_scheduling_on_assigns_one_continuous_hour(self):
        charger = self.create_charger(block_scheduling=True)

        self.run_assignment()

        self.assertEqual(self.assigned_slot_indexes(charger), [4, 5, 6, 7])


class CloudflareAccessSSOTest(TestCase):
    """A verified Access JWT logs the matching user in; bad tokens do not."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        from cryptography.hazmat.primitives.asymmetric import rsa
        cls._key = rsa.generate_private_key(public_exponent=65537, key_size=2048)

    def setUp(self):
        self.user = User.objects.create_user(
            username="eero", password="x", email="eluotio@gmail.com"
        )
        self.team = "energypilot-team.cloudflareaccess.com"
        self.aud = "test-aud-tag"

    def _make_token(self, email="eluotio@gmail.com", aud=None, iss=None, exp_delta=3600):
        import jwt as pyjwt
        from datetime import datetime, timezone, timedelta
        now = datetime.now(timezone.utc)
        payload = {
            "email": email,
            "aud": aud or self.aud,
            "iss": iss or f"https://{self.team}",
            "iat": now,
            "exp": now + timedelta(seconds=exp_delta),
        }
        return pyjwt.encode(payload, self._key, algorithm="RS256")

    def _client_get(self, token=None):
        # Patch the JWKS lookup to return our local public key (no network).
        from unittest.mock import patch
        with self.settings(
            CF_ACCESS_TEAM_DOMAIN=self.team, CF_ACCESS_AUD=self.aud
        ), patch("app.cf_access._get_signing_key", return_value=self._key.public_key()):
            headers = {"HTTP_CF_ACCESS_JWT_ASSERTION": token} if token else {}
            # Middleware self-disables unless configured; instantiate per-request
            # via a fresh client so override_settings is picked up.
            from django.test import Client
            return Client().get("/shellyapp/", **headers, secure=True)

    def test_valid_token_logs_in_matching_user(self):
        resp = self._client_get(self._make_token())
        # Authenticated -> the dashboard renders (200), not a redirect to login.
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.wsgi_request.user.username, "eero")

    def test_no_token_is_not_logged_in(self):
        resp = self._client_get(None)
        self.assertFalse(resp.wsgi_request.user.is_authenticated)

    def test_wrong_audience_is_rejected(self):
        resp = self._client_get(self._make_token(aud="some-other-app"))
        self.assertFalse(resp.wsgi_request.user.is_authenticated)

    def test_expired_token_is_rejected(self):
        resp = self._client_get(self._make_token(exp_delta=-120))  # beyond 30s leeway
        self.assertFalse(resp.wsgi_request.user.is_authenticated)

    def test_unknown_email_is_not_provisioned(self):
        resp = self._client_get(self._make_token(email="stranger@example.com"))
        self.assertFalse(resp.wsgi_request.user.is_authenticated)
        self.assertFalse(User.objects.filter(email="stranger@example.com").exists())

    def test_plain_email_header_alone_does_not_authenticate(self):
        # Without a signed JWT, the plaintext identity header must be ignored.
        from unittest.mock import patch
        with self.settings(CF_ACCESS_TEAM_DOMAIN=self.team, CF_ACCESS_AUD=self.aud), \
             patch("app.cf_access._get_signing_key", return_value=self._key.public_key()):
            from django.test import Client
            resp = Client().get(
                "/shellyapp/",
                HTTP_CF_ACCESS_AUTHENTICATED_USER_EMAIL="eluotio@gmail.com",
                secure=True,
            )
        self.assertFalse(resp.wsgi_request.user.is_authenticated)

    def test_unreachable_certs_logs_team_domain_hint(self):
        # A stale team domain makes the certs URL 404; that must fail closed and
        # log a message pointing at CF_ACCESS_TEAM_DOMAIN, not just "HTTP 404".
        import jwt
        from unittest.mock import patch
        from app.cf_access import verify_access_jwt
        err = jwt.PyJWKClientConnectionError("HTTP Error 404: Not Found")
        with self.settings(CF_ACCESS_TEAM_DOMAIN=self.team, CF_ACCESS_AUD=self.aud), \
             patch("app.cf_access._get_signing_key", side_effect=err), \
             self.assertLogs("app.cf_access", level="ERROR") as logs:
            self.assertIsNone(verify_access_jwt(self._make_token()))
        self.assertIn("CF_ACCESS_TEAM_DOMAIN", logs.output[0])


class DeviceSettingsAccessTest(TestCase):
    """Owners edit their own devices in-app; they cannot touch others' or the admin."""

    def setUp(self):
        self.eero = User.objects.create_user("eero2", password="Epw!12345")
        self.kirsti = User.objects.create_user("kirsti2", password="Kpw!12345")
        self.eero_device = ShellyDevice.objects.create(
            familiar_name="Eero Heater", shelly_api_key="k", user=self.eero,
            status=1, run_hours_per_day=3,
            day_transfer_price=Decimal("1.0"), night_transfer_price=Decimal("1.0"),
        )
        self.kirsti_device = ShellyDevice.objects.create(
            familiar_name="Kirsti Heater", shelly_api_key="k", user=self.kirsti,
            status=1, run_hours_per_day=3,
            day_transfer_price=Decimal("1.0"), night_transfer_price=Decimal("1.0"),
        )

    def test_owner_can_open_own_device_settings(self):
        self.client.force_login(self.eero)
        resp = self.client.get(f"/shellyapp/settings/device/{self.eero_device.device_id}/")
        self.assertEqual(resp.status_code, 200)

    def test_owner_can_save_own_device_settings(self):
        self.client.force_login(self.eero)
        resp = self.client.post(
            f"/shellyapp/settings/device/{self.eero_device.device_id}/",
            {
                "familiar_name": "Eero Heater", "status": "1",
                "run_hours_per_day": "7", "minimum_run_hours_per_day": "0",
                "block_scheduling": "on", "switch_on_cost": "1.0",
                "min_run_block_minutes": "30",
                "day_transfer_price": "1.0", "night_transfer_price": "1.0",
                "auto_assign_price_threshold": "0", "relay_channel": "0",
                "shelly_server": "https://shelly-137-eu.shelly.cloud",
                "shelly_api_key": "k", "shelly_device_name": "x",
            },
        )
        self.assertEqual(resp.status_code, 302)
        self.eero_device.refresh_from_db()
        self.assertEqual(self.eero_device.run_hours_per_day, 7)

    def test_cannot_open_another_users_device(self):
        self.client.force_login(self.eero)
        resp = self.client.get(f"/shellyapp/settings/device/{self.kirsti_device.device_id}/")
        self.assertEqual(resp.status_code, 404)

    def test_cannot_save_another_users_device(self):
        self.client.force_login(self.eero)
        resp = self.client.post(
            f"/shellyapp/settings/device/{self.kirsti_device.device_id}/",
            {"familiar_name": "hijacked", "status": "1", "run_hours_per_day": "9",
             "minimum_run_hours_per_day": "0", "switch_on_cost": "1.0",
             "min_run_block_minutes": "30", "day_transfer_price": "1.0",
             "night_transfer_price": "1.0", "auto_assign_price_threshold": "0",
             "relay_channel": "0", "shelly_api_key": "k"},
        )
        self.assertEqual(resp.status_code, 404)
        self.kirsti_device.refresh_from_db()
        self.assertEqual(self.kirsti_device.familiar_name, "Kirsti Heater")

    def test_settings_list_only_shows_own_devices(self):
        self.client.force_login(self.eero)
        resp = self.client.get("/shellyapp/settings/")
        self.assertContains(resp, "Eero Heater")
        self.assertNotContains(resp, "Kirsti Heater")

    def test_settings_requires_login(self):
        resp = self.client.get("/shellyapp/settings/")
        self.assertEqual(resp.status_code, 302)
        self.assertIn("/login/", resp.url)


class DeviceSettingsAddTest(TestCase):
    """Owners can create devices in each category; new objects belong to them."""

    def setUp(self):
        self.eero = User.objects.create_user("eero3", password="Epw!12345")
        self.other = User.objects.create_user("other3", password="Opw!12345")
        self.client.force_login(self.eero)

    def test_add_device_is_owned_by_creator(self):
        resp = self.client.post("/shellyapp/settings/device/add/", {
            "familiar_name": "New Heater", "status": "1",
            "run_hours_per_day": "4", "minimum_run_hours_per_day": "0",
            "switch_on_cost": "1.0", "min_run_block_minutes": "30",
            "day_transfer_price": "2.0", "night_transfer_price": "1.0",
            "auto_assign_price_threshold": "0", "relay_channel": "0",
            "shelly_server": "https://example.com",
            "shelly_api_key": "abc", "shelly_device_name": "n",
        })
        self.assertEqual(resp.status_code, 302)
        d = ShellyDevice.objects.get(familiar_name="New Heater")
        self.assertEqual(d.user, self.eero)

    def test_add_device_ignores_client_supplied_owner(self):
        # Even if a user field is POSTed, ownership is forced to the creator.
        self.client.post("/shellyapp/settings/device/add/", {
            "familiar_name": "Forced Owner", "status": "1",
            "run_hours_per_day": "1", "minimum_run_hours_per_day": "0",
            "switch_on_cost": "1.0", "min_run_block_minutes": "30",
            "day_transfer_price": "1.0", "night_transfer_price": "1.0",
            "auto_assign_price_threshold": "0", "relay_channel": "0",
            "shelly_server": "https://example.com",
            "shelly_api_key": "abc", "user": self.other.id,
        })
        d = ShellyDevice.objects.get(familiar_name="Forced Owner")
        self.assertEqual(d.user, self.eero)

    def test_add_thermostat_is_owned_by_creator(self):
        resp = self.client.post("/shellyapp/settings/thermostat/add/", {
            "familiar_name": "New Thermostat",
            "min_temperature_winter": "18", "min_temperature_summer": "15",
            "max_temperature": "24", "headroom_winter": "0", "headroom_summer": "4",
            "shelly_server": "https://example.com",
            "shelly_api_key": "abc", "shelly_device_name": "t",
        })
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(ShellyTemperature.objects.get(familiar_name="New Thermostat").user, self.eero)

    def test_add_charger_is_owned_by_creator(self):
        resp = self.client.post("/shellyapp/settings/charger/add/", {
            "familiar_name": "New Charger", "status": "1",
            "charge_current_a": "6", "charge_current_reduced_a": "3",
            "run_hours_per_day": "5", "switch_on_cost": "1.0",
            "min_run_block_minutes": "30", "day_transfer_price": "1.0",
            "night_transfer_price": "1.0", "auto_assign_price_threshold": "0",
            "tuya_client_id": "", "tuya_client_secret": "", "tuya_device_id": "",
            "tuya_base_url": "https://openapi.tuyaeu.com",
            # A slot added on the create page is saved with the new charger.
            "always_on_slots-TOTAL_FORMS": "1", "always_on_slots-INITIAL_FORMS": "0",
            "always_on_slots-0-enabled": "on", "always_on_slots-0-start_time": "06:00",
            "always_on_slots-0-duration_hours": "1",
        })
        self.assertEqual(resp.status_code, 302)
        charger = EVCharger.objects.get(familiar_name="New Charger")
        self.assertEqual(charger.user, self.eero)
        self.assertEqual(charger.always_on_slots.count(), 1)

    def test_add_requires_login(self):
        self.client.logout()
        resp = self.client.get("/shellyapp/settings/device/add/")
        self.assertEqual(resp.status_code, 302)
        self.assertIn("/login/", resp.url)


class FieldEncryptionTest(TestCase):
    """Credential fields are ciphertext at rest but transparent plaintext in code."""

    def test_shelly_api_key_is_encrypted_in_db_but_plaintext_in_orm(self):
        from django.db import connection
        secret = "super-secret-shelly-key-12345"
        d = ShellyDevice.objects.create(
            familiar_name="Enc Test", shelly_api_key=secret, user=User.objects.create_user("enc1"),
            status=1, run_hours_per_day=1,
            day_transfer_price=Decimal("1.0"), night_transfer_price=Decimal("1.0"),
        )
        # ORM read returns plaintext (transparent decryption)
        self.assertEqual(ShellyDevice.objects.get(pk=d.pk).shelly_api_key, secret)
        # Raw column does NOT contain the plaintext
        with connection.cursor() as cur:
            cur.execute("SELECT shelly_api_key FROM app_shellydevice WHERE device_id=%s", [d.pk])
            raw = cur.fetchone()[0]
        self.assertNotEqual(raw, secret)
        self.assertNotIn(secret, raw)
        self.assertTrue(raw.startswith("gAAAAA"))  # Fernet token prefix

    def test_appsetting_value_encrypted(self):
        from django.db import connection
        from app.models import AppSetting
        AppSetting.objects.create(key="TEST_SECRET", value="my-api-key-value")
        self.assertEqual(AppSetting.objects.get(key="TEST_SECRET").value, "my-api-key-value")
        with connection.cursor() as cur:
            cur.execute("SELECT value FROM app_appsetting WHERE key=%s", ["TEST_SECRET"])
            raw = cur.fetchone()[0]
        self.assertNotIn("my-api-key-value", raw)


class NightPriceFallbackTest(TestCase):
    """When no price data exists, the device tops up its daily hours at night."""

    def setUp(self):
        from app.tasks import DeviceController
        self.DeviceController = DeviceController
        self.user = User.objects.create_user("fb-user")
        self.device = ShellyDevice.objects.create(
            familiar_name="Heater", shelly_api_key="k", user=self.user, status=1,
            run_hours_per_day=2, minimum_run_hours_per_day=0,
            day_transfer_price=Decimal("1.0"), night_transfer_price=Decimal("1.0"),
        )

    def at(self, hour, minute=0):
        # A UTC start_time; user tz defaults to Europe/Helsinki (UTC+2/+3).
        return datetime(2026, 1, 15, hour, minute, tzinfo=UTC)

    def test_runs_at_night_when_no_price_data(self):
        # 23:00 UTC = 01:00 Helsinki (night)
        self.assertTrue(self.DeviceController._apply_night_fallback(self.device, self.at(23, 0)))
        self.device.refresh_from_db()
        self.assertEqual(self.device.fallback_periods_today, 1)

    def test_does_not_run_during_day(self):
        # 10:00 UTC = 12:00 Helsinki (daytime)
        self.assertFalse(self.DeviceController._apply_night_fallback(self.device, self.at(10, 0)))

    def test_stops_at_daily_target(self):
        # target = run_hours 2 -> 8 periods. Simulate 8 already done tonight.
        from datetime import date
        self.device.fallback_date = date(2026, 1, 15)
        self.device.fallback_periods_today = 8
        self.device.save()
        # 23:00 UTC is still Jan 15 in Helsinki? 23:00 UTC = 01:00 Jan 16 Helsinki.
        # Use 21:00 UTC = 23:00 Helsinki, still Jan 15, night.
        self.assertFalse(self.DeviceController._apply_night_fallback(self.device, self.at(21, 0)))

    def test_counter_resets_on_new_day(self):
        from datetime import date
        self.device.fallback_date = date(2026, 1, 14)
        self.device.fallback_periods_today = 8
        self.device.save()
        self.assertTrue(self.DeviceController._apply_night_fallback(self.device, self.at(21, 0)))
        self.device.refresh_from_db()
        self.assertEqual(self.device.fallback_periods_today, 1)

    def test_disabled_flag_prevents_fallback(self):
        self.device.price_fallback_enabled = False
        self.device.save()
        self.assertFalse(self.DeviceController._apply_night_fallback(self.device, self.at(23, 0)))

    def test_force_off_prevents_fallback(self):
        self.device.force_off = True
        self.device.save()
        self.assertFalse(self.DeviceController._apply_night_fallback(self.device, self.at(23, 0)))

    def test_zero_target_does_nothing(self):
        self.device.run_hours_per_day = 0
        self.device.minimum_run_hours_per_day = 0
        self.device.save()
        self.assertFalse(self.DeviceController._apply_night_fallback(self.device, self.at(23, 0)))

    def test_minimum_hours_takes_precedence_as_target(self):
        self.device.minimum_run_hours_per_day = 1  # 4 periods
        self.device.fallback_date = None
        self.device.fallback_periods_today = 0
        self.device.save()
        for _ in range(4):
            self.assertTrue(self.DeviceController._apply_night_fallback(self.device, self.at(21, 0)))
        # 5th period exceeds the 1h (4-period) minimum target -> stop
        self.assertFalse(self.DeviceController._apply_night_fallback(self.device, self.at(21, 0)))


class EntsoeMaintenanceLogTest(TestCase):
    """A 503 from ENTSO-E logs one clear maintenance WARN, not a noisy error dump.

    Elering (the fallback source) is mocked to fail too here, so this covers
    the "both sources down" case. See EntsoeEleringFallbackTest for the case
    where Elering picks up the slack.
    """

    def _run(self):
        from unittest.mock import patch
        import requests
        from app.models import AppSetting
        AppSetting.objects.update_or_create(key="ENTSOE_API_KEY", defaults={"value": "test-key"})

        resp = requests.models.Response()
        resp.status_code = 503
        resp._content = b"<html><head><title>Service Temporarily Unavailable</title></head></html>"
        err = requests.exceptions.HTTPError("503 Server Error: Service Unavailable")
        err.response = resp

        with patch("app.price_views.EntsoeRawClient") as Client:
            Client.return_value.query_day_ahead_prices.side_effect = err
            with patch("app.price_views._fetch_elering_prices", side_effect=Exception("elering also down")):
                with patch("app.price_views.ElectricityPrice.objects") as prices:
                    prices.filter.return_value.exists.return_value = False
                    from app.price_views import call_fetch_prices
                    return call_fetch_prices(None)

    def test_maintenance_returns_503_and_logs_one_warn(self):
        from app.models import DeviceLog
        before = DeviceLog.objects.count()
        r = self._run()
        self.assertEqual(r.status_code, 503)
        logs = DeviceLog.objects.all()[before:] if False else DeviceLog.objects.order_by("-created_at")[:6]
        msgs = [(l.status, l.message) for l in logs]
        # exactly one maintenance WARN, and no ERROR-level noise about the fetch
        warns = [m for s, m in msgs if s == "WARN" and "maintenance" in m.lower()]
        errors = [m for s, m in msgs if s == "ERROR" and "ENTSOE" in m]
        self.assertTrue(warns, f"expected a maintenance WARN, got {msgs}")
        self.assertFalse(errors, f"did not expect ENTSOE ERROR logs, got {errors}")


class EntsoeEleringFallbackTest(TestCase):
    """When ENTSO-E fails, Elering (no API key needed) fills in and the fetch succeeds."""

    def _run(self):
        from unittest.mock import patch
        import requests
        import pandas as pd
        from app.models import AppSetting
        from app.utils.time_utils import TimeUtils
        AppSetting.objects.update_or_create(key="ENTSOE_API_KEY", defaults={"value": "test-key"})

        resp = requests.models.Response()
        resp.status_code = 503
        resp._content = b"<html><head><title>Service Temporarily Unavailable</title></head></html>"
        err = requests.exceptions.HTTPError("503 Server Error: Service Unavailable")
        err.response = resp

        # Points spanning well before/after "now", so whatever start/end
        # window call_fetch_prices computes falls inside this range.
        now_utc = TimeUtils.now_utc()
        index = pd.date_range(now_utc - pd.Timedelta(days=2), periods=4 * 24 * 5, freq="15min", tz="UTC")
        elering_series = pd.Series([50.0] * len(index), index=index)

        with patch("app.price_views.EntsoeRawClient") as Client:
            Client.return_value.query_day_ahead_prices.side_effect = err
            with patch("app.price_views._fetch_elering_prices", return_value=elering_series):
                with patch("app.price_views.set_cheapest_hours"):
                    with patch("app.tasks.DeviceController.control_shelly_devices"):
                        from app.price_views import call_fetch_prices
                        return call_fetch_prices(None)

    def test_fallback_success_returns_200_and_logs_warn_not_error(self):
        from app.models import DeviceLog
        r = self._run()
        self.assertEqual(r.status_code, 200)
        logs = DeviceLog.objects.order_by("-created_at")[:8]
        msgs = [(l.status, l.message) for l in logs]
        fallback_warns = [m for s, m in msgs if s == "WARN" and "elering" in m.lower()]
        errors = [m for s, m in msgs if s == "ERROR" and "ENTSOE" in m]
        self.assertTrue(fallback_warns, f"expected an Elering fallback WARN, got {msgs}")
        self.assertFalse(errors, f"did not expect ENTSOE ERROR logs, got {errors}")

    def test_fallback_saves_prices(self):
        from app.models import ElectricityPrice
        r = self._run()
        self.assertEqual(r.status_code, 200)
        self.assertTrue(ElectricityPrice.objects.exists())


class FetchEleringPricesTest(TestCase):
    """Unit coverage for the Elering response parser, independent of ENTSO-E."""

    def test_parses_finnish_prices_into_utc_series(self):
        from unittest.mock import patch, MagicMock
        from app.price_views import _fetch_elering_prices
        from app.utils.time_utils import TimeUtils

        payload = {
            "success": True,
            "data": {
                "fi": [
                    {"timestamp": 1735689600, "price": 45.0},
                    {"timestamp": 1735690500, "price": 47.5},
                ],
                "ee": [{"timestamp": 1735689600, "price": 99.0}],
            },
        }
        mock_response = MagicMock()
        mock_response.json.return_value = payload
        mock_response.raise_for_status.return_value = None

        with patch("app.price_views.requests.get", return_value=mock_response) as get:
            series = _fetch_elering_prices(TimeUtils.now_utc(), TimeUtils.now_utc())

        self.assertEqual(len(series), 2)
        self.assertEqual(list(series.values), [45.0, 47.5])
        self.assertTrue(str(series.index.tz) in ("UTC", "utc"))
        self.assertEqual(get.call_args.kwargs["params"]["start"][-1], "Z")

    def test_raises_when_no_finnish_data(self):
        from unittest.mock import patch, MagicMock
        from app.price_views import _fetch_elering_prices
        from app.utils.time_utils import TimeUtils

        mock_response = MagicMock()
        mock_response.json.return_value = {"success": True, "data": {"ee": []}}
        mock_response.raise_for_status.return_value = None

        with patch("app.price_views.requests.get", return_value=mock_response):
            with self.assertRaises(ValueError):
                _fetch_elering_prices(TimeUtils.now_utc(), TimeUtils.now_utc())


class EVNightPriceFallbackTest(TestCase):
    """EV charger tops up its daily hours at night with no price data — car permitting."""

    def setUp(self):
        from app.tasks import DeviceController
        self.DeviceController = DeviceController
        self.user = User.objects.create_user("ev-fb-user")
        self.charger = EVCharger.objects.create(
            familiar_name="Laturi", user=self.user, status=1, run_hours_per_day=2,
            day_transfer_price=Decimal("1.0"), night_transfer_price=Decimal("1.0"),
        )

    def at(self, hour, minute=0):
        return datetime(2026, 1, 15, hour, minute, tzinfo=UTC)

    def test_charges_at_night_when_no_price_data(self):
        # 21:00 UTC = 23:00 Helsinki (night). Caller guards car_connected; the
        # helper itself just applies night + target + counter.
        self.assertTrue(self.DeviceController._apply_night_fallback(self.charger, self.at(21, 0)))
        self.charger.refresh_from_db()
        self.assertEqual(self.charger.fallback_periods_today, 1)

    def test_not_during_day(self):
        self.assertFalse(self.DeviceController._apply_night_fallback(self.charger, self.at(10, 0)))

    def test_stops_at_target(self):
        from datetime import date
        self.charger.fallback_date = date(2026, 1, 15)
        self.charger.fallback_periods_today = 8   # 2h * 4
        self.charger.save()
        self.assertFalse(self.DeviceController._apply_night_fallback(self.charger, self.at(21, 0)))

    def test_disabled_flag(self):
        self.charger.price_fallback_enabled = False
        self.charger.save()
        self.assertFalse(self.DeviceController._apply_night_fallback(self.charger, self.at(21, 0)))

    def test_zero_target(self):
        self.charger.run_hours_per_day = 0
        self.charger.save()
        self.assertFalse(self.DeviceController._apply_night_fallback(self.charger, self.at(21, 0)))


class LogRetentionTest(TestCase):
    def test_prune_old_logs_keeps_recent_and_respects_setting(self):
        from app.models import AppSetting, DeviceLog
        from app.tasks import DeviceController

        now = datetime(2026, 1, 20, 12, 0, tzinfo=UTC)
        AppSetting.objects.update_or_create(key="LOG_RETENTION_DAYS", defaults={"value": "7"})
        old = DeviceLog.objects.create(message="old")
        recent = DeviceLog.objects.create(message="recent")
        DeviceLog.objects.filter(id=old.id).update(created_at=now - timedelta(days=8))
        DeviceLog.objects.filter(id=recent.id).update(created_at=now - timedelta(days=6))

        with patch.object(TimeUtils, "now_utc", return_value=now):
            DeviceController.prune_old_logs()

        self.assertFalse(DeviceLog.objects.filter(id=old.id).exists())
        self.assertTrue(DeviceLog.objects.filter(id=recent.id).exists())

    def test_invalid_retention_setting_falls_back_to_default(self):
        from app.models import AppSetting, DeviceLog
        from app.tasks import DeviceController

        now = datetime(2026, 1, 20, 12, 0, tzinfo=UTC)
        AppSetting.objects.update_or_create(key="LOG_RETENTION_DAYS", defaults={"value": "abc"})
        log = DeviceLog.objects.create(message="ten days")
        DeviceLog.objects.filter(id=log.id).update(created_at=now - timedelta(days=10))

        with patch.object(TimeUtils, "now_utc", return_value=now):
            DeviceController.prune_old_logs()

        self.assertTrue(DeviceLog.objects.filter(id=log.id).exists())


class EVAlwaysOnSlotTest(TestCase):
    """Always-on slots run the charger regardless of price, recorded only when due."""

    def setUp(self):
        from datetime import time
        from app.tasks import DeviceController
        self.DeviceController = DeviceController
        self.user = User.objects.create_user("ev-slot-user")
        self.charger = EVCharger.objects.create(
            familiar_name="Laturi", user=self.user, status=1, run_hours_per_day=0,
            day_transfer_price=Decimal("1.0"), night_transfer_price=Decimal("1.0"),
        )
        # 06:00 Helsinki for 1 h = 04:00-05:00 UTC in January.
        self.slot = EVChargerAlwaysOnSlot.objects.create(
            charger=self.charger, start_time=time(6, 0), duration_hours=1,
        )

    def at(self, hour, minute=0):
        return datetime(2026, 1, 15, hour, minute, tzinfo=UTC)

    def test_covers_window_and_midnight_wrap(self):
        from datetime import time
        slot = EVChargerAlwaysOnSlot(start_time=time(23, 30), duration_hours=2)
        self.assertTrue(slot.covers(datetime(2026, 1, 1, 23, 30)))
        self.assertTrue(slot.covers(datetime(2026, 1, 2, 1, 15)))
        self.assertFalse(slot.covers(datetime(2026, 1, 2, 1, 30)))
        self.assertFalse(slot.covers(datetime(2026, 1, 1, 23, 15)))

    def test_in_slot_uses_owner_local_time(self):
        self.assertTrue(self.DeviceController._in_always_on_slot(self.charger, self.at(4, 0)))
        self.assertTrue(self.DeviceController._in_always_on_slot(self.charger, self.at(4, 45)))
        self.assertFalse(self.DeviceController._in_always_on_slot(self.charger, self.at(5, 0)))
        self.assertFalse(self.DeviceController._in_always_on_slot(self.charger, self.at(6, 0)))

    def test_disabled_slot_is_ignored(self):
        self.slot.enabled = False
        self.slot.save()
        self.assertFalse(self.DeviceController._in_always_on_slot(self.charger, self.at(4, 0)))

    def _run_period(self, start):
        price = ElectricityPrice.objects.create(
            start_time=start, end_time=start + timedelta(minutes=15), price_kwh=Decimal("50"),
        )
        service = Mock()
        service.get_status.return_value = Mock(
            is_charging=False, work_state="charger_insert", connection_state="online",
            power_w=0, temp_c=10, session_energy_kwh=0, total_energy_kwh=0,
        )
        service.start_charging.return_value = True
        with patch("app.services.ev_charger_factory.get_ev_charger_service", return_value=service):
            self.DeviceController._process_single_ev_charger(self.charger, [price.id], start)
        return service, price

    def test_starts_charging_and_records_period_when_due(self):
        service, price = self._run_period(self.at(4, 15))
        service.start_charging.assert_called_once()
        a = EVChargerAssignment.objects.get(charger=self.charger, electricity_price=price)
        self.assertEqual(a.assignment_type, "always_on")

    def test_nothing_outside_slot(self):
        service, _ = self._run_period(self.at(8, 0))
        service.start_charging.assert_not_called()
        self.assertFalse(EVChargerAssignment.objects.filter(charger=self.charger).exists())


class EVAlwaysOnSlotSettingsTest(TestCase):
    """Slots are edited on the charger settings page, scoped to the owner."""

    def setUp(self):
        self.owner = User.objects.create_user("slot-owner", password="Opw!12345")
        self.other = User.objects.create_user("slot-other", password="Xpw!12345")
        self.charger = EVCharger.objects.create(
            familiar_name="Laturi", user=self.owner, status=1,
            day_transfer_price=Decimal("1.0"), night_transfer_price=Decimal("1.0"),
        )
        self.url = f"/shellyapp/settings/charger/{self.charger.pk}/"

    def _post(self, extra, total, initial=0):
        data = {
            "familiar_name": "Laturi", "status": "1",
            "charge_current_a": "6", "charge_current_reduced_a": "3",
            "run_hours_per_day": "0", "switch_on_cost": "1.0",
            "min_run_block_minutes": "30",
            "day_transfer_price": "1.0", "night_transfer_price": "1.0",
            "auto_assign_price_threshold": "0",
            "tuya_base_url": "https://openapi.tuyaeu.com",
            "always_on_slots-TOTAL_FORMS": str(total),
            "always_on_slots-INITIAL_FORMS": str(initial),
            "always_on_slots-MIN_NUM_FORMS": "0",
            "always_on_slots-MAX_NUM_FORMS": "24",
        }
        data.update(extra)
        return self.client.post(self.url, data)

    def test_owner_adds_and_removes_slots(self):
        self.client.force_login(self.owner)
        resp = self._post({
            "always_on_slots-0-enabled": "on",
            "always_on_slots-0-start_time": "06:30",
            "always_on_slots-0-duration_hours": "2",
            "always_on_slots-1-start_time": "07:00",
            "always_on_slots-1-duration_hours": "1",
        }, total=2)
        self.assertEqual(resp.status_code, 302)
        slots = list(self.charger.always_on_slots.all())
        self.assertEqual([(s.start_time.strftime("%H:%M"), s.duration_hours, s.enabled) for s in slots],
                         [("06:30", 2, True), ("07:00", 1, False)])

        # The edit page renders the saved time back in the 24 h select.
        self.assertContains(self.client.get(self.url), 'value="06:30" selected')

        first = slots[0]
        resp = self._post({
            "always_on_slots-0-id": str(first.pk),
            "always_on_slots-0-start_time": "06:30",
            "always_on_slots-0-duration_hours": "2",
            "always_on_slots-0-DELETE": "on",
        }, total=1, initial=1)
        self.assertEqual(resp.status_code, 302)
        self.assertFalse(EVChargerAlwaysOnSlot.objects.filter(pk=first.pk).exists())

    def test_rejects_off_grid_time_and_bad_duration(self):
        self.client.force_login(self.owner)
        resp = self._post({
            "always_on_slots-0-start_time": "06:10",
            "always_on_slots-0-duration_hours": "5",
        }, total=1)
        self.assertEqual(resp.status_code, 200)
        self.assertFalse(self.charger.always_on_slots.exists())

    def test_slot_count_is_capped(self):
        self.client.force_login(self.owner)
        extra = {}
        for i in range(25):
            extra[f"always_on_slots-{i}-start_time"] = "06:00"
            extra[f"always_on_slots-{i}-duration_hours"] = "1"
        resp = self._post(extra, total=25)
        self.assertEqual(resp.status_code, 200)
        self.assertFalse(self.charger.always_on_slots.exists())

    def test_other_user_cannot_edit_slots(self):
        self.client.force_login(self.other)
        resp = self._post({
            "always_on_slots-0-start_time": "06:00",
            "always_on_slots-0-duration_hours": "1",
        }, total=1)
        self.assertEqual(resp.status_code, 404)
        self.assertFalse(EVChargerAlwaysOnSlot.objects.exists())
