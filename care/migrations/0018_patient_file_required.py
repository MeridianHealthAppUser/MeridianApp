"""Every PathCare form and lab report now has its PatientFile; the inline PDF columns go.

A separate migration from 0017 so the copied rows are committed before these tables are altered.
"""

import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('care', '0017_patient_files'),
    ]

    operations = [
        migrations.RemoveField(
            model_name='pathcarerequestform',
            name='content',
        ),
        migrations.RemoveField(
            model_name='labresult',
            name='content',
        ),
        migrations.AlterField(
            model_name='pathcarerequestform',
            name='file',
            field=models.OneToOneField(on_delete=django.db.models.deletion.PROTECT, related_name='pathcare_form',
                                       to='care.patientfile'),
        ),
        migrations.AlterField(
            model_name='labresult',
            name='file',
            field=models.OneToOneField(on_delete=django.db.models.deletion.PROTECT, related_name='lab_result',
                                       to='care.patientfile'),
        ),
    ]
