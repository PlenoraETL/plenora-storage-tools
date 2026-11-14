"""Initialize cloud fixtures and prove each resource is usable before success."""
import sys
from urllib.request import Request
from fixture_readiness import FixtureFailure, exchange, status_failure, transport_failure, wait_ready


def gcs_attempt():
    exchange(Request('http://gcs:4443/storage/v1/b', data=b'{"name":"plenora-test"}',
                     headers={'Content-Type': 'application/json'}), 'create_bucket', allowed=(200, 201, 409))
    exchange(Request('http://gcs:4443/storage/v1/b/plenora-test',
                     headers={'Authorization': 'Bearer fixture-token'}), 'verify_bucket', missing=True)


def azure_attempt(client, exists_error, http_error, network_errors):
    phase = 'create_container'
    try:
        try:
            client.create_container('plenora-test')
        except exists_error as error:
            if error.error_code != 'ContainerAlreadyExists':
                raise FixtureFailure('container_conflict', phase, status=409) from None
        phase = 'verify_container'
        client.get_container_client('plenora-test').get_container_properties()
    except http_error as error:
        status = error.status_code
        if not isinstance(status, int):
            raise FixtureFailure('protocol', phase) from None
        raise status_failure(status, phase, missing=phase == 'verify_container') from None
    except network_errors as error:
        inner = getattr(error, 'inner_exception', None)
        if inner is not None:
            raise transport_failure(inner, phase) from None
        raise FixtureFailure('transport', phase, retry=True) from None


def initialize(service):
    if service == 'gcs':
        return wait_ready('gcs', gcs_attempt)
    if service != 'azure':
        raise ValueError('unknown fixture initializer')
    from azure.storage.blob import BlobServiceClient
    from azure.core.exceptions import ResourceExistsError, HttpResponseError, ServiceRequestError, ServiceResponseError
    # The bounded startup loop owns retries, including authenticated verification.
    with BlobServiceClient('http://azure:10000/devstoreaccount1', credential={
        'account_name': 'devstoreaccount1',
        'account_key': 'Eby8vdM02xNOcqFlqUwJPLlmEtlCDXJ1OUzFT50uSRZ6IFsuFq2UVErCz4I6tq/K1SZFPTOtr/KBHBeksoGMGw==',
    }, retry_total=0, connection_timeout=5, read_timeout=5) as client:
        return wait_ready('azure', lambda: azure_attempt(
            client, ResourceExistsError, HttpResponseError, (ServiceRequestError, ServiceResponseError)))


def main():
    service = sys.argv[1] if len(sys.argv) == 2 and sys.argv[1] in ('azure', 'gcs') else 'unknown'
    try:
        return 0 if initialize(service) else 1
    except Exception:
        # Client configuration/SDK errors must not expose addresses or bodies.
        print(FixtureFailure('configuration', 'client_setup').diagnostic(service, 1))
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
