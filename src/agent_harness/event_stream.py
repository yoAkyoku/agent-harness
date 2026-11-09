"""Bounded durable replay and live delivery; observers never own Run execution."""

import asyncio
import json

from starlette.websockets import WebSocket, WebSocketDisconnect

from .storage import Store, TERMINAL

PAGE_SIZE = 8
DELIVERY_TIMEOUT_SECONDS = 5


async def stream_run(websocket: WebSocket, store: Store, changed: asyncio.Condition,
                     run_id: str, after: int, require_ack: bool) -> None:
    acknowledged = after
    sent = after
    ack_changed = asyncio.Condition()
    send_lock = asyncio.Lock()
    closing = False

    async def close(code: int = 1000, reason: str = "") -> None:
        nonlocal closing
        try:
            async with asyncio.timeout(DELIVERY_TIMEOUT_SECONDS):
                async with send_lock:
                    if not closing:
                        closing = True
                        await websocket.close(code=code, reason=reason)
        except TimeoutError:
            pass

    async def sender() -> None:
        nonlocal sent
        cursor = after
        try:
            while True:
                close_code = None
                async with changed:
                    events = await store.events(run_id, cursor, limit=PAGE_SIZE)
                    while not events:
                        position = await store.event_position(run_id)
                        if position is None:
                            close_code = 4404
                            break
                        if position["last_sequence"] > cursor:
                            events = await store.events(run_id, cursor, limit=PAGE_SIZE)
                            continue
                        if position["status"] in TERMINAL:
                            close_code = 1000
                            break
                        await changed.wait()
                        events = await store.events(run_id, cursor, limit=PAGE_SIZE)
                if close_code is not None:
                    await close(close_code)
                    return
                for event in events:
                    async with asyncio.timeout(DELIVERY_TIMEOUT_SECONDS):
                        async with send_lock:
                            if closing:
                                return
                            sent = event["sequence"]
                            await websocket.send_json(event)
                    cursor = sent
                if require_ack:
                    async with asyncio.timeout(DELIVERY_TIMEOUT_SECONDS):
                        async with ack_changed:
                            await ack_changed.wait_for(lambda: acknowledged >= cursor)
        except TimeoutError:
            await close(1013, "slow_client")

    async def receiver() -> None:
        nonlocal acknowledged
        while True:
            message = await websocket.receive()
            if message["type"] == "websocket.disconnect":
                return
            if not require_ack:
                continue
            try:
                text = message.get("text", "")
                if not text or len(text) > 64:
                    raise ValueError
                value = json.loads(text)
                if not isinstance(value, dict) or set(value) != {"ack"} or type(value["ack"]) is not int:
                    raise ValueError
                if value["ack"] < acknowledged or value["ack"] > sent:
                    raise ValueError
            except (ValueError, TypeError):
                await close(4400, "invalid_ack")
                return
            async with ack_changed:
                acknowledged = value["ack"]
                ack_changed.notify_all()

    tasks = {asyncio.create_task(sender()), asyncio.create_task(receiver())}
    try:
        done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for task in done:
            task.result()
    except (WebSocketDisconnect, asyncio.CancelledError):
        pass
    finally:
        for task in tasks:
            task.cancel()
        try:
            await asyncio.gather(*tasks, return_exceptions=True)
        except asyncio.CancelledError:
            pass
