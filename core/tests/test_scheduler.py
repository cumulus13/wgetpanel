import os
import threading
from datetime import timedelta

from django.db import connection
from django.utils import timezone

from core import conf, live, scheduler, tasks
from core.models import Download, SchedulerState
from core.tests.helpers import PanelTestCase


class SchedulerTests(PanelTestCase):
    def queue(self, n, **kw):
        return [self.make_download(f"http://h/f{i}.bin", status="queued", **kw) for i in range(n)]

    def test_respects_max_downloads_and_fifo(self):
        conf.set("scheduler.max_downloads", 2)
        items = self.queue(5)
        ids = scheduler.tick()
        self.assertEqual(ids, [items[0].pk, items[1].pk])
        self.assertEqual(Download.objects.filter(status="running").count(), 2)
        self.assertEqual(scheduler.tick(), [])  # full
        Download.objects.filter(pk=items[0].pk).update(status="completed")
        self.assertEqual(scheduler.tick(), [items[2].pk])  # slot freed

    def test_paused_does_not_use_slot_and_is_not_started(self):
        conf.set("scheduler.max_downloads", 1)
        a, b = self.queue(2)
        Download.objects.filter(pk=a.pk).update(status="paused")
        self.assertEqual(scheduler.tick(), [b.pk])

    def test_unlimited(self):
        conf.set("scheduler.max_downloads", 0)
        self.queue(7)
        self.assertEqual(len(scheduler.tick()), 7)

    def test_not_before(self):
        conf.set("scheduler.max_downloads", 5)
        later, now_ = self.queue(2)
        Download.objects.filter(pk=later.pk).update(not_before=timezone.now() + timedelta(hours=1))
        self.assertEqual(scheduler.tick(), [now_.pk])
        Download.objects.filter(pk=later.pk).update(not_before=timezone.now() - timedelta(seconds=1))
        self.assertEqual(scheduler.tick(), [later.pk])

    def test_reorder_changes_start_order(self):
        from core import services
        conf.set("scheduler.max_downloads", 1)
        user = self.make_user()
        a, b, c = self.queue(3, user=user)
        services.apply_action(user, "top", [c.pk])
        self.assertEqual(scheduler.tick(), [c.pk])
        Download.objects.filter(pk=c.pk).update(status="completed")
        services.apply_action(user, "bottom", [a.pk])
        self.assertEqual(scheduler.tick(), [b.pk])

    def test_concurrent_ticks_never_exceed_limit(self):
        conf.set("scheduler.max_downloads", 3)
        self.queue(20)
        claimed, lock = [], threading.Lock()

        def worker():
            try:
                for _ in range(5):
                    ids = scheduler.tick()
                    with lock:
                        claimed.extend(ids)
            finally:
                connection.close()

        threads = [threading.Thread(target=worker) for _ in range(8)]
        [t.start() for t in threads]
        [t.join() for t in threads]
        self.assertEqual(len(claimed), 3)
        self.assertEqual(len(set(claimed)), 3)
        self.assertEqual(Download.objects.filter(status="running").count(), 3)

    def test_tick_records_heartbeat_of_scheduler(self):
        scheduler.tick()
        self.assertIsNotNone(SchedulerState.objects.get(id=1).last_tick)

    def test_reaper_requeues_dead_worker(self):
        d = self.make_download("http://h/x.bin", status="running", dispatched_at=timezone.now() - timedelta(minutes=5))
        self.assertEqual(scheduler.reap_stale(), 1)
        d.refresh_from_db()
        self.assertEqual((d.status, d.requeues), ("queued", 1))

    def test_reaper_leaves_alive_and_recent(self):
        alive = self.make_download("http://h/a", status="running", dispatched_at=timezone.now() - timedelta(minutes=5))
        live.set_live(alive.pk, 10, 100, 5, 123)
        recent = self.make_download("http://h/b", status="running", dispatched_at=timezone.now())
        self.assertEqual(scheduler.reap_stale(), 0)
        alive.refresh_from_db(); recent.refresh_from_db()
        self.assertEqual((alive.status, recent.status), ("running", "running"))

    def test_reaper_gives_up_after_max_requeues(self):
        d = self.make_download("http://h/x", status="running", dispatched_at=timezone.now() - timedelta(minutes=5), requeues=scheduler.MAX_REQUEUES)
        scheduler.reap_stale()
        d.refresh_from_db()
        self.assertEqual(d.status, "error")

    def test_reaper_only_kills_our_wget(self):
        # a pid that is alive but is not a "wgp-<id>-" wget (PID reuse) must not be signalled
        d = self.make_download("http://h/x", status="running", dispatched_at=timezone.now() - timedelta(minutes=5), pid=os.getpid())
        scheduler.reap_stale()
        os.kill(os.getpid(), 0)  # still here
        d.refresh_from_db()
        self.assertEqual(d.status, "queued")

    def test_metrics_aggregate(self):
        u1, u2 = self.make_user("a"), self.make_user("b")
        x = self.make_download("http://h/1", user=u1); y = self.make_download("http://h/2", user=u2)
        live.set_live(x.pk, 1, 10, 1000, 1); live.set_live(y.pk, 1, 10, 500, 2)
        self.assertEqual(scheduler.record_metrics(), 1500)
        self.assertEqual(live.get_aggregate("global")[-1]["speed"], 1500)
        self.assertEqual(live.get_aggregate(f"user:{u1.pk}")[-1]["speed"], 1000)

    def test_maintenance_prunes_old_finished(self):
        conf.set("history.retention_days", 7)
        old = self.make_download("http://h/o", status="completed", finished_at=timezone.now() - timedelta(days=10))
        new = self.make_download("http://h/n", status="completed", finished_at=timezone.now())
        active = self.make_download("http://h/a", status="queued")
        self.assertEqual(tasks.maintenance(), 1)
        self.assertEqual(set(Download.objects.values_list("pk", flat=True)), {new.pk, active.pk})
