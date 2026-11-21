# Credit card module: installment numbers on recurring items and a history of imported invoices.

import decimal

import django.db.models.deletion
import django.utils.timezone
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ('app', '0008_portuguese_choice_labels'),
    ]

    operations = [
        migrations.AddField(
            model_name='recurringtransaction',
            name='installment_first',
            field=models.PositiveSmallIntegerField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name='recurringtransaction',
            name='installment_total',
            field=models.PositiveSmallIntegerField(blank=True, null=True),
        ),
        migrations.CreateModel(
            name='CardInvoice',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('issuer', models.CharField(blank=True, default='', max_length=60)),
                ('reference_date', models.DateField()),
                ('closing_date', models.DateField(blank=True, null=True)),
                ('due_date', models.DateField(blank=True, null=True)),
                ('total', models.DecimalField(blank=True, decimal_places=2, max_digits=12, null=True)),
                ('source', models.CharField(default='', max_length=20)),
                ('file_name', models.CharField(blank=True, default='', max_length=120)),
                ('lines_imported', models.PositiveIntegerField(default=0)),
                ('amount_imported', models.DecimalField(decimal_places=2, default=decimal.Decimal('0.00'), max_digits=12)),
                ('plans_created', models.PositiveIntegerField(default=0)),
                ('created_at', models.DateTimeField(default=django.utils.timezone.now, editable=False)),
                ('account', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='invoices', to='app.account')),
                ('user', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='card_invoices', to=settings.AUTH_USER_MODEL)),
            ],
            options={
                'ordering': ('-reference_date', '-id'),
            },
        ),
    ]
