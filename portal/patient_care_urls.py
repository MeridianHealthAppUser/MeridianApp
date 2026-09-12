"""Extend portal.urlpatterns so these names use the existing portal namespace."""

from django.urls import path
from .patient_care_views import (
    PatientAppointmentCalendarView, PatientBookingView, PatientMedicalProfileView, PatientUpdatesView,
)


urlpatterns = [
    path('patient/appointments/book/', PatientBookingView.as_view(), name='patient-book-appointment'),
    path('patient/appointments/<int:pk>/calendar.ics', PatientAppointmentCalendarView.as_view(), name='patient-appointment-calendar'),
    path('patient/medical-profile/', PatientMedicalProfileView.as_view(), name='patient-medical-profile'),
    path('patient/updates/', PatientUpdatesView.as_view(), name='patient-updates'),
]
