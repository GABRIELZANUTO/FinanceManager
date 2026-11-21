"""Credit cards without a closing day get the default: 7 days before the due day (from the card's
due day or, without one, from the last imported invoice). Same rule as planning.closing_day,
written down so the screen shows it and the person can change it.

Runs the same way on the site and on the phone, so both end with the same value."""
from django.db import migrations


def default_closing_day(due_day):
    return (due_day - 7 - 1) % 30 + 1


def fill(apps, schema_editor):
    Account = apps.get_model("app", "Account")
    CardInvoice = apps.get_model("app", "CardInvoice")
    for card in Account.objects.filter(type="credit", closing_day__isnull=True):
        due = card.due_day
        if not due:
            last = (CardInvoice.objects.filter(account=card, due_date__isnull=False)
                    .order_by("-due_date").values_list("due_date", flat=True).first())
            due = last.day if last else None
        if due:
            Account.objects.filter(pk=card.pk).update(closing_day=default_closing_day(due))


class Migration(migrations.Migration):
    dependencies = [("app", "0018_sync_tables")]

    operations = [migrations.RunPython(fill, migrations.RunPython.noop)]
