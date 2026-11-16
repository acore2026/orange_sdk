package com.rayneo.agent.sdk.masque

import java.io.File
import org.junit.Assert.assertTrue
import org.junit.Test

class NativeMasqueTransportThreadingTest {
    @Test
    fun nativeTunnelLifecycleRunsOutsideTheCallerMainThread() {
        val source = File(
            "src/main/kotlin/com/rayneo/agent/sdk/masque/NativeMasqueBridge.kt",
        ).readText()

        assertTrue(source.contains("withContext(ioDispatcher)"))
        assertTrue(source.contains("withContext(NonCancellable + ioDispatcher)"))
        assertTrue(source.contains("bridge.nativeStop(startedHandle)"))
        assertTrue(source.contains("nativeGetStat"))
        assertTrue(source.contains("lastStatistics = bridge.statistics(currentHandle)"))
    }
}
