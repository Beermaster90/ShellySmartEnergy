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
            "shelly_server": "https://shelly-137-eu.shelly.cloud",
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
            "shelly_server": "https://shelly-137-eu.shelly.cloud",
            "shelly_api_key": "abc", "user": self.other.id,
        })
        d = ShellyDevice.objects.get(familiar_name="Forced Owner")
        self.assertEqual(d.user, self.eero)

    def test_add_thermostat_is_owned_by_creator(self):
        resp = self.client.post("/shellyapp/settings/thermostat/add/", {
            "familiar_name": "New Thermostat",
            "min_temperature_winter": "18", "min_temperature_summer": "15",
            "max_temperature": "24", "headroom_winter": "0", "headroom_summer": "4",
            "shelly_server": "https://shelly-137-eu.shelly.cloud",
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
        })
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(EVCharger.objects.get(familiar_name="New Charger").user, self.eero)

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
