"""Live Zernio comment.received payload shape (observed 2026-09-05)."""

from __future__ import annotations

from app.features.inbox.service import _normalize_inbound_payload


def test_normalize_live_comment_received_nested_shape() -> None:
    payload = {
        "id": "bdfa60b4-d6aa-44a8-933b-e5607689bc9e",
        "post": {
            "id": "6a9bcc1761e8159e5134301d",
            "content": "hello",
            "platformPostId": "1084309778106128_122136805935349599",
        },
        "event": "comment.received",
        "account": {
            "id": "6a95f22877555aae016679c0",
            "platform": "facebook",
            "username": "Zernio Dev",
            "accountId": "6a95f22877555aae016679c0",
        },
        "comment": {
            "id": "122136805935349599_1416484943760870",
            "text": "Thanks for asking!",
            "author": {
                "id": "1084309778106128",
                "name": "Zernio Dev",
                "isOwnAccount": True,
            },
            "postId": "6a9bcc1761e8159e5134301d",
            "isReply": False,
            "platform": "facebook",
        },
        "timestamp": "2026-09-05T08:00:55.488Z",
    }
    fields = _normalize_inbound_payload("comment.received", payload)
    assert fields["zernio_account_id"] == "6a95f22877555aae016679c0"
    assert fields["external_thread_id"] == "6a9bcc1761e8159e5134301d"
    assert fields["external_message_id"] == "122136805935349599_1416484943760870"
    assert fields["body"] == "Thanks for asking!"
    assert fields["participant_name"] == "Zernio Dev"
    assert fields["own_account"] is True
    assert fields["platform"] == "facebook"


def test_normalize_synthetic_flat_data_shape() -> None:
    payload = {
        "id": "evt_1",
        "type": "comment.received",
        "data": {
            "accountId": "acct_1",
            "postId": "post_1",
            "commentId": "cmt_1",
            "message": "hello",
            "authorName": "Sara",
            "authorHandle": "@sara",
        },
    }
    fields = _normalize_inbound_payload("comment.received", payload)
    assert fields["zernio_account_id"] == "acct_1"
    assert fields["external_thread_id"] == "post_1"
    assert fields["external_message_id"] == "cmt_1"
    assert fields["body"] == "hello"
    assert fields["own_account"] is False
