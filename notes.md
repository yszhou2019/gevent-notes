### gevent中的recv操作

整体流程：

某个协程进行recv操作，抛出异常，操作会阻塞->
当前协程调用hub.wait，向hub协程中注册"事件以及对应的回调函数"->
为当前协程创建waiter，保存协程的运行现场，为当前协程调用waiter.get()，尝试获取运行结果->
切换到hub协程，调度可运行协程



标准库的socket recv是阻塞的，读写操作会抛出异常

协程捕获异常之后，调用hub.wait

gevent大多数的阻塞的函数都会在它们的实现里调用Hub.wait().下面是gevent.socket.socket.recv()大致的流程：

```python
def recv(self, *args):
    while True:
        try:
            return _socket.socket.recv(self._sock, *args)
        except error as ex:
            # 当错误是EWOULDBLOCK时，代表操作将会阻塞
            if ex.args[0] != EWOULDBLOCK:
                raise
        self.hub.wait(self._read_event)
```



Hub.wait大致的实现是下面这样（这是简化的例子

```python
def wait(self, watcher): # Hub.wait()
    # `watcher` is an event-loop object with a callback. When the
    # event it's waiting for happens, its callback gets called.
    current_greenlet = getcurrent()
    # The callback for this event will switch back to
    # this current greenlet
    watcher.start(current_greenlet.switch) # Ask the event loop to watch this
    try:
        # Start running the hub.
        self.switch()
        # Once we get here, it's because the watcher's callback
        # fired and this greenlet got switched into.
        return # Let the blocking code continue, its event is ready
    finally:
        watcher.stop()
```





见`_hub_primitives.py`

```python
def wait_on_socket(socket, watcher, timeout_exc=None):
    if socket is None or watcher is None:
        raise ConcurrentObjectUseError("The socket has already been closed by another greenlet")
    _primitive_wait(watcher, socket.timeout,
                    timeout_exc if timeout_exc is not None else _NONE,
                    socket.hub)

def _primitive_wait(watcher, timeout, timeout_exc, hub):
    if watcher.callback is not None:
        raise ConcurrentObjectUseError('This socket is already used by another greenlet: %r'
                                       % (watcher.callback, ))
    if hub is None:
        hub = get_hub()
    if timeout is None:
        # 调用wait，从而保存当前协程的现场，切换到hub协程，见下一小节
        hub.wait(watcher)
        return
    timeout = Timeout._start_new_or_dummy(
        timeout,
        (timeout_exc
         if timeout_exc is not _NONE or timeout is None
         else _timeout_error('timed out')))
    with timeout:
        hub.wait(watcher)
```



```cpp
#define EAGAIN 11 /* Try again */ 
#define EINTR 4 /* Interrupted system call */
#define EWOULDBLOCK EAGAIN /* Operation would block */
```



### gevent中的watcher

watcher的作用？

向hub协程中注册事件以及对应的回调函数，这里的watcher实际上就是"事件以及对应的回调函数"

类`WaitOperationsGreenlet`是Hub的父类



`hub.wait`函数的逻辑：

创建waiter，注册回调函数，然后调用wait.get()，如果当前协程已经得到了运行结果，那么就返回；否则，保存当前的工作协程，就切换到hub协程，调度其他可运行协程



见`_hub_primitives.py`

```python
class WaitOperationsGreenlet(SwitchOutGreenletWithLoop):

    # yszhou 2022-01-31
    # 形参watcher对应的实参，是loop.timer 定时器
    def wait(self, watcher):
        """
        Wait until the *watcher* (which must not be started) is ready.
        The current greenlet will be unscheduled during this time.
        """
        # `_waiter.py` 创建Waiter 用于存储协程的返回值 类似于future的概念
        waiter = Waiter(hub=self)
        # 定时器注册回调函数，在经过设定的时间之后调用waiter.switch 切换回工作协程
        # 创建waiter & 注册callback时，还没有给waiter指定greenlet
        watcher.start(waiter.switch, waiter)
        try:
            # 这里调用waiter.get() 才会设定greenlet到waiter中
            # 将当前的工作协程保存到waiter中，并切换到hub协程
            result = waiter.get()
            if result is not waiter:
                raise InvalidSwitchError(
                    'Invalid switch into %s: got %r (expected %r; waiting on %r with %r)' % (
                        getcurrent(), # pylint:disable=undefined-variable
                        result,
                        waiter,
                        self,
                        watcher
                    )
                )
        finally:
            # 卸载定时器
            watcher.stop()
```



