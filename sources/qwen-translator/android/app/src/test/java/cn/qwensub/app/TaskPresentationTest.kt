package cn.qwensub.app

import org.junit.Assert.*
import org.junit.Test

class TaskPresentationTest {
    private val base = DownloadTask(id = "fixture", title = "测试 Video", url = "https://youtu.be/jNQXAC9IVRw")

    @Test fun independentTracksDoNotReportPrematureCompletion() {
        assertEquals(TaskFilter.ACTIVE, base.copy(videoState = TaskState.READY, subtitleState = TaskState.RUNNING).category())
        assertEquals(TaskFilter.ATTENTION, base.copy(videoState = TaskState.READY, subtitleState = TaskState.FAILED).category())
        assertEquals(TaskFilter.READY, base.copy(videoState = TaskState.READY, subtitleState = TaskState.SKIPPED).category())
        assertEquals(TaskFilter.READY, base.copy(videoState = TaskState.READY, subtitleState = TaskState.READY).category())
    }

    @Test fun cleanupStatusTakesPrecedenceOverCancelledTransfers() {
        assertEquals(TaskFilter.ACTIVE, base.copy(localCleanupPending = true, videoState = TaskState.CANCELLED).category())
        assertEquals(TaskFilter.ATTENTION, base.copy(remoteCleanupPending = true, cleanupError = "offline").category())
    }

    @Test fun searchingFiltersWithoutChangingQueueOrExposingHiddenHistory() {
        val ready = base.copy(id = "ready", videoState = TaskState.READY, subtitleState = TaskState.READY)
        val hidden = base.copy(id = "hidden", historyHidden = true)
        val tasks = listOf(base, ready, hidden)
        assertEquals(listOf(base, ready), filterTasks(tasks, TaskFilter.ALL, " VIDEO "))
        assertEquals(listOf(ready), filterTasks(tasks, TaskFilter.READY, "ready"))
        assertTrue(filterTasks(tasks, TaskFilter.ALL, "missing").isEmpty())
        assertEquals(listOf(base, ready, hidden), tasks)
    }
}
