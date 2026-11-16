"""Validate the explicitly included live Studio additions without HTTP or motion."""
import asyncio
import os
from pathlib import Path
import sys
import unittest
from unittest.mock import AsyncMock, patch

# Run with DOG_SOURCE pointing at the staged target dog source.
sys.path.insert(0,str(Path(os.environ['DOG_SOURCE'])/'tests'))
from test_orange_dog_role_b import make_agent

class StudioOverlayTests(unittest.IsolatedAsyncioTestCase):
    async def test_active_playback_releases_in_order_without_reset(self):
        agent,_,_=make_agent()
        agent._studio_state=AsyncMock(return_value={'status':'playing'})
        agent._studio_control=AsyncMock()
        agent._release_studio_checkpoint=AsyncMock()
        await agent._follow_studio_patrol()
        agent._studio_control.assert_not_awaited()
        self.assertEqual([c.args[0] for c in agent._release_studio_checkpoint.await_args_list],[1,2,3])
        self.assertTrue(all(c.kwargs=={'wait_until_reached':True} for c in agent._release_studio_checkpoint.await_args_list))

    async def test_idle_playback_resets_then_releases(self):
        agent,_,_=make_agent()
        agent._studio_state=AsyncMock(return_value={'status':'idle'})
        agent._studio_control=AsyncMock()
        agent._release_studio_checkpoint=AsyncMock()
        await agent._follow_studio_patrol()
        agent._studio_control.assert_awaited_once_with('reset_checkpoint2')
        self.assertEqual(agent._release_studio_checkpoint.await_count,3)

    async def test_cancellation_stops_follow_chain(self):
        agent,_,_=make_agent()
        agent._studio_state=AsyncMock(return_value={'status':'waiting'})
        agent._studio_control=AsyncMock()
        agent._release_studio_checkpoint=AsyncMock(side_effect=asyncio.CancelledError())
        with self.assertRaises(asyncio.CancelledError):await agent._follow_studio_patrol()
        self.assertEqual(agent._release_studio_checkpoint.await_count,1)

if __name__=='__main__':unittest.main()