### gevent中的waiter

waiter的作用？

waiter对象，类似与python中的future概念，该对象有一个switch()方法以及get()方法，当没有得到结果没有准备好时，调用waiter.get()方法回导致当前协程被挂起；



当前工作协程，调用hub.wait()，并在这个函数中，创建waiter，并调用waiter.get()，保存当前协程，切换到hub协程

waiter.get() 获取当前协程的运行结果（如果还没有运行完毕，那么就切换到hub协程等待调度）

waiter.switch() 用于hub协程切换到目标工作协程，恢复工作现场



见`_waiter.py`

**hub.wait函数非常关键**

对于任何阻塞性操作，比如timer、io都会调用这个函数，其作用一句话概括：
1 注册事件以及对应的回调函数（实际上就是把对应工作协程的switch作为回调）
2 调用waiter.get()，将保存工作协程到waiter中，并从当前协程切换到hub，直到watcher对应的事件就绪，再通过callback从hub切换回来工作协程

```python
class Waiter(object):
    """
    A low level communication utility for greenlets.
    Waiter is a wrapper around greenlet's ``switch()`` and ``throw()`` calls that makes them somewhat safer:
    The :meth:`switch` and :meth:`throw` methods must only be called from the :class:`Hub` greenlet.
    The :meth:`get` method must be called from a greenlet other than :class:`Hub`.
    """
    
    def __init__(self, hub=None):
        self.hub = get_hub() if hub is None else hub
        self.greenlet = None # 创建waiter时，协程为空; 调用waiter.get()时，才会设定保存当前的工作协程
        self.value = None
        self._exception = _NONE
        
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
```





### gevent中的joinall

简单的来讲，就是对协程列表逐一调用get()，等待所有协程运行完毕



见`_hub_primitives.py`

```python
# joinall的默认实现
def wait_on_objects(objects=None, timeout=None, count=None):
  '''
  Wait for ``objects`` to become ready or for event loop to finish.
  '''
  if objects is None:
    hub = get_hub()
    return hub.join(timeout=timeout)
  # 这里返回的实际上是一个迭代器，然后通过list一次性迭代完毕 对所有的greenlet调用get()
  return list(iwait_on_objects(objects, timeout, count))

def iwait_on_objects(objects, timeout=None, count=None):
  hub = get_hub()
  if objects is None:
    return [hub.join(timeout=timeout)]
  return _WaitIterator(objects, hub, timeout, count)

# 返回一个迭代器
class _WaitIterator(object):
    def __init__(self, objects, hub, timeout, count):
        self._hub = hub
        self._waiter = MultipleWaiter(hub)
        self._switch = self._waiter.switch
        self._timeout = timeout
        self._objects = objects
        self._timer = None
        self._begun = False
        self._count = len(objects) if count is None else min(count, len(objects))
        
    def __next__(self):
        self._begin()
        if self._count == 0:
            self._cleanup()
            raise StopIteration()
        self._count -= 1
        try:
            # 对于所有的协程，逐一调用Waiter.get，等待目标协程运行完毕得到结果
            item = self._waiter.get()
            self._waiter.clear()
            if item is self:
                # Timer expired, no more
                self._cleanup()
                raise StopIteration()
            return item
        except:
            self._cleanup()
            raise
```





### gevent中的hub协程

Hub协程的run方法实现如下



见`hub.py`

