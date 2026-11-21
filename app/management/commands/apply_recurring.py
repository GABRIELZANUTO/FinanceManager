from django.core.management.base import BaseCommand

from app import services


class Command(BaseCommand):
    help = "Lança todas as recorrências vencidas (pode rodar várias vezes, por exemplo diariamente pelo agendador)."

    def handle(self, *args, **options):
        created = services.apply_due()
        purged = services.purge_deleted()
        self.stdout.write(f"{created} transação(ões) criada(s); {purged} transação(ões) excluída(s) há mais de 30 dias removida(s) da lixeira.")
