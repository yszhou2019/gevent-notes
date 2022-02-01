
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