```python
class Hub(WaitOperationsGreenlet):
    '''
    当切换到hub协程时，自动执行hub.run()
    '''
    def run(self):
        """
        Entry-point to running the loop. This method is called automatically
        when the hub greenlet is scheduled; do not call it directly.
        """
        assert self is getcurrent(), 'Do not call Hub.run() directly'
        self.start_periodic_monitoring_thread()
        while 1:
            # 总loop
            # 创建的各种定时器，就是注册到总loop上
            # 创建定时器的时候 loop.timer(seconds) 以及对应的waiter(实际上就是future) 注册到loop中
            # hub协程就是不断筛选协程进行执行
            loop = self.loop
            loop.error_handler = self
            try:
                loop.run()
            finally:
                loop.error_handler = None  # break the refcount cycle
```





### gevent中的sleep

sleep的作用很简单，触发一个阻塞的操作，导致调用hub.wait，向hub协程注册定时器以及对应的回调函数（切换到目标协程），然后t切换至Hub；超时之后再从hub切换到之前的greenlet继续执行。

通过这个例子可以知道，**gevent将任何阻塞性的操作封装成一个Watcher，然后从调用阻塞操作的协程切换到Hub，等到阻塞操作完成之后，再从Hub切换到之前的协程**。



见`hub.py`

```python
def sleep(seconds=0, ref=True):
    """
    Put the current greenlet to sleep for at least *seconds*.
    *seconds* may be specified as an integer, or a float if fractional
    seconds are desired.
    """
    hub = _get_hub_noargs()
    loop = hub.loop
    if seconds <= 0:
        waiter = Waiter(hub)
        loop.run_callback(waiter.switch, None)
        waiter.get()
    else:
        with loop.timer(seconds, ref=ref) as t:
            # 创建定时器，只设定了定时器的时间，没有设定callback
            loop.update_now()
            # yszhou 2022-01-31
            # gevent核心操作，执行IO阻塞操作时候的具体细节:
            # 1 执行阻塞操作的时候，先从当前协程切换到hub协程
            # 2 watcher对应的事件就绪，再从hub协程返回原协程
            # 具体原理在于`_hub_primitives.py: class WaitOperationsGreenlet: def wait()`
            hub.wait(t)
            
```



函数`_get_hub_noargs`见`_hub_local.py`

hub是线程内唯一的，greenlet是线程独立的，hubtype默认就是gevent.hub.Hub

在hub的初始化函数中，会创建loop属性，默认也就是libev的python封装

```python
def get_hub_noargs():
    hub = _threadlocal.hub
    if hub is None:
        hubtype = get_hub_class()
        hub = _threadlocal.hub = hubtype()
    return hub
```



### gevent中的spawn

函数作用，创建一个协程并调度运行

这个类方法调用了Greenlet的两个函数，`__init__ `和 start

分别初始化协程，以及将新创建的协程的回调函数self.switch注册到hub.loop，等待被Hub协程调度。

注意greenlet.switch（这里**不是waiter.switch!**）最终会调用到greenlet._run



spawn这里仅仅是注册，但还没有开始事件轮询，gevent.joinall就是用来启动事件轮询并等待运行结果的。



见`greenlet.py`

```python
class Greenlet(greenlet):
    """
    A light-weight cooperatively-scheduled execution unit.
    """

    spawning_stack_limit = 10

    def __init__(self, run=None, *args, **kwargs):
        # 设置协程的parent为hub协程
        _greenlet__init__(self, None, get_hub())
        if run is not None:
            self._run = run
        if not callable(self._run):
            raise TypeError("The run argument or self._run must be callable")

        self.args = args
        self.kwargs = kwargs
        self.value = None
    
    @classmethod
    def spawn(cls, *args, **kwargs):
        """
        spawn(function, *args, **kwargs) -> Greenlet

        Create a new :class:`Greenlet` object and schedule it to run ``function(*args, **kwargs)``.
        This can be used as ``gevent.spawn`` or ``Greenlet.spawn``.

        The arguments are passed to :meth:`Greenlet.__init__`.
        """
        g = cls(*args, **kwargs)
        g.start()
        return g
      
      
    def start(self):
        """Schedule the greenlet to run in this loop iteration"""
        # 什么情况下才代表一个协程开始运行？
        # 将当前协程的self.switch作为回调函数，注册到loop循环中，等待被hub协程调度
        if self._start_event is None:
            _call_spawn_callbacks(self)
            hub = get_my_hub(self)
            self._start_event = hub.loop.run_callback(self.switch)
```





