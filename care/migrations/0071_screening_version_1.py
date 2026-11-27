from django.db import migrations
from django.utils import timezone

# A literal copy of care.screening_v1: a migration must not change when that module does.
_YES_NO = [
    {'value': 'yes', 'label': 'Yes', 'flag': False, 'reason': ''},
    {'value': 'no', 'label': 'No', 'flag': False, 'reason': ''},
]
_YES_NO_UNSURE = _YES_NO + [{'value': 'unsure', 'label': 'Not sure', 'flag': False, 'reason': ''}]
# Copy of Lead.HEARD_FROM as it stood when Version 1 was frozen.
_HEARD_FROM = (('google', 'Google (sponsored link)'), ('ai_overview', 'AI overview'), ('word_of_mouth', 'Word of mouth'),
               ('facebook', 'Facebook'), ('instagram', 'Instagram'))

VERSION_1_NOTE = 'Set up from the existing form'

VERSION_1_QUESTIONS = (
    {'key': 'adult', 'tier': 'core', 'answer_type': 'yes_no', 'label': 'Are you 18 or older?', 'help_text': '',
     'required': True, 'options': [dict(o) for o in _YES_NO], 'position': 1},
    {'key': 'weight_related_condition', 'tier': 'core', 'answer_type': 'yes_no',
     'label': 'Do you have a weight-related condition?',
     'help_text': 'High blood pressure, type 2 diabetes, sleep apnoea, fatty liver, high cholesterol.',
     'required': True, 'options': [dict(o) for o in _YES_NO], 'position': 2},
    {'key': 'pregnancy', 'tier': 'core', 'answer_type': 'yes_no',
     'label': 'Are you pregnant, breastfeeding, or planning a pregnancy within a year?', 'help_text': '',
     'required': True, 'options': [dict(o) for o in _YES_NO], 'position': 3},
    {'key': 'thyroid_history', 'tier': 'core', 'answer_type': 'yes_no_unsure',
     'label': 'Any personal or family history of medullary thyroid cancer or MEN2?', 'help_text': '',
     'required': True, 'options': [dict(o) for o in _YES_NO_UNSURE], 'position': 4},
    {'key': 'pancreatitis', 'tier': 'core', 'answer_type': 'yes_no', 'label': 'Have you ever had pancreatitis?',
     'help_text': '', 'required': True, 'options': [dict(o) for o in _YES_NO], 'position': 5},
    {'key': 'health_context', 'tier': 'core', 'answer_type': 'long_text', 'label': 'Chronic medication and allergies',
     'help_text': 'Include the names and doses of any regular medication, and any allergies. If none, say “None”.',
     'required': False, 'options': [], 'position': 90},
    {'key': 'heard_from', 'tier': 'core', 'answer_type': 'multiple', 'label': 'How did you hear about us?',
     'help_text': 'Optional. Tick all that apply.', 'required': False,
     'options': [{'value': v, 'label': l, 'flag': False, 'reason': ''} for v, l in _HEARD_FROM], 'position': 91},
)


def create_version_1(apps, schema_editor):
    Company = apps.get_model('practices', 'Company')
    Version = apps.get_model('care', 'ScreeningFormVersion')
    Question = apps.get_model('care', 'ScreeningQuestion')
    Questionnaire = apps.get_model('care', 'ScreeningQuestionnaire')
    for company in Company.objects.all():
        version = Version.objects.filter(company=company).order_by('number').first()
        if version is None:
            version = Version.objects.create(company=company, number=1, status='published',
                                             published_at=timezone.now(), note=VERSION_1_NOTE)
            Question.objects.bulk_create([
                Question(company=company, version=version, **{**q, 'options': [dict(o) for o in q['options']]})
                for q in VERSION_1_QUESTIONS
            ])
        Questionnaire.objects.filter(company=company, form_version__isnull=True).update(form_version=version)


class Migration(migrations.Migration):
    dependencies = [('care', '0070_screening_form_versions')]
    operations = [migrations.RunPython(create_version_1, migrations.RunPython.noop)]
