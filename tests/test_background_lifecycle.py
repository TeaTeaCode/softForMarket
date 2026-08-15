import asyncio

from app.services import background


async def test_stop_all_cancels_and_forgets_tracked_tasks():
    started = asyncio.Event()

    async def wait_forever():
        started.set()
        await asyncio.Event().wait()

    task = asyncio.create_task(wait_forever())
    background._track(task)
    await started.wait()

    await background.stop_all()

    assert task.cancelled()
    assert task not in background._tasks
