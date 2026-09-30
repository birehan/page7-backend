"""Unit tests for Zernio inbox conversation/message mapping."""

from __future__ import annotations

from app.integrations.social.zernio import _map_inbox_conversations, _map_inbox_messages


def test_map_inbox_conversations_accepts_updated_time_and_last_message() -> None:
    result = _map_inbox_conversations(
        {
            "data": [
                {
                    "_id": "conv_1",
                    "accountId": "acc_1",
                    "platform": "facebook",
                    "participantName": "Ada",
                    "participantUsername": "ada",
                    "updatedTime": "2024-11-02T08:00:00Z",
                    "lastMessage": "Hello there",
                    "unreadCount": 2,
                }
            ],
            "pagination": {"hasMore": True, "nextCursor": "cur_2"},
        }
    )
    assert len(result.conversations) == 1
    conv = result.conversations[0]
    assert conv.id == "conv_1"
    assert conv.last_message_at == "2024-11-02T08:00:00Z"
    assert conv.last_message_preview == "Hello there"
    assert conv.participant_handle == "ada"
    assert result.has_more is True
    assert result.next_cursor == "cur_2"


def test_map_inbox_messages_paginates_and_normalizes_direction() -> None:
    result = _map_inbox_messages(
        {
            "messages": [
                {
                    "id": "msg_1",
                    "direction": "incoming",
                    "text": "Hi",
                    "sender": {"name": "Ada", "username": "ada"},
                    "timestamp": "2024-11-02T08:00:00Z",
                },
                {
                    "id": "msg_2",
                    "direction": "outbound",
                    "message": "Thanks",
                    "createdAt": "2024-11-02T08:05:00Z",
                },
            ],
            "hasMore": False,
            "nextCursor": None,
        },
        conversation_id="conv_1",
        account_id="acc_1",
    )
    assert len(result.messages) == 2
    assert result.messages[0].direction == "incoming"
    assert result.messages[0].sender_name == "Ada"
    assert result.messages[1].direction == "outgoing"
    assert result.messages[1].text == "Thanks"
    assert result.has_more is False
