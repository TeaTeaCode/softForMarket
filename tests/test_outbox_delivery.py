import httpx
import pytest

from app.services.outbox.delivery import DeliveryOutcome, NonRetryableDeliveryError, classify_delivery_error


def http_error(status_code):
    request = httpx.Request("POST", "https://supplier.example/orders")
    response = httpx.Response(status_code, request=request)
    return httpx.HTTPStatusError(f"HTTP {status_code}", request=request, response=response)


@pytest.mark.parametrize("status_code", [408, 429, 500, 502, 503, 504])
def test_temporary_http_status_is_retried(status_code):
    assert classify_delivery_error(http_error(status_code)) == DeliveryOutcome.RETRY


def test_unexpected_redirect_is_retried():
    assert classify_delivery_error(http_error(302)) == DeliveryOutcome.RETRY


@pytest.mark.parametrize("status_code", [400, 401, 403, 404, 409, 422])
def test_final_client_error_is_dead(status_code):
    assert classify_delivery_error(http_error(status_code)) == DeliveryOutcome.DEAD


@pytest.mark.parametrize(
    "error",
    [
        httpx.ConnectError("connection failed"),
        httpx.ReadTimeout("supplier did not answer"),
        httpx.RemoteProtocolError("connection closed"),
        RuntimeError("unexpected error"),
    ],
)
def test_network_and_unknown_errors_are_retried(error):
    assert classify_delivery_error(error) == DeliveryOutcome.RETRY


def test_explicit_non_retryable_delivery_error_is_dead():
    assert classify_delivery_error(NonRetryableDeliveryError("invalid contract")) == DeliveryOutcome.DEAD
