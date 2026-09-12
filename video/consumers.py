"""Authenticated one-to-one signalling. Media never passes through Django."""

import asyncio
import json
import time
import uuid
from collections import deque
from contextlib import suppress

from channels.db import database_sync_to_async
from channels.generic.websocket import AsyncWebsocketConsumer
from django.conf import settings
from django.db import DatabaseError
from redis.exceptions import RedisError

from .access import VideoAccessDenied, resolve_room_access
from .presence import PresenceUnavailable, presence_backend
from .services import authenticated_socket_user, record_presence


MAX_MESSAGE_BYTES = 72 * 1024
BACKEND_ERRORS = (PresenceUnavailable, DatabaseError, RedisError, OSError)


def validated_signal(kind, payload):
    if not isinstance(payload, dict):
        raise ValueError
    if kind in ('offer', 'answer'):
        sdp = payload.get('sdp')
        if payload.get('type') != kind or not isinstance(sdp, str) or not sdp.startswith('v=0') or len(sdp.encode()) > 64 * 1024:
            raise ValueError
        return {'type': kind, 'sdp': sdp}
    if kind == 'ice-candidate':
        candidate, mid, index = payload.get('candidate'), payload.get('sdpMid'), payload.get('sdpMLineIndex')
        if not isinstance(candidate, str) or len(candidate.encode()) > 2048 or (candidate and not candidate.startswith('candidate:')):
            raise ValueError
        if mid is not None and (not isinstance(mid, str) or len(mid) > 64):
            raise ValueError
        if index is not None and (type(index) is not int or not 0 <= index <= 255):
            raise ValueError
        fragment = payload.get('usernameFragment')
        if fragment is not None and (not isinstance(fragment, str) or len(fragment) > 256):
            raise ValueError
        return dict(candidate=candidate, sdpMid=mid, sdpMLineIndex=index, usernameFragment=fragment)
    if kind == 'media_state' and all(type(payload.get(key)) is bool for key in ('audio', 'video', 'screen')):
        return {key: payload[key] for key in ('audio', 'video', 'screen')}
    raise ValueError


class FreshSocket(AsyncWebsocketConsumer):
    async def fresh_user(self):
        return await database_sync_to_async(authenticated_socket_user)(self.scope)

    async def send_data(self, data):
        await self.send(text_data=json.dumps(data, separators=(',', ':')))

    def start_heartbeat(self):
        self.heartbeat_task = asyncio.create_task(self.heartbeat_loop())

    async def stop_heartbeat(self):
        task = getattr(self, 'heartbeat_task', None)
        if task and task is not asyncio.current_task():
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task

    def check_rate(self):
        now = time.monotonic()
        while self.received_times and self.received_times[0] <= now - 10:
            self.received_times.popleft()
        self.received_times.append(now)
        return len(self.received_times) <= 160


