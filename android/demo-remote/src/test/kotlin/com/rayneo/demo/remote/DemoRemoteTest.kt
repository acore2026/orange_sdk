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
    @Test fun utf8AndBoundsWithoutPairing() {
        val text="{\"command\":\"PATROL\",\"area\":\"园区 A\"}"
        assertEquals(text,DemoRemote.readLine(ByteArrayInputStream((text+"\n").toByteArray())))
        assertTrue(runCatching { DemoRemote.readLine(ByteArrayInputStream("a".repeat(8193).toByteArray())) }.isFailure)
        assertTrue(runCatching { DemoRemote.envelope("STATUS\nEXPEL") }.isFailure)
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
    @Test fun dogDeregistrationNeedsNoPairingAndUsesSeparateStatusQuery() {
        val server = com.sun.net.httpserver.HttpServer.create(java.net.InetSocketAddress("127.0.0.1", 0), 0)
        val requests = java.util.concurrent.CopyOnWriteArrayList<String>()
        server.createContext("/identity") { exchange ->
            requests.add(exchange.requestMethod + " " + exchange.requestURI.toString())
            assertNull(exchange.requestHeaders.getFirst("Authorization"))
            if (exchange.requestMethod == "POST") {
                assertEquals("{\"request_id\":\"test-request\"}", exchange.requestBody.bufferedReader().readText())
            }
            val body = "{\"state\":\"NO_IDENTITY\"}".toByteArray()
            exchange.sendResponseHeaders(200, body.size.toLong())
            exchange.responseBody.use { it.write(body) }
        }
        server.start()
        try {
            val endpoint = "127.0.0.1:${server.address.port}"
            DemoRemote.dogIdentity(endpoint, "test-request", true)
            DemoRemote.dogIdentity(endpoint, "test-request", false)
            assertEquals(listOf("POST /identity/deregister", "GET /identity/status?request_id=test-request"), requests)
            assertTrue(runCatching { DemoRemote.dogIdentity(endpoint, "bad id", true) }.isFailure)
        } finally { server.stop(0) }
    }

    @Test fun dogAdminConnectAndSaveUseSeparateReadAndWriteRequests() {
        val server = com.sun.net.httpserver.HttpServer.create(java.net.InetSocketAddress("127.0.0.1", 0), 0)
        val requests = java.util.concurrent.CopyOnWriteArrayList<String>()
        server.createContext("/") { exchange ->
            requests.add(exchange.requestMethod + " " + exchange.requestURI.toString())
            val payload = exchange.requestBody.bufferedReader().readText()
            if (exchange.requestMethod == "POST") assertEquals("{\"host\":\"192.168.1.13\"}", payload)
            val body = "{\"ok\":true}".toByteArray()
            exchange.sendResponseHeaders(200, body.size.toLong())
            exchange.responseBody.use { it.write(body) }
        }
        server.start()
        try {
            val address = "127.0.0.1:${server.address.port}"
            DemoRemote.dogAdmin(address)
            DemoRemote.dogAdmin(address, "192.168.1.13")
            assertEquals(listOf("GET /health", "POST /config/core"), requests)
            assertTrue(runCatching { DemoRemote.dogAdmin(address, "host;reboot") }.isFailure)
            assertEquals(2, requests.size)
        } finally { server.stop(0) }
        assertTrue(DemoRemote.commands.containsAll(listOf("FORWARD", "BACK", "LEFT", "RIGHT", "STOP")))
    }

}
