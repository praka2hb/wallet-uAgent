from uagents import Agent, Context
from uagents_core.contrib.protocols.chat import ChatMessage, ChatAcknowledgement, TextContent
from uuid import uuid4
from datetime import datetime

test_agent = Agent(
    name="test_client",
    seed="test_client_seed",
    port=8001,
)

@test_agent.on_event("startup")
async def send_query(ctx: Context):
    wallet = "8Xcfsp74GC3bHHTxq7RyX2n7iUwynB8Bs3yg2go1x2g3"
    await ctx.send(
        "agent1qdz8a9569fu9dagkattke6ax0j2ew34ha0k6dh990ktz086yr4ga6h7y07l",  # From wallet_agent.py startup log
        ChatMessage(
            timestamp=datetime.now(),
            msg_id=str(uuid4()),
            content=[TextContent(type="text", text=f"What's my wallet activity in the last 7 days?")]
        )
    )

@test_agent.on_message(ChatMessage)
async def handle_response(ctx: Context, sender: str, msg: ChatMessage):
    ctx.logger.info(f"Received summary: {msg.content[0].text}")

@test_agent.on_message(ChatAcknowledgement)
async def handle_ack(ctx: Context, sender: str, msg: ChatAcknowledgement):
    ctx.logger.info(f"Acknowledged: {msg.acknowledged_msg_id}")

if __name__ == "__main__":
    test_agent.run()