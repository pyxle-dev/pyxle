# Background Tasks

Some work shouldn't make the user wait: sending a welcome email, emitting a
webhook, warming a cache. Pyxle gives you two ways to run it off the request's
critical path — pick by *when* the work should run.

| You want… | Use | Runs |
|-----------|-----|------|
| Work tied to a response (after this `@action` replies) | `request.state.background` | After the response is sent |
| Fire-and-forget work from anywhere (loader, action, API) | `pyxle.tasks.enqueue` | On a background worker pool |

Both run **in-process** on the app's event loop. For work that must survive a
restart or run exactly once across machines, reach for a real job queue —
[see below](#production-job-queues).

## Post-response work in an `@action`

Inside an `@action`, `request.state.background` is a Starlette
[`BackgroundTasks`](https://www.starlette.io/background/). Add tasks to it and
Pyxle runs them **after** the response is sent, so the client isn't kept
waiting:

```python
from pyxle.runtime import action

async def send_welcome_email(email: str):
    await mailer.send(email, template="welcome")

@action
async def signup(request, body: Signup):
    user = await db.create_user(body.email)
    # The client gets its response immediately; the email sends afterward.
    request.state.background.add_task(send_welcome_email, user.email)
    return {"id": user.id}
```

`add_task(func, *args, **kwargs)` accepts a coroutine function or a plain
function (run in a threadpool). The task runs after the response — a failure in
it can't change what the client already received, so log inside the task if you
need to know it ran.

> `request.state.background` exists **only inside an `@action`**. A `@server`
> loader or a plain API route never gets it, so `request.state.background` raises
> `AttributeError` there — use `pyxle.tasks.enqueue` (below) from a loader or API
> route instead.

### The `{"background": [...]}` shorthand

For a single task, you can return it from the action instead of touching
`request.state`:

```python
@action
async def signup(request, body: Signup):
    user = await db.create_user(body.email)
    return {"id": user.id, "background": [send_welcome_email, user.email]}
```

The value is `[callable, *args]`; Pyxle strips the `background` key from the
response body and schedules `callable(*args)` to run after the response. The
shorthand passes **positional args only** — there's no way to express keyword
args in the flat list. For keyword args or more than one task, use
`request.state.background.add_task(...)`.

### When the action refuses

A task you scheduled **before** raising [`ActionError`](../reference/runtime-api.md#actionerror)
still runs. `add_task` is a statement that executed, and a later `raise` no more
undoes it than it rolls back a database write on the line above — so the rule is
simply **where you scheduled it**:

```python
@action
async def transfer(request, body: Transfer):
    # Runs either way: scheduled before the check that can refuse.
    request.state.background.add_task(audit, request.state.user.id, body.amount)

    if await balance_of(request.state.user) < body.amount:
        raise ActionError("Insufficient funds", status_code=400)

    await do_transfer(body)
    # Only on success: never scheduled if the check above refused.
    request.state.background.add_task(send_receipt, request.state.user.email)
    return {"ok": True}
```

So an audit record that must exist whether or not the transfer went through goes
above the check, and a receipt that must not be sent for a refused transfer goes
below it. There is no flag to remember, and the failure mode you can see in the
code is the one you get. (If you scheduled something and then decided against
it, `request.state.background.tasks.clear()` discards what is pending.)

An **unhandled** exception is different. `ActionError` is an answer you wrote;
an unexpected exception is a bug, and the action's intent is unknown — so
nothing it staged is applied, neither its tasks nor its cookies, and the request
fails with a `500`.

## Fire-and-forget work — `pyxle.tasks`

When the work isn't tied to a particular response — a loader that wants to
refresh something, an API route emitting an event — enqueue it on the app's
in-process task queue:

```python
from pyxle.tasks import enqueue

@server
async def load_article(request):
    article = await db.get_article(request.path_params["slug"])
    enqueue(record_view, article.id)   # fire-and-forget; the loader returns now
    return {"article": article.to_dict()}
```

`enqueue(func, *args, **kwargs)` schedules `func` on a small pool of background
workers (coroutine functions are awaited; plain callables run in a thread, so a
blocking SDK call won't stall the loop). It returns immediately. A task that
raises is logged — it never takes a worker down. The queue is bounded; under
sustained overload `enqueue` raises `TaskQueueFull` rather than growing without
limit.

The queue starts and stops with the app, so `enqueue` works inside any request.
Called outside a running app it raises `TaskQueueNotRunning`.

> **Multi-worker caveat.** The in-process queue lives in **one** process, so
> under `pyxle serve --workers N` each worker has its own queue, and queued work
> is lost if that worker restarts. It's the same trade-off as the in-process
> [WebSocket broker](websockets.md). For durability or exactly-once semantics
> across workers, use a real job queue.

## Production job queues

Pyxle's in-process tasks are deliberately small — perfect for "send an email
after signup," not for a video-transcoding pipeline. When you need retries,
scheduling, durability, or workers on separate machines, hand off to a
dedicated queue. They slot in cleanly because an `@action` is just async Python:

- **[Celery](https://docs.celeryq.dev/)** — the most common choice. Define a
  `@celery_app.task`, then call `your_task.delay(arg)` from an action. Run
  `celery -A app worker` as a separate process.
- **[ARQ](https://arq-docs.helpmanual.io/)** — async-native, Redis-backed.
  `await redis.enqueue_job("task_name", arg)` from an action; ARQ's worker runs
  it. A natural fit since Pyxle handlers are already async.
- **[Dramatiq](https://dramatiq.io/)** — `@dramatiq.actor` then `task.send(arg)`.

In every case the pattern is the same: the action enqueues a job and returns;
the external worker does the heavy lifting. Pyxle doesn't wrap these — use their
own client directly so you keep their full feature set.

## See also

- [Server Actions](../core-concepts/server-actions.md)
- [Runtime API → `pyxle.tasks`](../reference/runtime-api.md#background-tasks-pyxletasks)
