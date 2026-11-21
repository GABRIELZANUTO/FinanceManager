"""Backup of the whole database as a zipped JSON.

    python manage.py backup_drive              # send to Google Drive now (see app/drive_backup.py)
    python manage.py backup_drive --local DIR  # only write the zip in DIR (no Google)
    python manage.py backup_drive --status     # last backup and last error
"""
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError

from app import drive_backup


class Command(BaseCommand):
    help = "Backup diário do banco (JSON zipado) para o Google Drive."

    def add_arguments(self, parser):
        parser.add_argument("--local", metavar="PASTA", help="Só grava o zip nesta pasta, sem enviar.")
        parser.add_argument("--status", action="store_true", help="Mostra o último backup e o último erro.")

    def handle(self, *args, **options):
        if options["status"]:
            for key, value in drive_backup.status().items():
                self.stdout.write(f"{key}: {value}")
            return
        if options["local"]:
            folder = Path(options["local"])
            folder.mkdir(parents=True, exist_ok=True)
            name, content = drive_backup.build_zip()
            (folder / name).write_bytes(content)
            self.stdout.write(self.style.SUCCESS(f"{folder / name} ({len(content)} bytes)"))
            return
        try:
            summary = drive_backup.run_backup()
        except drive_backup.DriveError as exc:
            raise CommandError(str(exc)) from None
        self.stdout.write(self.style.SUCCESS(f"Enviado: {summary['name']} ({summary['bytes']} bytes)"))
        if summary["removed"]:
            self.stdout.write(f"Apagados por serem antigos: {', '.join(summary['removed'])}")
