import hashlib
import secrets

from django.db import migrations, models
from django.utils import timezone


def backfill_active_delivery_codes(apps, schema_editor):
    Order = apps.get_model('orders', 'Order')
    database = schema_editor.connection.alias
    orders = Order.objects.using(database).filter(
        status__in=['accepted_delivery', 'picked', 'at_gate'],
        delivery_code__isnull=True,
    )

    for order in orders.iterator():
        code = str(secrets.randbelow(9000) + 1000)
        order.delivery_code = code
        order.delivery_code_hash = hashlib.sha256(code.encode('utf-8')).hexdigest()
        order.delivery_code_sent_at = timezone.now()
        order.save(
            using=database,
            update_fields=['delivery_code', 'delivery_code_hash', 'delivery_code_sent_at'],
        )


class Migration(migrations.Migration):

    dependencies = [
        ('orders', '0030_productorderitem'),
    ]

    operations = [
        migrations.AddField(
            model_name='order',
            name='delivery_code',
            field=models.CharField(blank=True, max_length=4, null=True),
        ),
        migrations.RunPython(backfill_active_delivery_codes),
    ]
