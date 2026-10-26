import time
from django.core.management.base import BaseCommand
from crm.mail.worker import run_once

class Command(BaseCommand):
    help = "Run the persistent Gmail scheduled-email worker."
    def add_arguments(self, parser):
        parser.add_argument("--poll-seconds", type=float, default=5)
        parser.add_argument("--batch-size", type=int, default=20)
        parser.add_argument("--once", action="store_true")
    def handle(self, *args, **options):
        delay=max(0.5,options["poll_seconds"]); batch=max(1,min(100,options["batch_size"]))
        self.stdout.write(self.style.SUCCESS("MONATA email worker started"))
        while True:
            count=run_once(batch)
            if count: self.stdout.write(f"Processed {count} scheduled email job(s).")
            if options["once"]: break
            time.sleep(delay)
