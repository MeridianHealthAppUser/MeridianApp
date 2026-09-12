from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models
from django.utils.text import slugify


class TimeStampedModel(models.Model):
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        abstract = True


class Company(TimeStampedModel):
    """The tenancy boundary. A company represents a medical practice."""

    name = models.CharField(max_length=255)
    slug = models.SlugField(unique=True, max_length=80)
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ('name',)
        verbose_name_plural = 'companies'

    def __str__(self):
        return self.name

    def save(self, *args, **kwargs):
        if not self.slug:
            self.slug = slugify(self.name)
        super().save(*args, **kwargs)


class CompanyScopedQuerySet(models.QuerySet):
    def for_company(self, company):
        return self.filter(company=company)


class CompanyScopedModel(TimeStampedModel):
    """Base for all future clinical/business records that belong to one practice."""

    company = models.ForeignKey(
        Company,
        on_delete=models.PROTECT,
        related_name='%(app_label)s_%(class)s_records',
    )

    objects = CompanyScopedQuerySet.as_manager()

    class Meta:
        abstract = True


class CompanyMembership(TimeStampedModel):
    class Role(models.TextChoices):
        DOCTOR = 'doctor', 'Doctor'
        PRACTICE_ADMIN = 'practice_admin', 'Practice administrator'
        SUPER_ADMIN = 'super_admin', 'Super admin'

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='company_memberships',
    )
    company = models.ForeignKey(
        Company,
        on_delete=models.PROTECT,
        related_name='memberships',
    )
    role = models.CharField(max_length=32, choices=Role.choices)
    is_active = models.BooleanField(default=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=('user', 'company'), name='one_role_per_user_per_company'),
        ]
        indexes = [models.Index(fields=('user', 'company', 'is_active'))]
        ordering = ('company__name', 'user__email')

    def __str__(self):
        return f'{self.user} — {self.company} ({self.get_role_display()})'


class Patient(CompanyScopedModel):
    """A company-local patient record optionally linked to a portal sign-on."""

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='patient_records',
    )
    first_name = models.CharField(max_length=150)
    last_name = models.CharField(max_length=150)
    date_of_birth = models.DateField(null=True, blank=True)
    id_number = models.CharField(max_length=32, blank=True)
    phone = models.CharField(max_length=32, blank=True)
    city = models.CharField(max_length=120, blank=True)
    assigned_doctor = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='assigned_patients',
    )
    medical_record_number = models.CharField(max_length=64, blank=True)
    is_active = models.BooleanField(default=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=('company', 'medical_record_number'),
                condition=~models.Q(medical_record_number=''),
                name='unique_patient_record_number_per_company',
            ),
            models.UniqueConstraint(
                fields=('company', 'user'),
                condition=models.Q(user__isnull=False),
                name='one_patient_login_per_company',
            ),
            models.UniqueConstraint(
                fields=('company', 'id_number'),
                condition=~models.Q(id_number=''),
                name='unique_patient_id_number_per_company',
            ),
        ]
        indexes = [
            models.Index(fields=('company', 'last_name', 'first_name')),
        ]
        ordering = ('last_name', 'first_name')

    def __str__(self):
        return f'{self.first_name} {self.last_name}'

    def clean(self):
        super().clean()
        if self.assigned_doctor_id and self.company_id and not CompanyMembership.objects.filter(
            user_id=self.assigned_doctor_id,
            company_id=self.company_id,
            role=CompanyMembership.Role.DOCTOR,
            is_active=True,
        ).exists():
            raise ValidationError({
                'assigned_doctor': 'The assigned doctor must have an active doctor role in this practice.'
            })

# Create your models here.
