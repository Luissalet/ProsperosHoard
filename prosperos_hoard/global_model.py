"""Consumer of Faustus938's stateless global model contract; no SDK changes.

Pin once per request/job, including its repair calls. A synchronous SDK facade
normally clones the base Link: this owned facade preserves the overrides.
"""
import asyncio
from contextlib import contextmanager
from contextvars import ContextVar
from functools import wraps
import threading

import httpx

from .hoard_link import Link
from .hoard_link._sync import SyncFacade
from .hoard_link.types import Resolution
from .hoard_link.reasoning import timeout_for

_job = ContextVar('prospero_global_job', default=None)


class Pin:
    def __init__(self):
        self.lock, self.choice = threading.Lock(), None


@contextmanager
def scope():
    token = _job.set(Pin())
    try:
        yield
    finally:
        _job.reset(token)


def job(function):
    @wraps(function)
    def wrapped(*args, **kwargs):
        with scope():
            return function(*args, **kwargs)
    return wrapped


def operation(function):
    @wraps(function)
    def wrapped(*args, **kwargs):
        if _job.get() is not None:
            return function(*args, **kwargs)
        with scope():
            return function(*args, **kwargs)
    return wrapped


def descriptor(config):
    if not config.faustus_urls:
        raise RuntimeError('Faustus no tiene un servicio global configurado.')
    origin = config.faustus_urls[0].rstrip('/')
    headers = {'Authorization':'Bearer '+config.faustus_token} if config.faustus_token else {}
    with httpx.Client(timeout=10, trust_env=False) as client:
        response = client.get(origin+'/api/global-model', headers=headers)
        response.raise_for_status()
        data = response.json()
    if data.get('protocol') != 'openai' or data.get('chat_path') != '/api/global-model/v1/chat/completions':
        raise RuntimeError('El servicio global no ofrece chat OpenAI; no se ha elegido otro modelo.')
    if any(not isinstance(data.get(k),str) or not data[k] for k in ('endpoint_id','model','connection_revision')):
        raise RuntimeError('El descriptor global está incompleto.')
    return {**data, 'url':origin+data['chat_path'], 'headers':headers}


def payload(body, choice):
    return {**body, 'model':choice['model'],
        **{'expected_'+k:choice[k] for k in ('endpoint_id','model','connection_revision')}}


class GlobalSync(SyncFacade):
    def close(self, timeout=2.0):
        loop, thread = self._loop, self._thread
        if loop is not None and loop.is_running() and thread is not threading.current_thread():
            async def drain(_link):
                await asyncio.get_running_loop().shutdown_default_executor()
            self._run(drain)
        super().close(timeout)
        if thread is not None and thread is not threading.current_thread():
            thread.join()

    def _twin(self):
        with self._start_lock:
            if self._inner is None:
                parent = self._link
                self._inner = GlobalLink(parent.config, client=None if parent._owns_client else parent._client)
                self._inner._now, self._inner._sleep = parent._now, parent._sleep
            return self._inner


class GlobalLink(Link):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.sync = GlobalSync(self)
        self._selection = ContextVar('prospero_call_selection', default=None)

    def global_selection(self):
        pin = _job.get()
        if pin is None:
            return descriptor(self.config)
        with pin.lock:
            if pin.choice is None:
                pin.choice = descriptor(self.config)
            return pin.choice

    async def resolve(self, capability, task=None):
        if capability not in ('llm','vision'):
            return await super().resolve(capability, task)
        try:
            choice = await asyncio.to_thread(self.global_selection)
            if capability == 'vision' and choice.get('vision') is not True:
                raise RuntimeError('El modelo global seleccionado no declara visión.')
            self._selection.set(choice)
            return Resolution(capability,'global',choice['url'],choice['model'],'openai','resolved',
                'Modelo global exacto de Faustus; sin fallback ni carga local.',
                {'endpoint_id':choice['endpoint_id'],'connection_revision':choice['connection_revision']})
        except Exception:
            return Resolution(capability,None,None,None,None,'unavailable',
                'El servicio global no está disponible o su selección no es válida.',{})

    async def _post_chat(self, provider, url, body, level):
        choice = self._selection.get()
        if choice is None or url != choice['url']:
            raise RuntimeError('La llamada no coincide con el modelo global fijado.')
        response = await super()._post_json(provider, url, payload(body,choice),
            timeout=timeout_for(120,level), headers=choice['headers'])
        if response.json()['choices'][0].get('finish_reason') != 'stop':
            raise RuntimeError('El modelo global no completó la respuesta.')
        return response
