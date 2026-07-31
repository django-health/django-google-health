"""Delete expired mobile-OAuth state rows.

``start_mobile_flow`` clears a customer's *pending* rows when they start a new
flow, but consumed rows and rows belonging to customers who never retried are
left behind — one per connect attempt, useless after the TTL. Schedule this
like Django's own ``clearsessions``.
"""

from __future__ import annotations

from datetime import timedelta

from django.core.management.base import BaseCommand
from django.utils import timezone

from googlehealth.models import GoogleHealthOAuthState


class Command(BaseCommand):
    help = "Delete Google Health OAuth state rows whose expires_at is in the past."

    def add_arguments(self, parser) -> None:
        parser.add_argument(
            "--keep-days",
            type=int,
            default=0,
            help=(
                "Retain expired rows this many days past expiry (default 0). "
                "Useful when debugging a flow and you want the trail to survive."
            ),
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Report how many rows would be deleted without deleting them.",
        )

    def handle(self, *args, **options) -> None:
        cutoff = timezone.now() - timedelta(days=options["keep_days"])
        queryset = GoogleHealthOAuthState.objects.filter(expires_at__lt=cutoff)

        if options["dry_run"]:
            self.stdout.write(f"{queryset.count()} expired state row(s) would be deleted")
            return

        deleted, _ = queryset.delete()
        self.stdout.write(self.style.SUCCESS(f"Deleted {deleted} expired state row(s)"))
