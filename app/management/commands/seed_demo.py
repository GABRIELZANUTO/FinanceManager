"""Create a demo user with a year of realistic data. For throwaway databases only."""
from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError

from app import demo, services


class Command(BaseCommand):
    help = "Cria um usuário de demonstração com 12 meses de dados (só roda com DEBUG=1, a menos que use --force)."

    def add_arguments(self, parser):
        parser.add_argument("--username", default="demo")
        parser.add_argument("--password", default="demo-password-123")
        parser.add_argument("--seed", type=int, default=7)
        parser.add_argument("--force", action="store_true", help="roda mesmo com DEBUG desligado")

    def handle(self, *args, **opts):
        if not settings.DEBUG and not opts["force"]:
            raise CommandError("seed_demo só roda com DEBUG=1 (ou --force). Ele é feito para bancos de dados descartáveis.")
        User = get_user_model()
        user, created = User.objects.get_or_create(
            username=opts["username"], defaults={"email": "demo@example.com", "first_name": "Usuário", "last_name": "Demo"})
        if not created:
            raise CommandError(f"O usuário {opts['username']!r} já existe.")
        user.set_password(opts["password"])
        user.save()
        demo.seed_user(user, seed=opts["seed"])
        count = user.transactions.count()
        self.stdout.write(self.style.SUCCESS(
            f"Usuário de demonstração {opts['username']!r} criado com {count} transações. Saldo: {services.balance_for(user)}"))