class VideoRoomConsumer(FreshSocket):
    async def connect(self):
        self.admitted = False
        self.stopped = False
        self.received_times = deque()
        self.backend = None
        if not getattr(settings, 'VIDEO_ENABLED', True):
            await self.close(code=4503)
            return
        try:
            user = await self.fresh_user()
        except DatabaseError:
            await self.close(code=4503)
            return
        if user is None:
            await self.close(code=4001)
            return
        try:
            self.appointment_id = int(self.scope['url_route']['kwargs']['appointment_id'])
            self.access = await database_sync_to_async(resolve_room_access)(user.pk, self.appointment_id)
            self.backend = presence_backend()
            self.user_id = user.pk
            self.connection_key = str(uuid.uuid4())
            self.room_group = f'video_appointment_{self.appointment_id}'
            # Join the delivery group before atomic admission so simultaneous
            # peers cannot lose a queued offer between admission and group_add.
            await self.channel_layer.group_add(self.room_group, self.channel_name)
            result = await self.backend.apply('join', self.appointment_id, self.user_id, self.channel_name, self.connection_key)
            if not result['accepted']:
                await self.channel_layer.group_discard(self.room_group, self.channel_name)
                await self.close(code=4004)
                return
            self.admitted = True
            self.room_epoch = result['state']['epoch']
            await database_sync_to_async(record_presence)(self.access, result['state'], result['removed'])
            await self.accept()
            if result['replaced']:
                await self.channel_layer.send(result['replaced'], {'type': 'room.replaced'})
            await self.publish_removed(result, skip_user=self.user_id)
            await self.channel_layer.group_send(self.room_group, {
                'type': 'room.peer', 'event': 'joined', 'from_user_id': self.user_id,
                'from_user_name': self.access.user_name, 'sender_channel': self.channel_name,
                'room_epoch': self.room_epoch,
            })
            await self.send_status(result['state'])
            ring = await self.backend.apply('ring', self.appointment_id, self.user_id, self.channel_name)
            if ring['ring']:
                await self.channel_layer.group_send(f'notify_{self.access.peer_id}', {
                    'type': 'notify.call', 'appointment_id': self.appointment_id,
                    'caller_id': self.user_id, 'caller_channel': self.channel_name,
                })
            self.start_heartbeat()
        except VideoAccessDenied:
            await self.end(4003)
        except BACKEND_ERRORS:
            await self.end(4503)

    async def send_status(self, state):
        await self.send_data({'type': 'room_status', 'peer_count': len(state['users']),
            'should_initiate': state.get('initiator') == str(self.user_id),
            'my_user_id': self.user_id, 'room_epoch': state['epoch']})

    async def current_access(self):
        user = await self.fresh_user()
        if user is None:
            await self.end(4001)
            return None
        try:
            self.access = await database_sync_to_async(resolve_room_access)(user.pk, self.appointment_id)
            return self.access
        except VideoAccessDenied:
            await self.end(4003)
            return None

    async def current_state(self, *, heartbeat=False):
        result = await self.backend.apply('heartbeat' if heartbeat else 'inspect', self.appointment_id,
                                          self.user_id, self.channel_name)
        member = result['state']['users'].get(str(self.user_id))
        if not member or member['channel'] != self.channel_name:
            await self.end(4005 if member else 4006)
            return None
        if heartbeat or result['removed']:
            await database_sync_to_async(record_presence)(self.access, result['state'], result['removed'])
        if result['removed']:
            await self.publish_removed(result)
        if result['state']['epoch'] != self.room_epoch:
            self.room_epoch = result['state']['epoch']
        return result['state']

    async def heartbeat_loop(self):
        try:
            while not self.stopped:
                remaining = self.access.join_closes_at.timestamp() - time.time()
                await asyncio.sleep(min(getattr(settings, 'VIDEO_HEARTBEAT_SECONDS', 15), max(.05, remaining + .01)))
                if not await self.current_access():
                    return
                state = await self.current_state(heartbeat=True)
                if state is None:
                    return
                await self.send_data({'type': 'pong', 'room_epoch': state['epoch'], 'peer_count': len(state['users'])})
        except BACKEND_ERRORS:
            await self.end(4503)

    async def receive(self, text_data=None, bytes_data=None):
        if self.stopped or not self.admitted:
            return
        if bytes_data is not None or text_data is None or len(text_data.encode()) > MAX_MESSAGE_BYTES or not self.check_rate():
            await self.end(4008)
            return
        try:
            data = json.loads(text_data)
        except (ValueError, RecursionError):
            await self.end(4008)
            return
        if not isinstance(data, dict):
            await self.end(4008)
            return
        try:
            if not await self.current_access():
                return
            state = await self.current_state()
            if state is None:
                return
            kind = data.get('type')
            if kind in ('ping', 'pong'):
                if kind == 'ping':
                    await self.send_data({'type': 'pong', 'room_epoch': state['epoch'], 'peer_count': len(state['users'])})
                return
            if kind not in ('offer', 'answer', 'ice-candidate', 'media_state'):
                return
            if data.get('room_epoch') != state['epoch']:
                await self.send_status(state)
                return
            if len(state['users']) != 2:
                return
            if kind == 'offer' and state.get('initiator') != str(self.user_id):
                return
            if kind == 'answer' and state.get('initiator') == str(self.user_id):
                return
            try:
                payload = validated_signal(kind, data.get('payload'))
            except ValueError:
                await self.end(4008)
                return
            await self.channel_layer.group_send(self.room_group, {
                'type': 'room.signal', 'signal_type': kind, 'payload': payload, 'from_user_id': self.user_id,
                'sender_channel': self.channel_name, 'room_epoch': state['epoch'],
            })
        except BACKEND_ERRORS:
            await self.end(4503)

    async def room_signal(self, event):
        if self.stopped or not self.admitted or event.get('sender_channel') == self.channel_name:
            return
        try:
            if not await self.current_access():
                return
            state = await self.current_state()
            sender = state['users'].get(str(event.get('from_user_id'))) if state else None
            if not sender or sender['channel'] != event.get('sender_channel') or event.get('room_epoch') != state['epoch']:
                return
            await self.send_data({'type': event['signal_type'], 'payload': event['payload'],
                'from_user_id': event['from_user_id'], 'room_epoch': state['epoch']})
        except BACKEND_ERRORS:
            await self.end(4503)

    async def room_peer(self, event):
        if self.stopped or not self.admitted or event.get('sender_channel') == self.channel_name:
            return
        try:
            if not await self.current_access():
                return
            state = await self.current_state()
            if state is None or state['epoch'] != event['room_epoch']:
                return
            await self.send_data({'type': f"peer_{event['event']}", 'from_user_id': event['from_user_id'],
                'from_user_name': event.get('from_user_name', ''), 'room_epoch': state['epoch']})
        except BACKEND_ERRORS:
            await self.end(4503)

    async def room_replaced(self, event):
        await self.end(4005)

    async def publish_removed(self, result, *, skip_user=None):
        for member in result['removed']:
            uid = int(member['user_id'])
            if uid == skip_user:
                continue
            await self.channel_layer.group_send(self.room_group, {
                'type': 'room.peer', 'event': 'left', 'from_user_id': uid,
                'from_user_name': self.access.peer_name if uid == self.access.peer_id else self.access.user_name,
                'sender_channel': member['channel'], 'room_epoch': result['state']['epoch'],
            })

    async def release(self):
        if not getattr(self, 'admitted', False):
            if hasattr(self, 'room_group'):
                with suppress(*BACKEND_ERRORS):
                    await self.channel_layer.group_discard(self.room_group, self.channel_name)
            return
        self.admitted = False
        try:
            result = await self.backend.apply('leave', self.appointment_id, self.user_id, self.channel_name)
            if result['accepted']:
                await database_sync_to_async(record_presence)(self.access, result['state'], result['removed'])
                await self.publish_removed(result)
                await self.channel_layer.group_send(f'notify_{self.access.peer_id}', {
                    'type': 'notify.cancelled', 'appointment_id': self.appointment_id, 'caller_id': self.user_id,
                })
        except BACKEND_ERRORS:
            pass  # Bounded leases expire independently if a worker/backend dies.
        finally:
            with suppress(*BACKEND_ERRORS):
                await self.channel_layer.group_discard(self.room_group, self.channel_name)

    async def end(self, code):
        if getattr(self, 'stopped', False):
            return
        self.stopped = True
        await self.stop_heartbeat()
        await self.release()
        await self.close(code=code)

    async def disconnect(self, close_code):
        self.stopped = True
        await self.stop_heartbeat()
        await self.release()
        if self.backend:
            with suppress(*BACKEND_ERRORS):
                await self.backend.close()


