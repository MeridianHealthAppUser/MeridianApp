import asyncio
import os
import uuid

from asgiref.sync import async_to_sync
from django.test import SimpleTestCase, override_settings

from .presence import LEASE_SECONDS, MemoryPresence, PresenceUnavailable, RedisPresence, presence_backend


class MemoryPresenceTests(SimpleTestCase):
    async def test_second_distinct_user_is_the_only_initiator_and_capacity_is_two(self):
        store = MemoryPresence()
        first, second = await asyncio.gather(store.apply('join', 1, 11, 'a', uuid.uuid4(), now=100),
                                            store.apply('join', 1, 22, 'b', uuid.uuid4(), now=100))
        self.assertEqual((first['count'], second['count']), (1, 2))
        self.assertEqual(first['state']['initiator'], '')
        self.assertEqual(second['state']['initiator'], '22')
        denied = await store.apply('join', 1, 33, 'c', uuid.uuid4(), now=100)
        self.assertFalse(denied['accepted'])
        self.assertEqual(denied['count'], 2)

    async def test_duplicate_user_replaces_old_channel_without_adding_capacity_or_old_eviction(self):
        store = MemoryPresence()
        await store.apply('join', 2, 11, 'old', uuid.uuid4(), now=100)
        await store.apply('join', 2, 22, 'peer', uuid.uuid4(), now=100)
        replacement = await store.apply('join', 2, 11, 'new', uuid.uuid4(), now=101)
        self.assertEqual(replacement['replaced'], 'old')
        self.assertEqual(replacement['count'], 2)
        self.assertEqual(replacement['state']['initiator'], '11')
        late_disconnect = await store.apply('leave', 2, 11, 'old', now=102)
        self.assertFalse(late_disconnect['accepted'])
        self.assertEqual(late_disconnect['state']['users']['11']['channel'], 'new')

    async def test_crash_leases_expire_while_active_heartbeat_preserves_only_its_identity(self):
        store = MemoryPresence()
        await store.apply('join', 3, 11, 'active', uuid.uuid4(), now=100)
        joined = await store.apply('join', 3, 22, 'crashed', uuid.uuid4(), now=100)
        await store.apply('heartbeat', 3, 11, 'active', now=130)
        pruned = await store.apply('heartbeat', 3, 11, 'active', now=100 + LEASE_SECONDS + 1)
        self.assertEqual(pruned['count'], 1)
        self.assertEqual(pruned['removed'][0]['channel'], 'crashed')
        self.assertNotEqual(pruned['state']['epoch'], joined['state']['epoch'])
        self.assertEqual(pruned['state']['session_key'], joined['state']['session_key'])
        empty = await store.apply('inspect', 3, now=250)
        self.assertEqual(empty['count'], 0)
        next_join = await store.apply('join', 3, 11, 'returning', uuid.uuid4(), now=251)
        self.assertNotEqual(next_join['state']['session_key'], joined['state']['session_key'])

    async def test_ring_debounce_and_presence_ownership_are_server_side(self):
        store = MemoryPresence()
        self.assertFalse((await store.apply('ring', 4, 11, 'a', now=100))['ring'])
        await store.apply('join', 4, 11, 'a', uuid.uuid4(), now=100)
        self.assertTrue((await store.apply('ring', 4, 11, 'a', now=100))['ring'])
        self.assertFalse((await store.apply('ring', 4, 11, 'a', now=101))['ring'])
        await store.apply('join', 4, 11, 'replacement', uuid.uuid4(), now=102)
        self.assertFalse((await store.apply('ring', 4, 11, 'replacement', now=102))['ring'])
        self.assertFalse((await store.apply('ring', 4, 11, 'a', now=140))['ring'])
        self.assertTrue((await store.apply('ring', 4, 11, 'replacement', now=140))['ring'])

    @override_settings(DEBUG=False, VIDEO_REDIS_URL='', REDIS_URL='')
    def test_production_cannot_silently_use_process_local_presence(self):
        with self.assertRaises(PresenceUnavailable):
            presence_backend()

    @override_settings(DEBUG=True, VIDEO_REDIS_URL='', REDIS_URL='')
    def test_development_can_use_memory_presence(self):
        self.assertIsInstance(presence_backend(), MemoryPresence)


class RedisPresenceTests(SimpleTestCase):
    def test_real_redis_cross_client_atomic_replacement_and_capacity(self):
        url = os.environ.get('VIDEO_TEST_REDIS_URL')
        if not url:
            self.skipTest('Set VIDEO_TEST_REDIS_URL to an isolated Redis test database.')
        async_to_sync(self.check_redis)(url)

    async def check_redis(self, url):
        first, second = RedisPresence(url), RedisPresence(url)
        room = uuid.uuid4().int % 1000000000 + 1000000000
        try:
            results = await asyncio.gather(first.apply('join', room, 1, 'a', uuid.uuid4()),
                                            second.apply('join', room, 2, 'b', uuid.uuid4()))
            self.assertEqual(sorted(result['count'] for result in results), [1, 2])
            state = (await first.apply('inspect', room))['state']
            self.assertEqual(len(state['users']), 2)
            self.assertIn(state['initiator'], ('1', '2'))
            full = await second.apply('join', room, 3, 'c', uuid.uuid4())
            self.assertFalse(full['accepted'])
            replacement = await second.apply('join', room, 1, 'new', uuid.uuid4())
            self.assertEqual(replacement['replaced'], 'a')
            stale_leave = await first.apply('leave', room, 1, 'a')
            self.assertFalse(stale_leave['accepted'])
            self.assertEqual(stale_leave['state']['users']['1']['channel'], 'new')
            # Corrupt only this synthetic test room lease to emulate a crash.
            import json
            state = stale_leave['state']
            state['users']['2']['expires'] = 1
            await first.client.set(f'video:room:{{{room}}}', json.dumps(state), ex=100)
            pruned = await second.apply('heartbeat', room, 1, 'new')
            self.assertEqual(pruned['count'], 1)
            self.assertEqual(pruned['removed'][0]['user_id'], '2')
        finally:
            await first.client.delete(f'video:room:{{{room}}}', f'video:ring:{{{room}}}')
            await first.close()
            await second.close()