参考 https://www.cnblogs.com/lit10050528/p/13549532.html

https://goalong.github.io/2019/02/21/gevent%20hub/

https://www.cnblogs.com/xybaby/p/6370799.html



### 目标协程的创建和hub调度

`waiter.get()`和`waiter.switch()`配合使用组成了Gevent的基本运行流程：通过`waiter.get()`将工作协程保存到waiter中，并切换到Hub协程，注册事件以及对应的回调函数`waiter.switch()`将Hub协程切换到运行协程，返回执行结果。



### 目标协程的执行目标函数以及结束返回



Gevent的目的是执行目标协程里的目标函数，那么目标协程是如何调度运行以及运行返回的呢？

工作协程通过`waiter.get()`切换到Hub协程之后，事件循环启动，这里是通过**Cython**调用libev库实现相关功能，在这里不做详细讨论，有兴趣的同学可以查看源码目录下的libev下的相关实现。



目标协程如何运行？

将目标协程的`switch()`函数作为`ev_perpare`事件的回调函数，注册到`ev_loop`中；

在Hub协程中，事件循环启动之后，在`ev_loop`主循环每次启动之前会触发`ev_perpare`事件，由Hub协程切换到目标协程执行`run()`函数:



- 在iwait函数中，会将main协程的`switch()`函数rawlink至目标协程，`gr.rawlink(self, callback)`会将回调函数保存在内部队列`self._links`中，供目标函数执行完毕后执行。
- 目标函数执行完之后，通过`_report_result()`，保存运行结果，将`self._notify_links()`函数作为`ev_prepare`事件注册到Hub事件循环中，此时目标协程执行完毕。返回Hub协程
- Hub事件循环在下一轮事件循环开始之前触发`ev_prepare`，调用`_notify_links()`，遍历并执行回调队列`self._links`中得函数，运行协程的`switch(self)`函数将会执行，由Hub协程切换到main协程，main协程获得工作协程执行结果。

```python
class Greenlet(greenlet):
  
    def run(self):
        try:
            self.__cancel_start()
            self._start_event = _start_completed_event
            try:
                # 执行目标函数
                result = self._run(*self.args, **self.kwargs)
            except: 
                # 保存异常信息
                self._report_error(sys.exc_info())
                return
            # 上报处理结果
            self._report_result(result)
        finally:
            self.__dict__.pop('_run', None)
            self.__dict__.pop('args', None)
            self.__dict__.pop('kwargs', None)
            
    def rawlink(self, callback):
        if not callable(callback):
            raise TypeError('Expected callable: %r' % (callback, ))
        self._links.append(callback)
        if self.ready() and self._links and not self._notifier:
            self._notifier = self.parent.loop.run_callback(self._notify_links)    

    def _report_result(self, result):
        self._exc_info = (None, None, None)
        self.value = result
        if self._has_links() and not self._notifier:
            self._notifier = self.parent.loop.run_callback(self._notify_links)

    def _notify_links(self):
        while self._links:
            link = self._links.popleft()
            try:
                link(self)
            except:
                self.parent.handle_error((link, self), *sys.exc_info())
```



参考 https://zhuanlan.zhihu.com/p/382130341



### 其他注意事项

其他的注意事项

