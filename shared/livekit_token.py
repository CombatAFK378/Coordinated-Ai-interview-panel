"""Room-scoped LiveKit tokens. API secrets stay on the server."""
from datetime import timedelta

from livekit import api

from shared.config import Settings


def build_room_token(settings: Settings, room: str, identity: str, *, agent=False) -> str:
    return (
        api.AccessToken(settings.livekit_api_key, settings.livekit_api_secret)
        .with_identity(identity)
        .with_name("Interview panel" if agent else identity)
        .with_ttl(timedelta(hours=1))
        .with_grants(api.VideoGrants(
            room_join=True, room=room, can_publish=True, can_subscribe=True,
            can_publish_data=True, agent=agent,
        ))
        .to_jwt()
    )
