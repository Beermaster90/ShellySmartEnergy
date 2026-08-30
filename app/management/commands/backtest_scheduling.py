"""
Replay stored electricity prices to see what block scheduling costs and saves.

For every local day in the window it plans that day twice — plain cheapest
periods, and block scheduling with the given settings — then reports the cost
premium and how much switching each one causes.

Run:
    python manage.py backtest_scheduling
    python manage.py backtest_scheduling --days 90 --switch-on-cost 2.0
    python manage.py backtest_scheduling --device 2 --by-month
"""

from collections import defaultdict
from datetime import timedelta
from decimal import Decimal

from django.core.management.base import BaseCommand

from app.models import ElectricityPrice, EVCharger, ShellyDevice
from app.price_views import LOCAL_TZ, plan_run_slots
from app.utils.time_utils import TimeUtils

SLOTS_PER_DAY = 96


class Command(BaseCommand):
    help = "Backtest block scheduling against stored electricity prices"

    def add_arguments(self, parser):
        parser.add_argument("--days", type=int, default=365,
                            help="How many days back to replay (default 365)")
        parser.add_argument("--device", type=int, default=None,
                            help="Only this ShellyDevice id")
        parser.add_argument("--switch-on-cost", type=float, default=None,
                            help="Override each device's switch-on cost (c/kWh)")
        parser.add_argument("--min-block", type=int, default=None,
                            help="Override the minimum run block (minutes)")
        parser.add_argument("--by-month", action="store_true",
                            help="Break the premium down per month")

    def handle(self, *args, **options):
        days = self.load_days(options["days"])
        if not days:
            self.stdout.write(self.style.ERROR("No stored prices to replay."))
            return

        self.stdout.write(
            f"Replaying {len(days)} full days: {min(days)} .. {max(days)}\n"
        )

        for target in self.targets(options["device"]):
            self.report(target, days, options)

    def load_days(self, days_back):
        """Group stored prices into local calendar days, keeping only full ones."""
        cutoff = TimeUtils.now_utc() - timedelta(days=days_back)
        rows = (
            ElectricityPrice.objects.filter(start_time__gte=cutoff)
            .order_by("start_time")
            .values("id", "start_time", "price_kwh")
        )
        grouped = defaultdict(list)
        for row in rows:
            local_day = TimeUtils.to_utc(row["start_time"]).astimezone(LOCAL_TZ).date()
            grouped[local_day].append(row)
        return {day: slots for day, slots in grouped.items() if len(slots) >= SLOTS_PER_DAY}

    def targets(self, device_id):
        devices = ShellyDevice.objects.all()
        if device_id:
            devices = devices.filter(device_id=device_id)
            return list(devices)
        return list(devices) + list(EVCharger.objects.all())

    def report(self, target, days, options):
        hours = target.run_hours_per_day
        if not hours:
            return

        switch_on_cost = options["switch_on_cost"]
        if switch_on_cost is None:
            switch_on_cost = float(target.switch_on_cost)
        min_block = options["min_block"] or target.min_run_block_minutes

        totals = defaultdict(Decimal)
        runs = defaultdict(int)
        slot_count = 0
        fallbacks = 0
        worst = (Decimal("0"), None)
        monthly = defaultdict(lambda: [Decimal("0"), Decimal("0")])

        for day, slots in sorted(days.items()):
            plan = plan_run_slots(
                slots,
                target.day_transfer_price,
                target.night_transfer_price,
                hours,
                block_scheduling=True,
                min_run_block_minutes=min_block,
                switch_on_cost=switch_on_cost,
            )
            totals["plain"] += plan.plain_cost
            totals["smoothed"] += plan.chosen_cost
            runs["plain"] += plan.plain_runs
            runs["smoothed"] += plan.chosen_runs
            slot_count += hours * 4
            fallbacks += int(plan.over_budget)
            if plan.premium_percent > worst[0]:
                worst = (plan.premium_percent, day)
            month = monthly[day.strftime("%Y-%m")]
            month[0] += plan.plain_cost
            month[1] += plan.chosen_cost

        n_days = len(days)
        premium = (totals["smoothed"] - totals["plain"]) / totals["plain"] * 100
        per_kwh = (totals["smoothed"] - totals["plain"]) / slot_count

        label = getattr(target, "familiar_name", str(target))
        self.stdout.write(self.style.MIGRATE_HEADING(
            f"{label} — {hours} h/day, switch-on cost {switch_on_cost} c/kWh, "
            f"min block {min_block} min"
        ))
        self.stdout.write(
            f"  cheapest periods : {totals['plain'] / slot_count:6.2f} c/kWh   "
            f"{runs['plain'] / n_days:5.2f} switch-ons/day"
        )
        self.stdout.write(
            f"  block scheduling : {totals['smoothed'] / slot_count:6.2f} c/kWh   "
            f"{runs['smoothed'] / n_days:5.2f} switch-ons/day"
        )
        self.stdout.write(
            f"  cost of smoothing: +{premium:.2f}%  (+{per_kwh:.3f} c/kWh)"
        )
        self.stdout.write(
            f"  worst single day : +{worst[0]:.1f}% on {worst[1]}   "
            f"budget fallbacks: {fallbacks}/{n_days}"
        )

        if options["by_month"]:
            for month in sorted(monthly):
                plain_cost, smoothed_cost = monthly[month]
                month_premium = (smoothed_cost - plain_cost) / plain_cost * 100
                self.stdout.write(f"    {month}  +{month_premium:5.2f}%")
        self.stdout.write("")
