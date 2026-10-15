from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [("care", "0032_remove_stock")]

    operations = [
        migrations.AddField(
            model_name="practicesettings",
            name="shipping_weekday",
            field=models.PositiveSmallIntegerField(
                choices=[(0, "Monday"), (1, "Tuesday"), (2, "Wednesday"), (3, "Thursday"), (4, "Friday"), (5, "Saturday"),
                         (6, "Sunday")],
                default=1,
                verbose_name="Parcels go out on",
            ),
        ),
    ]
