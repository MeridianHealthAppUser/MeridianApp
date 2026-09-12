"""Atomic, lease-bounded room admission; never stores SDP, ICE or media."""

import asyncio
import copy
import json
import time
import uuid

from django.conf import settings
from redis.exceptions import RedisError


class PresenceUnavailable(Exception):
    pass


LEASE_SECONDS = 50
RING_SECONDS = 30

# One Redis script serializes admission, replacement and lease removal across
# workers. Redis TIME, rather than worker clocks, determines lease ownership.
PRESENCE_LUA = r'''
local clock = redis.call('TIME')
local now = tonumber(clock[1]) + tonumber(clock[2]) / 1000000
local action, uid, channel, connection = ARGV[1], ARGV[2], ARGV[3], ARGV[4]
local ttl, newepoch, newkey = tonumber(ARGV[5]), ARGV[6], ARGV[7]
local raw = redis.call('GET', KEYS[1])
local state = raw and cjson.decode(raw) or {users={}, epoch='', session_key='', revision=0, started=now}
local removed, changed = {}, false
for id, entry in pairs(state.users) do
  if entry.expires <= now then
    entry.user_id = id; entry.reason = 'expired'; table.insert(removed, entry)
    state.users[id] = nil; changed = true
  end
end
if changed then state.epoch = newepoch; state.initiator = '' end
local function count_users()
  local n=0; for _ in pairs(state.users) do n=n+1 end; return n
end
local accepted, replaced, ring = false, '', false
if action == 'join' then
  local count = count_users()
  if state.users[uid] or count < 2 then
    if count == 0 then
      state.session_key = newkey; state.started = now; state.revision = 0
    end
    if state.users[uid] then
      local old = state.users[uid]
      replaced = old.channel; old.user_id = uid; old.reason = 'replaced'; table.insert(removed, old)
    end
    state.users[uid] = {channel=channel, connection=connection, joined=now, seen=now, expires=now+ttl}
    state.epoch = newepoch; changed = true; accepted = true
    state.initiator = count_users() == 2 and uid or ''
  end
elseif action == 'heartbeat' then
  if state.users[uid] and state.users[uid].channel == channel then
    state.users[uid].seen=now; state.users[uid].expires=now+ttl
    changed=true; accepted=true
  end
elseif action == 'leave' then
  if state.users[uid] and state.users[uid].channel == channel then
    local old=state.users[uid]; old.user_id=uid; old.reason='left'; table.insert(removed, old)
    state.users[uid]=nil; state.epoch=newepoch; state.initiator=''; changed=true; accepted=true
  end
elseif action == 'ring' then
  if state.users[uid] and state.users[uid].channel == channel and count_users() == 1 then
    ring = redis.call('SET', KEYS[2], '1', 'NX', 'EX', tonumber(ARGV[8])) and true or false
  end
end
state.now=now
if changed then
  state.revision=state.revision+1
  redis.call('SET', KEYS[1], cjson.encode(state), 'EX', math.ceil(ttl*2))
end
return cjson.encode({state=state, removed=removed, accepted=accepted, replaced=replaced, ring=ring, count=count_users()})
'''


class RedisPresence:
    def __init__(self, url):
        import redis.asyncio as redis
        self.client = redis.from_url(url, decode_responses=True, socket_connect_timeout=2, socket_timeout=2)

    async def apply(self, action, appointment_id, user_id=0, channel='', connection='', *, now=None):
        try:
            value = await self.client.eval(PRESENCE_LUA, 2, f'video:room:{{{int(appointment_id)}}}',
                f'video:ring:{{{int(appointment_id)}}}', action, str(user_id), channel, str(connection),
                LEASE_SECONDS, str(uuid.uuid4()), str(uuid.uuid4()), RING_SECONDS)
            result = json.loads(value)
            # Redis Lua cjson represents an empty table as [] on some versions.
            result['state']['users'] = result['state']['users'] or {}
            return result
        except (OSError, ValueError, TypeError, RedisError):
            # Exception details may contain credentials; never forward/log them.
            raise PresenceUnavailable('Shared video presence is unavailable.') from None

    async def close(self):
        await self.client.aclose()

    async def healthcheck(self):
        try:
            await self.client.ping()
        except (RedisError, OSError):
            raise PresenceUnavailable('Shared video presence is unavailable.') from None


class MemoryPresence:
    """Explicitly development-only equivalent, useful for deterministic tests."""

    def __init__(self):
        self.rooms = {}
        self.rings = {}
        self.lock = asyncio.Lock()

    async def apply(self, action, appointment_id, user_id=0, channel='', connection='', *, now=None):
        async with self.lock:
            now = time.time() if now is None else now
            uid, key = str(user_id), str(appointment_id)
            state = copy.deepcopy(self.rooms.get(key, {'users': {}, 'epoch': '', 'session_key': '', 'revision': 0, 'started': now}))
            removed, changed = [], False
            for member_id, member in list(state['users'].items()):
                if member['expires'] <= now:
                    removed.append({**member, 'user_id': member_id, 'reason': 'expired'})
                    del state['users'][member_id]
                    changed = True
            if changed:
                state['epoch'], state['initiator'] = str(uuid.uuid4()), ''
            accepted, replaced, ring = False, '', False
            member = state['users'].get(uid)
            if action == 'join' and (member or len(state['users']) < 2):
                if not state['users']:
                    state.update(session_key=str(uuid.uuid4()), started=now, revision=0)
                if member:
                    replaced = member['channel']
                    removed.append({**member, 'user_id': uid, 'reason': 'replaced'})
                state['users'][uid] = dict(channel=channel, connection=str(connection), joined=now, seen=now, expires=now + LEASE_SECONDS)
                state['epoch'] = str(uuid.uuid4())
                state['initiator'] = uid if len(state['users']) == 2 else ''
                accepted = changed = True
            elif action == 'heartbeat' and member and member['channel'] == channel:
                member.update(seen=now, expires=now + LEASE_SECONDS)
                accepted = changed = True
            elif action == 'leave' and member and member['channel'] == channel:
                removed.append({**member, 'user_id': uid, 'reason': 'left'})
                del state['users'][uid]
                state['epoch'], state['initiator'] = str(uuid.uuid4()), ''
                accepted = changed = True
            elif action == 'ring' and member and member['channel'] == channel and len(state['users']) == 1:
                if self.rings.get(key, 0) <= now:
                    self.rings[key] = now + RING_SECONDS
                    ring = True
            state['now'] = now
            if changed:
                state['revision'] += 1
                self.rooms[key] = copy.deepcopy(state)
            # Development process state is bounded too, even after tab crashes.
            for room_id, room in list(self.rooms.items()):
                last_lease = max((member['expires'] for member in room['users'].values()), default=room.get('now', 0))
                if last_lease + 2 * LEASE_SECONDS < now:
                    del self.rooms[room_id]
            self.rings = {room_id: expires for room_id, expires in self.rings.items() if expires > now}
            return dict(state=state, removed=removed, accepted=accepted, replaced=replaced, ring=ring, count=len(state['users']))

    async def close(self):
        pass

    async def healthcheck(self):
        pass


_memory = MemoryPresence()


def presence_backend():
    url = getattr(settings, 'VIDEO_REDIS_URL', '') or getattr(settings, 'REDIS_URL', '')
    if url:
        return RedisPresence(url)
    if settings.DEBUG:
        return _memory
    raise PresenceUnavailable('Shared video presence is required outside development.')
