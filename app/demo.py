"""Realistic sample data for one user (used by seed_demo and the "load sample data" button)."""
import datetime
import random
from decimal import Decimal

from django.db import transaction as db_transaction
from django.utils import timezone

from . import services
from .models import (
    Account, Budget, Category, CategoryRule, Contribution, Goal, LedgerEntry, Person, RecurringTransaction, Tag, Transaction,
    Transfer,
)

D = Decimal

PAYEES = {
    "Mercado": ["Carrefour", "Pão de Açúcar", "Feira livre"],
    "Transporte": ["Uber", "99", "Bilhete Único"],
    "Restaurantes": ["iFood", "Padaria Real", "Restaurante Sabor Caseiro"],
    "Compras": ["Mercado Livre", "Renner", "Livraria Cultura"],
    "Contas da casa": ["Enel", "Sabesp"],
    "Saúde": ["Drogasil", "Clínica Bem Estar"],
    "Presentes": ["Loja de Presentes"],
}


@db_transaction.atomic
def seed_user(user, seed=7):
    rng = random.Random(seed)
    today = timezone.localdate()
    services.set_up_new_user(user)
    main = services.default_account(user)
    if not main.transactions.exists():
        main.opening_balance = D("6000.00")
        main.save()
    def account(name, kind, opening="0"):
        found = Account.objects.filter(user=user, name__iexact=name).first()
        return found or Account.objects.create(user=user, name=name, type=kind, opening_balance=D(opening))

    def category(name, kind, color, icon, parent=None):
        found = Category.objects.filter(user=user, name__iexact=name).first()
        return found or Category.objects.create(user=user, name=name, kind=kind, color=color, icon=icon, parent=parent)

    cash = account("Dinheiro", "cash", "500.00")
    wallet = account("Carteira digital", "wallet", "250.00")
    credit = account("Cartão de crédito", "credit")
    savings = account("Poupança", "bank", "4000.00")

    cats = {c.name: c for c in user.categories.all()}
    for name, kind, color, icon in (("Internet", "expense", "blue", "wifi"), ("Freelance", "income", "olive", "briefcase"),
                                    ("Presentes", "expense", "plum", "gift")):
        cats[name] = category(name, kind, color, icon)
    cats["Café"] = category("Café", "expense", "amber", "coffee", cats["Restaurantes"])
    cats["Combustível"] = category("Combustível", "expense", "red", "fuel", cats["Transporte"])

    tags = {n: (Tag.objects.filter(user=user, name__iexact=n).first() or Tag.objects.create(user=user, name=n))
            for n in ("trabalho", "fim de semana", "essencial", "lazer")}
    for pattern, cat in (("carrefour", "Mercado"), ("uber", "Transporte"), ("mercado livre", "Compras"), ("padaria", "Café")):
        CategoryRule.objects.create(user=user, pattern=pattern, category=cats[cat])

    def add(day, name, kind, category, amount, payee="", account=main, tag_names=(), note="", recurring=None):
        if day > today:
            return None
        tx = Transaction.objects.create(
            user=user, name=name, kind=kind, category=cats[category], amount=D(amount).quantize(D("0.01")), date=day,
            payee=payee, account=account, note=note, recurring=recurring, recurring_date=day if recurring else None)
        if tag_names:
            tx.tags.set([tags[t] for t in tag_names])
        return tx

    rent = RecurringTransaction.objects.create(
        user=user, name="Aluguel do apartamento", amount=D("2200"), kind="expense", category=cats["Aluguel"], payee="Imobiliária",
        account=main, frequency="monthly", start_date=services.add_months(today.replace(day=1), -11),
        next_due=services.add_months(today.replace(day=1), 1))
    net = RecurringTransaction.objects.create(
        user=user, name="Plano de internet", amount=D("120"), kind="expense", category=cats["Internet"], payee="Vivo Fibra",
        account=main, frequency="monthly", start_date=services.add_months(today.replace(day=5), -11),
        next_due=today.replace(day=5) if today.day < 5 else services.add_months(today.replace(day=5), 1))
    weekly = RecurringTransaction.objects.create(
        user=user, name="Feira da semana", amount=D("120"), kind="expense", category=cats["Mercado"],
        payee="Feira livre", account=cash, frequency="weekly", start_date=today - datetime.timedelta(days=2),
        next_due=today + datetime.timedelta(days=5))
    RecurringTransaction.objects.create(
        user=user, name="Academia", amount=D("130"), kind="expense", category=cats["Saúde"],
        account=main, frequency="monthly", start_date=today.replace(day=1),
        next_due=services.add_months(today.replace(day=1), 1), active=False)
    add(today - datetime.timedelta(days=2), "Feira da semana", "expense", "Mercado", 120, "Feira livre", cash, recurring=weekly)

    first = services.add_months(today.replace(day=1), -11)
    for i in range(12):
        month = services.add_months(first, i)
        d = lambda n: month.replace(day=n)  # noqa: E731
        add(d(1), "Aluguel do apartamento", "expense", "Aluguel", 2200, "Imobiliária", recurring=rent, tag_names=("essencial",))
        add(d(5), "Plano de internet", "expense", "Internet", 120, "Vivo Fibra", recurring=net, tag_names=("essencial",))
        add(d(1), "Salário", "income", "Salário", 7200 + (i // 4) * 300, "Empresa")
        if rng.random() < 0.55:
            add(d(14), "Projeto freelance", "income", "Freelance", rng.randrange(800, 2600, 50), "Cliente", tag_names=("trabalho",))
        add(d(9), "Luz e água", "expense", "Contas da casa", rng.randrange(220, 420, 5), rng.choice(PAYEES["Contas da casa"]),
            tag_names=("essencial",))
        for _ in range(rng.randint(4, 6)):
            add(d(rng.randint(2, 27)), rng.choice(["Compra do mês", "Mercado", "Supermercado"]), "expense", "Mercado",
                rng.randrange(180, 620, 5), rng.choice(PAYEES["Mercado"]), rng.choice([main, main, cash]))
        for _ in range(rng.randint(2, 4)):
            add(d(rng.randint(2, 27)), rng.choice(["Corrida de Uber", "Recarga do bilhete", "Gasolina"]), "expense",
                rng.choice(["Transporte", "Combustível"]), rng.randrange(40, 260, 5), rng.choice(PAYEES["Transporte"]), cash)
        for _ in range(rng.randint(3, 6)):
            add(d(rng.randint(2, 27)), rng.choice(["Almoço com amigos", "Café", "Jantar fora"]), "expense",
                rng.choice(["Restaurantes", "Café", "Café"]), rng.randrange(15, 340, 5), rng.choice(PAYEES["Restaurantes"]),
                wallet, tag_names=rng.choice([(), ("fim de semana",), ("lazer",)]))
        if rng.random() < 0.6:
            add(d(rng.randint(3, 25)), rng.choice(["Roupas", "Fone de ouvido", "Livros"]), "expense", "Compras",
                rng.randrange(150, 900, 10), rng.choice(PAYEES["Compras"]), credit, tag_names=("lazer",))
        if rng.random() < 0.35:
            add(d(rng.randint(3, 25)), "Farmácia", "expense", "Saúde", rng.randrange(50, 350, 5), rng.choice(PAYEES["Saúde"]))
        if rng.random() < 0.25:
            add(d(rng.randint(3, 25)), "Presente de aniversário", "expense", "Presentes", rng.randrange(100, 400, 10), "Loja de Presentes")
        for day, target, amount, note in ((3, cash, "1300", "Saque para o mês"), (4, wallet, "950", "Recarga da carteira"),
                                          (6, savings, "1500", "Poupança do mês"), (28, credit, "300", "Pagamento do cartão")):
            if d(day) <= today:
                Transfer.objects.create(user=user, from_account=main, to_account=target, amount=D(amount), date=d(day), note=note)

    for back in range(0, 7):
        month = services.add_months(today.replace(day=1), -back)
        Budget.objects.update_or_create(user=user, category=None, month=month, defaults={"amount": D("6600"), "warn_percent": 85})
        for name, amount, roll in (("Mercado", 2400, True), ("Restaurantes", 900, False), ("Transporte", 800, False),
                                   ("Compras", 700, False), ("Contas da casa", 400, False), ("Internet", 120, False)):
            Budget.objects.update_or_create(user=user, category=cats[name], month=month, defaults={"amount": D(amount), "rollover": roll})

    goal = Goal.objects.create(user=user, name="Reserva de emergência", target_amount=D("15000"), account=savings,
                               target_date=today + datetime.timedelta(days=300))
    laptop = Goal.objects.create(user=user, name="Notebook novo", target_amount=D("6000"),
                                 target_date=today + datetime.timedelta(days=170))
    Goal.objects.create(user=user, name="Viagem para o Nordeste", target_amount=D("4500"), target_date=today + datetime.timedelta(days=90))
    for i in range(8):
        Contribution.objects.create(goal=goal, amount=D("800"), date=services.add_months(today.replace(day=15), -i) if i or today.day >= 15 else today)
    for i in range(3):
        Contribution.objects.create(goal=laptop, amount=D("600"), date=services.add_months(today.replace(day=20), -i - 1))
    trip = Goal.objects.get(user=user, name="Viagem para o Nordeste")
    Contribution.objects.create(goal=trip, amount=D("900"), date=today - datetime.timedelta(days=12))
    ana = Person.objects.create(user=user, name="Ana Souza")
    joao = Person.objects.create(user=user, name="João Lima")
    for person, kind, amount, back, note, due in (
            (ana, "lent", "600", 14, "Empréstimo para o curso", 20), (ana, "paid_me", "150", 6, "Pagou uma parte", None),
            (ana, "split_me", "120", 3, "Jantar, ela deve a metade", None), (joao, "borrowed", "250", 9, "Táxi e almoço", 12)):
        sign = 1 if kind in ("lent", "split_me", "paid_them") else -1
        LedgerEntry.objects.create(person=person, kind=kind, amount=sign * D(amount), description=note,
                                   date=today - datetime.timedelta(days=back),
                                   due_date=today + datetime.timedelta(days=due) if due else None)
    profile = services.get_profile(user)
    profile.setup_done = True
    profile.save(update_fields=["setup_done"])
    return user
