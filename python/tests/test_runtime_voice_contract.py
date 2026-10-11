"""No hardware: exercise the exact A2A payload with a dry-run actuator."""
import asyncio
import importlib.util
import os
import sys
from pathlib import Path

import pytest

source = Path(os.environ.get('ORANGE_DOG_SOURCE', str(Path(__file__).parents[1] / 'examples/dog_patrol_agent.py')))
spec = importlib.util.spec_from_file_location('voice_contract_dog', source)
dog = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = dog
spec.loader.exec_module(dog)

class Sdk:
    def __init__(self): self.sent = []
    async def send_message(self, *args, **kwargs): self.sent.append(args[2])
    async def close(self): pass

def agent():
    sdk, actions = Sdk(), dog.DryRunDogActions()
    config = dog.DogPatrolConfig(runtime_ip='127.0.0.1', runtime_port=8080,
        local_tcp_port=4002, local_udp_port=28444, masque_url='https://127.0.0.1:4433',
        masque_authorization=None, owner='test', agent_name='D-1')
    return dog.DogPatrolAgent(sdk, actions, config), actions, sdk

@pytest.mark.parametrize('direction,expected', [('forward','forward'),('backward','back'),
    ('left','left'),('right','right'),('wave','Hello'),('content','Content'),('deter','Content'),('happy','Content'),('expel','FrontPounce')])
def test_english_a2a_and_duplicate_id(direction, expected):
    async def run():
        a, actions, sdk = agent()
        payload = dict(type='robot_action', direction=direction, command_id='voice-1', confirmed=True)
        await a.on_group_message('g', 'eyes', payload)
        assert actions.calls[-1] == expected
        count = len(actions.calls)
        await a.on_group_message('g', 'eyes', payload)
        assert len(actions.calls) == count
        assert sdk.sent[-1]['type'].endswith('_completed')
    asyncio.run(run())

@pytest.mark.parametrize('confirmed', [False, 'false', 'true'])
def test_explicit_non_boolean_confirmation_cannot_be_overridden_by_risk(confirmed):
    async def run():
        a, actions, sdk = agent()
        await a.on_group_message('g', 'eyes', dict(type='robot_action', command='expel',
            command_id='voice-1', risk='high', confirmed=confirmed))
        assert not actions.calls
        assert sdk.sent[-1]['type'] == 'command_confirmation_required'
    asyncio.run(run())


def test_high_risk_label_without_explicit_confirmation_is_rejected():
    async def run():
        a, actions, sdk = agent()
        await a.on_group_message('g', 'eyes', dict(type='robot_action', command='expel',
            command_id='voice-risk-only', risk='high'))
        assert not actions.calls
        assert sdk.sent[-1]['type'] == 'command_confirmation_required'
    asyncio.run(run())

@pytest.mark.parametrize('command,path', [('content','/content'),('deter','/content'),('happy','/content'),('wave','/wave'),('expel','/front-pounce')])
def test_a2a_selects_exact_http_endpoint_without_network(command, path):
    from unittest.mock import AsyncMock, call
    async def run():
        a, _, sdk = agent()
        actions = dog.HttpDogActions('http://unused.invalid')
        actions._post = AsyncMock(return_value={'ok': True})
        a.actions = actions
        await a.on_group_message('g', 'eyes', dict(type='robot_action', command=command,
            action=command, command_id='voice-http-1', confirmed=True))
        assert actions._post.await_args_list == [call("/stop"), call(path)]
        assert sdk.sent[-1]['type'].endswith('_completed')
    asyncio.run(run())
