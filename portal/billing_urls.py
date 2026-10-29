from django.urls import path

from . import billing_views as views

urlpatterns = [
    path('patient/billing/', views.PatientBillingView.as_view(), name='patient-billing'),
    path('patient/billing/debit-order/', views.PatientDebitOrderView.as_view(), name='patient-debit-order'),
    path('patient/billing/cycles/<int:pk>/pay/', views.PatientCyclePayView.as_view(), name='patient-cycle-pay'),
    path('patient/billing/invoices/<int:pk>/', views.PatientInvoiceView.as_view(), name='patient-invoice'),
    path('patient/billing/invoices/<int:pk>/download/', views.PatientInvoiceDownloadView.as_view(), name='patient-invoice-download'),
    path('patient/billing/sandbox/mandate/<int:pk>/', views.PatientSandboxMandateView.as_view(), name='patient-billing-sandbox-mandate'),
    path('patient/billing/sandbox/checkout/<int:pk>/', views.PatientSandboxCheckoutView.as_view(), name='patient-billing-sandbox-checkout'),
    path('patient/billing/card/<str:kind>/<int:pk>/', views.PatientCardFormView.as_view(), name='patient-card-form'),
    path('patient/billing/return/<str:kind>/<int:pk>/', views.PatientPaymentReturnView.as_view(), name='patient-payment-return'),
    path('billing/webhooks/<str:name>/', views.PaymentWebhookView.as_view(), name='payment-webhook'),
    path('patient/orders/<int:pk>/pay/', views.PatientOrderPayView.as_view(), name='patient-order-pay'),
    path('patient/delivery-address/', views.PatientDeliveryAddressView.as_view(), name='patient-delivery-address'),
    path('billing/', views.BillingListView.as_view(), name='ops-billing'),
    path('billing/run/', views.DebitRunView.as_view(), name='ops-billing-run'),
    path('billing/debicheck-files/', views.DebiCheckFilesView.as_view(), name='ops-billing-debicheck'),
    path('billing/<int:pk>/', views.BillingDetailView.as_view(), name='ops-billing-detail'),
    path('shipping/<int:pk>/payment/', views.ParcelPaymentView.as_view(), name='ops-parcel-payment'),
]
