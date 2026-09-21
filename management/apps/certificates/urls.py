from django.urls import path

from apps.certificates.apis import (
    AcmeAccountDetailApi,
    AcmeAccountListCreateApi,
    CertificateDetailApi,
    CertificateImportApi,
    CertificateListApi,
    CertificateRenewApi,
    CertificateRequestApi,
    CertificateSelfSignedApi,
    CertificateSummaryApi,
)

urlpatterns = [
    path("", CertificateListApi.as_view(), name="certificate-list"),
    path("summary/", CertificateSummaryApi.as_view(), name="certificate-summary"),
    path("request/", CertificateRequestApi.as_view(), name="certificate-request"),
    path("self-signed/", CertificateSelfSignedApi.as_view(), name="certificate-self-signed"),
    path("import/", CertificateImportApi.as_view(), name="certificate-import"),
    path("acme-accounts/", AcmeAccountListCreateApi.as_view(), name="acme-account-list-create"),
    path("acme-accounts/<int:account_id>/", AcmeAccountDetailApi.as_view(), name="acme-account-detail"),
    path("<int:certificate_id>/", CertificateDetailApi.as_view(), name="certificate-detail"),
    path("<int:certificate_id>/renew/", CertificateRenewApi.as_view(), name="certificate-renew"),
]
