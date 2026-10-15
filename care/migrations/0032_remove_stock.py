"""Remove stock batches and the stock ledger: nothing waits on stock, and parcels keep their batch notes."""

from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [("care", "0031_shipment_item_batch_note_data")]

    operations = [
        migrations.RemoveField(model_name="shipmentitem", name="batch"),
        migrations.DeleteModel(name="StockMovement"),
        migrations.DeleteModel(name="MedicationBatch"),
    ]
