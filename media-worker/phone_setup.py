"""One-time setup for Ask the Panel by phone: route calls to the panel's number.

Rent a number in LiveKit Cloud (Telephony → Phone numbers; every plan includes
one free US number), then run:

    docker compose run --rm media-worker python phone_setup.py +14155550123

This creates the dispatch rule that puts each call to that number in a fresh
`call-…` room and dispatches this worker's phone agent. Safe to re-run: an
existing rule of the same name is replaced.
"""
import asyncio
import sys

from livekit import api
from livekit.protocol import sip

from shared.config import get_settings

RULE_NAME = "ask-the-panel-phone"


async def setup(number: str):
    s = get_settings()
    lk = api.LiveKitAPI(s.livekit_url.replace("wss://", "https://", 1),
                        s.livekit_api_key, s.livekit_api_secret)
    try:
        existing = await lk.sip.list_sip_dispatch_rule(api.ListSIPDispatchRuleRequest())
        for rule in existing.items:
            if rule.name == RULE_NAME:
                await lk.sip.delete_sip_dispatch_rule(
                    api.DeleteSIPDispatchRuleRequest(sip_dispatch_rule_id=rule.sip_dispatch_rule_id))
                print(f"replaced existing rule {rule.sip_dispatch_rule_id}")
        rule = await lk.sip.create_sip_dispatch_rule(api.CreateSIPDispatchRuleRequest(
            dispatch_rule=sip.SIPDispatchRuleInfo(
                name=RULE_NAME,
                numbers=[number],
                rule=sip.SIPDispatchRule(dispatch_rule_individual=sip.SIPDispatchRuleIndividual(
                    room_prefix="call-")),
                room_config=api.RoomConfiguration(
                    agents=[api.RoomAgentDispatch(agent_name=s.phone_agent_name)]),
                # The panel session applies BVC telephony noise cancellation itself.
                krisp_enabled=False,
            )))
        print(f"created {rule.sip_dispatch_rule_id}: calls to {number} → "
              f"agent {s.phone_agent_name!r}")
    finally:
        await lk.aclose()


if __name__ == "__main__":
    if len(sys.argv) != 2 or not sys.argv[1].startswith("+"):
        sys.exit("usage: python phone_setup.py +<number in E.164, e.g. +14155550123>")
    asyncio.run(setup(sys.argv[1]))
