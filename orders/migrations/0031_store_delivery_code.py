from django.db import migrations, models


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
    ]
