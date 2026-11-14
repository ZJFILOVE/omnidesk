package cn.qwensub.app

/** Presentation only: never changes task order or the background download queue. */
internal enum class TaskFilter(val label: String) {
    ALL("全部"), ACTIVE("进行中"), READY("已完成"), ATTENTION("需要处理");
}

internal fun DownloadTask.category(): TaskFilter = when {
    cleanupPending -> if (cleanupError != null) TaskFilter.ATTENTION else TaskFilter.ACTIVE
    videoState in setOf(TaskState.FAILED, TaskState.CANCELLED) ||
        subtitleState in setOf(TaskState.FAILED, TaskState.CANCELLED) -> TaskFilter.ATTENTION
    videoState == TaskState.READY && subtitleState in setOf(TaskState.READY, TaskState.SKIPPED) -> TaskFilter.READY
    else -> TaskFilter.ACTIVE
}

internal fun filterTasks(tasks: List<DownloadTask>, filter: TaskFilter, query: String): List<DownloadTask> = tasks.filter {
    !it.historyHidden && (filter == TaskFilter.ALL || it.category() == filter) &&
        (query.isBlank() || it.title.contains(query.trim(), ignoreCase = true) || it.id.contains(query.trim(), ignoreCase = true))
}
