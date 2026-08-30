# app/urls.py

from django.urls import path
from . import views
from .shelly_views import fetch_device_status, toggle_device_output
from .price_views import call_fetch_prices
from .graph_views import graphs, get_graph_data, get_temperature_data, get_run_history_data
from .ev_charger_views import ev_charger_energy_history, ev_charger_refresh_status, ev_charger_monthly_cost, ev_charger_raw_dps
from django.contrib.auth import views as auth_views
from django.contrib.auth.decorators import login_required

urlpatterns = [
    path("", views.index, name="index"),  # /shellyapp/ calls index()
    path(
        "fetch-device-status/", fetch_device_status, name="fetch_device_status"
    ),  # Specific shelly view
    path(
        "toggle-device-output/", toggle_device_output, name="toggle_device_output"
    ),  # page to toggle shelly device output on / off]
    path(
        # Guarded here rather than on the function: the scheduler calls
        # call_fetch_prices(None) internally, with no request to authenticate.
        "fetch-prices/", login_required(call_fetch_prices), name="fetch_prices"
    ),  # fetch electricity prices
    path("graphs/", graphs, name="graphs"),  # Cost comparison graphs page
    path(
        "get-graph-data/", get_graph_data, name="get_graph_data"
    ),  # AJAX endpoint for cost comparison graph data
    path(
        "get-temperature-data/", get_temperature_data, name="get_temperature_data"
    ),  # AJAX endpoint for temperature chart data
    path(
        "get-run-history-data/", get_run_history_data, name="get_run_history_data"
    ),  # AJAX endpoint for device run history
    path(
        "admin-test/", views.admin_test_page, name="admin_test_page"
    ),  # Admin test page
    path(
        "toggle-assignment/", views.toggle_device_assignment, name="toggle_device_assignment"
    ),  # AJAX endpoint for toggling device assignments
    path(
        "toggle-device-status/", views.toggle_device_status, name="toggle_device_status"
    ),  # AJAX endpoint for toggling device automation status
    path(
        "set-scheduled-device-override/",
        views.set_scheduled_device_override,
        name="set_scheduled_device_override",
    ),
    path(
        "toggle-force-device-on/",
        views.toggle_force_device_on,
        name="toggle_force_device_on",
    ),
    path(
        "toggle-force-device-off/",
        views.toggle_force_device_off,
        name="toggle_force_device_off",
    ),
    path(
        "ev-charger-energy/", ev_charger_energy_history, name="ev_charger_energy_history"
    ),  # AJAX endpoint for EV charger energy history
    path(
        "ev-charger-refresh/", ev_charger_refresh_status, name="ev_charger_refresh_status"
    ),  # AJAX live-poll EV charger status
    path(
        "ev-charger-monthly-cost/", ev_charger_monthly_cost, name="ev_charger_monthly_cost"
    ),  # AJAX monthly cost for EV charger
    path(
        "ev-charger-raw-dps/", ev_charger_raw_dps, name="ev_charger_raw_dps"
    ),  # Debug: raw Tuya DP codes for EV charger
    # In-app settings so owners can add/edit their own devices without the admin.
    path("settings/", views.settings_home, name="settings_home"),
    path("settings/device/add/", views.settings_add_device, name="settings_add_device"),
    path("settings/thermostat/add/", views.settings_add_thermostat, name="settings_add_thermostat"),
    path("settings/charger/add/", views.settings_add_charger, name="settings_add_charger"),
    path("settings/device/<int:pk>/", views.settings_device, name="settings_device"),
    path("settings/thermostat/<int:pk>/", views.settings_thermostat, name="settings_thermostat"),
    path("settings/charger/<int:pk>/", views.settings_charger, name="settings_charger"),
]
