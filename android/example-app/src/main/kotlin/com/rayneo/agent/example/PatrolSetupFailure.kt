package com.rayneo.agent.example

enum class PatrolSetupStage(val failureTitle: String) {
    GROUP_CREATE("建组失败"),
    GROUP_CONFIG("群组配置未就绪"),
    MESSAGING("群组消息通道未就绪"),
    DISPATCH("巡检任务派遣失败"),
    ENTRY("巡逻起步失败或结果未确认"),
    VIDEO("视频算力连接失败"),
}

class PatrolSetupFailure(val stage: PatrolSetupStage, cause: Exception) :
    Exception("${stage.failureTitle}：${cause.message ?: cause.javaClass.simpleName}", cause)
