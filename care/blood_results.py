"""Blood results a clinician records by hand on the record's Blood results tab.

Most results are read on the PathCare portal; this is for the ones from another lab. The lab's PDF may be
attached. It is kept like the PathCare forms (care.patient_files) and served only after the record's checks.
"""

from datetime import date

from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone

from .clinical import _lock_context, _require_clinician, _text, validate_lab_pdf
from .models import BloodResult, PatientFile
from .patient_files import store_patient_file
from .services import record_audit


@transaction.atomic
def record_blood_result(*, company, patient, actor, taken_on, lab_name, results, report=None, request=None):
    """report: (filename, bytes) of the lab's PDF, or None."""
    company, patient = _lock_context(company, patient, actor)
    _require_clinician(company, actor)
    if not isinstance(taken_on, date) or taken_on > timezone.localdate():
        raise ValidationError('Enter the date the blood was taken. It can’t be in the future.')
    lab_name = ' '.join(_text(lab_name, label='The lab', maximum=120).split())
    results = _text(results, label='The results', maximum=5000).strip()
    file = None
    if report is not None:
        filename, content, _ = validate_lab_pdf(*report)
        file = store_patient_file(company=company, patient=patient, kind=PatientFile.Kind.BLOOD_RESULT,
                                  filename=filename, content=content, uploaded_by=actor)
    result = BloodResult(company=company, patient=patient, taken_on=taken_on, lab_name=lab_name, results=results,
                         recorded_by=actor, file=file)
    result.full_clean()
    result.save()
    record_audit(company=company, actor=actor, patient=patient, action='blood_result.recorded', target=result,
                 request=request, metadata={'report_attached': file is not None})
    return result
