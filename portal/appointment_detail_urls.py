from django.urls import path
from .appointment_detail_views import (
    BookingTimesView, PatientAppointmentDetailView, StaffAppointmentDetailView, StaffAppointmentTimeView, StaffBookingView,
)

urlpatterns = [
    path('schedule/book/', StaffBookingView.as_view(), name='appointment-book'),
    path('schedule/book/times/', BookingTimesView.as_view(), name='appointment-book-times'),
    path('schedule/appointments/<int:pk>/', StaffAppointmentDetailView.as_view(), name='appointment-detail'),
    path('schedule/appointments/<int:pk>/time/', StaffAppointmentTimeView.as_view(), name='appointment-change-time'),
    path('patient/appointments/<int:pk>/', PatientAppointmentDetailView.as_view(), name='patient-appointment-detail'),
]
