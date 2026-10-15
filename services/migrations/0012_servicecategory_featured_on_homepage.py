from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('marketplace', '0006_productcategory_featured_on_homepage'),
        ('services', '0011_move_services_to_cache_liqour'),
    ]

    operations = [
        migrations.AddField(
            model_name='servicecategory',
            name='featured_on_homepage',
            field=models.BooleanField(
                default=False,
                help_text='Show this category first on the home page. Selecting it clears the previous featured category.',
            ),
        ),
    ]
