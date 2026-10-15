"""Pens are ordered for each week's parcels instead of kept in stock. A parcel line notes its batch and expiry
instead of pointing at a stock batch. 0031 copies the batch each line was allocated; 0032 removes stock."""

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [("care", "0029_remove_months_and_practice_pricing")]

    operations = [
        migrations.AddField(
            model_name="shipmentitem",
            name="batch_note",
            field=models.CharField(blank=True, max_length=120),
        ),
    ]
