from app.core.logging import mask_secrets

# ─── маскировка секретов в логах ─────────────────────────────────────────────


def test_secrets_are_masked_in_logs():
    token = "eyJhbGciOiJIUzI1NiJ9.eyJleHAiOjIwMDAwMDAwMDB9.c2lnbmF0dXJl"
    record = {
        "message": (
            f"GET https://seller.ggsel.com/api_sellers/api/debates/v2?token={token}&id_i=55190855 -> 429 "
            f"params={{'token': '{token}'}} https://api.digiseller.com/api/x?token=abc123DEF "
            "POST https://api.telegram.org/bot123456:AAH-secret_x/sendMessage"
        )
    }
    mask_secrets(record)
    message = record["message"]

    assert token not in message
    assert "abc123DEF" not in message
    assert "AAH-secret_x" not in message
    assert "token=***&id_i=55190855" in message  # остальной URL на месте
    assert "/bot***/sendMessage" in message
