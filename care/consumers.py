"""Private, content-free message notifications; HTTP remains the access boundary."""

import asyncio
from contextlib import suppress

from channels.db import database_sync_to_async
from channels.generic.websocket import AsyncJsonWebsocketConsumer
from django.conf import settings
from django.db import DatabaseError
from redis.exceptions import RedisError

from video.services import authenticated_socket_user
from .realtime import user_group


BACKEND_ERRORS = (DatabaseError, RedisError, OSError, asyncio.TimeoutError)


class MessageNotificationConsumer(AsyncJsonWebsocketConsumer):
    """Notify only the signed-in user, without trusting client-supplied user IDs.

    Every event is an invitation to fetch the current authorised HTTP view. No
    message bodies, patient identifiers or conversation membership travel here.
    This socket is independent of whether video calls are enabled.
    """

    async def connect(self):
        self.stopped = False
        self.heartbeat = None
        self.group = None
        if not settings.DEBUG and not settings.VIDEO_REDIS_URL:
            await self.end(4503)
            return
        try:
            user = await database_sync_to_async(authenticated_socket_user)(self.scope)
            if user is None:
                await self.end(4001)
                return
            self.group = user_group(user.pk)
            await self.join_group()
            await self.accept()
            await self.send_json({'type': 'ready'})
            self.heartbeat = asyncio.create_task(self.heartbeat_loop())
        except BACKEND_ERRORS:
            await self.end(4503)

    async def join_group(self):
        # Also renewed periodically: a Redis restart must not leave an open
        # browser silently unsubscribed forever.
        await asyncio.wait_for(self.channel_layer.group_add(self.group, self.channel_name), timeout=5)

    async def authenticated(self):
        if self.stopped:
            return False
        if await database_sync_to_async(authenticated_socket_user)(self.scope) is None:
            await self.end(4001)
            return False
        return True

    async def heartbeat_loop(self):
        try:
            while not self.stopped:
                await asyncio.sleep(25)
                if not await self.authenticated():
                    return
                await self.join_group()
                await self.send_json({'type': 'heartbeat'})
        except BACKEND_ERRORS:
            await self.end(4503)

    async def messages_changed(self, event):
        try:
            if await self.authenticated():
                # Do not forward arbitrary event payloads to the browser.
                await self.send_json({'type': 'messages.changed'})
        except BACKEND_ERRORS:
            await self.end(4503)

    async def receive(self, text_data=None, bytes_data=None, **kwargs):
        # Sending and reading messages use CSRF-protected HTTP requests.
        # Clients cannot subscribe to groups or publish events on this socket.
        await self.end(4008)

    async def cleanup(self):
        self.stopped = True
        task = self.heartbeat
        if task and task is not asyncio.current_task():
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task
        if self.group:
            with suppress(*BACKEND_ERRORS):
                await asyncio.wait_for(self.channel_layer.group_discard(self.group, self.channel_name), timeout=5)
            self.group = None

    async def end(self, code):
        await self.cleanup()
        await self.close(code=code)

    async def disconnect(self, close_code):
        await self.cleanup()
