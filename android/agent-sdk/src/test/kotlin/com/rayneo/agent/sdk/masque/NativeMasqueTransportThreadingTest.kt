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

        assertTrue(source.contains("withContext(Dispatchers.IO)"))
        assertTrue(source.contains("withContext(NonCancellable + Dispatchers.IO)"))
        assertTrue(source.contains("bridge.nativeStop(startedHandle)"))
    }
}
