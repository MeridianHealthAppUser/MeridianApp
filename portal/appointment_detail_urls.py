from django.urls import path
from .appointment_detail_views import (
    BookingTimesView, PatientAppointmentCancelView, PatientAppointmentDetailView, StaffAppointmentCalendarView, StaffAppointmentDetailView,
    StaffAppointmentTimeView, StaffBookingView,
)

urlpatterns = [
    path('schedule/book/', StaffBookingView.as_view(), name='appointment-book'),
    path('schedule/book/times/', BookingTimesView.as_view(), name='appointment-book-times'),
    path('schedule/appointments/<int:pk>/', StaffAppointmentDetailView.as_view(), name='appointment-detail'),
    path('schedule/appointments/<int:pk>/time/', StaffAppointmentTimeView.as_view(), name='appointment-change-time'),
    path('schedule/appointments/<int:pk>/calendar.ics', StaffAppointmentCalendarView.as_view(), name='appointment-calendar'),
    # The old address of a patient's appointment page (in emails already sent): My Appointments now.
    path('patient/appointments/<int:pk>/', PatientAppointmentDetailView.as_view(), name='patient-appointment-detail'),
    path('patient/appointments/<int:pk>/cancel/', PatientAppointmentCancelView.as_view(), name='patient-appointment-cancel'),
]
