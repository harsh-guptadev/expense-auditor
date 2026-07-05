import os
import asyncio
from google.adk.models import Gemini
from google.genai import types
from app.config import config
from app.agent import communication_drafter

async def test():
    async for event in communication_drafter.run_async("Write a draft for approved dinner expense of $45."):
        print("Event type:", type(event))
        print("Event:", event)
        print("Event properties:", dir(event))
        if hasattr(event, "output"):
            print("event.output:", event.output)

asyncio.run(test())