- 一个greenlet执行完成后会发生什么？
  控制将回到它的parent greenlet. gevent让hub作为所有它运行的gevent的parent，因此当一个parent不论是成功完成执行还是有未捕获的异常而意外终止，执行流都会回到hub，以便继续运行事件循环。
- 当我们想开始一个新的greenlet时会发生什么？
  gevent.spawn()创建一个新的greenlet，并且安排它在事件循环的下一轮循环时开始运行。猜想一下它是如何实现的，它创建一个事件的watcher，并且绑定一个new_greenlet.switch()的回调函数。这种事件的watcher是一个”prepare” watcher，这种类型的事件会在每一轮循环的开始就变的就绪。
- gevent的锁和超时机制是如何实现的？
  你得等到下一篇文章来探讨这些主题了。



注册到事件循环中的回调函数并不是协程run函数`func()`,而是`switch()`,以此来控制协程的“switch-in”和“switch-out”，完成多协程的并发







gevent如何实现阻塞IO时自动切换、自动调度到其他协程？

执行阻塞IO的操作，会抛出异常EWOULDBLOCK->
hub.wait() 保存当前协程，注册事件以及回调，切换hub协程->
调度其他可运行协程





hub协程什么时间会再次切换回到工作协程？

hub协程跑事件循环，事件触发时执行对应的回调函数，切换到工作协程

比如timer+callback

`gevnet.sleep()`是个“绿化”函数，它会向Hub事件循环注册一个`ev_timer`事件，并把程序控制权switch给Hub协程，由Hub协程调度其他目标协程运行，从而实现并发；

当等待时间到达时，触发`ev_timer`事件，目标函数获得程序控制权，继续执行



猴子补丁之后的阻塞函数，需要由libev支持的13种异步事件表示

事件还没有触发时，注册事件以及回调，切换hub协程；

事件触发时，事件发生恢复执行的函数





最主要的几个api
spawn 创建协程
greenlet.join 等待一个协程运行结束
gevent.joinall 等待所有协程运行结束
greenlet.get 获取某个协程的返回值

class Waiter: get() switch()
class Greenlet: gevent包装后的协程 所有协程的parent都是hub协程
class Hub: 调度器协程 不断运行事件循环

libev: 网络库 提供事件循环
greenlet: 协程库




2022-02-01 参考 https://www.jianshu.com/p/f55148c41f54
`greenlet.py class:Greenlet method: spawn`->创建Greenlet并返回
`greenlet.py class:Greenlet method: start`->将当前协程添加到loop中
`greenlet.py method: joinall` -> `_hub_primitives.py method:wait_on_objects`

spawn 创建一个协程
g.join 等待当前协程运行完毕，原理：切换到hub协程，等待切换回到当前协程
joinall 等待所有协程运行完毕



2022-01-31 参考 https://www.jianshu.com/p/f55148c41f54

小结
- gevent系列包含main协程 hub协程(调度器协程) 以及若干用户创建的工作协程
- `_waiter.py`中提供的`class wait`类似于future的概念，其中提供`method:switch`，用于保存当前工作协程的状态，从而被调度器选中之后可以恢复工作状态
- 执行阻塞IO时，gevent会首先为当前协程创建`waiter`，通过`loop.timer`向loop中注册时间以及回调函数(其实就是waiter.switch)，然后从中选择协程进行执行
- 以`hub.py: gevent.sleep()`函数为例，讲解了一下运行机制
- 机制: 创建loop.timer，注册计时器，并将工作协程封装为`_waiter.py: class Waiter`类似于future(通过switch来恢复工作协程)，对应计时器的callback实际上就是工作协程的switch；最终切换回协程时，再调用`_waiter.py: class Waiter: method get()`方法，工作协程运行完毕

```python
from gevent.hub import Waiter
from gevent import get_hub
result = Waiter()
timer = get_hub().loop.timer(0.1)
timer.start(result.switch, 'hello from Waiter')
result.get() # blocks for 0.1 seconds
# 'hello from Waiter'
timer.close()
```