class NotificationConsumer(FreshSocket):
    async def notification_user(self):
        try:
            user = await self.fresh_user()
        except DatabaseError:
            await self.end(4503)
            return None
        if user is None:
            await self.end(4001)
        return user

    async def connect(self):
        self.stopped = False
        self.backend = None
        self.received_times = deque()
        if not getattr(settings, 'VIDEO_ENABLED', True):
            await self.close(code=4503)
            return
        try:
            user = await self.fresh_user()
        except DatabaseError:
            await self.close(code=4503)
            return
        if user is None:
            await self.close(code=4001)
            return
        try:
            self.backend = presence_backend()
            await self.backend.healthcheck()
            self.user_id = user.pk
            self.notify_group = f'notify_{user.pk}'
            await self.channel_layer.group_add(self.notify_group, self.channel_name)
            await self.accept()
            self.start_heartbeat()
        except BACKEND_ERRORS:
            await self.end(4503)

    async def heartbeat_loop(self):
        try:
            while not self.stopped:
                await asyncio.sleep(getattr(settings, 'VIDEO_HEARTBEAT_SECONDS', 15))
                if await self.fresh_user() is None:
                    await self.end(4001)
                    return
        except BACKEND_ERRORS:
            await self.end(4503)

    async def receive(self, text_data=None, bytes_data=None):
        if bytes_data is not None or text_data is None or len(text_data.encode()) > 256 or not self.check_rate():
            await self.end(4008)
            return
        if await self.notification_user() is None:
            return
        # This socket is receive-only; client content can never ring another user.

    async def notify_call(self, event):
        if self.stopped:
            return
        user = await self.notification_user()
        if user is None:
            return
        try:
            access = await database_sync_to_async(resolve_room_access)(user.pk, event.get('appointment_id'))
            if access.peer_id != event.get('caller_id'):
                return
            result = await self.backend.apply('inspect', access.appointment_id)
            caller = result['state']['users'].get(str(access.peer_id))
            if not caller or caller['channel'] != event.get('caller_channel'):
                return
            await self.send_data({'type': 'incoming_call', 'appointment_id': access.appointment_id,
                'booking_id': access.appointment_id, 'caller_name': access.peer_name,
                'practice_name': access.practice_name, 'room_url': access.room_url})
        except VideoAccessDenied:
            return
        except BACKEND_ERRORS:
            await self.end(4503)

    async def notify_cancelled(self, event):
        user = await self.notification_user()
        if user is None:
            return
        try:
            access = await database_sync_to_async(resolve_room_access)(user.pk, event.get('appointment_id'), require_window=False)
            if access.peer_id != event.get('caller_id'):
                return
            result = await self.backend.apply('inspect', access.appointment_id)
            if str(access.peer_id) not in result['state']['users']:
                await self.send_data({'type': 'call_cancelled', 'appointment_id': access.appointment_id})
        except VideoAccessDenied:
            return
        except BACKEND_ERRORS:
            await self.end(4503)

    async def end(self, code):
        self.stopped = True
        await self.stop_heartbeat()
        if hasattr(self, 'notify_group'):
            with suppress(*BACKEND_ERRORS):
                await self.channel_layer.group_discard(self.notify_group, self.channel_name)
        await self.close(code=code)

    async def disconnect(self, close_code):
        self.stopped = True
        await self.stop_heartbeat()
        if hasattr(self, 'notify_group'):
            with suppress(*BACKEND_ERRORS):
                await self.channel_layer.group_discard(self.notify_group, self.channel_name)
        if self.backend:
            with suppress(*BACKEND_ERRORS):
                await self.backend.close()
