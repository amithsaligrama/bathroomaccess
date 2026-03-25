from django.core.management.base import BaseCommand

from bathroom_map.models import Bathroom


class Command(BaseCommand):
    help = "Backfill placeholder address/zip values from prior CSV imports"

    def add_arguments(self, parser):
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Show what would change without saving",
        )

    def handle(self, *args, **options):
        dry_run = options.get("dry_run", False)
        updated_address = 0
        updated_zip = 0

        qs = Bathroom.objects.all()
        for b in qs:
            changed = False

            # Replace generic placeholder with a better on-map label when available.
            if (b.address or "").strip() == "View on Google Maps":
                name = (b.name or "").strip()
                if name:
                    b.address = name
                    updated_address += 1
                    changed = True

            # NYC imports previously used 00000 fallback; use a real NYC zip placeholder.
            # Heuristic: only for records that look NYC-specific from name/remarks.
            if (b.zip or "").strip() == "00000":
                blob = "{} {}".format((b.name or ""), (b.remarks or "")).lower()
                nyc_signals = (
                    "nyc",
                    "nypl",
                    "bpl",
                    "qpl",
                    "new york",
                    "brooklyn",
                    "queens",
                    "manhattan",
                    "bronx",
                    "staten island",
                    "operator: nyc parks",
                )
                if any(sig in blob for sig in nyc_signals):
                    b.zip = "10001"
                    updated_zip += 1
                    changed = True

            if changed and not dry_run:
                b.save(update_fields=["address", "zip"])

        self.stdout.write("Address updates: {}".format(updated_address))
        self.stdout.write("NYC zip updates: {}".format(updated_zip))
        if dry_run:
            self.stdout.write("Dry run only; no records saved.")
