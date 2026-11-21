# Choice labels translated to Brazilian Portuguese. Only the display labels change;
# the stored values ("expense", "bank", ...) are the same, so no data is touched.

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('app', '0007_restrict_instead_of_protect'),
    ]

    operations = [
        migrations.AlterField(
            model_name='account',
            name='type',
            field=models.CharField(choices=[('bank', 'Banco'), ('cash', 'Dinheiro'), ('wallet', 'Carteira digital'), ('credit', 'Cartão de crédito')], default='bank', max_length=10),
        ),
        migrations.AlterField(
            model_name='category',
            name='color',
            field=models.CharField(choices=[('gray', 'Cinza'), ('blue', 'Azul'), ('teal', 'Verde-azulado'), ('green', 'Verde'), ('olive', 'Oliva'), ('amber', 'Âmbar'), ('red', 'Vermelho'), ('plum', 'Ameixa')], default='gray', max_length=10),
        ),
        migrations.AlterField(
            model_name='category',
            name='kind',
            field=models.CharField(choices=[('both', 'Receita e despesa'), ('expense', 'Só despesa'), ('income', 'Só receita')], default='both', max_length=7),
        ),
        migrations.AlterField(
            model_name='ledgerentry',
            name='kind',
            field=models.CharField(choices=[('lent', 'Emprestei dinheiro'), ('borrowed', 'Peguei dinheiro emprestado'), ('split_me', 'Paguei uma conta dividida'), ('split_them', 'A pessoa pagou uma conta dividida'), ('paid_me', 'A pessoa me pagou de volta'), ('paid_them', 'Paguei de volta')], max_length=10),
        ),
        migrations.AlterField(
            model_name='recurringtransaction',
            name='frequency',
            field=models.CharField(choices=[('monthly', 'Mensal'), ('weekly', 'Semanal')], default='monthly', max_length=7),
        ),
        migrations.AlterField(
            model_name='recurringtransaction',
            name='kind',
            field=models.CharField(choices=[('expense', 'Despesa'), ('income', 'Receita')], default='expense', max_length=7),
        ),
        migrations.AlterField(
            model_name='transaction',
            name='kind',
            field=models.CharField(choices=[('expense', 'Despesa'), ('income', 'Receita')], default='expense', max_length=7),
        ),
    ]
