"""Offline transport tests using the actual nested Discord handlers."""
import ast
import asyncio
import copy
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock
import discord_ai_bot as bot


class HttpError(Exception):
    pass


class NotFound(HttpError):
    def __init__(self, code=10008):
        self.code = code


def handlers():
    tree = ast.parse(Path(bot.__file__).read_text(encoding='utf-8'))
    run = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == 'run_discord_bot')
    nodes = []
    for node in run.body:
        if isinstance(node, ast.AsyncFunctionDef) and node.name in ('reply_image', 'interaction_image', 'ask_command', 'on_message'):
            node = copy.deepcopy(node)
            node.decorator_list = []
            nodes.append(node)
    config = SimpleNamespace(ephemeral=False, slash_command_name='ask', command_prefix='!ace')
    engine = Mock()
    engine.answer.return_value = bot.AnswerResult('結果', 'weekly_pick', 1, 3.0)
    guard = Mock()
    guard.check_permission.return_value = ''
    guard.acquire.return_value = ''
    files = []

    async def image_file(*args, **kwargs):
        file = Mock()
        files.append(file)
        return file

    env = dict(asyncio=asyncio, print=Mock(), discord=SimpleNamespace(NotFound=NotFound, HTTPException=HttpError),
               image_file=image_file, no_mentions=object(), engine=engine, config=config, guard=guard,
               is_weekly_pick_question=bot.is_weekly_pick_question, WEEKLY_PICK_ACK=bot.WEEKLY_PICK_ACK,
               HELP_MESSAGE=bot.HELP_MESSAGE, prefix='!ace')
    exec(compile(ast.Module(body=nodes, type_ignores=[]), '<actual-discord-handlers>', 'exec'), env)
    env['files'] = files
    return env


def interaction():
    item = SimpleNamespace(user=SimpleNamespace(id=1), channel_id=2, guild_id=3, guild=object(),
                           response=SimpleNamespace(), followup=SimpleNamespace(send=AsyncMock()),
                           edit_original_response=AsyncMock(), is_expired=Mock(return_value=False))
    state = {'done': False}

    async def defer(**kwargs):
        state['done'] = True

    item.response.defer = AsyncMock(side_effect=defer)
    item.response.is_done = lambda: state['done']
    return item


def message():
    class Typing:
        async def __aenter__(self):
            return self
        async def __aexit__(self, *args):
            pass
    pending = SimpleNamespace(edit=AsyncMock())
    msg = SimpleNamespace(author=SimpleNamespace(bot=False, id=1), content='!ace 本週精選',
                          guild=SimpleNamespace(id=3), channel=SimpleNamespace(id=2, typing=Typing),
                          reply=AsyncMock(return_value=pending))
    return msg, pending


class WaitingReplyTests(unittest.IsolatedAsyncioTestCase):
    async def test_slash_replaces_wait_attachment_with_result(self):
        env, item = handlers(), interaction()
        await env['ask_command'](item, '本週精選')
        self.assertEqual(item.edit_original_response.await_count, 2)
        item.followup.send.assert_not_awaited()
        item.response.defer.assert_awaited_once()
        self.assertEqual(item.edit_original_response.call_args.kwargs['attachments'], [env['files'][-1]])
        self.assertNotEqual(item.edit_original_response.call_args_list[0].kwargs['attachments'], item.edit_original_response.call_args.kwargs['attachments'])
        env['guard'].release.assert_called_once_with(1)

    async def test_prefix_edits_wait_message_instead_of_sending_again(self):
        env = handlers()
        msg, pending = message()
        await env['on_message'](msg)
        msg.reply.assert_awaited_once()
        pending.edit.assert_awaited_once()
        self.assertEqual(pending.edit.call_args.kwargs['attachments'], [env['files'][-1]])
        self.assertIsNone(pending.edit.call_args.kwargs['content'])

    async def test_prefix_failure_replaces_wait_with_error_card(self):
        env = handlers()
        env['engine'].answer.side_effect = RuntimeError('test')
        msg, pending = message()
        await env['on_message'](msg)
        msg.reply.assert_awaited_once()
        pending.edit.assert_awaited_once()
        env['guard'].release.assert_called_once_with(1)

    async def test_ephemeral_failure_uses_same_private_original(self):
        env, item = handlers(), interaction()
        env['config'].ephemeral = True
        env['engine'].answer.side_effect = RuntimeError('test')
        await env['ask_command'](item, '本週精選')
        item.response.defer.assert_awaited_once_with(thinking=True, ephemeral=True)
        self.assertEqual(item.edit_original_response.await_count, 2)
        item.followup.send.assert_not_awaited()

    async def test_deleted_prefix_wait_can_be_recreated(self):
        env = handlers()
        msg, pending = message()
        pending.edit.side_effect = NotFound()
        await env['reply_image'](msg, '題目', '結果', pending=pending)
        msg.reply.assert_awaited_once()
        env['files'][0].reset.assert_called_once()
        env['files'][0].close.assert_called_once()

    async def test_deleted_slash_original_can_be_recreated_privately(self):
        env, item = handlers(), interaction()
        item.edit_original_response.side_effect = NotFound()
        await env['interaction_image'](item, '題目', '結果', ephemeral=True)
        item.followup.send.assert_awaited_once()
        self.assertTrue(item.followup.send.call_args.kwargs['ephemeral'])
        self.assertTrue(item.followup.send.call_args.kwargs['wait'])

    async def test_expired_token_does_not_fall_back_to_public_message(self):
        env, item = handlers(), interaction()
        item.edit_original_response.side_effect = NotFound(10015)
        item.is_expired.return_value = True
        with self.assertRaises(NotFound):
            await env['interaction_image'](item, '題目', '結果', ephemeral=True)
        item.followup.send.assert_not_awaited()
        env['files'][0].close.assert_called_once()


if __name__ == '__main__':
    unittest.main()
