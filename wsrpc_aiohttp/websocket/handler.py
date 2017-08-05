# encoding: utf-8
import abc
import logging
import time
import uuid
import struct
from typing import Callable, Union

import aiohttp
import asyncio
import types
from collections import defaultdict

from aiohttp import web, WSMessage, WebSocketError, hdrs
from functools import partial

from aiohttp.abc import AbstractView

from .route import WebSocketRoute
from .tools import Lazy, json
from .common import WSRPCBase, ClientException

global_log = logging.getLogger("wsrpc")
log = logging.getLogger("wsrpc.handler")


class WebSocketBase(WSRPCBase, AbstractView):
    __slots__ = ('_request', 'socket', 'id', '__pending_tasks',
                 '__handlers', 'store', 'serial', '_ping', 'protocol_version')

    _KEEPALIVE_PING_TIMEOUT = 30
    _CLIENT_TIMEOUT = int(_KEEPALIVE_PING_TIMEOUT / 3)

    def __init__(self, request):
        AbstractView.__init__(self, request)
        WSRPCBase.__init__(self, loop=self.request.app.loop)

        self._ping = defaultdict(self._loop.create_future)
        self.id = uuid.uuid4()
        self.protocol_version = None
        self.serial = 0
        self.socket = None      # type: web.WebSocketResponse

    @classmethod
    def configure(cls, keepalive_timeout=_KEEPALIVE_PING_TIMEOUT, client_timeout=_CLIENT_TIMEOUT):
        cls._KEEPALIVE_PING_TIMEOUT = keepalive_timeout
        cls._CLIENT_TIMEOUT = client_timeout

    @asyncio.coroutine
    def __iter__(self):
        return (yield from self.__handle_request())

    def __await__(self):
        return (yield from self.__iter__())

    async def __handle_request(self):
        self.socket = web.WebSocketResponse()

        protocol_version = self.request.headers.get(hdrs.SEC_WEBSOCKET_VERSION, '')
        if protocol_version and protocol_version.isdigit():
            self.protocol_version = int(protocol_version)

        await self.socket.prepare(self.request)

        self.clients[self.id] = self
        self._create_task(self._start_ping())

        async for msg in self.socket:
            try:
                await self._handle_message(msg)
            except WebSocketError:
                log.error('Client connection %s closed with exception %s', self.id, self.socket.exception())
                break
        else:
            log.info('Client connection %s closed', self.id)

        return self.socket

    @classmethod
    def broadcast(cls, func, callback=WebSocketRoute.placebo, **kwargs):
        loop = asyncio.get_event_loop()

        for client_id, client in cls.get_clients().items():
            loop.create_task(client.call, func, callback, **kwargs)

    async def on_message(self, message: WSMessage):
        log.debug('Client %s send message: "%s"', self.id, message)

        # deserialize message
        data = message.json(loads=json.loads)
        serial = data.get('serial', -1)
        msg_type = data.get('type', 'call')

        assert serial >= 0

        log.debug("Acquiring lock for %s serial %s", self, serial)
        async with self._locks[serial]:
            try:
                if msg_type == 'call':
                    args, kwargs = self._prepare_args(data.get('arguments', None))
                    callback = data.get('call', None)

                    if callback is None:
                        raise ValueError('Require argument "call" does\'t exist.')

                    callee = self.resolver(callback)
                    callee_is_route = hasattr(callee, '__self__') and isinstance(callee.__self__, WebSocketRoute)
                    if not callee_is_route:
                        a = [self]
                        a.extend(args)
                        args = a

                    result = await self._executor(partial(callee, *args, **kwargs))
                    self._send(data=result, serial=serial, type='callback')

                elif msg_type == 'callback':
                    cb = self._futures.pop(serial, None)
                    cb.set_result(data.get('data', None))

                elif msg_type == 'error':
                    self._reject(data.get('serial', -1), data.get('data', None))
                    log.error('Client return error: \n\t{0}'.format(data.get('data', None)))

            except Exception as e:
                log.exception(e)
                self._send(data=self._format_error(e), serial=serial, type='error')

            finally:
                def clean_lock():
                    log.debug("Release and delete lock for %s serial %s", self, serial)
                    if serial in self._locks:
                        self._locks.pop(serial)

                self._call_later(self._CLIENT_TIMEOUT, clean_lock)

    def _send(self, **kwargs):
        try:
            log.debug(
                "Sending message to %s serial %s: %s",
                Lazy(lambda: str(self.id)),
                Lazy(lambda: str(kwargs.get('serial'))),
                Lazy(lambda: str(kwargs))
              )
            self._loop.create_task(self.socket.send_json(kwargs, dumps=json.dumps))
        except aiohttp.WebSocketError:
            self._create_task(self.close())

    @staticmethod
    def _format_error(e):
        return {'type': str(type(e).__name__), 'message': str(e)}

    def _reject(self, serial, error):
        future = self._futures.get(serial)
        if future:
            future.set_exception(ClientException(error))

    async def close(self):
        await self.socket.close()
        await super().close()

        if self.id in self.clients:
            self.clients.pop(self.id)

        for name, obj in self._handlers.items():
            self._loop.create_task(asyncio.coroutine(obj._onclose)())

    def _log_client_list(self):
        log.debug('CLIENTS: %s', Lazy(lambda: ''.join(['\n\t%r' % i for i in self.clients.values()])))

    async def _start_ping(self):
        while True:
            if self.socket.closed:
                return

            future = self.call('ping', seq=self._loop.time())

            def on_timeout():
                if future.done():
                    return
                future.set_exception(TimeoutError)

            handle = self._loop.call_later(self._KEEPALIVE_PING_TIMEOUT, on_timeout)
            future.add_done_callback(lambda f: handle.cancel())

            try:
                resp = await future
                delta = (self._loop.time() - resp.get('seq', 0))

                log.debug("%r Pong recieved: %.4f" % (self, delta))

            except TimeoutError:
                log.info('Client "%r" connection should be closed because ping timeout', self)
                self._loop.create_task(self.close())
                break

            if delta > self._CLIENT_TIMEOUT:
                log.info('Client "%r" connection should be closed because ping '
                         'response time gather then client timeout', self)
                self._loop.create_task(self.close())
                break

            await asyncio.sleep(self._KEEPALIVE_PING_TIMEOUT, loop=self._loop)


class WebSocket(WebSocketBase):
    async def _executor(self, func):
        return await asyncio.coroutine(func)()


class WebSocketThreaded(WebSocketBase):
    async def _executor(self, func):
        return self._loop.run_in_executor(None, func)
