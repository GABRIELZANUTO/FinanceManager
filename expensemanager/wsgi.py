"""
WSGI config for expensemanager project.

It exposes the WSGI callable as a module-level variable named ``application``.

For more information on this file, see
https://docs.djangoproject.com/en/4.1/howto/deployment/wsgi/
"""

import os

from django.core.wsgi import get_wsgi_application

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'expensemanager.settings')

application = get_wsgi_application()

# Daily backup to Google Drive (only when GDRIVE_* are set; see app/drive_backup.py).
from app.drive_backup import start_daily  # noqa: E402

start_daily()
