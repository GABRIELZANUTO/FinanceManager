# Credit card purchases may be dated up to the invoice due date (see Transaction.clean).

import app.models
import django.utils.timezone
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('app', '0009_credit_card_invoices'),
    ]

    operations = [
        migrations.AlterField(
            model_name='transaction',
            name='date',
            field=models.DateField(default=django.utils.timezone.localdate, validators=[app.models.validate_transaction_date]),
        ),
    ]
