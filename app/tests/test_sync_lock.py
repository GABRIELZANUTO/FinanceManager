"""Phone app: screen writes and synchronisation never run at the same time."""
import threading
import time

from django.http import HttpResponse
from django.test import RequestFactory, SimpleTestCase

from app.sync import client
from expensemanager.middleware import SyncLockMiddleware


class SyncLockTests(SimpleTestCase):
    def setUp(self):
        self.calls = []
        self.middleware = SyncLockMiddleware(lambda request: self.calls.append(request.path) or HttpResponse("ok"))
        self.factory = RequestFactory()

    def test_write_waits_for_the_running_sync(self):
        client._lock.acquire()
        done = []
        worker = threading.Thread(target=lambda: done.append(self.middleware(self.factory.post("/api/transactions/"))))
        worker.start()
        time.sleep(0.3)
        self.assertEqual(self.calls, [])          # still waiting
        client._lock.release()
        worker.join(5)
        self.assertEqual((self.calls, done[0].status_code), (["/api/transactions/"], 200))

    def test_gives_up_with_a_clear_answer(self):
        self.middleware.WAIT = 0.1
        client._lock.acquire()
        try:
            response = self.middleware(self.factory.post("/api/transactions/"))
        finally:
            client._lock.release()
        self.assertEqual(response.status_code, 503)
        self.assertIn(b"syncing", response.content)

    def test_reads_and_sync_routes_never_wait(self):
        client._lock.acquire()
        try:
            for request in (self.factory.get("/api/transactions/"), self.factory.post("/api/sync/run/")):
                self.assertEqual(self.middleware(request).status_code, 200)
        finally:
            client._lock.release()
