"""Keep which batch went into each parcel: 'SG-2604, expiry 04/2027' from the stock batch it was allocated."""

from django.db import migrations


def forwards(apps, schema_editor):
    ShipmentItem = apps.get_model('care', 'ShipmentItem')
    for item in ShipmentItem.objects.filter(batch__isnull=False).select_related('batch').order_by('pk'):
        item.batch_note = f'{item.batch.batch_number}, expiry {item.batch.expires_on:%m/%Y}'[:120]
        item.save(update_fields=('batch_note',))


class Migration(migrations.Migration):
    dependencies = [('care', '0030_shipment_item_batch_note')]

    operations = [migrations.RunPython(forwards, migrations.RunPython.noop)]
