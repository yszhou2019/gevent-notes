# -*- coding: utf-8 -*-
# copyright 2018 gevent
# cython: auto_pickle=False,embedsignature=True,always_allow_keywords=False
"""
Low-level waiting primitives.

"""
from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

import sys

from gevent._hub_local import get_hub_noargs as get_hub
from gevent.exceptions import ConcurrentObjectUseError

__all__ = [
    'Waiter',
]

_NONE = object()

locals()['getcurrent'] = __import__('greenlet').getcurrent
locals()['greenlet_init'] = lambda: None

'''
yszhou 2022-01-31
`class Waiter`用来存储协程的返回值或者抛出的异常, 封装greenlet的switch()和throw()函数
类似于promise或者future的概念，创建一个waiter之后，需要用定时器来约定时间和返回值
之后直接get()，阻塞若干时间之后即可取得返回值
'''
class Waiter(object):
    """
    A low level communication utility for greenlets.

    Waiter is a wrapper around greenlet's ``switch()`` and ``throw()`` calls that makes them somewhat safer:

    * switching will occur only if the waiting greenlet is executing :meth:`get` method currently;
    * any error raised in the greenlet is handled inside :meth:`switch` and :meth:`throw`
    * if :meth:`switch`/:meth:`throw` is called before the receiver calls :meth:`get`, then :class:`Waiter`
      will store the value/exception. The following :meth:`get` will return the value/raise the exception.

    The :meth:`switch` and :meth:`throw` methods must only be called from the :class:`Hub` greenlet.
    The :meth:`get` method must be called from a greenlet other than :class:`Hub`.

        >>> from gevent.hub import Waiter
        >>> from gevent import get_hub
        >>> result = Waiter()
        >>> timer = get_hub().loop.timer(0.1)
        >>> timer.start(result.switch, 'hello from Waiter')
        >>> result.get() # blocks for 0.1 seconds
        'hello from Waiter'
        >>> timer.close()

    If switch is called before the greenlet gets a chance to call :meth:`get` then
    :class:`Waiter` stores the value.

        >>> from gevent.time import sleep
        >>> result = Waiter()
        >>> timer = get_hub().loop.timer(0.1)
        >>> timer.start(result.switch, 'hi from Waiter')
        >>> sleep(0.2)
        >>> result.get() # returns immediately without blocking
        'hi from Waiter'
        >>> timer.close()

    .. warning::

        This is a limited and dangerous way to communicate between
        greenlets. It can easily leave a greenlet unscheduled forever
        if used incorrectly. Consider using safer classes such as
        :class:`gevent.event.Event`, :class:`gevent.event.AsyncResult`,
        or :class:`gevent.queue.Queue`.
    """

    __slots__ = ['hub', 'greenlet', 'value', '_exception']

    def __init__(self, hub=None):
        self.hub = get_hub() if hub is None else hub
        self.greenlet = None
        self.value = None
        self._exception = _NONE

    def clear(self):
        self.greenlet = None
        self.value = None
        self._exception = _NONE

    def __str__(self):
        if self._exception is _NONE:
            return '<%s greenlet=%s>' % (type(self).__name__, self.greenlet)
        if self._exception is None:
            return '<%s greenlet=%s value=%r>' % (type(self).__name__, self.greenlet, self.value)
        return '<%s greenlet=%s exc_info=%r>' % (type(self).__name__, self.greenlet, self.exc_info)

    def ready(self):
        """Return true if and only if it holds a value or an exception"""
        return self._exception is not _NONE

    def successful(self):
        """Return true if and only if it is ready and holds a value"""
        return self._exception is None

    @property
    def exc_info(self):
        "Holds the exception info passed to :meth:`throw` if :meth:`throw` was called. Otherwise ``None``."
        if self._exception is not _NONE:
            return self._exception

    '''
    future中保存当前的协程，用于hub协程调度当前协程后，恢复工作现场
    '''
    def switch(self, value):
        """
        Switch to the greenlet if one's available. Otherwise store the *value*.
        """
        greenlet = self.greenlet
        if greenlet is None:
            self.value = value
            self._exception = None
        else:
            # 确保当前运行的协程必须是hub协程
            if getcurrent() is not self.hub:
                raise AssertionError("Can only use Waiter.switch method from the Hub greenlet")
            switch = greenlet.switch
            try:
                # 恢复到工作协程
                switch(value)
            except:
                self.hub.handle_error(switch, *sys.exc_info())

    def switch_args(self, *args):
        return self.switch(args)

    def throw(self, *throw_args):
        """Switch to the greenlet with the exception. If there's no greenlet, store the exception."""
        greenlet = self.greenlet
        if greenlet is None:
            self._exception = throw_args
        else:
            if getcurrent() is not self.hub:
                raise AssertionError("Can only use Waiter.switch method from the Hub greenlet")
            throw = greenlet.throw
            try:
                throw(*throw_args)
            except: # pylint:disable=bare-except
                self.hub.handle_error(throw, *sys.exc_info())

    '''
    future(Waiter).get()
    如果val已经计算出来，那么直接返回
    如果没有计算出来，就挂起当前协程 恢复hub协程
    '''
    def get(self):
        """If a value/an exception is stored, return/raise it. Otherwise until switch() or throw() is called."""
        if self._exception is not _NONE:
            # 如果正常执行完毕 那么获取返回值
            if self._exception is None:
                return self.value
            getcurrent().throw(*self._exception)
        else:
            # 否则结果为空，就代表当前协程没有执行完毕
            # 需要挂起当前协程 切换到hub协程
            if self.greenlet is not None:
                raise ConcurrentObjectUseError('This Waiter is already used by %r' % (self.greenlet, ))
            # getcurrent()函数: 存储当前工作协程，用于之后恢复
            self.greenlet = getcurrent()
            try:
                # 切换到hub协程
                return self.hub.switch()
            finally:
                # 工作协程被选中调度返回，清空self.greenlet
                self.greenlet = None

    def __call__(self, source):
        if source.exception is None:
            self.switch(source.value)
        else:
            self.throw(source.exception)

    # can also have a debugging version, that wraps the value in a tuple (self, value) in switch()
    # and unwraps it in wait() thus checking that switch() was indeed called



class MultipleWaiter(Waiter):
    """
    An internal extension of Waiter that can be used if multiple objects
    must be waited on, and there is a chance that in between waits greenlets
    might be switched out. All greenlets that switch to this waiter
    will have their value returned.

    This does not handle exceptions or throw methods.
    """
    __slots__ = ['_values']

    def __init__(self, hub=None):
        Waiter.__init__(self, hub)
        self._values = []

    def switch(self, value):
        self._values.append(value)
        Waiter.switch(self, True)

    def get(self):
        if not self._values:
            Waiter.get(self)
            Waiter.clear(self)

        return self._values.pop(0)

def _init():
    greenlet_init() # pylint:disable=undefined-variable

_init()


from gevent._util import import_c_accel
import_c_accel(globals(), 'gevent.__waiter')
