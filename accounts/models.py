from django.contrib.auth.models import AbstractUser, BaseUserManager
from django.db import models


class UserManager(BaseUserManager):
    """Create users whose email address is their single sign-on identity."""

    use_in_migrations = True

    def _create_user(self, email, password, **extra_fields):
        if not email:
            raise ValueError('An email address is required.')
        email = self.normalize_email(email)
        user = self.model(email=email, **extra_fields)
        user.set_password(password)
        user.save(using=self._db)
        return user

    def create_user(self, email, password=None, **extra_fields):
        extra_fields.setdefault('is_staff', False)
        extra_fields.setdefault('is_superuser', False)
        return self._create_user(email, password, **extra_fields)

    def create_superuser(self, email, password=None, **extra_fields):
        extra_fields.setdefault('is_staff', True)
        extra_fields.setdefault('is_superuser', True)
        if extra_fields.get('is_staff') is not True:
            raise ValueError('Superuser must have is_staff=True.')
        if extra_fields.get('is_superuser') is not True:
            raise ValueError('Superuser must have is_superuser=True.')
        return self._create_user(email, password, **extra_fields)


class User(AbstractUser):
    """A person with one account that may participate in several companies."""

    class Title(models.TextChoices):
        # The same titles a patient can choose.
        MR = 'MR', 'Mr'
        MRS = 'MRS', 'Mrs'
        MS = 'MS', 'Ms'
        MISS = 'MISS', 'Miss'
        DR = 'DR', 'Dr'
        PROF = 'PROF', 'Prof'

    username = None
    email = models.EmailField(unique=True)
    # Staff details, kept by a Super Admin in Users and roles and shown on My profile. A clinician can
    # change how they sign consult notes and their PathCare details there; everything else is read-only.
    title = models.CharField(max_length=8, choices=Title.choices, blank=True)
    contact_number = models.CharField(max_length=30, blank=True)
    hpcsa_number = models.CharField('HPCSA registration number', max_length=20, blank=True)
    note_signature = models.CharField('How consult notes are signed', max_length=120, blank=True,
                                      help_text='For example: S. Marchant, MBChB')
    # Printed on PathCare blood-test request forms as the referring doctor.
    pathcare_name = models.CharField('PathCare referring name', max_length=60, blank=True,
                                     help_text='As PathCare holds it: SURNAME,DR INITIALS, for example CZECH,DR J C.')
    pathcare_code = models.CharField('PathCare doctor code', max_length=20, blank=True, help_text='For example CZEJC0.')
    # When the person last set a new password themselves: from the emailed forgot-password link, and with Change password.
    last_forgot_password_reset = models.DateTimeField('Last forgot-password reset', null=True, blank=True, editable=False)
    last_change_password = models.DateTimeField('Last password change', null=True, blank=True, editable=False)

    USERNAME_FIELD = 'email'
    REQUIRED_FIELDS = []

    objects = UserManager()

    @property
    def full_name(self):
        return self.get_full_name() or self.email

# Create your models here.
