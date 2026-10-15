import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('marketplace', '0004_import_legacy_services_as_products'),
        ('orders', '0031_store_delivery_code'),
    ]

    operations = [
        migrations.CreateModel(
            name='MerchantOrderFulfillment',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('status', models.CharField(choices=[('pending', 'Pending acceptance'), ('accepted', 'Accepted'), ('prepared', 'Prepared for pickup')], db_index=True, default='pending', max_length=20)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('merchant', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='order_fulfillments', to='marketplace.merchantprofile')),
                ('order', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='merchant_fulfillments', to='orders.order')),
            ],
            options={
                'ordering': ['-updated_at'],
            },
        ),
        migrations.AddConstraint(
            model_name='merchantorderfulfillment',
            constraint=models.UniqueConstraint(fields=('merchant', 'order'), name='unique_merchant_order_fulfillment'),
        ),
        migrations.CreateModel(
            name='MerchantWallet',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('balance', models.DecimalField(decimal_places=2, default=0, max_digits=12)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('merchant', models.OneToOneField(on_delete=django.db.models.deletion.CASCADE, related_name='wallet', to='marketplace.merchantprofile')),
            ],
        ),
        migrations.CreateModel(
            name='MerchantWithdrawal',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('amount', models.DecimalField(decimal_places=2, max_digits=12)),
                ('phone_number', models.CharField(max_length=20)),
                ('status', models.CharField(choices=[('pending', 'Pending'), ('processing', 'Processing'), ('paid', 'Paid'), ('failed', 'Failed')], db_index=True, default='pending', max_length=20)),
                ('conversation_id', models.CharField(blank=True, db_index=True, max_length=128)),
                ('originator_conversation_id', models.CharField(blank=True, max_length=128)),
                ('transaction_reference', models.CharField(blank=True, max_length=128)),
                ('failure_reason', models.TextField(blank=True)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('merchant', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='withdrawals', to='marketplace.merchantprofile')),
            ],
            options={
                'ordering': ['-created_at'],
            },
        ),
        migrations.CreateModel(
            name='MerchantLedgerEntry',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('entry_type', models.CharField(choices=[('earning', 'Product sale'), ('withdrawal', 'Withdrawal'), ('refund', 'Withdrawal refund')], max_length=20)),
                ('amount', models.DecimalField(decimal_places=2, max_digits=12)),
                ('description', models.CharField(max_length=255)),
                ('created_at', models.DateTimeField(db_index=True, auto_now_add=True)),
                ('merchant', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='ledger_entries', to='marketplace.merchantprofile')),
                ('order', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name='merchant_earnings', to='orders.order')),
                ('withdrawal', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name='ledger_entries', to='marketplace.merchantwithdrawal')),
            ],
            options={
                'ordering': ['-created_at'],
            },
        ),
        migrations.AddConstraint(
            model_name='merchantledgerentry',
            constraint=models.UniqueConstraint(condition=models.Q(('entry_type', 'earning')), fields=('merchant', 'order'), name='unique_merchant_order_earning'),
        ),
        migrations.AddConstraint(
            model_name='merchantledgerentry',
            constraint=models.UniqueConstraint(condition=models.Q(('entry_type', 'withdrawal')), fields=('merchant', 'withdrawal'), name='unique_merchant_withdrawal_debit'),
        ),
        migrations.AddConstraint(
            model_name='merchantledgerentry',
            constraint=models.UniqueConstraint(condition=models.Q(('entry_type', 'refund')), fields=('merchant', 'withdrawal'), name='unique_merchant_withdrawal_refund'),
        ),
    ]