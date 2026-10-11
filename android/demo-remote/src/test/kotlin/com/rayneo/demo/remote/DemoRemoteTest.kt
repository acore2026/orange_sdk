package com.rayneo.demo.remote

import org.junit.Assert.*
import org.junit.Test
import java.io.ByteArrayInputStream
import java.net.Socket
import java.net.ServerSocket
import java.util.concurrent.CountDownLatch
import java.util.concurrent.TimeUnit

class DemoRemoteTest {
    @Test fun validateHostAndPreserveRolePortSeparation() {
        assertEquals("core.example.org",DemoRemote.coreHost(" Core.Example.org "))
        assertEquals("101.245.78.174",DemoRemote.coreHost("101.245.78.174"))
        for(host in listOf("https://core", "core:8443", "999.1.2.3", "a..b", "x;reboot", "-bad", "a/b", "")) {
            assertTrue(host,runCatching { DemoRemote.coreHost(host) }.isFailure)
        }
        assertEquals(8791,DemoRemote.endpoint("192.168.137.225").port)
        assertTrue(runCatching { DemoRemote.endpoint("a:0") }.isFailure)
        assertTrue(runCatching { DemoRemote.endpoint("a:99999") }.isFailure)
    }
    @Test fun utf8AndBoundsAndAuthentication() {
        val text="AUTH 12345678 {\"command\":\"PATROL\",\"area\":\"园区 A\"}"
        assertEquals(text,DemoRemote.readLine(ByteArrayInputStream((text+"\n").toByteArray())))
        assertTrue(runCatching { DemoRemote.readLine(ByteArrayInputStream("a".repeat(8193).toByteArray())) }.isFailure)
        assertTrue(runCatching { DemoRemote.envelope("12345678","STATUS\nEXPEL") }.isFailure)
        assertFalse(DemoRemote.authorized("12345678","87654321"))
        assertFalse(DemoRemote.authorized("",""))
        assertTrue(DemoRemote.authorized("12345678","12345678"))
    }
    @Test fun tcpRoundTripHasNoAutomaticRetry() {
        val listener=ServerSocket(0)
        val received=CountDownLatch(1)
        val thread=Thread {
            listener.use { it.accept().use { s ->
                assertEquals("STATUS", DemoRemote.readLine(s.getInputStream()))
                s.getOutputStream().write("OK 待命\n".toByteArray())
                received.countDown()
            } }
        }
        thread.start()
        assertEquals("OK 待命",DemoRemote.exchange("127.0.0.1:${listener.localPort}","STATUS"))
        assertTrue(received.await(1,TimeUnit.SECONDS))
        thread.join(1000)
    }
}
