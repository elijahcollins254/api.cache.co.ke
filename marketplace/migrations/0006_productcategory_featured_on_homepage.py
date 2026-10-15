from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('marketplace', '0005_merchant_fulfillment_wallet'),
    ]

    operations = [
        migrations.AddField(
            model_name='productcategory',
            name='featured_on_homepage',
            field=models.BooleanField(
                default=False,
                help_text='Show this category first on the home page. Selecting it clears the previous featured category.',
            ),
        ),
    ]
