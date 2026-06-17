"""
Management command to backfill energy_kwh on EVChargerAssignment rows
using session totals from EVChargerEnergyLog.

For each detected charging session (identified by total_energy_kwh increasing),
the command finds the session start in the logs, locates all assignments that
fall within that window, and distributes the session kWh evenly across them.

Run:
    python manage.py backfill_ev_session_energy
    python manage.py backfill_ev_session_energy --dry-run
"""

from django.core.management.base import BaseCommand
from django.utils import timezone

from app.models import EVCharger, EVChargerAssignment, EVChargerEnergyLog, ElectricityPrice


class Command(BaseCommand):
    help = "Backfill energy_kwh on EV charger assignments from session log data"

    def add_arguments(self, parser):
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Print what would be updated without writing to the database",
        )

    def handle(self, *args, **options):
        dry_run = options["dry_run"]
        if dry_run:
            self.stdout.write("DRY RUN — no changes will be written\n")

        for charger in EVCharger.objects.all():
            self._backfill_charger(charger, dry_run)

    def _backfill_charger(self, charger, dry_run):
        self.stdout.write(f"\nCharger: {charger}")

        entries = list(
            EVChargerEnergyLog.objects.filter(charger=charger)
            .order_by("recorded_at")
            .values("recorded_at", "total_energy_kwh", "session_energy_kwh", "is_charging")
        )
        if not entries:
            self.stdout.write("  No energy logs found, skipping.")
            return

        # Detect session boundaries
        sessions = []
        session_start_time = None
        prev = None

        for e in entries:
            # Mark start of a charging session
            if e["is_charging"] and (prev is None or not prev["is_charging"]):
                session_start_time = e["recorded_at"]

            # Detect session end: total_energy_kwh increased
            if prev and float(e["total_energy_kwh"]) > float(prev["total_energy_kwh"]):
                session_kwh = float(e["session_energy_kwh"])
                if session_kwh > 0 and session_start_time:
                    sessions.append({
                        "start": session_start_time,
                        "end": e["recorded_at"],
                        "kwh": session_kwh,
                    })
                session_start_time = None  # reset for next session

            prev = e

        self.stdout.write(f"  Sessions detected: {len(sessions)}")

        total_updated = 0
        for s in sessions:
            # Find assignments whose price slot falls within the session window
            assignments = list(
                EVChargerAssignment.objects.filter(
                    charger=charger,
                    electricity_price__start_time__gte=s["start"],
                    electricity_price__start_time__lt=s["end"],
                )
            )

            if not assignments:
                self.stdout.write(
                    f"  {s['start'].strftime('%Y-%m-%d %H:%M')} → {s['end'].strftime('%H:%M')} "
                    f"({s['kwh']:.3f} kWh) — no matching assignments"
                )
                continue

            kwh_per_slot = s["kwh"] / len(assignments)
            slot_times = [a.electricity_price.start_time.strftime("%H:%M") for a in assignments]
            self.stdout.write(
                f"  {s['start'].strftime('%Y-%m-%d %H:%M')} → {s['end'].strftime('%H:%M')} "
                f"({s['kwh']:.3f} kWh) — {len(assignments)} slot(s) × {kwh_per_slot:.3f} kWh "
                f"[{', '.join(slot_times)}]"
            )

            if not dry_run:
                for a in assignments:
                    a.energy_kwh = round(kwh_per_slot, 3)
                    # Mark as confirmed charging (use charger default; no per-slot historical data)
                    if a.charge_current_a is None:
                        a.charge_current_a = charger.charge_current_a
                    a.save(update_fields=["energy_kwh", "charge_current_a"])
                total_updated += len(assignments)

        if not dry_run:
            self.stdout.write(f"  Updated {total_updated} assignment(s).")
        else:
            self.stdout.write("  (dry run — nothing written)")
