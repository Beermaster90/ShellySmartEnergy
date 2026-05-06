"""
This file demonstrates writing tests using the unittest module. These will pass
when you run "manage.py test".
"""

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from unittest.mock import patch

import django
from django.contrib.auth.models import User
from django.test import TestCase

from app.models import (
    DeviceAssignment,
    ElectricityPrice,
    ShellyDevice,
    ShellyTemperature,
)
from app.thermostat_manager import ThermostatAssignmentManager

# TODO: Configure your database in settings.py and sync before running tests.

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
            min_temperature=Decimal("18.0"),
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

        self.assertFalse(
            DeviceAssignment.objects.filter(
                user=self.user,
                device=device,
                electricity_price=next_price,
            ).exists()
        )
